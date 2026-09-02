import os
import re
import asyncio
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
    ForceReply,
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

SEARXNG_URL = os.getenv(
    "SEARXNG_URL",
    "http://searxng.railway.internal:8080/search",
)

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

gemini_api_key = os.getenv("GEMINI_API_KEY", "")

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
# Telegram Rich Message API
# ==========================================

TELEGRAM_API_BASE = (
    f"https://api.telegram.org/bot{API_TOKEN}"
)


async def telegram_api(
    method: str,
    payload: dict,
) -> dict:
    """
    Direct Bot API helper.

    This is used for Bot API 10.3 Rich Messages because
    installed aiogram versions may not yet expose all
    Rich Message classes.
    """

    url = f"{TELEGRAM_API_BASE}/{method}"

    async with httpx.AsyncClient(
        timeout=15.0
    ) as client:

        response = await client.post(
            url,
            json=payload,
        )

        try:
            data = response.json()
        except Exception:
            data = {
                "ok": False,
                "description": response.text,
            }

        if not data.get("ok"):
            raise RuntimeError(
                f"Telegram API {method} failed: "
                f"{data}"
            )

        return data


async def send_rich_message(
    chat_id: int,
    html: str,
    *,
    receiver_user_id: int | None = None,
    callback_query_id: str | None = None,
    replace_callback_query_message: bool = False,
    reply_to_message_id: int | None = None,
):
    """
    Sends a Rich Message using Bot API 10.3.

    If callback_query_id is provided, the message becomes
    an ephemeral response associated with that button press.
    """

    payload = {
        "chat_id": chat_id,
        "rich_message": {
            "html": html,
        },
    }

    if reply_to_message_id is not None:
        payload["reply_parameters"] = {
            "message_id": reply_to_message_id,
        }

    if receiver_user_id is not None:
        ephemeral = {
            "receiver_user_id": receiver_user_id,
        }

        if callback_query_id:
            ephemeral["callback_query_id"] = (
                callback_query_id
            )

        if replace_callback_query_message:
            ephemeral[
                "replace_callback_query_message"
            ] = True

        payload[
            "ephemeral_message_parameters"
        ] = ephemeral

    return await telegram_api(
        "sendRichMessage",
        payload,
    )


async def answer_callback(
    callback: CallbackQuery,
    text: str | None = None,
    show_alert: bool = False,
):
    try:
        await bot.answer_callback_query(
            callback.id,
            text=text,
            show_alert=show_alert,
        )
    except Exception as e:
        print(
            f"Callback answer error: {e}"
        )


# ==========================================
# Rich UI
# ==========================================

def rich_button(
    text: str,
    callback_data: str,
    style: str = "primary",
) -> str:
    """
    Creates a Telegram Rich Message callback button.
    """

    return (
        f'<tg-button '
        f'type="callback_data" '
        f'style="{style}" '
        f'data="{callback_data}">'
        f'{text}'
        f'</tg-button>'
    )


def rich_row(*buttons: str) -> str:
    return (
        "<tg-button-row>"
        + "".join(buttons)
        + "</tg-button-row>"
    )


def escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def get_help_rich_message() -> str:
    return (
        "<h1>Sen Bot</h1>"
        "<p>Memory and bot controls</p>"

        + rich_row(
            rich_button(
                "Remember",
                "mem_remember",
                "success",
            ),
            rich_button(
                "View Memories",
                "mem_view",
                "primary",
            ),
        )

        + rich_row(
            rich_button(
                "Edit Memory",
                "mem_edit",
                "primary",
            ),
            rich_button(
                "Forget Memory",
                "mem_forget",
                "danger",
            ),
        )

        + rich_row(
            rich_button(
                "Forget All",
                "mem_forget_all",
                "danger",
            ),
            rich_button(
                "Dismiss",
                "menu_dismiss",
                "link",
            ),
        )
    )


