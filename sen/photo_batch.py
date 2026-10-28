"""Batch photo summarisation for Telegram albums and ZIP dumps.

Telegram delivers every photo of an album as its own message sharing one
``media_group_id``, so a 38-shot dump lands as a burst of messages. Answering
each message as it arrives trips two limits at once: the per-user cooldown in
the conversation path and the ~20 messages per minute a bot may send to one
group. The batch path therefore collects the burst first, analyses the images
one at a time, and pushes a single combined reply through a paced per-chat
queue.

Images are only summarised when they are a direct request: an @tag or a
reply to the bot on a photo/album, or a ZIP document. Bare photos and
albums — the common case in a busy group — are ignored completely.

Two entry points back that rule up:

* an album (or a lone photo) - collected once the burst stops and
  summarised together, but only when aimed at the bot;
* a ZIP document - every file inside the bot can read (images, PDF, Office,
  text and code, audio, video, nested archive listings) is extracted and
  summarised together.

The feature is opt-in per chat through ``PHOTO_BATCH_CHAT_IDS``.
"""

from __future__ import annotations

import asyncio
import html
import io
import re
import time
import zipfile
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

from aiogram import Bot, Router
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import BaseFilter
from aiogram.types import InputRichMessage, Message, ReplyParameters

from . import config as _cfg
from .media import download_telegram_media, is_readable_document, prepare_document

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# How long to wait after the last photo of an album before treating it as
# complete. Telegram delivers album members back to back, so a short quiet
# period is enough to know the burst ended.
ALBUM_IDLE_SECONDS = 2.5

# Pause between Gemini calls so a large batch does not burst the model API.
ANALYSIS_GAP_SECONDS = 1.2

# Spacing between outgoing messages in one chat. Telegram allows a bot ~20
# messages per minute per group; 3.5s keeps us comfortably under that.
REPLY_GAP_SECONDS = 3.5

# Minimum spacing between interim edits of the progress message. Edits are
# cheap but not free, so the counter refreshes at most this often.
PROGRESS_EDIT_GAP = 3.0

# Guards against a pathological upload — per member and for the whole
# archive, so a small ZIP bomb cannot turn into a gigabyte of extraction.
MAX_BATCH_IMAGES = 60
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_MEMBER_BYTES = 100 * 1024 * 1024

# Telegram's own message limit is 4096 characters; stay well below it.
CHUNK_LIMIT = 3500

# Rich messages (sendRichMessage) accept 32768 UTF-8 characters — eight times
# a plain message — so a full-batch summary lands as ONE message instead of a
# string of 3500-char chunks. Stay under the cap with headroom to spare.
RICH_CHUNK_BYTES = 30000

ZIP_MIMES = {"application/zip", "application/x-zip-compressed", "multipart/x-zip"}


# ---------------------------------------------------------------------------
# Paced sender
# ---------------------------------------------------------------------------


class _PacedSender:
    """Serialises outgoing messages per chat so flood limits are never hit."""

    def __init__(self, gap: float = REPLY_GAP_SECONDS) -> None:
        self._gap = gap
        self._queues: dict[int, asyncio.Queue] = {}
        self._workers: dict[int, asyncio.Task] = {}

    async def submit(self, chat_id: int, send: Callable[[], Awaitable[Any]]) -> None:
        queue = self._queues.get(chat_id)
        if queue is None:
            queue = asyncio.Queue()
            self._queues[chat_id] = queue
            self._workers[chat_id] = asyncio.create_task(self._run(chat_id, queue))
        await queue.put(send)

    async def _run(self, chat_id: int, queue: asyncio.Queue) -> None:
        while True:
            send = await queue.get()
            try:
                await send()
            except TelegramRetryAfter as e:
                # Telegram told us to slow down; honour it and carry on.
                print(f"[batch] flood wait of {e.retry_after}s in chat {chat_id}")
                await asyncio.sleep(e.retry_after + 1)
            except Exception as e:
                print(f"[batch] send failed in chat {chat_id}: {type(e).__name__}: {e}")
            finally:
                queue.task_done()
                await asyncio.sleep(self._gap)


