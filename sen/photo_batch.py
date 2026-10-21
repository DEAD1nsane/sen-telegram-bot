"""Batch photo summarisation for Telegram albums and ZIP dumps.

Telegram delivers every photo of an album as its own message sharing one
``media_group_id``, so a 38-shot dump lands as a burst of messages. Answering
each message as it arrives trips two limits at once: the per-user cooldown in
the conversation path and the ~20 messages per minute a bot may send to one
group. The batch path therefore collects the burst first, analyses the images
one at a time, and pushes a single combined reply through a paced per-chat
queue.

Two entry points are supported:

* an album (or a lone photo) - the photos are collected until the album stops
  growing, then summarised together;
* a ZIP document - every image inside is extracted and summarised together.

The feature is opt-in per chat through ``PHOTO_BATCH_CHAT_IDS``.
"""

from __future__ import annotations

import asyncio
import html
import io
import os
import zipfile
from typing import Any, Awaitable, Callable

from aiogram import Bot, Router
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import BaseFilter
from aiogram.types import Message, ReplyParameters

from . import config as _cfg
from .media import download_telegram_media

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

# Guards against a pathological upload.
MAX_BATCH_IMAGES = 60
MAX_IMAGE_BYTES = 10 * 1024 * 1024

# Telegram's own message limit is 4096 characters; stay well below it.
CHUNK_LIMIT = 3500

IMAGE_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}

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

# Albums whose first item addressed the bot directly. The conversation path
# owns those, so the batch path must not double-answer them.
_addressed_albums: set[tuple[int, str]] = set()


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
    if replied and replied.from_user and _cfg.BOT_INFO and replied.from_user.id == _cfg.BOT_INFO.id:
        return True
    return False


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


def _format_summaries(entries: list[tuple[str, str]]) -> str:
    """Render the per-image summaries as one reply body."""
    total = len(entries)
    if total == 1:
        header = "📸 Read 1 image - here is the gist:"
    else:
        header = f"📸 Read {total} images - here's the gist:"
    body = "\n\n".join(f"{label}\n{summary}" for label, summary in entries)
    return f"{header}\n\n{body}"


async def _summarize(data: bytes, mime: str, caption: str, label: str) -> str:
    """Ask Gemini for a short, grounded description of one image."""
    from google.genai import types

    # Imported here: handlers imports this module, so a top-level import
    # would be circular.
    from .handlers import generate_gemini_response

    part = types.Part.from_bytes(data=data, mime_type=mime)
    prompt = (
        "Describe this image in 2-4 short sentences for a chat. "
        "Say what the screen or scene is, the key visible content or text, and anything notable. "
        "For a screenshot, quote the most important readable text. "
        "Only describe what is actually visible; never guess at anything you cannot read."
    )
    if caption:
        prompt += f"\n\nThe sender captioned it: {caption[:500]}"

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
        system_instruction=(
            "You are a precise visual summariser. Report only what is visible in the image. "
            "No preamble, no closing remarks, no headings, no bullet symbols - just the summary text."
        ),
        safety_settings=safety,
    )
    try:
        response = await generate_gemini_response([part, prompt], config)
        text = (getattr(response, "text", "") or "").strip()
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
                "I could not download any of those images, so there is nothing to summarise.",
                reply_parameters=ReplyParameters(message_id=reply_to_message_id),
            ),
        )
        return

    entries: list[tuple[str, str]] = []
    for index, image in enumerate(images, 1):
        label = image["label"]
        async with _analysis_lock:
            summary = await _summarize(image["data"], image["mime"], image.get("caption", ""), label)
        entries.append((label, summary))
        if index < len(images):
            await asyncio.sleep(ANALYSIS_GAP_SECONDS)

    body = _format_summaries(entries)
    for chunk in _chunk(body):
        # The bot defaults to parse_mode="HTML", and model output can contain
        # angle brackets or ampersands that Telegram would reject as markup.
        safe = html.escape(chunk)
        await _sender.submit(
            chat_id,
            lambda text=safe: bot.send_message(
                text,
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
    """Extract every image from a ZIP and summarise the whole set."""
    data = await download_telegram_media(bot, message.document.file_id)
    if not data:
        await _sender.submit(
            message.chat.id,
            lambda: bot.send_message(
                "I could not download that archive, so there is nothing to summarise.",
                reply_parameters=ReplyParameters(message_id=message.message_id),
            ),
        )
        return

    images: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = [m for m in archive.infolist() if not m.is_dir()]
            for member in members:
                ext = os.path.splitext(member.filename)[1].lower()
                if ext not in IMAGE_MIME:
                    continue
                if member.file_size > MAX_IMAGE_BYTES:
                    continue
                if len(images) >= MAX_BATCH_IMAGES:
                    break
                with archive.open(member) as handle:
                    payload = handle.read()
                images.append(
                    {
                        "label": os.path.basename(member.filename) or f"Image {len(images) + 1}",
                        "data": payload,
                        "mime": IMAGE_MIME[ext],
                        "caption": "",
                    }
                )
    except zipfile.BadZipFile:
        await _sender.submit(
            message.chat.id,
            lambda: bot.send_message(
                "That file is not a readable ZIP archive.",
                reply_parameters=ReplyParameters(message_id=message.message_id),
            ),
        )
        return

    if not images:
        await _sender.submit(
            message.chat.id,
            lambda: bot.send_message(
                "I looked inside that archive but found no images in it.",
                reply_parameters=ReplyParameters(message_id=message.message_id),
            ),
        )
        return

    await _process_batch(bot, message.chat.id, images, message.message_id)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


class BatchPhotoFilter(BaseFilter):
    """Match photos and ZIP dumps in chats that opted into batch summarising."""

    async def __call__(self, message: Message) -> bool:
        if not _cfg.PHOTO_BATCH_CHAT_IDS:
            return False
        if message.chat.id not in _cfg.PHOTO_BATCH_CHAT_IDS:
            return False
        if message.from_user and message.from_user.is_bot:
            return False

        group_id = message.media_group_id
        if group_id and (message.chat.id, group_id) in _addressed_albums:
            return False

        if message.photo:
            if _addresses_bot(message):
                # The conversation path answers direct requests, so the album
                # must not be summarised twice.
                if group_id:
                    _addressed_albums.add((message.chat.id, group_id))
                return False
            return True

        if message.document and _is_zip(message.document):
            if _addresses_bot(message):
                return False
            return True

        return False


def register_batch_photos(router: Router, bot: Bot) -> None:
    """Register the batch photo handler. Call before the conversation handler."""

    @router.message(BatchPhotoFilter())
    async def handle_batch_photos(message: Message) -> None:
        if message.photo:
            await _on_photo(bot, message)
        elif message.document:
            await _on_zip(bot, message)
