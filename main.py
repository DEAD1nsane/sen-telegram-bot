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

# Railway variable:
# SEARXNG_URL=http://searxng.railway.internal:8080/search
SEARXNG_URL = os.getenv(
    "SEARXNG_URL",
    "http://searxng.railway.internal:8080/search",
).rstrip("/")

if not API_TOKEN:
    raise ValueError(
        "CRITICAL CONFIGURATION ERROR: 'BOT_TOKEN' missing."
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


gemini_api_key = os.getenv(
    "GEMINI_API_KEY",
    "",
)

if not gemini_api_key:
    raise ValueError(
        "CRITICAL CONFIGURATION ERROR: "
        "'GEMINI_API_KEY' missing."
    )

gemini_client = genai.Client(
    api_key=gemini_api_key,
)

OWNER_ID = int(
    os.getenv(
        "OWNER_ID",
        "0",
    )
)


# ==========================================
# Redis Interactive State
# ==========================================

INTERACTION_TTL = 300


def interaction_key(user_id: int) -> str:
    return f"memory_interaction:{user_id}"


async def set_interaction(
    user_id: int,
    action: str,
):
    await redis_client.set(
        interaction_key(user_id),
        action,
        ex=INTERACTION_TTL,
    )


async def get_interaction(
    user_id: int,
):
    value = await redis_client.get(
        interaction_key(user_id)
    )

    if isinstance(value, bytes):
        return value.decode("utf-8")

    return value


async def clear_interaction(
    user_id: int,
):
    await redis_client.delete(
        interaction_key(user_id)
    )


# ==========================================
# Memory Helpers
# ==========================================

async def get_memories(
    user_id_str: str,
):
    try:
        raw_items = await redis_client.lrange(
            f"memory_list:{user_id_str}",
            0,
            -1,
        )

        return [
            item.decode("utf-8")
            if isinstance(item, bytes)
            else str(item)
            for item in raw_items
        ]

    except Exception as e:
        print(
            f"Error retrieving memories: {e}"
        )
        return []


async def get_formatted_memories(
    user_id_str: str,
) -> str:

    memories = await get_memories(
        user_id_str
    )

    if not memories:
        return (
            "<b>🧠 Your Memories</b>\n\n"
            "Your memory list is currently empty."
        )

    lines = [
        "<b>🧠 Your Memories</b>",
        "",
    ]

    for index, memory in enumerate(
        memories,
        start=1,
    ):
        safe_memory = html.escape(
            memory
        )

        lines.append(
            f"<b>{index}.</b> {safe_memory}"
        )

    return "\n".join(lines)


# ==========================================
# Interactive Menu Keyboards
# ==========================================

def main_menu_keyboard(
    user_id: int,
) -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🧠 Memories",
                    callback_data="memory_view",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="➕ Remember",
                    callback_data="memory_add",
                ),
                InlineKeyboardButton(
                    text="✏️ Edit",
                    callback_data="memory_edit",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Forget",
                    callback_data="memory_forget",
                ),
                InlineKeyboardButton(
                    text="🔥 Forget All",
                    callback_data="memory_forget_all",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ Close",
                    callback_data=f"memory_close:{user_id}",
                ),
            ],
        ]
    )


def memories_keyboard(
    user_id: int,
) -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Remember",
                    callback_data="memory_add",
                ),
                InlineKeyboardButton(
                    text="✏️ Edit",
                    callback_data="memory_edit",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Forget",
                    callback_data="memory_forget",
                ),
                InlineKeyboardButton(
                    text="🔥 Forget All",
                    callback_data="memory_forget_all",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Back",
                    callback_data="memory_back",
                ),
                InlineKeyboardButton(
                    text="❌ Close",
                    callback_data=f"memory_close:{user_id}",
                ),
            ],
        ]
    )


def back_close_keyboard(
    user_id: int,
) -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Back",
                    callback_data="memory_back",
                ),
                InlineKeyboardButton(
                    text="❌ Close",
                    callback_data=f"memory_close:{user_id}",
                ),
            ]
        ]
    )


def forget_all_confirmation_keyboard(
    user_id: int,
) -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔥 Yes, delete everything",
                    callback_data="memory_confirm_forget_all",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Cancel",
                    callback_data="memory_back",
                ),
                InlineKeyboardButton(
                    text="❌ Close",
                    callback_data=f"memory_close:{user_id}",
                ),
            ],
        ]
    )