def get_back_menu_message() -> str:
    return (
        "<p><b>Memory Controls</b></p>"
        "<p>Choose an action below.</p>"

        + rich_row(
            rich_button(
                "Remember",
                "mem_remember",
                "success",
            ),
            rich_button(
                "View Memories",
                "mem_view",
                "primary",
            ),
        )

        + rich_row(
            rich_button(
                "Edit Memory",
                "mem_edit",
                "primary",
            ),
            rich_button(
                "Forget Memory",
                "mem_forget",
                "danger",
            ),
        )

        + rich_row(
            rich_button(
                "Forget All",
                "mem_forget_all",
                "danger",
            ),
            rich_button(
                "Dismiss",
                "menu_dismiss",
                "link",
            ),
        )
    )


# ==========================================
# Memory Helpers
# ==========================================

async def get_memories(
    user_id_str: str,
) -> list[str]:

    raw_items = await redis_client.lrange(
        f"memory_list:{user_id_str}",
        0,
        -1,
    )

    return [
        item.decode("utf-8")
        if isinstance(item, bytes)
        else item
        for item in raw_items
    ]


async def get_formatted_memories(
    user_id_str: str,
) -> str:

    try:
        memories = await get_memories(
            user_id_str
        )

        if not memories:
            return (
                "<b>Active Memories</b>"
                "<p>Your memory list is currently empty.</p>"
                + rich_row(
                    rich_button(
                        "Remember Something",
                        "mem_remember",
                        "success",
                    ),
                    rich_button(
                        "Back",
                        "mem_menu",
                        "link",
                    ),
                )
            )

        lines = [
            "<h2>Active Memories</h2>",
        ]

        for index, memory in enumerate(
            memories,
            start=1,
        ):
            safe_memory = escape_html(
                memory
            )

            lines.append(
                f"<p><b>{index}.</b> "
                f"{safe_memory}</p>"
            )

        return (
            "".join(lines)

            + rich_row(
                rich_button(
                    "Remember",
                    "mem_remember",
                    "success",
                ),
                rich_button(
                    "Edit",
                    "mem_edit",
                    "primary",
                ),
            )

            + rich_row(
                rich_button(
                    "Forget",
                    "mem_forget",
                    "danger",
                ),
                rich_button(
                    "Back",
                    "mem_menu",
                    "link",
                ),
            )
        )

    except Exception as e:
        print(
            f"Error fetching memory list: {e}"
        )

        return (
            "<b>Memory Error</b>"
            "<p>Could not retrieve your memory list.</p>"
            + rich_row(
                rich_button(
                    "Back",
                    "mem_menu",
                    "link",
                )
            )
        )


async def clear_memory_state(
    user_id_str: str,
):
    await redis_client.delete(
        f"memory_state:{user_id_str}"
    )


async def set_memory_state(
    user_id_str: str,
    state: str,
    extra: str = "",
):
    await redis_client.hset(
        f"memory_state:{user_id_str}",
        mapping={
            "state": state,
            "extra": extra,
        },
    )

    await redis_client.expire(
        f"memory_state:{user_id_str}",
        120,
    )


async def get_memory_state(
    user_id_str: str,
):
    data = await redis_client.hgetall(
        f"memory_state:{user_id_str}"
    )

    if not data:
        return None

    def decode(value):
        if isinstance(value, bytes):
            return value.decode(
                "utf-8",
                errors="replace",
            )
        return value

    return {
        decode(k): decode(v)
        for k, v in data.items()
    }


# ==========================================
# Interactive Memory UI
# ==========================================

