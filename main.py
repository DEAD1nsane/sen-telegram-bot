import os
import re
import asyncio
import html
from datetime import datetime, timezone

from aiohttp import web

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message,
    FSInputFile,
    LinkPreviewOptions,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties

import redis.asyncio as redis
import httpx

from google import genai
from google.genai import types


# ==========================================
# Environment & Config
# ==========================================

redis_url = os.environ.get("REDIS_URL", "")

if not redis_url:
    host = os.environ.get("REDISHOST", "localhost")
    port = os.environ.get("REDISPORT", "6379")
    password = os.environ.get("REDISPASSWORD", "")

    if password:
        redis_url = f"redis://default:{password}@{host}:{port}"
    else:
        redis_url = f"redis://{host}:{port}"

if "upstash" in redis_url.lower() and redis_url.startswith("redis://"):
    redis_url = redis_url.replace("redis://", "rediss://", 1)

if redis_url.startswith("rediss://"):
    redis_client = redis.from_url(
        redis_url,
        ssl_cert_reqs=None,
    )
else:
    redis_client = redis.from_url(redis_url)


API_TOKEN = os.getenv("BOT_TOKEN", "")

if not API_TOKEN:
    raise ValueError(
        "CRITICAL CONFIGURATION ERROR: 'BOT_TOKEN' missing."
    )


# Railway private networking URL for your SearXNG service.
#
# You can override this with:
#
# SEARXNG_URL=http://searxng.railway.internal:8080/search
#
SEARXNG_URL = os.getenv(
    "SEARXNG_URL",
    "http://searxng.railway.internal:8080/search",
)


bot = Bot(
    token=API_TOKEN,
    default=DefaultBotProperties(
        parse_mode="HTML",
    ),
)

dp = Dispatcher()
router = Router()

dp.include_router(router)


BOT_INFO = None


# ==========================================
# Gemini
# ==========================================

gemini_api_key = os.getenv("GEMINI_API_KEY", "")

if not gemini_api_key:
    raise ValueError(
        "CRITICAL CONFIGURATION ERROR: "
        "'GEMINI_API_KEY' missing."
    )

gemini_client = genai.Client(
    api_key=gemini_api_key,
)


# Primary model.
PRIMARY_MODEL = "gemini-3.5-flash-lite"

# Fallback model.
#
# We intentionally use another current 3.x model instead
# of falling back to the old 2.5 model.
FALLBACK_MODEL = "gemini-3.5-flash"


OWNER_ID = int(
    os.getenv("OWNER_ID", "0")
)


# ==========================================
# Helpers
# ==========================================

def get_dismiss_keyboard(
    user_id: int,
) -> InlineKeyboardMarkup:
    """Creates the interactive dismiss button."""

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Dismiss",
                    callback_data=f"dismiss_{user_id}",
                )
            ]
        ]
    )


@router.callback_query(
    F.data.startswith("dismiss_")
)
async def handle_dismiss_callback(
    callback: CallbackQuery,
):
    try:
        target_id = int(
            callback.data.split("_", 1)[1]
        )

    except (
        IndexError,
        ValueError,
        AttributeError,
    ):
        await callback.answer(
            "Invalid callback data.",
            show_alert=True,
        )
        return

    if callback.from_user.id != target_id:
        await callback.answer(
            "You cannot dismiss this menu.",
            show_alert=True,
        )
        return

    try:
        if callback.message:
            await callback.message.delete()

        await callback.answer(
            "Menu closed."
        )

    except Exception:
        await callback.answer(
            "Failed to delete message.",
            show_alert=True,
        )


# ==========================================
# Telegram HTML Helpers
# ==========================================

