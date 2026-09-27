"""Media handling: Telegram downloads, Gemini uploads, rich message detection."""

from __future__ import annotations

import asyncio
import html
import io
import os
import re
import tempfile
import zipfile
from contextvars import ContextVar
from typing import Any
from xml.etree import ElementTree

from google.genai import types

from aiogram.types import Message, FSInputFile, ReplyParameters

from .config import gemini_client, AUDIO_METADATA

_RAW_UPDATE: ContextVar[Any] = ContextVar("sen_raw_update", default=None)

# ---------------------------------------------------------------------------
# RichMessage video compatibility: expose Message.video from rich blocks
# ---------------------------------------------------------------------------

_ORIGINAL_MESSAGE_GETATTRIBUTE = Message.__getattribute__


def _find_rich_video(obj: Any, seen: set[int] | None = None) -> Any | None:
    """Recursively search for a video/animation media object in rich blocks."""
    if obj is None:
        return None
    if seen is None:
        seen = set()
    obj_id = id(obj)
    if obj_id in seen:
        return None
    seen.add(obj_id)
    if isinstance(obj, (list, tuple)):
        for item in obj:
            found = _find_rich_video(item, seen)
            if found is not None:
                return found
        return None
    if isinstance(obj, dict):
        block_type = str(obj.get("type", "")).lower()
        if block_type in {"video", "animation"}:
            media = obj.get("video") if block_type == "video" else obj.get("animation")
            if media and (media.get("file_id") if isinstance(media, dict) else getattr(media, "file_id", None)):
                return media
        for key in ("blocks", "items", "cells"):
            value = obj.get(key)
            found = _find_rich_video(value, seen)
            if found is not None:
                return found
        return None
    block_type = str(getattr(obj, "type", "")).lower()
    if block_type in {"video", "animation"}:
        media = getattr(obj, "video", None) if block_type == "video" else getattr(obj, "animation", None)
        if media is not None and getattr(media, "file_id", None):
            return media
    for attr in ("blocks", "items", "cells"):
        try:
            value = getattr(obj, attr, None)
        except Exception:
            value = None
        if value:
            found = _find_rich_video(value, seen)
            if found is not None:
                return found
    return None


def _message_getattribute(self: Message, name: str) -> Any:
    value = _ORIGINAL_MESSAGE_GETATTRIBUTE(self, name)
    if name != "video" or value is not None:
        return value
    try:
        rich_message = _ORIGINAL_MESSAGE_GETATTRIBUTE(self, "rich_message")
        rich_video = _find_rich_video(rich_message)
        if rich_video is not None:
            return rich_video
    except Exception:
        pass
    return value


Message.__getattribute__ = _message_getattribute

# ---------------------------------------------------------------------------
# Media tuple helpers
# ---------------------------------------------------------------------------


def _media_tuple(media: Any, mime: str, description: str) -> tuple | None:
    """Extract (file_id, mime, size, description) from a media object."""
    if media is None:
        return None
    file_id = getattr(media, "file_id", None)
    if file_id:
        return (
            file_id,
            getattr(media, "mime_type", None) or mime,
            getattr(media, "file_size", None),
            description,
        )
    return None


