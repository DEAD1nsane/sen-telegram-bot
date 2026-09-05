"""Media handling: Telegram downloads, Gemini uploads, rich message detection."""

from __future__ import annotations

import os
import re
import tempfile
from contextvars import ContextVar
from typing import Any

from aiogram.types import Message, FSInputFile

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
            if media and (
                media.get("file_id") if isinstance(media, dict) else getattr(media, "file_id", None)
            ):
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
            "message", "edited_message", "channel_post", "edited_channel_post",
            "business_message", "edited_business_message",
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
        print(f"Telegram media download error: {e}")
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
        return video.file_id, getattr(video, "mime_type", None) or "video/mp4", getattr(video, "file_size", None), "Replied-to video"
    video_note = getattr(replied, "video_note", None)
    if video_note:
        return video_note.file_id, "video/mp4", getattr(video_note, "file_size", None), "Replied-to video note"
    document = getattr(replied, "document", None)
    if document:
        mime = (getattr(document, "mime_type", None) or "").lower()
        name = (getattr(document, "file_name", None) or "").lower()
        if mime.startswith("video/") or name.endswith((".mp4", ".mov", ".webm", ".avi", ".mkv", ".mpeg", ".mpg", ".wmv", ".3gp")):
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
            print(f"Advanced editor reply media found: message_id={getattr(replied, 'message_id', None)} description={found[3]} mime={found[1]}")
            return found
    try:
        dumped = replied.model_dump(mode="python", exclude_none=True)
        found = _walk_rich_media(dumped)
        if found:
            print(f"Advanced editor reply media found in dump: message_id={getattr(replied, 'message_id', None)} description={found[3]}")
            return found
    except Exception:
        pass
    raw_update = _RAW_UPDATE.get()
    if raw_update is not None:
        raw_replied = _find_message_by_id(raw_update, getattr(replied, "message_id", None))
        if raw_replied is not None:
            found = _walk_rich_media(raw_replied)
            if found:
                print(f"Advanced editor reply media found in raw update: message_id={getattr(replied, 'message_id', None)} description={found[3]}")
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
        print(f"Gemini video upload starting: {len(media_bytes)} bytes, mime={media_mime}, description={media_description}")
        uploaded = await __import__("asyncio").to_thread(
            gemini_client.files.upload,
            file=temp_path,
            config=__import__("google.genai", fromlist=["types"]).types.UploadFileConfig(mime_type=media_mime),
        )
        print(f"Gemini video uploaded: name={getattr(uploaded, 'name', None)} state={getattr(getattr(uploaded, 'state', None), 'name', getattr(uploaded, 'state', None))}")
        import asyncio
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
        await __import__("asyncio").to_thread(gemini_client.files.delete, name=uploaded.name)
    except Exception as e:
        print(f"Gemini temporary video cleanup error: {e}")


# ---------------------------------------------------------------------------
# Keyword audio delivery
# ---------------------------------------------------------------------------


_AUDIO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def send_keyword_audio(message: Message, filename: str) -> bool:
    """Send a keyword-triggered audio file with metadata."""
    metadata = AUDIO_METADATA.get(filename, (None, None))
    title, performer = metadata
    try:
        reply_params = (
            None
            if message.chat.type == "private"
            else __import__("aiogram.types", fromlist=["ReplyParameters"]).ReplyParameters(message_id=message.message_id),
        )
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
        print(f"Keyword audio delivery error ({filename}): {e}")
        return False