def clean_telegram_html(text: str) -> str:
    """
    Cleans common Markdown artifacts from Gemini output.

    Gemini is instructed to return Telegram HTML, but models
    occasionally decide Markdown is their true calling.
    This removes the obvious offenders without destroying
    legitimate HTML.
    """

    if not text:
        return ""

    text = text.strip()

    # Remove Markdown code fences.
    text = re.sub(
        r"```(?:html|markdown|text)?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )

    text = text.replace(
        "```",
        "",
    )

    # Remove bullet glyphs that Gemini may insert.
    text = text.replace(
        "\u2022",
        "",
    )

    # Remove Markdown heading markers.
    text = re.sub(
        r"(?m)^\s{0,3}#{1,6}\s+",
        "",
        text,
    )

    # Convert common Markdown bold/italic into HTML.
    text = re.sub(
        r"\*\*(.+?)\*\*",
        r"<b>\1</b>",
        text,
        flags=re.DOTALL,
    )

    text = re.sub(
        r"(?<!\*)\*([^*\n]+?)\*(?!\*)",
        r"<i>\1</i>",
        text,
    )

    # Remove unsupported Markdown table separators.
    text = re.sub(
        r"(?m)^\s*\|?(?:\s*:?-+:?\s*\|)+\s*$",
        "",
        text,
    )

    # Telegram accepts these common HTML tags.
    # Nothing else should be blindly introduced.
    allowed_tags = {
        "b",
        "strong",
        "i",
        "em",
        "u",
        "ins",
        "s",
        "strike",
        "del",
        "code",
        "pre",
        "a",
        "blockquote",
        "tg-spoiler",
        "span",
    }

    # Remove HTML tags that Telegram doesn't support.
    def sanitize_tag(match):
        tag = match.group(0)

        tag_name_match = re.match(
            r"</?\s*([a-zA-Z0-9-]+)",
            tag,
        )

        if not tag_name_match:
            return ""

        tag_name = (
            tag_name_match.group(1)
            .lower()
        )

        if tag_name not in allowed_tags:
            return html.escape(tag)

        return tag

    text = re.sub(
        r"</?[^>]+>",
        sanitize_tag,
        text,
    )

    return text.strip()


async def safe_answer(
    message: Message,
    text: str,
    reply_to: int | None = None,
):
    """
    Sends Telegram HTML.

    If Gemini accidentally generates malformed HTML,
    retry once as plain text instead of letting the bot
    explode over a formatting tag.
    """

    text = clean_telegram_html(text)

    if not text:
        text = "I didn't receive a response."

    preview_opts = LinkPreviewOptions(
        is_disabled=False,
        prefer_small_media=True,
    )

    try:
        if reply_to is not None:
            return await message.answer(
                text=text,
                reply_to_message_id=reply_to,
                link_preview_options=preview_opts,
            )

        return await message.answer(
            text=text,
            link_preview_options=preview_opts,
        )

    except Exception as first_error:
        print(
            "Telegram HTML send failed: "
            f"{first_error}"
        )

        # Fall back to plain text.
        plain_text = re.sub(
            r"<[^>]+>",
            "",
            text,
        )

        plain_text = html.unescape(
            plain_text
        )

        if reply_to is not None:
            return await message.answer(
                text=plain_text,
                reply_to_message_id=reply_to,
            )

        return await message.answer(
            text=plain_text,
        )


# ==========================================
# SearXNG Search
# ==========================================

async def free_web_search(
    query: str,
) -> str:
    """
    Searches the Railway-hosted SearXNG instance.

    SearXNG_URL should point directly to:
        /search

    Example:
        http://searxng.railway.internal:8080/search
    """

    query = query.strip()

    if not query:
        return ""

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/137.0.0.0 "
            "Safari/537.36"
        ),
        "Accept": "application/json",
    }

    params = {
        "q": query,
        "format": "json",
        "language": "en",
        "safesearch": 0,
    }

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
        ) as client:

            response = await client.get(
                SEARXNG_URL,
                params=params,
                headers=headers,
                timeout=10.0,
            )

        if response.status_code != 200:
            print(
                "SearXNG returned HTTP "
                f"{response.status_code}: "
                f"{response.text[:500]}"
            )
            return ""

        try:
            data = response.json()

        except Exception as json_error:
            print(
                "SearXNG returned invalid JSON: "
                f"{json_error}"
            )
            return ""

        results = data.get(
            "results",
            [],
        )[:10]

        snippets = []

        for item in results:
            title = item.get(
                "title",
                "",
            ).strip()

            content = item.get(
                "content",
                "",
            ).strip()

            url = item.get(
                "url",
                "",
            ).strip()

            if not (
                title
                or content
                or url
            ):
                continue

            snippets.append(
                f"Title: {title}\n"
                f"Content: {content}\n"
                f"URL: {url}"
            )

        if snippets:
            return "\n\n".join(
                snippets
            )

        print(
            f"SearXNG returned no results "
            f"for query: {query}"
        )

    except Exception as error:
        print(
            "SearXNG request failed: "
            f"{error}"
        )

    return ""


# ==========================================
# Memory
# ==========================================