@router.callback_query(
    F.data.startswith("mem_")
    | F.data.startswith("menu_")
)
async def handle_rich_memory_callback(
    callback: CallbackQuery,
):
    if not callback.data:
        await answer_callback(
            callback,
            "Invalid button.",
            True,
        )
        return

    action = callback.data

    user_id = callback.from_user.id
    user_id_str = str(user_id)

    message = callback.message

    if not message:
        await answer_callback(
            callback,
            "This menu is no longer available.",
            True,
        )
        return

    chat_id = message.chat.id

    try:
        # --------------------------------------
        # Main menu
        # --------------------------------------

        if action == "mem_menu":

            await answer_callback(
                callback,
                "Opening memory controls...",
            )

            await send_rich_message(
                chat_id,
                get_back_menu_message(),
                receiver_user_id=user_id,
                callback_query_id=callback.id,
                replace_callback_query_message=True,
            )

            return

        # --------------------------------------
        # View memories
        # --------------------------------------

        if action == "mem_view":

            await answer_callback(
                callback,
                "Loading memories...",
            )

            content = (
                await get_formatted_memories(
                    user_id_str
                )
            )

            await send_rich_message(
                chat_id,
                content,
                receiver_user_id=user_id,
                callback_query_id=callback.id,
                replace_callback_query_message=True,
            )

            return

        # --------------------------------------
        # Remember
        # --------------------------------------

        if action == "mem_remember":

            await set_memory_state(
                user_id_str,
                "remember",
            )

            await answer_callback(
                callback,
                "Memory mode enabled.",
            )

            prompt = (
                "<h2>Remember Something</h2>"
                "<p>Send me the fact you want me "
                "to remember.</p>"
                "<p>You can add multiple memories by "
                "separating them with <code>,,</code>.</p>"
                "<p><i>This interaction expires "
                "automatically.</i></p>"
                + rich_row(
                    rich_button(
                        "Cancel",
                        "mem_cancel",
                        "link",
                    )
                )
            )

            await send_rich_message(
                chat_id,
                prompt,
                receiver_user_id=user_id,
                callback_query_id=callback.id,
                replace_callback_query_message=True,
            )

            return

        # --------------------------------------
        # Edit
        # --------------------------------------

        if action == "mem_edit":

            memories = await get_memories(
                user_id_str
            )

            if not memories:

                await answer_callback(
                    callback,
                    "You don't have any memories.",
                    True,
                )

                return

            await set_memory_state(
                user_id_str,
                "edit_index",
            )

            await answer_callback(
                callback,
                "Choose a memory number.",
            )

            lines = [
                "<h2>Edit Memory</h2>",
                "<p>Send the number of the memory "
                "you want to edit.</p>",
                "",
            ]

            for index, memory in enumerate(
                memories,
                start=1,
            ):
                lines.append(
                    f"<p><b>{index}.</b> "
                    f"{escape_html(memory)}</p>"
                )

            lines.append(
                "<p><i>Example: "
                "<code>2</code></i></p>"
            )

            lines.append(
                rich_row(
                    rich_button(
                        "Cancel",
                        "mem_cancel",
                        "link",
                    )
                )
            )

            await send_rich_message(
                chat_id,
                "".join(lines),
                receiver_user_id=user_id,
                callback_query_id=callback.id,
                replace_callback_query_message=True,
            )

            return

        # --------------------------------------
        # Forget
        # --------------------------------------

        if action == "mem_forget":

            memories = await get_memories(
                user_id_str
            )

            if not memories:

                await answer_callback(
                    callback,
                    "You don't have any memories.",
                    True,
                )

                return

            await set_memory_state(
                user_id_str,
                "forget_index",
            )

            await answer_callback(
                callback,
                "Choose a memory number.",
            )

            lines = [
                "<h2>Forget Memory</h2>",
                "<p>Send the number of the memory "
                "you want to remove.</p>",
                "",
            ]

            for index, memory in enumerate(
                memories,
                start=1,
            ):
                lines.append(
                    f"<p><b>{index}.</b> "
                    f"{escape_html(memory)}</p>"
                )

            lines.append(
                "<p>You can remove multiple memories "
                "using <code>1,, 3,, 5</code>.</p>"
            )

            lines.append(
                rich_row(
                    rich_button(
                        "Cancel",
                        "mem_cancel",
                        "link",
                    )
                )
            )

            await send_rich_message(
                chat_id,
                "".join(lines),
                receiver_user_id=user_id,
                callback_query_id=callback.id,
                replace_callback_query_message=True,
            )

            return

        # --------------------------------------
        # Forget all
        # --------------------------------------

        if action == "mem_forget_all":

            await set_memory_state(
                user_id_str,
                "confirm_forget_all",
            )

            await answer_callback(
                callback,
                "Confirmation required.",
            )

            confirmation = (
                "<h2>Forget Everything?</h2>"
                "<p>This will permanently remove "
                "all saved memories.</p>"
                "<p>Your normal chat history is not "
                "deleted by this action.</p>"

                + rich_row(
                    rich_button(
                        "Yes, Forget All",
                        "mem_confirm_all",
                        "danger",
                    ),
                    rich_button(
                        "Cancel",
                        "mem_cancel",
                        "link",
                    ),
                )
            )

            await send_rich_message(
                chat_id,
                confirmation,
                receiver_user_id=user_id,
                callback_query_id=callback.id,
                replace_callback_query_message=True,
            )

            return

        # --------------------------------------
        # Confirm forget all
        # --------------------------------------

        if action == "mem_confirm_all":

            await redis_client.delete(
                f"memory_list:{user_id_str}"
            )

            await clear_memory_state(
                user_id_str
            )

            await answer_callback(
                callback,
                "All memories deleted.",
            )

            await send_rich_message(
                chat_id,
                (
                    "<h2>Memories Cleared</h2>"
                    "<p>All of your saved memories "
                    "have been deleted.</p>"

                    + rich_row(
                        rich_button(
                            "Memory Menu",
                            "mem_menu",
                            "primary",
                        ),
                        rich_button(
                            "Dismiss",
                            "menu_dismiss",
                            "link",
                        ),
                    )
                ),
                receiver_user_id=user_id,
                callback_query_id=callback.id,
                replace_callback_query_message=True,
            )

            return

        # --------------------------------------
        # Cancel
        # --------------------------------------

        if action == "mem_cancel":

            await clear_memory_state(
                user_id_str
            )

            await answer_callback(
                callback,
                "Cancelled.",
            )

            await send_rich_message(
                chat_id,
                get_back_menu_message(),
                receiver_user_id=user_id,
                callback_query_id=callback.id,
                replace_callback_query_message=True,
            )

            return

        # --------------------------------------
        # Dismiss
        # --------------------------------------

        if action == "menu_dismiss":

            await clear_memory_state(
                user_id_str
            )

            await answer_callback(
                callback,
                "Closed.",
            )

            return

    except Exception as e:

        print(
            f"Rich memory callback error: {e}"
        )

        await answer_callback(
            callback,
            "Something went wrong.",
            True,
        )