# ==========================================
# /help
# ==========================================

@router.message(Command("help"))
async def handle_help(
    message: Message,
):

    try:
        await message.delete()
    except Exception:
        pass

    await clear_interaction(
        message.from_user.id
    )

    content = (
        "<b>🤖 Sen Bot</b>\n\n"
        "Use the buttons below to manage your "
        "personal memory."
    )

    await message.answer(
        text=content,
        reply_markup=main_menu_keyboard(
            message.from_user.id
        ),
    )


# Keep /commands as an alias
@router.message(Command("commands"))
async def handle_commands(
    message: Message,
):

    try:
        await message.delete()
    except Exception:
        pass

    await clear_interaction(
        message.from_user.id
    )

    content = (
        "<b>🤖 Sen Bot</b>\n\n"
        "Use the buttons below to manage your "
        "personal memory."
    )

    await message.answer(
        text=content,
        reply_markup=main_menu_keyboard(
            message.from_user.id
        ),
    )


# ==========================================
# Memory Button: View
# ==========================================

@router.callback_query(
    F.data == "memory_view"
)
async def handle_memory_view(
    callback: CallbackQuery,
):

    await clear_interaction(
        callback.from_user.id
    )

    content = await get_formatted_memories(
        str(callback.from_user.id)
    )

    await callback.answer()

    if not callback.message:
        return

    try:
        await callback.message.edit_text(
            text=content,
            reply_markup=memories_keyboard(
                callback.from_user.id
            ),
        )
    except Exception as e:
        print(
            f"Memory view edit error: {e}"
        )


# ==========================================
# Memory Button: Add
# ==========================================

@router.callback_query(
    F.data == "memory_add"
)
async def handle_memory_add(
    callback: CallbackQuery,
):

    await set_interaction(
        callback.from_user.id,
        "add",
    )

    await callback.answer(
        "Send me the memory you want to save."
    )

    if not callback.message:
        return

    content = (
        "<b>➕ Remember Something</b>\n\n"
        "Send the fact or instruction you want "
        "me to remember.\n\n"
        "You can save multiple items at once "
        "by separating them with <code>,,</code>."
    )

    try:
        await callback.message.edit_text(
            text=content,
            reply_markup=back_close_keyboard(
                callback.from_user.id
            ),
        )
    except Exception as e:
        print(
            f"Memory add menu error: {e}"
        )


# ==========================================
# Memory Button: Edit
# ==========================================

@router.callback_query(
    F.data == "memory_edit"
)
async def handle_memory_edit(
    callback: CallbackQuery,
):

    memories = await get_memories(
        str(callback.from_user.id)
    )

    await set_interaction(
        callback.from_user.id,
        "edit_number",
    )

    await callback.answer(
        "Choose the memory number to edit."
    )

    if not callback.message:
        return

    if not memories:
        content = (
            "<b>✏️ Edit Memory</b>\n\n"
            "You don't have any saved memories "
            "to edit yet."
        )
    else:
        lines = [
            "<b>✏️ Edit Memory</b>",
            "",
            "Which memory number do you want "
            "to edit?",
            "",
        ]

        for index, memory in enumerate(
            memories,
            start=1,
        ):
            lines.append(
                f"<b>{index}.</b> "
                f"{html.escape(memory)}"
            )

        lines.extend(
            [
                "",
                "Reply with just the number.",
            ]
        )

        content = "\n".join(lines)

    try:
        await callback.message.edit_text(
            text=content,
            reply_markup=back_close_keyboard(
                callback.from_user.id
            ),
        )
    except Exception as e:
        print(
            f"Memory edit menu error: {e}"
        )


# ==========================================
# Memory Button: Forget
# ==========================================

@router.callback_query(
    F.data == "memory_forget"
)
async def handle_memory_forget(
    callback: CallbackQuery,
):

    memories = await get_memories(
        str(callback.from_user.id)
    )

    await set_interaction(
        callback.from_user.id,
        "forget",
    )

    await callback.answer(
        "Choose the memory numbers to remove."
    )

    if not callback.message:
        return

    if not memories:
        content = (
            "<b>🗑 Forget Memories</b>\n\n"
            "Your memory list is already empty."
        )

    else:
        lines = [
            "<b>🗑 Forget Memories</b>",
            "",
            "Which memories should I remove?",
            "",
        ]

        for index, memory in enumerate(
            memories,
            start=1,
        ):
            lines.append(
                f"<b>{index}.</b> "
                f"{html.escape(memory)}"
            )

        lines.extend(
            [
                "",
                "Send one number or multiple numbers.",
                "Example: <code>1,, 3,, 5</code>",
            ]
        )

        content = "\n".join(lines)

    try:
        await callback.message.edit_text(
            text=content,
            reply_markup=back_close_keyboard(
                callback.from_user.id
            ),
        )
    except Exception as e:
        print(
            f"Memory forget menu error: {e}"
        )