_sender = _PacedSender()

# One image is analysed at a time, no matter how many batches are in flight.
_analysis_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Album collection
# ---------------------------------------------------------------------------


class _PendingAlbum:
    """Photos collected so far for one album."""

    def __init__(self, chat_id: int, first_message_id: int) -> None:
        self.chat_id = chat_id
        self.first_message_id = first_message_id
        self.items: list[dict[str, Any]] = []
        self.timer: asyncio.Task | None = None


_pending_albums: dict[tuple[int, str], _PendingAlbum] = {}

# Albums already claimed for batch summarising. Telegram stamps the caption
# and the reply on the album's first message only, so once that part aims
# at the bot, every remaining part must follow it into the same batch.
_batch_albums: set[tuple[int, str]] = set()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bot_username() -> str:
    if _cfg.BOT_INFO and _cfg.BOT_INFO.username:
        return f"@{_cfg.BOT_INFO.username}"
    return ""


def _addresses_bot(message: Message) -> bool:
    """True when the message is explicitly aimed at the bot."""
    text = (message.text or message.caption or "").lower()
    username = _bot_username().lower()
    if username and username in text:
        return True
    if "@gemini" in text:
        return True
    replied = message.reply_to_message
    return bool(replied and replied.from_user and _cfg.BOT_INFO and replied.from_user.id == _cfg.BOT_INFO.id)


def _is_zip(document) -> bool:
    name = (getattr(document, "file_name", "") or "").lower()
    mime = (getattr(document, "mime_type", "") or "").lower()
    return name.endswith(".zip") or mime in ZIP_MIMES


def _chunk(text: str) -> list[str]:
    """Split a long summary into message-sized pieces, preferring line breaks."""
    if len(text) <= CHUNK_LIMIT:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > CHUNK_LIMIT and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _format_summaries(entries: list[tuple[str, str]], kind: str = "image") -> str:
    """Render the per-entry summaries as one reply body."""
    total = len(entries)
    header = f"📸 Read 1 {kind} - here is the gist:" if total == 1 else f"📸 Read {total} {kind}s - here's the gist:"
    body = "\n\n".join(f"{label}\n{summary}" for label, summary in entries)
    return f"{header}\n\n{body}"


def _rich(html_text: str) -> InputRichMessage:
    """Wrap already-escaped HTML for Telegram's rich-message API.

    ``skip_entity_detection`` stops Telegram auto-linking cashtags,
    mentions, and phone-number-looking runs inside summaries — every
    auto-colored span is noise in a wall of text.
    """
    return InputRichMessage(html=html_text, skip_entity_detection=True)


def _format_rich_summary(entries: list[tuple[str, str]], kind: str = "image") -> list[str]:
    """Build the summary as rich-HTML groups, packed under RICH_CHUNK_BYTES.

    One group is one outgoing message. Labels and summaries are HTML-escaped,
    so the only markup Telegram sees is our own tags. Structure comes from
    real block tags — <h3> heading, <p> per entry with <br> after the
    filename — because rich HTML collapses literal newlines like any HTML,
    which would otherwise run all 39 entries into one unreadable paragraph.
    Packing splits strictly between blocks, so a group never cuts a tag or a
    summary in half. A typical batch fits in a single group — which is the
    point: a 39-image run that took six plain messages becomes one message.
    """
    total = len(entries)
    header = f"📸 Read 1 {kind} - here is the gist:" if total == 1 else f"📸 Read {total} {kind}s - here's the gist:"
    blocks = [f"<h3>{html.escape(header)}</h3>"]
    blocks += [f"<p><b>{html.escape(label)}</b><br>{html.escape(summary)}</p>" for label, summary in entries]

    groups: list[str] = []
    current = ""
    for block in blocks:
        candidate = current + block
        if current and len(candidate.encode("utf-8")) > RICH_CHUNK_BYTES:
            groups.append(current)
            current = block
        else:
            current = candidate
    if current:
        groups.append(current)
    return groups