# ==========================================
# Interactive Memory Text Input
# ==========================================

@router.message(
    F.text
)
async def handle_memory_input(
    message: Message,
):
    """
    Handles text input only when the user is currently
    inside a memory interaction.

    Normal Gemini conversation is handled later by the
    primary conversation handler.
    """

    user_id_str = str(
        message.from_user.id
    )

    state_data = await get_memory_state(
        user_id_str
    )

    if not state_data:
        return

    state = state_data.get(
        "state",
        "",
    )

    if state not in {
        "remember",
        "edit_index",
        "edit_value",
        "forget_index",
    }:
        return

    # --------------------------------------
    # Delete the user's command/input in groups
    # --------------------------------------

    if message.chat.type != "private":
        try:
            await message.delete()
        except Exception:
            pass

    text = message.text.strip()

    if not text:
        return

    # --------------------------------------
    # Remember
    # --------------------------------------

    if state == "remember":

        parts = [
            part.strip()[:200]
            for part in text.split(",,")
            if part.strip()
        ]

        saved = []

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

                    saved.append(part)

            except Exception as e:

                print(
                    f"Memory save error: {e}"
                )

        await redis_client.ltrim(
            f"memory_list:{user_id_str}",
            -25,
            -1,
        )

        await clear_memory_state(
            user_id_str
        )

        content = (
            "<h2>Memory Updated</h2>"
            f"<p>Saved <b>{len(saved)}</b> "
            "new memory item(s).</p>"

            + rich_row(
                rich_button(
                    "View Memories",
                    "mem_view",
                    "primary",
                ),
                rich_button(
                    "Memory Menu",
                    "mem_menu",
                    "link",
                ),
            )
        )

        if message.chat.type == "private":

            await send_rich_message(
                message.chat.id,
                content,
            )

        else:

            await send_rich_message(
                message.chat.id,
                content,
                receiver_user_id=message.from_user.id,
            )

        return

    # --------------------------------------
    # Edit index
    # --------------------------------------

    if state == "edit_index":

        if not text.isdigit():

            content = (
                "<b>Invalid memory number.</b>"
                "<p>Send a number from the memory "
                "list.</p>"
                + rich_row(
                    rich_button(
                        "Cancel",
                        "mem_cancel",
                        "link",
                    )
                )
            )

            if message.chat.type == "private":
                await send_rich_message(
                    message.chat.id,
                    content,
                )
            else:
                await send_rich_message(
                    message.chat.id,
                    content,
                    receiver_user_id=message.from_user.id,
                )

            return

        index = int(text) - 1

        memories = await get_memories(
            user_id_str
        )

        if not (
            0 <= index < len(memories)
        ):

            content = (
                "<b>Invalid memory number.</b>"
                "<p>That memory doesn't exist.</p>"
                + rich_row(
                    rich_button(
                        "Cancel",
                        "mem_cancel",
                        "link",
                    )
                )
            )

            if message.chat.type == "private":
                await send_rich_message(
                    message.chat.id,
                    content,
                )
            else:
                await send_rich_message(
                    message.chat.id,
                    content,
                    receiver_user_id=message.from_user.id,
                )

            return

        await set_memory_state(
            user_id_str,
            "edit_value",
            str(index),
        )

        content = (
            "<h2>Edit Memory</h2>"
            f"<p>Current value:</p>"
            f"<p><code>"
            f"{escape_html(memories[index])}"
            f"</code></p>"
            "<p>Now send the new value.</p>"

            + rich_row(
                rich_button(
                    "Cancel",
                    "mem_cancel",
                    "link",
                )
            )
        )

        if message.chat.type == "private":

            await send_rich_message(
                message.chat.id,
                content,
            )

        else:

            await send_rich_message(
                message.chat.id,
                content,
                receiver_user_id=message.from_user.id,
            )

        return

    # --------------------------------------
    # Edit value
    # --------------------------------------

    if state == "edit_value":

        try:
            index = int(
                state_data.get(
                    "extra",
                    "-1",
                )
            )
        except ValueError:
            index = -1

        memories = await get_memories(
            user_id_str
        )

        if not (
            0 <= index < len(memories)
        ):

            await clear_memory_state(
                user_id_str
            )

            return

        new_value = text[:200]

        await redis_client.lset(
            f"memory_list:{user_id_str}",
            index,
            new_value,
        )

        await clear_memory_state(
            user_id_str
        )

        content = (
            "<h2>Memory Updated</h2>"
            "<p>The memory has been edited.</p>"

            + rich_row(
                rich_button(
                    "View Memories",
                    "mem_view",
                    "primary",
                ),
                rich_button(
                    "Memory Menu",
                    "mem_menu",
                    "link",
                ),
            )
        )

        if message.chat.type == "private":

            await send_rich_message(
                message.chat.id,
                content,
            )

        else:

            await send_rich_message(
                message.chat.id,
                content,
                receiver_user_id=message.from_user.id,
            )

        return

    # --------------------------------------
    # Forget index
    # --------------------------------------

    if state == "forget_index":

        raw_numbers = [
            number.strip()
            for number in text.split(",,")
            if number.strip().isdigit()
        ]

        if not raw_numbers:

            content = (
                "<b>Invalid memory number.</b>"
                "<p>Example: "
                "<code>1,, 3,, 5</code></p>"

                + rich_row(
                    rich_button(
                        "Cancel",
                        "mem_cancel",
                        "link",
                    )
                )
            )

            if message.chat.type == "private":
                await send_rich_message(
                    message.chat.id,
                    content,
                )
            else:
                await send_rich_message(
                    message.chat.id,
                    content,
                    receiver_user_id=message.from_user.id,
                )

            return

        indices = [
            int(number) - 1
            for number in raw_numbers
        ]

        memories = await get_memories(
            user_id_str
        )

        if not memories:

            await clear_memory_state(
                user_id_str
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
            f"memory_list:{user_id_str}"
        )

        if memories:

            await redis_client.rpush(
                f"memory_list:{user_id_str}",
                *memories,
            )

        await clear_memory_state(
            user_id_str
        )

        content = (
            "<h2>Memory Updated</h2>"
            f"<p>Removed <b>{removed}</b> "
            "memory item(s).</p>"

            + rich_row(
                rich_button(
                    "View Memories",
                    "mem_view",
                    "primary",
                ),
                rich_button(
                    "Memory Menu",
                    "mem_menu",
                    "link",
                ),
            )
        )

        if message.chat.type == "private":

            await send_rich_message(
                message.chat.id,
                content,
            )

        else:

            await send_rich_message(
                message.chat.id,
                content,
                receiver_user_id=message.from_user.id,
            )

        return