def _walk_rich_media(obj: Any, seen: set[int] | None = None) -> tuple | None:
    """Recursively find file-backed media in a RichMessage tree."""
    if obj is None:
        return None
    if seen is None:
        seen = set()
    oid = id(obj)
    if oid in seen:
        return None
    seen.add(oid)
    block_type = str(getattr(obj, "type", "") or "").lower()
    if block_type == "video":
        found = _media_tuple(getattr(obj, "video", None), "video/mp4", "Advanced editor video")
        if found:
            return found
    elif block_type == "animation":
        found = _media_tuple(getattr(obj, "animation", None), "video/mp4", "Advanced editor animation")
        if found:
            return found
    elif block_type == "photo":
        photos = getattr(obj, "photo", None)
        if isinstance(photos, (list, tuple)):
            for photo in reversed(photos):
                found = _media_tuple(photo, "image/jpeg", "Advanced editor photo")
                if found:
                    return found
        else:
            found = _media_tuple(photos, "image/jpeg", "Advanced editor photo")
            if found:
                return found
    elif block_type == "document":
        found = _media_tuple(getattr(obj, "document", None), "application/octet-stream", "Advanced editor document")
        if found:
            return found
    elif block_type == "audio":
        found = _media_tuple(getattr(obj, "audio", None), "audio/mpeg", "Advanced editor audio")
        if found:
            return found
    elif block_type == "voice_note":
        found = _media_tuple(getattr(obj, "voice_note", None), "audio/ogg", "Advanced editor voice note")
        if found:
            return found
    for key, mime, description in (
        ("video", "video/mp4", "Advanced editor video"),
        ("animation", "video/mp4", "Advanced editor animation"),
        ("document", "application/octet-stream", "Advanced editor document"),
        ("audio", "audio/mpeg", "Advanced editor audio"),
        ("voice_note", "audio/ogg", "Advanced editor voice note"),
    ):
        found = _media_tuple(getattr(obj, key, None), mime, description)
        if found:
            return found
    photo = getattr(obj, "photo", None)
    if isinstance(photo, (list, tuple)):
        for item in reversed(photo):
            found = _media_tuple(item, "image/jpeg", "Advanced editor photo")
            if found:
                return found
    for key in ("blocks", "items", "children", "media", "content", "attachment", "cells", "model_extra", "api_kwargs"):
        value = getattr(obj, key, None)
        if value is not None:
            found = _walk_rich_media(value, seen)
            if found:
                return found
    try:
        dumped = obj.model_dump(mode="python", exclude_none=True)
    except Exception:
        dumped = None
    if dumped is not None and dumped is not obj:
        found = _walk_rich_media(dumped, seen)
        if found:
            return found
    if isinstance(obj, dict):
        for value in obj.values():
            found = _walk_rich_media(value, seen)
            if found:
                return found
    elif isinstance(obj, (list, tuple, set)):
        for value in obj:
            found = _walk_rich_media(value, seen)
            if found:
                return found
    return None


def _find_message_by_id(obj: Any, message_id: int, seen: set[int] | None = None) -> Any | None:
    """Find a specific message by ID in a nested update structure."""
    if obj is None:
        return None
    if seen is None:
        seen = set()
    oid = id(obj)
    if oid in seen:
        return None
    seen.add(oid)
    candidate_id = getattr(obj, "message_id", None)
    if candidate_id == message_id:
        return obj
    if isinstance(obj, (list, tuple, set)):
        for value in obj:
            found = _find_message_by_id(value, message_id, seen)
            if found is not None:
                return found
    elif isinstance(obj, dict) or hasattr(obj, "items"):
        try:
            for value in obj.values():
                found = _find_message_by_id(value, message_id, seen)
                if found is not None:
                    return found
        except Exception:
            pass
    else:
        for attr in (
            "message",
            "edited_message",
            "channel_post",
            "edited_channel_post",
            "business_message",
            "edited_business_message",
        ):
            try:
                value = getattr(obj, attr, None)
            except Exception:
                value = None
            if value is not None:
                found = _find_message_by_id(value, message_id, seen)
                if found is not None:
                    return found
        try:
            dump = obj.model_dump(mode="python", exclude_none=True)
            if dump is not obj:
                found = _find_message_by_id(dump, message_id, seen)
                if found is not None:
                    return found
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# Telegram media download
# ---------------------------------------------------------------------------


async def download_telegram_media(bot, file_id: str) -> bytes | None:
    """Download media from Telegram by file_id."""
    try:
        file_info = await bot.get_file(file_id)
        stream = await bot.download_file(file_info.file_path)
        return stream.read() if stream else None
    except Exception as e:
        print(f"Telegram media download error: {type(e).__name__}")
        return None


# ---------------------------------------------------------------------------
# Document handling
# ---------------------------------------------------------------------------

# Non-canonical spellings mapped onto the MIME names Gemini documents as supported.
MIME_ALIASES = {
    "application/rtf": "text/rtf",
    "application/xml": "text/xml",
    "application/xhtml+xml": "text/html",
    "application/x-javascript": "text/javascript",
}

# Gemini ingests these application types directly; everything else that is not
# convertible below has to be rejected or the API call errors out.
NATIVE_DOCUMENT_MIMES = frozenset({"application/pdf", "application/json"})