_MD_LINE_PREFIX_RE = re.compile(r"^\s{0,3}(?:#{1,6}\s+|[-*•]\s+)", re.MULTILINE)
_MD_WRAP_RE = re.compile(r"(\*\*|__|`{1,3})")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")


def _response_text(response: Any) -> str:
    """Best-effort plain text from a Gemini response, whatever shape it took.

    ``response.text`` raises when the candidate carries no text part (safety
    block, empty candidate), so fall back to walking the parts by hand before
    giving up — the caller logs whatever comes back.
    """
    try:
        text = response.text
        if text:
            return text.strip()
    except Exception:
        pass
    chunks: list[str] = []
    try:
        for candidate in getattr(response, "candidates", None) or []:
            for part in getattr(getattr(candidate, "content", None), "parts", None) or []:
                piece = getattr(part, "text", None)
                if piece:
                    chunks.append(piece)
    except Exception:
        pass
    return "\n".join(chunks).strip()


def _plain_summary(text: str, max_sentences: int = 4) -> str:
    """Force a model answer back into chat-shaped plain text.

    The prompt forbids formatting, but Gemini still emits markdown headings,
    bullets, and bold now and then; the reply is HTML-escaped plain text, so
    those markers would show up literally. Also caps runaway answers at the
    requested 2-4 sentences.
    """
    text = _MD_LINE_PREFIX_RE.sub("", text)
    text = _MD_WRAP_RE.sub("", text)
    text = " ".join(text.split()).strip().strip("\"'“”‘’")
    sentences = _SENTENCE_SPLIT_RE.split(text)
    if len(sentences) > max_sentences:
        text = " ".join(sentences[:max_sentences]).rstrip(",;:")
        if text and text[-1] not in ".!?…":
            text += "."
    return text


def _gemini_mime(mime: str) -> str:
    """Gemini ingests text/* only as plain text; every other type rides as-is."""
    return "text/plain" if (mime or "").startswith("text/") else mime


def _summarize_instruction(mime: str) -> tuple[str, str]:
    """Pick the (user prompt, system instruction) that fit one file's MIME family."""
    family = (mime or "").partition("/")[0]
    if family == "image":
        return (
            "Describe this image in 2-4 short sentences for a chat. "
            "Say what the screen or scene is, the key visible content or text, and anything notable. "
            "For a screenshot, quote the most important readable text. "
            "Only describe what is actually visible; never guess at anything you cannot read.",
            "You are a precise visual summariser. Report only what is visible in the image. "
            "No preamble, no closing remarks, no headings, no bullet symbols - just the summary text.",
        )
    if family in ("audio", "video"):
        return (
            f"Describe this {family} file in 2-4 short sentences for a chat: what happens in it and anything notable. "
            "Only describe what is actually there; never guess at anything you cannot hear or see.",
            "You are a precise media summariser. Report only what is actually in the file. "
            "No preamble, no closing remarks, no headings, no bullet symbols - just the summary text.",
        )
    return (
        "Summarize this file in 2-4 short sentences for a chat. "
        "Cover what it contains and anything notable - for data, the key numbers; for code, what it does; "
        "for documents and text, the main points. "
        "Only state what is actually in the file; never guess at anything you cannot read.",
        "You are a precise document summariser. Report only what the file actually contains. "
        "No preamble, no closing remarks, no headings, no bullet symbols - just the summary text.",
    )