async def get_formatted_memories(
    user_id_str: str,
) -> str:

    try:
        raw_items = await redis_client.lrange(
            f"memory_list:{user_id_str}",
            0,
            -1,
        )

        if not raw_items:
            return (
                "<b>Active Memory Directives</b>\n\n"
                "Your memory list is currently empty."
            )

        memories = [
            item.decode("utf-8")
            if isinstance(item, bytes)
            else item
            for item in raw_items
        ]

        lines = [
            "<b>Active Memory Directives</b>",
            "",
        ]

        for index, memory in enumerate(
            memories,
            start=1,
        ):
            lines.append(
                f"<b>[{index}]</b> "
                f"{html.escape(memory)}"
            )

        return "\n".join(lines)

    except Exception as error:
        print(
            "Error fetching memory list: "
            f"{error}"
        )

        return (
            "Could not retrieve memory list."
        )


# ==========================================
# Audio Engine
# ==========================================

async def send_audio_track(
    chat_id: int,
    msg_id: int,
    key: str,
    file_path: str,
    title: str,
    performer: str,
    is_private: bool,
):

    try:
        cached_id = await redis_client.get(
            f"audio_cache:{key}"
        )

        reply_to = (
            None
            if is_private
            else msg_id
        )

        msg = None

        async def attempt_send(
            audio_payload,
        ):
            if isinstance(
                audio_payload,
                str,
            ):
                return await bot.send_audio(
                    chat_id=chat_id,
                    audio=audio_payload,
                    title=title,
                    performer=performer,
                    reply_to_message_id=reply_to,
                )

            audio_file = FSInputFile(
                audio_payload
            )

            return await bot.send_audio(
                chat_id=chat_id,
                audio=audio_file,
                title=title,
                performer=performer,
                reply_to_message_id=reply_to,
            )

        if cached_id:

            cached_value = (
                cached_id.decode("utf-8")
                if isinstance(
                    cached_id,
                    bytes,
                )
                else cached_id
            )

            try:
                msg = await attempt_send(
                    cached_value
                )

            except Exception as error:

                if (
                    "message to be replied not found"
                    in str(error).lower()
                ):
                    msg = await bot.send_audio(
                        chat_id=chat_id,
                        audio=cached_value,
                        title=title,
                        performer=performer,
                    )
                else:
                    raise

        elif os.path.exists(file_path):

            try:
                msg = await attempt_send(
                    file_path
                )

            except Exception as error:

                if (
                    "message to be replied not found"
                    in str(error).lower()
                ):
                    audio_file = FSInputFile(
                        file_path
                    )

                    msg = await bot.send_audio(
                        chat_id=chat_id,
                        audio=audio_file,
                        title=title,
                        performer=performer,
                    )

                else:
                    raise

        if (
            msg
            and msg.audio
            and msg.audio.file_id
        ):
            await redis_client.set(
                f"audio_cache:{key}",
                msg.audio.file_id,
            )

    except Exception as error:
        print(
            f"Error sending audio ({key}): "
            f"{error}"
        )


# ==========================================
# Aiogram Handlers
# ==========================================

@router.message(
    Command("delete", "del")
)
async def handle_delete(
    message: Message,
):

    if (
        not message.from_user
        or message.from_user.id != OWNER_ID
    ):
        return

    chat_id = message.chat.id
    reply_msg = message.reply_to_message

    if (
        reply_msg
        and BOT_INFO
        and reply_msg.from_user
        and reply_msg.from_user.id
        == BOT_INFO.id
    ):
        try:
            await bot.delete_message(
                chat_id,
                reply_msg.message_id,
            )
        except Exception:
            pass

    try:
        await bot.delete_message(
            chat_id,
            message.message_id,
        )
    except Exception:
        pass


@router.message(
    Command("help", "commands")
)
async def handle_help(
    message: Message,
):

    try:
        await message.delete()
    except Exception:
        pass

    content = (
        "<b>Sen Bot Command Hub</b>\n\n"

        "<code>remember [item],, [item2]</code>\n"
        "Adds items to memory\n\n"

        "<code>what do you remember</code>\n"
        "Displays memories in a formatted list\n\n"

        "<code>edit [number] [new fact]</code>\n"
        "Edits a specific memory\n\n"

        "<code>forget [number],, [number2]</code>\n"
        "Removes memories\n\n"

        "<code>forget all</code>\n"
        "Clears all memory"
    )

    await message.answer(
        text=content,
        reply_markup=get_dismiss_keyboard(
            message.from_user.id
        ),
    )