# Office and ebook formats Gemini cannot read. They are all ZIP+XML containers,
# so the text is recovered with the standard library and sent as plain text.
# Note the legacy binary formats (.doc/.xls/.ppt) are NOT here: they are OLE2
# compound files and would need a third-party parser.
CONVERTIBLE_MIMES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/epub+zip": "epub",
}

# Telegram frequently reports application/octet-stream, so fall back to the
# filename extension when the declared type is not one Gemini understands.
EXTENSION_MIMES = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".text": "text/plain",
    ".log": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".jsonl": "application/json",
    ".xml": "text/xml",
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".py": "text/x-python",
    ".rtf": "text/rtf",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".epub": "application/epub+zip",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
}

DOCUMENT_LABELS = {
    "application/pdf": "PDF document",
    "text/plain": "Text file",
    "text/markdown": "Markdown file",
    "text/csv": "CSV file",
    "text/tab-separated-values": "TSV file",
    "text/html": "HTML file",
    "text/xml": "XML file",
    "text/css": "CSS file",
    "text/javascript": "JavaScript file",
    "text/x-python": "Python file",
    "text/yaml": "YAML file",
    "text/rtf": "RTF file",
    "application/json": "JSON file",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Spreadsheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "Presentation",
    "application/epub+zip": "eBook",
}

UNSUPPORTED_DOCUMENT_REPLY = (
    "I can't read that file type. Send a PDF, Word, Excel, PowerPoint, eBook, plain text, Markdown, "
    "CSV, JSON, HTML, XML, CSS, JS, YAML, RTF, image, audio, or video file and tell me what to do with it."
)
DOCUMENT_DOWNLOAD_ERROR = "I couldn't download that file. Try sending it again."
DOCUMENT_EMPTY_ERROR = "I couldn't find any readable text in that file."

_MEDIA_FAMILY = {"image": "image", "audio": "audio clip", "video": "video"}


def _fallback_document_label(mime: str) -> str:
    """Name a document type that has no dedicated label, e.g. image/png -> PNG image."""
    family, _, subtype = mime.partition("/")
    if not subtype:
        return "file"
    if family == "text":
        return "text file"
    if family in _MEDIA_FAMILY:
        name = subtype.upper() if len(subtype) <= 4 else subtype
        return f"{name} {_MEDIA_FAMILY[family]}"
    return f"{subtype} file"


def is_readable_document_mime(mime: str) -> bool:
    """Check whether Gemini can ingest a MIME type, natively or after conversion."""
    if not mime:
        return False
    if mime in NATIVE_DOCUMENT_MIMES or mime in CONVERTIBLE_MIMES:
        return True
    # text/* is extracted as plain text; media types go down the existing
    # image/audio/video pipelines. The documented lists are explicitly partial,
    # so accept the families rather than enumerating every subtype.
    return mime.startswith(("text/", "image/", "audio/", "video/"))


def document_mime_type(document: Any) -> str:
    """Resolve a document's MIME type: declared type first, then filename extension."""
    mime = (getattr(document, "mime_type", None) or "").split(";")[0].strip().lower()
    mime = MIME_ALIASES.get(mime, mime)
    if is_readable_document_mime(mime):
        return mime
    suffix = os.path.splitext(getattr(document, "file_name", None) or "")[1].lower()
    return EXTENSION_MIMES.get(suffix, "")


def is_readable_document(document: Any) -> bool:
    """Check whether a Telegram document is something the bot can read."""
    return document is not None and is_readable_document_mime(document_mime_type(document))


def describe_document(document: Any) -> str:
    """Describe a document for the model prompt, including its filename when known."""
    mime = document_mime_type(document)
    label = DOCUMENT_LABELS.get(mime) or _fallback_document_label(mime)
    name = re.sub(r"[\"'<>]", "", (getattr(document, "file_name", None) or "").strip())[:80]
    return f'{label} "{name}"' if name else label


def get_replied_document(message: Message) -> Any | None:
    """The readable document in the message the user replied to, if there is one."""
    replied = getattr(message, "reply_to_message", None)
    document = getattr(replied, "document", None) if replied is not None else None
    return document if is_readable_document(document) else None


def prepare_document(document: Any, data: bytes) -> tuple[bytes, str, str]:
    """Classify a document and convert it to plain text when Gemini cannot read it.

    Returns (data, mime, description) ready for ``Part.from_bytes``.
    """
    mime = document_mime_type(document)
    description = describe_document(document)
    kind = CONVERTIBLE_MIMES.get(mime)
    if not kind:
        return data, mime, description
    text = _convert_document_to_text(data, kind) or ""
    return text.encode("utf-8"), "text/plain", description