# ==========================================
# Memory Button: Forget All
# ==========================================

@router.callback_query(
    F.data == "memory_forget_all"
)
async def handle_memory_forget_all(
    callback: CallbackQuery,
):

    await clear_interaction(
        callback.from_user.id
    )

    await callback.answer(
        "Please confirm.",
        show_alert=False,
    )

    if not callback.message:
        return

    content = (
        "<b>🔥 Forget Everything?</b>\n\n"
        "This will permanently clear all of your "
        "saved memories and chat history for this "
        "conversation.\n\n"
        "<b>This cannot be undone.</b>"
    )

    try:
        await callback.message.edit_text(
            text=content,
            reply_markup=forget_all_confirmation_keyboard(
                callback.from_user.id
            ),
        )
    except Exception as e:
        print(
            f"Forget all confirmation error: {e}"
        )


# ==========================================
# Confirm Forget All
# ==========================================

@router.callback_query(
    F.data == "memory_confirm_forget_all"
)
async def handle_confirm_forget_all(
    callback: CallbackQuery,
):

    user_id_str = str(
        callback.from_user.id
    )

    chat_id = (
        callback.message.chat.id
        if callback.message
        else 0
    )

    await redis_client.delete(
        f"memory_list:{user_id_str}",
        f"chat_history:{chat_id}:{user_id_str}",
        interaction_key(
            callback.from_user.id
        ),
    )

    await callback.answer(
        "All memories cleared."
    )

    if not callback.message:
        return

    content = (
        "<b>🗑 Everything Cleared</b>\n\n"
        "All of your saved memories and chat "
        "history for this conversation have "
        "been deleted."
    )

    try:
        await callback.message.edit_text(
            text=content,
            reply_markup=main_menu_keyboard(
                callback.from_user.id
            ),
        )
    except Exception as e:
        print(
            f"Forget all result error: {e}"
        )


# ==========================================
# Memory Button: Back
# ==========================================

@router.callback_query(
    F.data == "memory_back"
)
async def handle_memory_back(
    callback: CallbackQuery,
):

    await clear_interaction(
        callback.from_user.id
    )

    await callback.answer()

    if not callback.message:
        return

    content = (
        "<b>🤖 Sen Bot</b>\n\n"
        "Use the buttons below to manage your "
        "personal memory."
    )

    try:
        await callback.message.edit_text(
            text=content,
            reply_markup=main_menu_keyboard(
                callback.from_user.id
            ),
        )
    except Exception as e:
        print(
            f"Memory back error: {e}"
        )


# ==========================================
# Memory Button: Close
# ==========================================

@router.callback_query(
    F.data.startswith("memory_close:")
)
async def handle_memory_close(
    callback: CallbackQuery,
):

    try:
        target_id = int(
            callback.data.split(":", 1)[1]
        )
    except (
        IndexError,
        ValueError,
        AttributeError,
    ):
        await callback.answer(
            "Invalid menu.",
            show_alert=True,
        )
        return

    if callback.from_user.id != target_id:
        await callback.answer(
            "This menu belongs to another user.",
            show_alert=True,
        )
        return

    await clear_interaction(
        callback.from_user.id
    )

    try:
        if callback.message:
            await callback.message.delete()

        await callback.answer(
            "Menu closed."
        )

    except Exception:
        await callback.answer(
            "Failed to close the menu.",
            show_alert=True,
        )


# ==========================================
# Interactive Memory Input
# ==========================================