def text_in(
    options: set,
):
    return lambda message: (
        message.text
        and message.text.lower()
        in options
    )


def text_startswith(
    prefix: str,
):
    return lambda message: (
        message.text
        and message.text.lower()
        .startswith(prefix)
    )


@router.message(
    text_in(
        {
            "what do you remember",
            "how do you remember",
        }
    )
)
async def handle_what_remember(
    message: Message,
):

    try:
        await message.delete()
    except Exception:
        pass

    user_id_str = str(
        message.from_user.id
    )

    content = await get_formatted_memories(
        user_id_str
    )

    await message.answer(
        text=content,
        reply_markup=get_dismiss_keyboard(
            message.from_user.id
        ),
    )


@router.message(
    text_startswith("remember ")
)
async def handle_remember(
    message: Message,
):

    try:
        await message.delete()
    except Exception:
        pass

    user_id_str = str(
        message.from_user.id
    )

    clean_prompt = (
        message.text.strip()
    )

    parts = [
        part.strip()[:200]
        for part in clean_prompt[9:]
        .split(",,")
        if part.strip()
    ]

    for part in parts[:10]:

        try:
            position = await redis_client.lpos(
                f"memory_list:{user_id_str}",
                part,
            )

            if position is None:
                await redis_client.rpush(
                    f"memory_list:{user_id_str}",
                    part,
                )

        except Exception as error:
            print(
                f"Memory save error: {error}"
            )

    await redis_client.ltrim(
        f"memory_list:{user_id_str}",
        -25,
        -1,
    )

    content = await get_formatted_memories(
        user_id_str
    )

    await message.answer(
        text=content,
        reply_markup=get_dismiss_keyboard(
            message.from_user.id
        ),
    )


@router.message(
    text_startswith("edit ")
)
async def handle_edit(
    message: Message,
):

    try:
        await message.delete()
    except Exception:
        pass

    user_id_str = str(
        message.from_user.id
    )

    clean_prompt = (
        message.text.strip()
    )

    parts = (
        clean_prompt[5:]
        .strip()
        .split(" ", 1)
    )

    if (
        len(parts) == 2
        and parts[0].isdigit()
    ):

        index = (
            int(parts[0]) - 1
        )

        new_value = (
            parts[1]
            .strip()
            [:200]
        )

        raw_items = await redis_client.lrange(
            f"memory_list:{user_id_str}",
            0,
            -1,
        )

        if 0 <= index < len(raw_items):

            await redis_client.lset(
                f"memory_list:{user_id_str}",
                index,
                new_value,
            )

            content = (
                await get_formatted_memories(
                    user_id_str
                )
            )

        else:

            content = (
                "Invalid memory index.\n\n"
                + await get_formatted_memories(
                    user_id_str
                )
            )

    else:

        content = (
            "Usage: "
            "<code>edit [number] [new text]</code>"
        )

    await message.answer(
        text=content,
        reply_markup=get_dismiss_keyboard(
            message.from_user.id
        ),
    )


@router.message(
    F.text.func(
        lambda text: (
            text
            and text.lower()
            == "forget all"
        )
    )
)
async def handle_forget_all(
    message: Message,
):

    try:
        await message.delete()
    except Exception:
        pass

    user_id_str = str(
        message.from_user.id
    )

    chat_id = message.chat.id

    await redis_client.delete(
        f"memory_list:{user_id_str}",
        f"chat_history:{chat_id}:{user_id_str}",
    )

    await message.answer(
        text="Cleared all your saved memories.",
        reply_markup=get_dismiss_keyboard(
            message.from_user.id
        ),
    )


@router.message(
    text_startswith("forget ")
)
async def handle_forget(
    message: Message,
):

    try:
        await message.delete()
    except Exception:
        pass

    user_id_str = str(
        message.from_user.id
    )

    clean_prompt = (
        message.text.strip()
    )

    try:

        indices = [
            int(number.strip()) - 1
            for number in (
                clean_prompt[7:]
                .split(",,")
            )
            if number.strip().isdigit()
        ]

        raw_items = await redis_client.lrange(
            f"memory_list:{user_id_str}",
            0,
            -1,
        )

        if raw_items and indices:

            memories = [
                item.decode("utf-8")
                if isinstance(item, bytes)
                else item
                for item in raw_items
            ]

            for index in sorted(
                set(indices),
                reverse=True,
            ):
                if 0 <= index < len(memories):
                    memories.pop(index)

            await redis_client.delete(
                f"memory_list:{user_id_str}"
            )

            if memories:
                await redis_client.rpush(
                    f"memory_list:{user_id_str}",
                    *memories,
                )

    except Exception as error:
        print(
            f"Memory deletion error: {error}"
        )

    content = await get_formatted_memories(
        user_id_str
    )

    await message.answer(
        text=content,
        reply_markup=get_dismiss_keyboard(
            message.from_user.id
        ),
    )