# ---------------------------------------------------------------------------
# Document -> text conversion
# ---------------------------------------------------------------------------

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_S_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_ZIP_NUMBERED_RE = re.compile(r"(\d+)\.xml$")


def _paragraph_text(paragraph: Any, tag: str) -> str:
    """Join every text run inside one paragraph element."""
    return "".join(node.text or "" for node in paragraph.iter(f"{tag}t"))


def _docx_to_text(data: bytes) -> str | None:
    """Extract paragraph text from a .docx body (ZIP + word/document.xml)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
    except Exception:
        return None
    lines = [text for text in (_paragraph_text(p, _W_NS).strip() for p in root.iter(f"{_W_NS}p")) if text]
    return "\n".join(lines) or None


def _xlsx_to_text(data: bytes) -> str | None:
    """Extract every non-empty cell as 'ref: value', grouped per worksheet."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                strings = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
                shared = ["".join(t.text or "" for t in si.iter(f"{_S_NS}t")) for si in strings.iter(f"{_S_NS}si")]
            sheets = sorted(
                (n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
                key=lambda n: int(_ZIP_NUMBERED_RE.search(n).group(1)),
            )
            if not sheets:
                return None
            labels = _xlsx_sheet_labels(archive, len(sheets))
            out: list[str] = []
            for index, sheet in enumerate(sheets):
                rows = ElementTree.fromstring(archive.read(sheet))
                cells: list[str] = []
                for row in rows.iter(f"{_S_NS}row"):
                    for cell in row.iter(f"{_S_NS}c"):
                        value = _xlsx_cell_value(cell, shared)
                        if value:
                            cells.append(f"{cell.get('r') or '?'}: {value}")
                if cells:
                    out.append(f"### Sheet: {labels[index]}")
                    out.extend(cells)
            return "\n".join(out) or None
    except Exception:
        return None


def _xlsx_sheet_labels(archive: zipfile.ZipFile, count: int) -> list[str]:
    """Best-effort worksheet names; falls back to the sheet file names."""
    fallback = [n.rsplit("/", 1)[-1][:-4] for n in sorted(archive.namelist(), key=lambda n: n) if "worksheets/" in n]
    try:
        book = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        names = [(s.get("name") or "").strip() for s in book.iter(f"{_S_NS}sheet")]
    except Exception:
        return fallback[:count]
    if len(names) != count:
        return fallback[:count]
    return names


def _xlsx_cell_value(cell: Any, shared: list[str]) -> str:
    """Read one cell, resolving shared-string and inline-string references."""
    kind = cell.get("t")
    if kind == "s":
        node = cell.find(f"{_S_NS}v")
        index = int(node.text) if node is not None and (node.text or "").lstrip("-").isdigit() else None
        return shared[index].strip() if index is not None and 0 <= index < len(shared) else ""
    if kind == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(f"{_S_NS}t")).strip()
    node = cell.find(f"{_S_NS}v")
    return (node.text or "").strip() if node is not None else ""


def _pptx_to_text(data: bytes) -> str | None:
    """Extract text from each slide of a .pptx deck (ZIP + ppt/slides/slideN.xml)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            slides = sorted(
                (n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                key=lambda n: int(_ZIP_NUMBERED_RE.search(n).group(1)),
            )
            if not slides:
                return None
            out: list[str] = []
            for slide in slides:
                root = ElementTree.fromstring(archive.read(slide))
                lines = [t for t in (_paragraph_text(p, _A_NS).strip() for p in root.iter(f"{_A_NS}p")) if t]
                if lines:
                    out.append(f"### {slide.rsplit('/', 1)[-1][:-4]}")
                    out.extend(lines)
            return "\n".join(out) or None
    except Exception:
        return None


def _epub_to_text(data: bytes) -> str | None:
    """Extract text from an .epub (ZIP of XHTML documents), stripping markup."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            parts = [
                n
                for n in archive.namelist()
                if n.lower().endswith((".xhtml", ".html", ".htm")) and not n.startswith("__MACOSX")
            ]
            if not parts:
                return None
            out: list[str] = []
            for part in sorted(
                parts, key=lambda n: int(_ZIP_NUMBERED_RE.search(n).group(1)) if _ZIP_NUMBERED_RE.search(n) else 0
            ):
                markup = archive.read(part).decode("utf-8", "replace")
                markup = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", markup)
                text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", markup))
                text = re.sub(r"[ \t]*\n\s*", "\n", re.sub(r"[ \t]{2,}", " ", text)).strip()
                if text:
                    out.append(f"### {part.rsplit('/', 1)[-1]}")
                    out.append(text)
            return "\n".join(out) or None
    except Exception:
        return None