# ==========================================
# Help / Commands
# ==========================================

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

    try:

        if message.chat.type == "private":

            await send_rich_message(
                message.chat.id,
                get_help_rich_message(),
            )

        else:

            await send_rich_message(
                message.chat.id,
                get_help_rich_message(),
            )

    except Exception as e:

        print(
            f"Rich help error: {e}"
        )

        await message.answer(
            "The interactive menu could not be loaded."
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

    reply_msg = (
        message.reply_to_message
    )

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
# Web Search
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

    params = {
        "q": query,
        "format": "json",
    }

    try:

        async with httpx.AsyncClient(
            timeout=8.0
        ) as client:

            response = await client.get(
                SEARXNG_URL,
                params=params,
                headers=headers,
            )

        if response.status_code != 200:

            print(
                "SearXNG HTTP error: "
                f"{response.status_code} "
                f"{response.text[:500]}"
            )

            return ""

        results = response.json().get(
            "results",
            [],
        )[:10]

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

        return "\n\n".join(
            snippets
        )

    except Exception as e:

        print(
            f"SearXNG error: {e}"
        )

        return ""


# ==========================================
# Audio
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
                cached_id.decode(
                    "utf-8"
                )
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

                    msg = (
                        await bot.send_audio(
                            chat_id=chat_id,
                            audio=cached_value,
                            title=title,
                            performer=performer,
                        )
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

                    msg = (
                        await bot.send_audio(
                            chat_id=chat_id,
                            audio=audio_file,
                            title=title,
                            performer=performer,
                        )
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

    except Exception as e:

        print(
            f"Error sending audio "
            f"({key}): {e}"
        )


# ==========================================
# Primary Conversation
# ==========================================

@router.message(
    F.text
    | F.caption
    | F.voice
    | F.audio
)
async def handle_conversation(
    message: Message,
):

    # Memory interaction messages have already
    # been handled by the memory handler.
    if message.text:

        state = await get_memory_state(
            str(message.from_user.id)
        )

        if state:
            return

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
    # Audio keyword triggers
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
                message.chat.type == "private",
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
                message.chat.type == "private",
            )
        )

    # --------------------------------------
    # Bot targeting
    # --------------------------------------

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
        in [
            "voice",
            "audio",
        ]
    ):
        return

    user_id_str = str(
        message.from_user.id
    )

    chat_id = message.chat.id
    msg_id = message.message_id

    # --------------------------------------
    # Clean prompt
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
    # Reply context
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
    # Voice/audio
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

        # --------------------------------------
        # Memories
        # --------------------------------------

        saved_facts = await get_memories(
            user_id_str
        )

        # --------------------------------------
        # Chat history
        # --------------------------------------

        history_key = (
            f"chat_history:"
            f"{chat_id}:"
            f"{user_id_str}"
        )

        raw_hist = (
            await redis_client.lrange(
                history_key,
                0,
                -1,
            )
        )

        chat_history = [
            item.decode(
                "utf-8"
            )
            if isinstance(
                item,
                bytes,
            )
            else item
            for item in raw_hist
        ]

        # --------------------------------------
        # Search detection
        # --------------------------------------

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

        # --------------------------------------
        # Build context
        # --------------------------------------

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

        # --------------------------------------
        # Gemini instructions
        # --------------------------------------

        today_str = (
            datetime.now(
                timezone.utc
            ).strftime(
                "%A, %B %d, %Y"
            )
        )

        bot_instructions = (
            f"Today's date is {today_str}.\n\n"

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
            "answer that accurately' without guessing.\n"

            "Do not assume personal details about "
            "the user unless they are explicitly "
            "provided in the memory list.\n\n"

            "CRITICAL FORMATTING RULE:\n"
            "Generate valid Telegram HTML.\n\n"

            "Use <b>text</b> for bold.\n"
            "Use <i>text</i> for italics.\n"
            "Use <code>text</code> for inline code.\n"
            "Use <pre>text</pre> for fixed-width blocks.\n"
            "Use <a href=\"URL\">text</a> for hyperlinks.\n\n"

            "Do NOT use Markdown syntax such as "
            "** or ## in normal responses.\n"

            "Generate clean HTML strings only."
        )

        if search_context:

            bot_instructions += (
                "\n\nWhen referencing Web Search "
                "Context, state the information "
                "directly without saying "
                "'According to my search' or "
                "'I found this online'.\n\n"

                "If the user explicitly asks for "
                "links, sources, or URLs, cite them "
                "using Telegram HTML hyperlinks.\n\n"

                "Use:\n"
                "<a href=\"URL\">Source Title</a>\n\n"

                "Do not output raw URLs when "
                "hyperlinks can be used.\n"

                "If the user does not explicitly "
                "ask for sources, do not include URLs."
            )

        if chat_history:

            bot_instructions += (
                "\n\nUse Recent Conversation Context "
                "to track pronouns and subjects, "
                "but never summarize or repeat the "
                "history back to the user."
            )

        if saved_facts:

            bot_instructions += (
                "\n\nYou must strictly follow these "
                "User Instructions:\n"
                + "\n".join(
                    f"- {fact}"
                    for fact in saved_facts
                )
            )

        # --------------------------------------
        # Safety settings
        # --------------------------------------

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

        # --------------------------------------
        # Gemini generation
        # --------------------------------------

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
                    model=(
                        "gemini-3.5-flash-lite"
                    ),
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
                    model=(
                        "gemini-3.5-flash-lite"
                    ),
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

        response_text = (
            response.text
            if response
            and response.text
            else "I didn't receive a response."
        )

        # --------------------------------------
        # Cleanup Gemini output
        # --------------------------------------

        response_text = (
            response_text
            .replace(
                "\u2022",
                "",
            )
            .replace(
                "```html",
                "",
            )
            .replace(
                "```",
                "",
            )
            .strip()
        )

        # --------------------------------------
        # Send normal Gemini response
        # --------------------------------------

        preview_opts = (
            LinkPreviewOptions(
                is_disabled=False,
                prefer_small_media=True,
            )
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

        # --------------------------------------
        # Save history
        # --------------------------------------

        clean_history_text = re.sub(
            r"<[^>]+>",
            "",
            response_text,
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

    except Exception as ai_err:

        print(
            f"Gemini API error: "
            f"{ai_err}"
        )

        error_content = (
            "I am currently broken right now, "
            "the owner needs to fix me."
        )

        if "429" in str(
            ai_err
        ):

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

        BOT_INFO = (
            await bot.get_me()
        )

        print(
            "Bot authenticated as "
            f"@{BOT_INFO.username}"
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

    print(
        "Rich Message API enabled "
        "(Bot API 10.3)"
    )

    print(
        f"SearXNG endpoint: {SEARXNG_URL}"
    )

    try:

        await dp.start_polling(
            bot
        )

    finally:

        print(
            "SIGTERM received! Cleaning "
            "up database connections..."
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