# ==========================================
# Primary Chat
# ==========================================

@router.message(
    F.text | F.caption | F.voice | F.audio
)
async def handle_conversation(
    message: Message,
):

    text = (
        message.text
        or message.caption
        or ""
    )

    text_no_html = re.sub(
        r"<[^>]+>",
        "",
        text,
    )

    # ------------------------------------------
    # Audio triggers
    # ------------------------------------------

    if re.search(
        r"\bsen\b",
        text_no_html,
        re.IGNORECASE,
    ):

        asyncio.create_task(
            send_audio_track(
                message.chat.id,
                message.message_id,
                "sen",
                "Devin_The_Dude_Anythang.mp3",
                "Anythang",
                "Devin The Dude",
                message.chat.type
                == "private",
            )
        )

    if re.search(
        r"\bmagic(?:al|ally)?\b",
        text_no_html,
        re.IGNORECASE,
    ):

        asyncio.create_task(
            send_audio_track(
                message.chat.id,
                message.message_id,
                "magic",
                "Do You Believe In Magic.mp3",
                "Do You Believe In Magic",
                "The Lovin' Spoonful",
                message.chat.type
                == "private",
            )
        )

    # ------------------------------------------
    # Determine whether bot should respond
    # ------------------------------------------

    bot_username = (
        f"@{BOT_INFO.username}"
        if (
            BOT_INFO
            and BOT_INFO.username
        )
        else ""
    )

    is_tagged = (
        bool(bot_username)
        and bot_username.lower()
        in text_no_html.lower()
    ) or (
        "@gemini"
        in text_no_html.lower()
    )

    is_reply_to_bot = bool(
        message.reply_to_message
        and BOT_INFO
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id
        == BOT_INFO.id
    )

    is_private = (
        message.chat.type
        == "private"
    )

    if not (
        is_tagged
        or is_reply_to_bot
        or is_private
        or message.content_type
        in ["voice", "audio"]
    ):
        return

    user_id_str = str(
        message.from_user.id
    )

    chat_id = message.chat.id
    msg_id = message.message_id

    # ------------------------------------------
    # Clean prompt
    # ------------------------------------------

    clean_prompt = text

    if bot_username:
        clean_prompt = re.sub(
            re.escape(bot_username),
            "",
            clean_prompt,
            flags=re.IGNORECASE,
        )

    clean_prompt = re.sub(
        r"@gemini\b",
        "",
        clean_prompt,
        flags=re.IGNORECASE,
    ).strip()

    # ------------------------------------------
    # Cooldown
    # ------------------------------------------

    cooldown_key = (
        f"cooldown:{user_id_str}"
    )

    if await redis_client.exists(
        cooldown_key
    ):

        warn_content = (
            "Slow down, request limit reached."
        )

        if is_private:

            await message.answer(
                text=warn_content
            )

        else:

            await message.answer(
                text=warn_content,
                reply_to_message_id=msg_id,
            )

        return

    await redis_client.set(
        cooldown_key,
        "1",
        ex=4,
    )

    # ------------------------------------------
    # Reply context
    # ------------------------------------------

    replied = (
        message.reply_to_message
    )

    replied_context = ""

    if replied:

        replied_context = (
            replied.text
            or replied.caption
            or ""
        )

    # ------------------------------------------
    # Voice/audio processing
    # ------------------------------------------

    audio_bytes = None
    audio_mime = "audio/ogg"

    if message.content_type in [
        "voice",
        "audio",
    ]:

        audio_obj = (
            message.voice
            or message.audio
        )

        if audio_obj:

            file_info = (
                await bot.get_file(
                    audio_obj.file_id
                )
            )

            audio_stream = (
                await bot.download_file(
                    file_info.file_path
                )
            )

            if audio_stream:
                audio_bytes = (
                    audio_stream.read()
                )

            if (
                hasattr(
                    audio_obj,
                    "mime_type",
                )
                and audio_obj.mime_type
            ):
                audio_mime = (
                    audio_obj.mime_type
                )

    if (
        not clean_prompt
        and replied_context
    ):

        clean_prompt = (
            "What are your thoughts on this?"
        )

    if not (
        clean_prompt
        or replied_context
        or audio_bytes
    ):
        return

    # ==========================================
    # Gemini Processing
    # ==========================================

    try:

        # ------------------------------------------
        # Memories
        # ------------------------------------------

        raw_mem = await redis_client.lrange(
            f"memory_list:{user_id_str}",
            0,
            -1,
        )

        saved_facts = [
            item.decode("utf-8")
            if isinstance(item, bytes)
            else item
            for item in raw_mem
        ]

        # ------------------------------------------
        # Chat history
        # ------------------------------------------

        history_key = (
            f"chat_history:"
            f"{chat_id}:"
            f"{user_id_str}"
        )

        raw_hist = await redis_client.lrange(
            history_key,
            0,
            -1,
        )

        chat_history = [
            item.decode("utf-8")
            if isinstance(item, bytes)
            else item
            for item in raw_hist
        ]

        # ------------------------------------------
        # Search detection
        # ------------------------------------------

        search_keywords = {
            "search",
            "google",
            "look up",
            "lookup",
            "find",
            "show",
            "show me",
            "table",
            "list",
            "info",
        }

        lower_prompt = (
            clean_prompt.lower()
        )

        explicit_search = any(
            keyword in lower_prompt
            for keyword in search_keywords
        )

        search_query = (
            clean_prompt
        )

        if (
            explicit_search
            and len(
                clean_prompt.split()
            ) <= 4
        ):

            if (
                replied_context
                and "I don't have enough details"
                not in replied_context
                and "I am currently broken"
                not in replied_context
            ):

                search_query = (
                    replied_context
                )

            elif chat_history:

                for past_msg in reversed(
                    chat_history
                ):

                    if (
                        past_msg.startswith(
                            "User: "
                        )
                        and len(
                            past_msg.split()
                        ) > 2
                    ):

                        search_query = (
                            past_msg.replace(
                                "User: ",
                                "",
                                1,
                            ).strip()
                        )

                        break

        search_context = ""

        if (
            explicit_search
            and search_query
        ):

            search_context = (
                await free_web_search(
                    search_query
                )
            )

        # ------------------------------------------
        # Context
        # ------------------------------------------

        context_parts = []

        if replied_context:

            context_parts.append(
                "Message User is Replying To:\n"
                f"\"{replied_context}\""
            )

        if chat_history:

            context_parts.append(
                "Recent Conversation Context:\n"
                + "\n".join(
                    chat_history
                )
            )

        if search_context:

            context_parts.append(
                "Web Search Context:\n"
                + search_context
            )

        final_prompt = (
            clean_prompt
            if clean_prompt
            else "Process and answer this voice note."
        )

        if context_parts:

            final_prompt = (
                "\n\n".join(
                    context_parts
                )
                + "\n\nUser Question: "
                + final_prompt
            )

        # ------------------------------------------
        # System instructions
        # ------------------------------------------

        today_str = datetime.now(
            timezone.utc
        ).strftime(
            "%A, %B %d, %Y"
        )

        bot_instructions = (
            f"Today's date is {today_str}.\n\n"

            "Keep responses structural using double "
            "line-breaks to separate ideas.\n"

            "Never use standard AI pleasantries.\n"

            "Do not start responses with 'As an AI' "
            "or end with generic offers for help.\n"

            "Keep casual replies brief, but dynamically "
            "expand your response length when explicitly "
            "asked for details or when playing "
            "interactive games.\n"

            "If the user changes the subject abruptly, "
            "drop the previous topic immediately and "
            "adapt to the new flow.\n"

            "If the user is clearly joking or sarcastic, "
            "match their energy rather than taking the "
            "prompt literally.\n"

            "If you do not know the answer or the provided "
            "context is insufficient, state exactly: "
            "'I don't have enough details to answer that "
            "accurately' without guessing.\n"

            "Do not assume personal details about the user "
            "unless they are explicitly provided in the "
            "memory list.\n\n"

            "CRITICAL FORMATTING RULE:\n"
            "Return valid Telegram HTML only.\n\n"

            "Use <b>text</b> for bold.\n"
            "Use <i>text</i> for italics.\n"
            "Use <u>text</u> for underline.\n"
            "Use <s>text</s> for strikethrough.\n"
            "Use <code>text</code> for inline code.\n"
            "Use <pre>text</pre> for fixed-width blocks.\n"
            "Use <a href=\"URL\">text</a> for hyperlinks.\n\n"

            "Do NOT use Markdown syntax such as "
            "**, ##, backticks, or pipe tables.\n\n"

            "Do not wrap the entire answer in a code block.\n"

            "Generate clean Telegram-compatible HTML."
        )

        # ------------------------------------------
        # Search instructions
        # ------------------------------------------

        if search_context:

            bot_instructions += (
                "\n\nWhen referencing Web Search Context, "
                "state the information directly without "
                "saying 'According to my search' or "
                "'I found this online'.\n\n"

                "If the user explicitly asks for links, "
                "sources, or URLs, cite them using "
                "Telegram HTML hyperlinks.\n\n"

                "Use this format:\n"
                "<a href=\"URL\">Source Title</a>\n\n"

                "Do not output raw URLs when a hyperlink "
                "can be used.\n\n"

                "If the user does not explicitly ask for "
                "sources, do not include URLs."
            )

        # ------------------------------------------
        # Chat history instructions
        # ------------------------------------------

        if chat_history:

            bot_instructions += (
                "\n\nUse the Recent Conversation Context "
                "to track pronouns and subjects, but "
                "never summarize or repeat the history "
                "back to the user."
            )

        # ------------------------------------------
        # Saved memories
        # ------------------------------------------

        if saved_facts:

            bot_instructions += (
                "\n\nYou must strictly follow these "
                "User Instructions:\n"
                + "\n".join(
                    f"- {fact}"
                    for fact in saved_facts
                )
            )

        # ------------------------------------------
        # Safety settings
        # ------------------------------------------

        safety_overrides = [
            types.SafetySetting(
                category=(
                    "HARM_CATEGORY_HATE_SPEECH"
                ),
                threshold="BLOCK_NONE",
            ),
            types.SafetySetting(
                category=(
                    "HARM_CATEGORY_HARASSMENT"
                ),
                threshold="BLOCK_NONE",
            ),
            types.SafetySetting(
                category=(
                    "HARM_CATEGORY_SEXUALLY_EXPLICIT"
                ),
                threshold="BLOCK_NONE",
            ),
            types.SafetySetting(
                category=(
                    "HARM_CATEGORY_DANGEROUS_CONTENT"
                ),
                threshold="BLOCK_NONE",
            ),
        ]

        # ==========================================
        # Generate response
        # ==========================================

        response = None
        used_model = None

        # ------------------------------------------
        # Audio
        # ------------------------------------------

        if audio_bytes:

            contents = [
                types.Part.from_bytes(
                    data=audio_bytes,
                    mime_type=audio_mime,
                ),
                final_prompt,
            ]

            try:

                response = (
                    await gemini_client
                    .aio
                    .models
                    .generate_content(
                        model=PRIMARY_MODEL,
                        contents=contents,
                        config=(
                            types.GenerateContentConfig(
                                system_instruction=(
                                    bot_instructions
                                ),
                                safety_settings=(
                                    safety_overrides
                                ),
                            )
                        ),
                    )
                )

                used_model = (
                    PRIMARY_MODEL
                )

            except Exception as primary_error:

                print(
                    "Primary audio model failed: "
                    f"{primary_error}"
                )

                print(
                    f"Trying fallback model "
                    f"{FALLBACK_MODEL}..."
                )

                response = (
                    await gemini_client
                    .aio
                    .models
                    .generate_content(
                        model=FALLBACK_MODEL,
                        contents=contents,
                        config=(
                            types.GenerateContentConfig(
                                system_instruction=(
                                    bot_instructions
                                ),
                                safety_settings=(
                                    safety_overrides
                                ),
                            )
                        ),
                    )
                )

                used_model = (
                    FALLBACK_MODEL
                )

        # ------------------------------------------
        # Normal text
        # ------------------------------------------

        else:

            try:

                chat = (
                    gemini_client
                    .aio
                    .chats
                    .create(
                        model=PRIMARY_MODEL,
                        config=(
                            types.GenerateContentConfig(
                                system_instruction=(
                                    bot_instructions
                                ),
                                safety_settings=(
                                    safety_overrides
                                ),
                            )
                        ),
                    )
                )

                response = (
                    await chat.send_message(
                        final_prompt
                    )
                )

                used_model = (
                    PRIMARY_MODEL
                )

            except Exception as primary_error:

                print(
                    "Primary model failed: "
                    f"{primary_error}"
                )

                print(
                    f"Trying fallback model "
                    f"{FALLBACK_MODEL}..."
                )

                chat = (
                    gemini_client
                    .aio
                    .chats
                    .create(
                        model=FALLBACK_MODEL,
                        config=(
                            types.GenerateContentConfig(
                                system_instruction=(
                                    bot_instructions
                                ),
                                safety_settings=(
                                    safety_overrides
                                ),
                            )
                        ),
                    )
                )

                response = (
                    await chat.send_message(
                        final_prompt
                    )
                )

                used_model = (
                    FALLBACK_MODEL
                )

        # ------------------------------------------
        # Response extraction
        # ------------------------------------------

        response_text = ""

        if response:

            try:
                response_text = (
                    response.text
                    or ""
                )
            except Exception:
                response_text = ""

        if not response_text:

            response_text = (
                "I didn't receive a response."
            )

        print(
            f"Gemini response generated using "
            f"{used_model}"
        )

        # ------------------------------------------
        # Send Telegram response
        #
        # NO DRAFT API.
        # Just normal sendMessage through aiogram.
        # ------------------------------------------

        if is_private:

            await safe_answer(
                message,
                response_text,
            )

        else:

            await safe_answer(
                message,
                response_text,
                reply_to=msg_id,
            )

        # ------------------------------------------
        # Save history
        # ------------------------------------------

        clean_history_text = re.sub(
            r"<[^>]+>",
            "",
            response_text,
        )

        clean_history_text = (
            html.unescape(
                clean_history_text
            )
        )

        await redis_client.rpush(
            history_key,
            (
                "User: "
                + (
                    clean_prompt
                    or "Voice Note"
                )
            ),
            (
                "Bot: "
                + clean_history_text
            ),
        )

        await redis_client.ltrim(
            history_key,
            -10,
            -1,
        )

    except Exception as ai_error:

        print(
            "Gemini API error: "
            f"{ai_error}"
        )

        error_string = (
            str(ai_error)
            .lower()
        )

        if (
            "429" in error_string
            or "resource exhausted"
            in error_string
        ):

            error_content = (
                "Whoa, I'm getting a little "
                "overwhelmed! Let me catch my "
                "breath for a minute."
            )

        else:

            error_content = (
                "I am currently broken right now, "
                "the owner needs to fix me."
            )

        if is_private:

            await message.answer(
                text=error_content
            )

        else:

            await message.answer(
                text=error_content,
                reply_to_message_id=msg_id,
            )