@router.message(F.text)
async def handle_memory_interaction(
    message: Message,
):

    # Never interfere with slash commands.
    if message.text.startswith("/"):
        return

    user_id = message.from_user.id
    action = await get_interaction(
        user_id
    )

    if not action:
        return

    text = message.text.strip()

    if not text:
        return

    # --------------------------------------
    # ADD MEMORY
    # --------------------------------------

    if action == "add":

        user_id_str = str(user_id)

        parts = [
            part.strip()[:200]
            for part in text.split(",,")
            if part.strip()
        ]

        if not parts:
            await message.answer(
                "<b>Nothing to save.</b>\n\n"
                "Send me a memory or instruction."
            )
            return

        saved_count = 0

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
                    saved_count += 1

            except Exception as e:
                print(
                    f"Memory save error: {e}"
                )

        await redis_client.ltrim(
            f"memory_list:{user_id_str}",
            -25,
            -1,
        )

        await clear_interaction(
            user_id
        )

        content = (
            f"<b>✅ Saved {saved_count} "
            f"memory"
            f"{'ies' if saved_count != 1 else ''}.</b>\n\n"
            + await get_formatted_memories(
                user_id_str
            )
        )

        try:
            await message.delete()
        except Exception:
            pass

        await message.answer(
            text=content,
            reply_markup=memories_keyboard(
                user_id
            ),
        )

        return

    # --------------------------------------
    # EDIT: RECEIVE NUMBER
    # --------------------------------------

    if action == "edit_number":

        if not text.isdigit():
            await message.answer(
                "<b>Invalid number.</b>\n\n"
                "Send the number of the memory "
                "you want to edit."
            )
            return

        index = int(text) - 1

        memories = await get_memories(
            str(user_id)
        )

        if index < 0 or index >= len(memories):
            await message.answer(
                "<b>That memory doesn't exist.</b>\n\n"
                "Send a valid memory number."
            )
            return

        await set_interaction(
            user_id,
            f"edit_value:{index}",
        )

        await message.answer(
            "<b>✏️ New Memory Text</b>\n\n"
            f"Current memory:\n"
            f"<i>{html.escape(memories[index])}</i>\n\n"
            "Now send the replacement text."
        )

        try:
            await message.delete()
        except Exception:
            pass

        return

    # --------------------------------------
    # EDIT: RECEIVE NEW VALUE
    # --------------------------------------

    if action.startswith("edit_value:"):

        try:
            index = int(
                action.split(":", 1)[1]
            )
        except (
            ValueError,
            IndexError,
        ):
            await clear_interaction(
                user_id
            )
            return

        new_value = text[:200]

        memories = await get_memories(
            str(user_id)
        )

        if index < 0 or index >= len(memories):
            await clear_interaction(
                user_id
            )

            await message.answer(
                "<b>That memory no longer exists.</b>"
            )
            return

        await redis_client.lset(
            f"memory_list:{user_id}",
            index,
            new_value,
        )

        await clear_interaction(
            user_id
        )

        try:
            await message.delete()
        except Exception:
            pass

        content = (
            "<b>✅ Memory updated.</b>\n\n"
            + await get_formatted_memories(
                str(user_id)
            )
        )

        await message.answer(
            text=content,
            reply_markup=memories_keyboard(
                user_id
            ),
        )

        return

    # --------------------------------------
    # FORGET
    # --------------------------------------

    if action == "forget":

        numbers = re.findall(
            r"\d+",
            text,
        )

        if not numbers:
            await message.answer(
                "<b>Invalid memory numbers.</b>\n\n"
                "Example: <code>1,, 3,, 5</code>"
            )
            return

        indices = [
            int(number) - 1
            for number in numbers
        ]

        memories = await get_memories(
            str(user_id)
        )

        if not memories:
            await clear_interaction(
                user_id
            )

            await message.answer(
                "<b>Your memory list is empty.</b>"
            )
            return

        removed = 0

        for index in sorted(
            set(indices),
            reverse=True,
        ):
            if 0 <= index < len(memories):
                memories.pop(index)
                removed += 1

        await redis_client.delete(
            f"memory_list:{user_id}"
        )

        if memories:
            await redis_client.rpush(
                f"memory_list:{user_id}",
                *memories,
            )

        await redis_client.ltrim(
            f"memory_list:{user_id}",
            -25,
            -1,
        )

        await clear_interaction(
            user_id
        )

        try:
            await message.delete()
        except Exception:
            pass

        content = (
            f"<b>🗑 Removed {removed} "
            f"memory"
            f"{'ies' if removed != 1 else ''}.</b>\n\n"
            + await get_formatted_memories(
                str(user_id)
            )
        )

        await message.answer(
            text=content,
            reply_markup=memories_keyboard(
                user_id
            ),
        )

        return


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

            except Exception as e:

                if (
                    "message to be replied not found"
                    in str(e).lower()
                ):
                    msg = await bot.send_audio(
                        chat_id=chat_id,
                        audio=cached_value,
                        title=title,
                        performer=performer,
                    )
                else:
                    raise

        elif os.path.exists(
            file_path
        ):

            try:
                msg = await attempt_send(
                    file_path
                )

            except Exception as e:

                if (
                    "message to be replied not found"
                    in str(e).lower()
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

    except Exception as send_err:

        print(
            f"Error sending audio ({key}): "
            f"{send_err}"
        )


# ==========================================
# Delete Command
# ==========================================

@router.message(
    Command("delete", "del")
)
async def handle_delete(
    message: Message,
):

    if message.from_user.id != OWNER_ID:
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


# ==========================================
# SearXNG Web Search
# ==========================================

async def free_web_search(
    query: str,
) -> str:

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64)"
        ),
        "Accept": "application/json",
    }

    if not query:
        return ""

    try:

        params = {
            "q": query,
            "format": "json",
        }

        async with httpx.AsyncClient(
            follow_redirects=True
        ) as client:

            res = await client.get(
                SEARXNG_URL,
                params=params,
                headers=headers,
                timeout=8.0,
            )

        if res.status_code == 200:

            results = (
                res.json()
                .get("results", [])[:10]
            )

            snippets = []

            for item in results:

                title = item.get(
                    "title",
                    "",
                )

                content = item.get(
                    "content",
                    "",
                )

                url = item.get(
                    "url",
                    "",
                )

                if title or content:

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
            "SearXNG returned HTTP "
            f"{res.status_code}: "
            f"{res.text[:500]}"
        )

    except Exception as e:

        print(
            f"SearXNG error: {e}"
        )

    return ""