async def _summarize(data: bytes, mime: str, caption: str, label: str) -> str:
    """Ask Gemini for a short, grounded summary of one batch entry."""
    from google.genai import types

    # Imported here: handlers imports this module, so a top-level import
    # would be circular.
    from .handlers import generate_gemini_response

    part = types.Part.from_bytes(data=data, mime_type=_gemini_mime(mime))
    prompt, system_instruction = _summarize_instruction(mime)
    if caption:
        prompt += f"\n\nThe sender captioned it: {caption[:500]}"
    if label:
        # ZIP-extracted entries carry their filename as the label, which is
        # real context for the model (e.g. "screenshot_02_error.png").
        prompt += f"\n\nThe file is named: {label[:120]}"

    safety = [
        types.SafetySetting(category=c, threshold="BLOCK_NONE")
        for c in (
            "HARM_CATEGORY_HATE_SPEECH",
            "HARM_CATEGORY_HARASSMENT",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT",
            "HARM_CATEGORY_DANGEROUS_CONTENT",
        )
    ]
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        safety_settings=safety,
    )
    try:
        print(f"[batch] gemini request: label={label} mime={mime} bytes={len(data)} caption={caption[:80]!r}")
        response = await generate_gemini_response([part, prompt], config)
        raw = _response_text(response)
        print(f"[batch] gemini response: label={label} chars={len(raw)} raw={raw[:400]!r}")
        text = _plain_summary(raw)
        return text or "I could not make out the contents of this one."
    except Exception as e:
        print(f"[batch] analysis failed for {label}: {type(e).__name__}: {e}")
        return "I could not read this one (the model call failed)."


# ---------------------------------------------------------------------------
# Batch processing
# ---------------------------------------------------------------------------


async def _process_batch(
    bot: Bot,
    chat_id: int,
    images: list[dict[str, Any]],
    reply_to_message_id: int,
) -> None:
    """Analyse images one by one and deliver one paced reply."""
    if not images:
        await _sender.submit(
            chat_id,
            lambda: bot.send_message(
                chat_id=chat_id,
                text="I could not download any of those images, so there is nothing to summarise.",
                reply_parameters=ReplyParameters(message_id=reply_to_message_id),
            ),
        )
        return

    total = len(images)
    # Mixed archives read as "files"; a pure image batch keeps the old wording.
    kind = "image" if all((image.get("mime") or "").startswith("image/") for image in images) else "file"
    noun = kind if total == 1 else f"{kind}s"

    # One status message is edited in place while the batch runs, so the chat
    # sees live progress without a flood of new messages. It is sent directly
    # (not through the paced sender) because we need its message_id back.
    # Rich is tried first so the final summary can flip in as rich HTML; if
    # the API rejects rich here, the whole run degrades to plain text.
    status_id: int | None = None
    status_rich = False
    rich_enabled = True
    last_edit = 0.0
    edits_allowed = True

    async def _set_progress(done: int) -> None:
        nonlocal status_id, status_rich, rich_enabled, last_edit, edits_allowed
        if not edits_allowed:
            return
        now = time.monotonic()
        if status_id is not None and now - last_edit < PROGRESS_EDIT_GAP:
            return
        text = f"📸 Found {total} {noun} - summarizing {done}/{total}..."
        reply = ReplyParameters(message_id=reply_to_message_id)
        try:
            if status_id is None:
                sent = None
                if rich_enabled:
                    try:
                        sent = await bot.send_rich_message(
                            chat_id=chat_id,
                            rich_message=_rich(text),
                            reply_parameters=reply,
                        )
                        status_rich = True
                    except Exception as e:
                        print(f"[batch] rich status failed: {type(e).__name__}: {e}; falling back to plain")
                        rich_enabled = False
                if sent is None:
                    sent = await bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        reply_parameters=reply,
                    )
                    status_rich = False
                status_id = sent.message_id
            elif status_rich:
                await bot.edit_message_text(rich_message=_rich(text), chat_id=chat_id, message_id=status_id)
            else:
                await bot.edit_message_text(text=text, chat_id=chat_id, message_id=status_id)
            last_edit = now
        except TelegramRetryAfter as e:
            # Flood control on edits: stop updating the counter, but still
            # attempt the final swap to the summary.
            print(f"[batch] progress edit flood-wait of {e.retry_after}s; going quiet")
            edits_allowed = False
        except Exception as e:
            print(f"[batch] progress update failed: {type(e).__name__}: {e}")
            if status_id is None:
                # Without a status message there is nothing to edit later.
                edits_allowed = False

    await _set_progress(0)

    entries: list[tuple[str, str]] = []
    for index, image in enumerate(images, 1):
        label = image["label"]
        async with _analysis_lock:
            summary = await _summarize(image["data"], image["mime"], image.get("caption", ""), label)
        entries.append((label, summary))
        await _set_progress(index)
        if index < total:
            await asyncio.sleep(ANALYSIS_GAP_SECONDS)

    # ---- Rich delivery: the status message becomes the whole summary ----
    if status_id is not None and status_rich:
        groups = _format_rich_summary(entries, kind)
        overflow: list[str] = []
        try:
            # Usually a single group, so this edit IS the delivery.
            await bot.edit_message_text(
                rich_message=_rich(groups[0]),
                chat_id=chat_id,
                message_id=status_id,
            )
            overflow = groups[1:]
        except Exception as e:
            print(f"[batch] final progress edit failed: {type(e).__name__}: {e}")
            overflow = groups  # status is stale; send everything instead
        for group in overflow:
            await _sender.submit(
                chat_id,
                lambda g=group: bot.send_rich_message(
                    chat_id=chat_id,
                    rich_message=_rich(g),
                    reply_parameters=ReplyParameters(message_id=reply_to_message_id),
                ),
            )
        return

    # ---- Plain delivery (status never existed or rich was unavailable) ----
    chunks = _chunk(_format_summaries(entries, kind))
    first = html.escape(chunks[0])
    rest = chunks[1:]

    delivered_via_edit = False
    if status_id is not None:
        try:
            # The status message flips into the first chunk of the summary;
            # anything that did not fit goes through the paced sender.
            await bot.edit_message_text(text=first, chat_id=chat_id, message_id=status_id)
            delivered_via_edit = True
        except Exception as e:
            print(f"[batch] final progress edit failed: {type(e).__name__}: {e}")

    for chunk in rest if delivered_via_edit else [first] + rest:
        # The bot defaults to parse_mode="HTML", and model output can contain
        # angle brackets or ampersands that Telegram would reject as markup.
        await _sender.submit(
            chat_id,
            lambda text=chunk: bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_parameters=ReplyParameters(message_id=reply_to_message_id),
            ),
        )