def _convert_document_to_text(data: bytes, kind: str) -> str | None:
    """Dispatch a convertible document kind to its text extractor."""
    if kind == "docx":
        return _docx_to_text(data)
    if kind == "xlsx":
        return _xlsx_to_text(data)
    if kind == "pptx":
        return _pptx_to_text(data)
    if kind == "epub":
        return _epub_to_text(data)
    return None


# ---------------------------------------------------------------------------
# Replied-to media resolution
# ---------------------------------------------------------------------------


def get_replied_video_media(message: Message) -> tuple | None:
    """Resolve media from a replied-to message (video, photo, document, rich blocks)."""
    replied = getattr(message, "reply_to_message", None)
    if not replied:
        return None
    video = getattr(replied, "video", None)
    if video:
        return (
            video.file_id,
            getattr(video, "mime_type", None) or "video/mp4",
            getattr(video, "file_size", None),
            "Replied-to video",
        )
    video_note = getattr(replied, "video_note", None)
    if video_note:
        return video_note.file_id, "video/mp4", getattr(video_note, "file_size", None), "Replied-to video note"
    document = getattr(replied, "document", None)
    if document:
        mime = (getattr(document, "mime_type", None) or "").lower()
        name = (getattr(document, "file_name", None) or "").lower()
        if mime.startswith("video/") or name.endswith(
            (".mp4", ".mov", ".webm", ".avi", ".mkv", ".mpeg", ".mpg", ".wmv", ".3gp")
        ):
            return document.file_id, mime or "video/mp4", getattr(document, "file_size", None), "Replied-to video file"
    photo = getattr(replied, "photo", None)
    if photo:
        try:
            largest = photo[-1]
        except (IndexError, TypeError):
            largest = None
        found = _media_tuple(largest, "image/jpeg", "Replied-to photo")
        if found:
            return found
    animation = getattr(replied, "animation", None)
    if animation:
        found = _media_tuple(animation, "video/mp4", "Replied-to animation")
        if found:
            return found
    for attr in ("rich_message", "model_extra", "api_kwargs"):
        raw = getattr(replied, attr, None)
        found = _walk_rich_media(raw)
        if found:
            print(
                f"Advanced editor reply media found: message_id={getattr(replied, 'message_id', None)} description={found[3]} mime={found[1]}"
            )
            return found
    try:
        dumped = replied.model_dump(mode="python", exclude_none=True)
        found = _walk_rich_media(dumped)
        if found:
            print(
                f"Advanced editor reply media found in dump: message_id={getattr(replied, 'message_id', None)} description={found[3]}"
            )
            return found
    except Exception:
        pass
    raw_update = _RAW_UPDATE.get()
    if raw_update is not None:
        raw_replied = _find_message_by_id(raw_update, getattr(replied, "message_id", None))
        if raw_replied is not None:
            found = _walk_rich_media(raw_replied)
            if found:
                print(
                    f"Advanced editor reply media found in raw update: message_id={getattr(replied, 'message_id', None)} description={found[3]}"
                )
                return found
    return None


# ---------------------------------------------------------------------------
# Gemini video upload
# ---------------------------------------------------------------------------