# ==========================================
# Healthcheck
# ==========================================

async def health_check(
    request,
):
    return web.Response(
        text="200 OK - Bot is running.",
        status=200,
    )


# ==========================================
# Main
# ==========================================

async def main():

    global BOT_INFO

    try:

        print(
            "Clearing conflicting webhooks "
            "from Telegram servers..."
        )

        await bot.delete_webhook(
            drop_pending_updates=True
        )

        BOT_INFO = (
            await bot.get_me()
        )

        print(
            "Bot authenticated as "
            f"@{BOT_INFO.username}"
        )

        print(
            "Gemini primary model: "
            f"{PRIMARY_MODEL}"
        )

        print(
            "Gemini fallback model: "
            f"{FALLBACK_MODEL}"
        )

        print(
            "SearXNG endpoint: "
            f"{SEARXNG_URL}"
        )

    except Exception as error:

        print(
            "Failed to initialize bot: "
            f"{error}"
        )

    app = web.Application()

    app.router.add_get(
        "/",
        health_check,
    )

    app.router.add_get(
        "/health",
        health_check,
    )

    runner = web.AppRunner(
        app
    )

    await runner.setup()

    port = int(
        os.environ.get(
            "PORT",
            "8080",
        )
    )

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port,
    )

    await site.start()

    print(
        "Healthcheck server listening "
        f"on port {port}"
    )

    try:

        await dp.start_polling(
            bot
        )

    finally:

        print(
            "SIGTERM received! "
            "Cleaning up connections..."
        )

        await bot.session.close()

        await redis_client.aclose()

        await runner.cleanup()

        print(
            "Cleanup complete. "
            "Process exiting."
        )


if __name__ == "__main__":
    asyncio.run(
        main()
    )