async def _flush_album(bot: Bot, key: tuple[int, str]) -> None:
    """Wait for the album to stop growing, then summarise it."""
    await asyncio.sleep(ALBUM_IDLE_SECONDS)
    album = _pending_albums.pop(key, None)
    if not album or not album.items:
        return
    images: list[dict[str, Any]] = []
    for item in album.items:
        data = await download_telegram_media(bot, item["file_id"])
        if not data:
            continue
        images.append(
            {
                "label": item["label"],
                "data": data,
                "mime": item["mime"],
                "caption": item["caption"],
            }
        )
    await _process_batch(bot, album.chat_id, images, album.first_message_id)


async def _on_photo(bot: Bot, message: Message) -> None:
    """Collect a photo into its album, or handle a lone photo directly."""
    group_id = message.media_group_id
    if not group_id:
        data = await download_telegram_media(bot, message.photo[-1].file_id)
        await _process_batch(
            bot,
            message.chat.id,
            (
                [
                    {
                        "label": "Photo 1",
                        "data": data,
                        "mime": "image/jpeg",
                        "caption": message.caption or "",
                    }
                ]
                if data
                else []
            ),
            message.message_id,
        )
        return

    key = (message.chat.id, group_id)
    album = _pending_albums.get(key)
    if album is None:
        album = _PendingAlbum(message.chat.id, message.message_id)
        _pending_albums[key] = album
    album.items.append(
        {
            "label": f"Photo {len(album.items) + 1}",
            "file_id": message.photo[-1].file_id,
            "mime": "image/jpeg",
            "caption": message.caption or "",
        }
    )
    if album.timer:
        album.timer.cancel()
    album.timer = asyncio.create_task(_flush_album(bot, key))