# ==========================================
# Primary Chat / Mentions / Audio
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

    # --------------------------------------
    # Audio Trigger: Sen
    # --------------------------------------

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

    # --------------------------------------
    # Audio Trigger: Magic
    # --------------------------------------

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

    # --------------------------------------
    # Bot Identity
    # --------------------------------------

    bot_username = (
        f"@{BOT_INFO.username}"
        if BOT_INFO
        and BOT_INFO.username
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

    # --------------------------------------
    # Ignore Normal Group Messages
    # --------------------------------------

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

    # --------------------------------------
    # Clean Mention
    # --------------------------------------

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

    # --------------------------------------
    # Cooldown
    # --------------------------------------

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

    # --------------------------------------
    # Reply Context
    # --------------------------------------

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

    # --------------------------------------
    # Voice / Audio
    # --------------------------------------

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

    try:

        # ==================================
        # Memories
        # ==================================

        raw_mem = await redis_client.lrange(
            f"memory_list:{user_id_str}",
            0,
            -1,
        )

        saved_facts = [
            item.decode("utf-8")
            if isinstance(
                item,
                bytes,
            )
            else item
            for item in raw_mem
        ]

        # ==================================
        # Chat History
        # ==================================

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
            if isinstance(
                item,
                bytes,
            )
            else item
            for item in raw_hist
        ]

        # ==================================
        # Search Detection
        # ==================================

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

        explicit_search = any(
            word in clean_prompt.lower()
            for word in search_keywords
        )

        search_query = clean_prompt

        if (
            explicit_search
            and len(
                clean_prompt.split()
            ) <= 4
        ):

            if (
                replied_context
                and
                "I don't have enough details"
                not in replied_context
                and
                "I am currently broken"
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

        # ==================================
        # Build Context
        # ==================================

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

        # ==================================
        # Gemini Instructions
        # ==================================

        today_str = datetime.now(
            timezone.utc
        ).strftime(
            "%A, %B %d, %Y"
        )

        bot_instructions = (
            f"Today's date is "
            f"{today_str}.\n\n"

            "Keep responses structural using "
            "double line-breaks to separate ideas.\n"

            "Never use standard AI pleasantries.\n"

            "Do not start responses with "
            "'As an AI' or end with generic "
            "offers for help.\n"

            "Keep casual replies brief, but "
            "dynamically expand your response "
            "length when explicitly asked for "
            "details or when playing interactive "
            "games.\n"

            "If the user changes the subject "
            "abruptly, drop the previous topic "
            "immediately and adapt to the new flow.\n"

            "If the user is clearly joking or "
            "sarcastic, match their energy rather "
            "than taking the prompt literally.\n"

            "If you do not know the answer or "
            "the provided context is insufficient, "
            "state exactly: "
            "'I don't have enough details to "
            "answer that accurately' without "
            "guessing.\n"

            "Do not assume personal details about "
            "the user unless they are explicitly "
            "provided in the memory list.\n\n"

            "CRITICAL FORMATTING RULE:\n"

            "You must natively structure all "
            "output using valid Telegram HTML.\n\n"

            "Use <b>text</b> for bold.\n"
            "Use <i>text</i> for italics.\n"
            "Use <code>text</code> for inline code.\n"
            "Use <pre>text</pre> for fixed-width blocks.\n"
            "Use <a href=\"URL\">text</a> for hyperlinks.\n\n"

            "Do NOT use Markdown syntax such as "
            "**, ##, or unformatted pipe tables.\n"

            "Generate clean, valid HTML strings only."
        )

        # ==================================
        # Search Instructions
        # ==================================

        if search_context:

            bot_instructions += (
                "\n\nWhen referencing Web Search "
                "Context, state the information "
                "directly without saying "
                "'According to my search' or "
                "'I found this online'.\n\n"

                "If the user explicitly asks for "
                "links, sources, or URLs, cite them "
                "directly using Telegram HTML "
                "hyperlinks.\n\n"

                "Format references using:\n"
                "<a href=\"URL\">Source Title</a>\n\n"

                "Use double quotes for all HTML "
                "attributes.\n"

                "Do not output raw URLs when "
                "hyperlinks can be used.\n"

                "If the user does not explicitly "
                "ask for sources, do not include URLs."
            )

        # ==================================
        # History Instructions
        # ==================================

        if chat_history:

            bot_instructions += (
                "\n\nUse the Recent Conversation "
                "Context to track pronouns and "
                "subjects, but never summarize or "
                "repeat the history back to the user."
            )

        # ==================================
        # Saved Memory Instructions
        # ==================================

        if saved_facts:

            bot_instructions += (
                "\n\nYou must strictly follow "
                "these User Instructions:\n"
                + "\n".join(
                    f"- {fact}"
                    for fact in saved_facts
                )
            )

        # ==================================
        # Safety Settings
        # ==================================

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

        # ==================================
        # Generate Response
        # ==================================

        if audio_bytes:

            contents = [

                types.Part.from_bytes(
                    data=audio_bytes,
                    mime_type=audio_mime,
                ),

                final_prompt,
            ]

            response = (
                await gemini_client.aio.models
                .generate_content(
                    model="gemini-3.5-flash-lite",
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

        else:

            chat = (
                gemini_client.aio.chats.create(
                    model="gemini-3.5-flash-lite",
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

        # ==================================
        # Response Cleanup
        # ==================================

        response_text = (
            response.text
            if response
            and response.text
            else "I didn't receive a response."
        )

        response_text = (
            response_text
            .replace(
                "\u2022",
                "",
            )
            .replace(
                "```",
                "",
            )
        )

        preview_opts = LinkPreviewOptions(
            is_disabled=False,
            prefer_small_media=True,
        )

        if is_private:

            await message.answer(
                text=response_text,
                link_preview_options=(
                    preview_opts
                ),
            )

        else:

            await message.answer(
                text=response_text,
                reply_to_message_id=msg_id,
                link_preview_options=(
                    preview_opts
                ),
            )

        # ==================================
        # Save History
        # ==================================

        clean_history_text = re.sub(
            r"<[^>]+>",
            "",
            response_text,
        )

        await redis_client.rpush(
            history_key,
            (
                f"User: "
                f"{clean_prompt or 'Voice Note'}"
            ),
            (
                f"Bot: "
                f"{clean_history_text}"
            ),
        )

        await redis_client.ltrim(
            history_key,
            -10,
            -1,
        )

    except Exception as ai_err:

        print(
            f"Gemini API error: {ai_err}"
        )

        error_content = (
            "I am currently broken right now, "
            "the owner needs to fix me."
        )

        if "429" in str(ai_err):

            error_content = (
                "Whoa, I'm getting a little "
                "overwhelmed! Let me catch my "
                "breath for a minute."
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

        BOT_INFO = await bot.get_me()

        print(
            "Bot authenticated as "
            f"@{BOT_INFO.username}"
        )

        print(
            f"SearXNG URL: {SEARXNG_URL}"
        )

    except Exception as e:

        print(
            f"Failed to fetch bot info: {e}"
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


# ==========================================
# Entry Point
# ==========================================

if __name__ == "__main__":
    asyncio.run(main())