async def get_gemini_video_file(media_bytes: bytes, media_mime: str, media_description: str):
    """Upload video to Gemini and wait for processing."""
    suffix = ".mp4" if media_mime == "video/mp4" else ".bin"
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(media_bytes)
            temp_path = tmp.name
        print(
            f"Gemini video upload starting: {len(media_bytes)} bytes, mime={media_mime}, description={media_description}"
        )
        uploaded = await asyncio.to_thread(
            gemini_client.files.upload,
            file=temp_path,
            config=types.UploadFileConfig(mime_type=media_mime),
        )
        print(
            f"Gemini video uploaded: name={getattr(uploaded, 'name', None)} state={getattr(getattr(uploaded, 'state', None), 'name', getattr(uploaded, 'state', None))}"
        )
        for attempt in range(60):
            state = getattr(uploaded, "state", None)
            state_name = str(getattr(state, "name", state) or "").upper()
            if state_name == "ACTIVE":
                return uploaded
            if state_name == "FAILED":
                error = getattr(uploaded, "error", None)
                raise RuntimeError(f"Gemini video processing failed: {error or 'unknown processing error'}")
            await asyncio.sleep(1)
            uploaded = await asyncio.to_thread(gemini_client.files.get, name=uploaded.name)
        raise RuntimeError("Gemini video processing timed out after 60 seconds")
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


async def delete_gemini_file(uploaded) -> None:
    """Clean up a temporary Gemini file."""
    if not uploaded or not getattr(uploaded, "name", None):
        return
    try:
        await asyncio.to_thread(gemini_client.files.delete, name=uploaded.name)
    except Exception as e:
        print(f"Gemini temporary video cleanup error: {type(e).__name__}")


async def _get_custom_emoji_input(bot, message: Message) -> tuple[bytes, str, str] | tuple[None, None, None]:
    """Resolve the first premium custom emoji in a message to downloadable bytes.

    Telegram only sends the fallback character in text; the artwork must be
    fetched via getCustomEmojiStickers with the entity's custom_emoji_id.
    """
    entities = list(getattr(message, "entities", None) or []) + list(getattr(message, "caption_entities", None) or [])
    ids: list[str] = []
    for e in entities:
        cid = getattr(e, "custom_emoji_id", None)
        if getattr(e, "type", "") == "custom_emoji" and cid and cid not in ids:
            ids.append(cid)
    if not ids:
        return None, None, None
    try:
        stickers = await bot.get_custom_emoji_stickers(ids[:1])
    except Exception as e:
        print(f"Custom emoji resolve error: {type(e).__name__}")
        return None, None, None
    if not stickers:
        return None, None, None
    sticker = stickers[0]
    file_id = sticker.file_id
    mime = "image/webp"
    if getattr(sticker, "is_video", False):
        mime = "video/mp4"
    elif getattr(sticker, "is_animated", False):
        mime = "application/x-tgsticker"
    emoji = getattr(sticker, "emoji", "") or ""
    description = f"Custom emoji{': ' + emoji if emoji else ''}"
    data = await download_telegram_media(bot, file_id)
    if not data:
        return None, None, None
    return data, mime, description


async def _get_sticker_input(bot, message: Message) -> tuple[bytes, str, str] | tuple[None, None, None]:
    """Download a sticker from a replied-to message and return (bytes, mime, description)."""
    sticker = getattr(message, "sticker", None)
    if not sticker:
        return None, None, None
    file_id = sticker.file_id
    mime = "image/webp"
    if getattr(sticker, "is_video", False):
        mime = "video/mp4"
    elif getattr(sticker, "is_animated", False):
        mime = "application/x-tgsticker"
    emoji = getattr(sticker, "emoji", "") or ""
    description = f"Sticker{': ' + emoji if emoji else ''}"
    data = await download_telegram_media(bot, file_id)
    if not data:
        return None, None, None
    return data, mime, description


# ---------------------------------------------------------------------------
# Keyword audio delivery
# ---------------------------------------------------------------------------


_AUDIO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def send_keyword_audio(message: Message, filename: str) -> bool:
    """Send a keyword-triggered audio file with metadata."""
    metadata = AUDIO_METADATA.get(filename, (None, None))
    title, performer = metadata
    try:
        reply_params = None if message.chat.type == "private" else ReplyParameters(message_id=message.message_id)
        path = os.path.join(_AUDIO_DIR, filename)
        if not os.path.isfile(path):
            print(f"Keyword audio file missing: {path}")
            return False
        kwargs: dict = {"reply_parameters": reply_params, "audio": FSInputFile(path)}
        if title:
            kwargs["title"] = title
        if performer:
            kwargs["performer"] = performer
        await message.answer_audio(**kwargs)
        return True
    except Exception as e:
        print(f"Keyword audio delivery error ({filename}): {type(e).__name__}")
        return False