async def _on_zip(bot: Bot, message: Message) -> None:
    """Extract every readable file from a ZIP and summarise the whole set.

    Anything the bot advertises as readable counts — images, PDF, Office,
    text/code, audio, video — so one archive in, one batch out, instead of
    one Gemini call per loose file.
    """
    data = await download_telegram_media(bot, message.document.file_id)
    if not data:
        await _sender.submit(
            message.chat.id,
            lambda: bot.send_message(
                chat_id=message.chat.id,
                text="I could not download that archive, so there is nothing to summarise.",
                reply_parameters=ReplyParameters(message_id=message.message_id),
            ),
        )
        return

    images: list[dict[str, Any]] = []
    extracted = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = [m for m in archive.infolist() if not m.is_dir()]
            for member in members:
                member_doc = SimpleNamespace(file_name=member.filename, mime_type=None)
                # Resolved from the file extension: the readable set is the
                # one the bot advertises (media.py EXTENSION_MIMES).
                if not is_readable_document(member_doc):
                    continue
                if member.file_size > MAX_IMAGE_BYTES:
                    continue
                if len(images) >= MAX_BATCH_IMAGES or extracted >= MAX_TOTAL_MEMBER_BYTES:
                    break
                with archive.open(member) as handle:
                    payload = handle.read()
                extracted += len(payload)
                # Converts Office/ebook/PDF to text and lists nested archives;
                # images and media pass through untouched for Gemini.
                prepared, prepared_mime, _description = prepare_document(member_doc, payload)
                if not prepared:
                    continue
                images.append(
                    {
                        "label": member_doc.file_name.rsplit("/", 1)[-1] or f"File {len(images) + 1}",
                        "data": prepared,
                        "mime": prepared_mime,
                        "caption": "",
                    }
                )
    except zipfile.BadZipFile:
        await _sender.submit(
            message.chat.id,
            lambda: bot.send_message(
                chat_id=message.chat.id,
                text="That file is not a readable ZIP archive.",
                reply_parameters=ReplyParameters(message_id=message.message_id),
            ),
        )
        return

    if not images:
        await _sender.submit(
            message.chat.id,
            lambda: bot.send_message(
                chat_id=message.chat.id,
                text="I looked inside that archive but found no files I can read.",
                reply_parameters=ReplyParameters(message_id=message.message_id),
            ),
        )
        return

    await _process_batch(bot, message.chat.id, images, message.message_id)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


class BatchPhotoFilter(BaseFilter):
    """Match images in opted-in chats only when they are a direct request.

    The rule: photos and albums are summarised only when explicitly aimed
    at the bot (@tag or reply); a ZIP of readable files is a request in itself.
    Anything bare — the common case in a busy group — stays silent.
    """

    async def __call__(self, message: Message) -> bool:
        if not _cfg.PHOTO_BATCH_CHAT_IDS:
            return False
        if message.chat.id not in _cfg.PHOTO_BATCH_CHAT_IDS:
            return False
        if message.from_user and message.from_user.is_bot:
            return False

        if message.photo:
            group_id = message.media_group_id
            key = (message.chat.id, group_id) if group_id else None
            if key is not None and key in _batch_albums:
                # Another part of this album already claimed it (later parts
                # carry no caption or reply of their own).
                return True
            if _addresses_bot(message):
                if key is not None:
                    _batch_albums.add(key)
                return True
            # Bare photo or album: ignore it, the conversation path skips
            # non-directed messages too, so nobody ever answers.
            return False

        # A ZIP of readable files is a request in itself, tagged or not.
        return bool(message.document and _is_zip(message.document))


def register_batch_photos(router: Router, bot: Bot) -> None:
    """Register the batch photo handler. Call before the conversation handler."""

    @router.message(BatchPhotoFilter())
    async def handle_batch_photos(message: Message) -> None:
        if message.photo:
            await _on_photo(bot, message)
        elif message.document:
            await _on_zip(bot, message)
