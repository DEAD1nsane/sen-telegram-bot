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
    CallbackQuery,
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    InputRichMessage,
    EphemeralMessageParameters,
    ReplyParameters,
)
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties

import redis.asyncio as redis
import httpx

from google import genai
from google.genai import types


# ============================================================
# Environment & Config
# ============================================================

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

redis_client = redis.from_url(
    redis_url,
    ssl_cert_reqs=None if redis_url.startswith("rediss://") else "required",
)


API_TOKEN = os.getenv("BOT_TOKEN", "")

if not API_TOKEN:
    raise ValueError("CRITICAL CONFIGURATION ERROR: 'BOT_TOKEN' missing.")


SEARXNG_URL = os.getenv(
    "SEARXNG_URL",
    "http://searxng.railway.internal:8080/search",
).rstrip("/")


gemini_api_key = os.getenv("GEMINI_API_KEY", "")

if not gemini_api_key:
    raise ValueError("CRITICAL CONFIGURATION ERROR: 'GEMINI_API_KEY' missing.")


gemini_client = genai.Client(api_key=gemini_api_key)

OWNER_ID = int(os.getenv("OWNER_ID", "0"))


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


# ============================================================
# Memory menu settings
# ============================================================

MENU_TTL = 30
INTERACTION_TTL = 300

# Key:
# (chat_id, user_id, ephemeral_message_id)
#
# Keeping the user ID in the key is important because each user's
# memory menu is private.
menu_tasks: dict[tuple[int, int, int], asyncio.Task] = {}


# ============================================================
# Redis interaction state
# ============================================================

def interaction_key(user_id: int) -> str:
    return f"memory_interaction:{user_id}"


async def set_interaction(user_id: int, action: str) -> None:
    await redis_client.set(
        interaction_key(user_id),
        action,
        ex=INTERACTION_TTL,
    )


async def get_interaction(user_id: int) -> str | None:
    value = await redis_client.get(interaction_key(user_id))

    if isinstance(value, bytes):
        return value.decode("utf-8")

    return value


async def clear_interaction(user_id: int) -> None:
    await redis_client.delete(interaction_key(user_id))


# ============================================================
# Memory helpers
# ============================================================

async def get_memories(user_id_str: str) -> list[str]:
    try:
        raw = await redis_client.lrange(
            f"memory_list:{user_id_str}",
            0,
            -1,
        )

        return [
            x.decode("utf-8") if isinstance(x, bytes) else str(x)
            for x in raw
        ]

    except Exception as e:
        print(f"Memory read error: {e}")
        return []


async def get_formatted_memories(user_id_str: str) -> str:
    memories = await get_memories(user_id_str)

    if not memories:
        return (
            "<h2>Your Memories</h2>"
            "<p>Your memory list is currently empty.</p>"
        )

    lines = [
        "<h2>Your Memories</h2>",
        "<ol>",
    ]

    for memory in memories:
        lines.append(
            f"<li>{html.escape(memory)}</li>"
        )

    lines.append("</ol>")

    return "".join(lines)


# ============================================================
# Rich Message UI
# ============================================================

def rich_main_menu() -> str:
    return """
<h2>Sen's Memory</h2>
<p>Manage your personal memory without cluttering the chat.</p>

<tg-button-row align="center">
    <tg-button
        type="callback_data"
        style="primary"
        data="memory_view"
    >Memories</tg-button>

    <tg-button
        type="callback_data"
        style="success"
        data="memory_add"
    >New memory</tg-button>
</tg-button-row>

<tg-button-row align="center">
    <tg-button
        type="callback_data"
        style="link"
        data="memory_close"
    >Close</tg-button>
</tg-button-row>
""".strip()


def rich_memory_menu(memories_html: str) -> str:
    return f"""
{memories_html}

<tg-button-row align="center">
    <tg-button
        type="callback_data"
        style="primary"
        data="memory_edit"
    >Edit</tg-button>

    <tg-button
        type="callback_data"
        data="memory_forget"
    >Forget</tg-button>
</tg-button-row>

<tg-button-row align="center">
    <tg-button
        type="callback_data"
        style="danger"
        data="memory_forget_all"
    >Forget all</tg-button>
</tg-button-row>

<tg-button-row align="center">
    <tg-button
        type="callback_data"
        style="link"
        data="memory_back"
    >Back</tg-button>

    <tg-button
        type="callback_data"
        style="link"
        data="memory_close"
    >Close</tg-button>
</tg-button-row>
""".strip()


def rich_back_close(prompt: str) -> str:
    return f"""
{prompt}

<tg-button-row align="center">
    <tg-button
        type="callback_data"
        style="link"
        data="memory_back"
    >Back</tg-button>

    <tg-button
        type="callback_data"
        style="link"
        data="memory_close"
    >Close</tg-button>
</tg-button-row>
""".strip()


def rich_forget_all_confirm() -> str:
    return """
<h2>Forget everything?</h2>

<p>
This permanently clears all saved memories and this user's
stored conversation history.
</p>

<p>
<b>This cannot be undone.</b>
</p>

<tg-button-row align="center">
    <tg-button
        type="callback_data"
        style="danger"
        data="memory_confirm_forget_all"
    >Delete everything</tg-button>
</tg-button-row>

<tg-button-row align="center">
    <tg-button
        type="callback_data"
        style="link"
        data="memory_back"
    >Cancel</tg-button>

    <tg-button
        type="callback_data"
        style="link"
        data="memory_close"
    >Close</tg-button>
</tg-button-row>
""".strip()


# ============================================================
# Memory menu timer
# ============================================================

async def cancel_menu_timer(
    chat_id: int,
    user_id: int,
    ephemeral_message_id: int,
) -> None:
    key = (
        chat_id,
        user_id,
        ephemeral_message_id,
    )

    task = menu_tasks.pop(key, None)

    if task and not task.done():
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass


async def schedule_menu_delete(
    chat_id: int,
    user_id: int,
    ephemeral_message_id: int,
) -> None:
    """
    Schedule deletion exactly 30 seconds from now.

    IMPORTANT:
    This function itself does not delete anything immediately.
    """

    key = (
        chat_id,
        user_id,
        ephemeral_message_id,
    )

    # Cancel an existing timer for this exact menu.
    old_task = menu_tasks.get(key)

    if old_task and not old_task.done():
        old_task.cancel()

        try:
            await old_task
        except asyncio.CancelledError:
            pass

    async def expire() -> None:
        try:
            # ------------------------------------------------
            # DO NOT REMOVE THIS.
            #
            # The previous implementation was deleting the
            # ephemeral message as soon as the menu was created.
            # ------------------------------------------------
            await asyncio.sleep(MENU_TTL)

            print(
                f"Memory menu expired: "
                f"chat={chat_id} "
                f"user={user_id} "
                f"ephemeral_id={ephemeral_message_id}"
            )

            await bot.delete_ephemeral_message(
                chat_id=chat_id,
                receiver_user_id=user_id,
                ephemeral_message_id=ephemeral_message_id,
            )

        except asyncio.CancelledError:
            # Normal when the user interacts with the menu.
            pass

        except Exception as e:
            print(
                f"Memory menu expiry error "
                f"(chat={chat_id}, user={user_id}, "
                f"ephemeral_id={ephemeral_message_id}): {e}"
            )

        finally:
            if menu_tasks.get(key) is asyncio.current_task():
                menu_tasks.pop(key, None)

    menu_tasks[key] = asyncio.create_task(expire())


# ============================================================
# Security helpers
# ============================================================

def callback_belongs_to_user(
    callback: CallbackQuery,
) -> bool:
    """
    Ephemeral messages are private by Telegram itself, but we still
    enforce the owner check at the application level.

    This prevents a callback from another user from ever touching
    somebody else's Redis memory data.
    """

    if not callback.message:
        return False

    receiver_user = getattr(
        callback.message,
        "receiver_user",
        None,
    )

    if receiver_user is not None:
        return receiver_user.id == callback.from_user.id

    # Some aiogram/API representations may not expose receiver_user
    # on callback.message. In that case, do not trust a callback that
    # lacks an explicit ephemeral message ID.
    return (
        getattr(
            callback.message,
            "ephemeral_message_id",
            None,
        )
        is not None
    )


def get_callback_ephemeral_id(
    callback: CallbackQuery,
) -> int | None:

    if not callback.message:
        return None

    return getattr(
        callback.message,
        "ephemeral_message_id",
        None,
    )


# ============================================================
# Send memory menu
# ============================================================

async def send_menu(
    chat_id: int,
    user_id: int,
    rich_html: str,
    *,
    reply_to_ephemeral_id: int | None = None,
    callback_query_id: str | None = None,
):
    """
    Send a private memory menu.

    In group chats Telegram requires an eligible ephemeral context
    for a non-admin bot.

    The two supported contexts are:

    1. Replying to the incoming ephemeral command.
    2. Responding to a callback query.

    We deliberately refuse to send a public memory menu.
    """

    kwargs = {
        "chat_id": chat_id,
        "rich_message": InputRichMessage(
            html=rich_html,
        ),
    }

    if chat_id != user_id:
        # ----------------------------------------------------
        # GROUP / SUPERGROUP
        # ----------------------------------------------------

        if reply_to_ephemeral_id is not None:
            kwargs["reply_parameters"] = ReplyParameters(
                ephemeral_message_id=reply_to_ephemeral_id,
            )

        elif callback_query_id is not None:
            kwargs["ephemeral_message_parameters"] = (
                EphemeralMessageParameters(
                    receiver_user_id=user_id,
                    callback_query_id=callback_query_id,
                )
            )

        else:
            print(
                "SECURITY: Refusing to send public memory menu. "
                f"chat={chat_id}, user={user_id}"
            )

            return None

    # --------------------------------------------------------
    # PRIVATE CHAT
    # --------------------------------------------------------
    # No ephemeral parameters are required.

    try:
        message = await bot.send_rich_message(
            **kwargs,
        )

    except Exception as e:
        print(
            f"Failed to send memory menu "
            f"(chat={chat_id}, user={user_id}): {e}"
        )

        return None

    # --------------------------------------------------------
    # Group menu MUST be ephemeral.
    # --------------------------------------------------------

    if chat_id != user_id:

        ephemeral_id = getattr(
            message,
            "ephemeral_message_id",
            None,
        )

        if ephemeral_id is None:
            print(
                "SECURITY ERROR: Telegram returned a non-ephemeral "
                "memory menu in a group. Refusing to keep it."
            )

            try:
                await bot.delete_message(
                    chat_id=chat_id,
                    message_id=message.message_id,
                )
            except Exception:
                pass

            return None

        receiver_user = getattr(
            message,
            "receiver_user",
            None,
        )

        if receiver_user is not None:
            if receiver_user.id != user_id:
                print(
                    "SECURITY ERROR: Ephemeral memory menu receiver "
                    "does not match requesting user."
                )

                try:
                    await bot.delete_ephemeral_message(
                        chat_id=chat_id,
                        receiver_user_id=user_id,
                        ephemeral_message_id=ephemeral_id,
                    )
                except Exception:
                    pass

                return None

        # ----------------------------------------------------
        # IMPORTANT:
        # Start the timer AFTER Telegram actually returned
        # the ephemeral message and its ID.
        # ----------------------------------------------------

        await schedule_menu_delete(
            chat_id=chat_id,
            user_id=user_id,
            ephemeral_message_id=ephemeral_id,
        )

    return message


# ============================================================
# Edit existing ephemeral menu
# ============================================================

async def edit_menu(
    callback: CallbackQuery,
    rich_html: str,
) -> bool:

    if not callback.message:
        return False

    if not callback_belongs_to_user(callback):
        await callback.answer(
            "This memory menu belongs to another user.",
            show_alert=True,
        )
        return False

    chat_id = callback.message.chat.id
    user_id = callback.from_user.id

    ephemeral_id = get_callback_ephemeral_id(
        callback,
    )

    if ephemeral_id is None:
        print(
            "SECURITY: Attempted to edit a non-ephemeral "
            "memory menu in a group."
        )
        return False

    # Cancel the previous timer.
    await cancel_menu_timer(
        chat_id,
        user_id,
        ephemeral_id,
    )

    try:
        await bot.edit_ephemeral_message_text(
            chat_id=chat_id,
            receiver_user_id=user_id,
            ephemeral_message_id=ephemeral_id,
            rich_message=InputRichMessage(
                html=rich_html,
            ),
        )

    except Exception as e:
        print(
            f"Memory menu edit error: {e}"
        )
        return False

    # Reset the timer to a fresh 30 seconds.
    await schedule_menu_delete(
        chat_id,
        user_id,
        ephemeral_id,
    )

    return True


# ============================================================
# Close menu
# ============================================================

async def close_menu(
    callback: CallbackQuery,
) -> None:

    if not callback.message:
        return

    if not callback_belongs_to_user(callback):
        await callback.answer(
            "This memory menu belongs to another user.",
            show_alert=True,
        )
        return

    chat_id = callback.message.chat.id
    user_id = callback.from_user.id

    ephemeral_id = get_callback_ephemeral_id(
        callback,
    )

    await clear_interaction(user_id)

    if ephemeral_id is not None:

        await cancel_menu_timer(
            chat_id,
            user_id,
            ephemeral_id,
        )

        try:
            await bot.delete_ephemeral_message(
                chat_id=chat_id,
                receiver_user_id=user_id,
                ephemeral_message_id=ephemeral_id,
            )

        except Exception as e:
            print(
                f"Memory menu close error: {e}"
            )

    else:
        # Never delete somebody else's normal group message.
        print(
            "SECURITY: Refusing to close non-ephemeral "
            "memory message."
        )


# ============================================================
# /memories
# ============================================================

async def show_memories(message: Message) -> None:

    user_id = message.from_user.id
    chat_id = message.chat.id

    await clear_interaction(user_id)

    # --------------------------------------------------------
    # EPHEMERAL COMMAND
    #
    # This is the correct group-chat path.
    # Telegram sends /memories as an ephemeral incoming message.
    # --------------------------------------------------------

    incoming_ephemeral_id = getattr(
        message,
        "ephemeral_message_id",
        None,
    )

    if incoming_ephemeral_id is not None:

        await send_menu(
            chat_id=chat_id,
            user_id=user_id,
            rich_html=rich_main_menu(),
            reply_to_ephemeral_id=incoming_ephemeral_id,
        )

        return

    # --------------------------------------------------------
    # PRIVATE CHAT
    # --------------------------------------------------------

    if message.chat.type == "private":

        try:
            await message.delete()
        except Exception:
            pass

        await send_menu(
            chat_id=chat_id,
            user_id=user_id,
            rich_html=rich_main_menu(),
        )

        return

    # --------------------------------------------------------
    # NORMAL MANUALLY-TYPED GROUP COMMAND
    #
    # A non-admin bot cannot turn this into an arbitrary
    # ephemeral message. Telegram requires an eligible
    # ephemeral context.
    #
    # We therefore do NOTHING instead of leaking the menu
    # publicly.
    # --------------------------------------------------------

    print(
        "Ignoring non-ephemeral /memories in group: "
        f"chat={chat_id}, user={user_id}, "
        f"text={message.text!r}"
    )


# ============================================================
# /memories command
# ============================================================

@router.message(Command("memories"))
async def handle_memories(
    message: Message,
):
    await show_memories(message)


# Keep /help as an alias.
@router.message(Command("help"))
async def handle_help(
    message: Message,
):
    await show_memories(message)


# ============================================================
# Rich Message callbacks
# ============================================================

@router.callback_query(F.data == "memory_view")
async def handle_memory_view(
    callback: CallbackQuery,
):

    if not callback_belongs_to_user(callback):
        await callback.answer(
            "This memory menu belongs to another user.",
            show_alert=True,
        )
        return

    await callback.answer()

    memories_html = await get_formatted_memories(
        str(callback.from_user.id),
    )

    await edit_menu(
        callback,
        rich_memory_menu(memories_html),
    )


@router.callback_query(F.data == "memory_add")
async def handle_memory_add(
    callback: CallbackQuery,
):

    if not callback_belongs_to_user(callback):
        await callback.answer(
            "This memory menu belongs to another user.",
            show_alert=True,
        )
        return

    await set_interaction(
        callback.from_user.id,
        "add",
    )

    await callback.answer(
        "Send the memory you want me to save."
    )

    await edit_menu(
        callback,
        rich_back_close(
            "<h2>New memory</h2>"
            "<p>"
            "Send the fact or instruction you want me to remember."
            "</p>"
            "<p>"
            "You can send multiple items separated by "
            "<code>,,</code>."
            "</p>"
        ),
    )


@router.callback_query(F.data == "memory_edit")
async def handle_memory_edit(
    callback: CallbackQuery,
):

    if not callback_belongs_to_user(callback):
        await callback.answer(
            "This memory menu belongs to another user.",
            show_alert=True,
        )
        return

    memories = await get_memories(
        str(callback.from_user.id),
    )

    await set_interaction(
        callback.from_user.id,
        "edit_number",
    )

    await callback.answer()

    if not memories:

        body = (
            "<h2>Edit memory</h2>"
            "<p>"
            "You don't have any saved memories yet."
            "</p>"
        )

    else:

        rows = [
            "<h2>Edit memory</h2>",
            "<p>"
            "Send the number and replacement text."
            "</p>",
            "<ol>",
        ]

        for memory in memories:
            rows.append(
                f"<li>{html.escape(memory)}</li>"
            )

        rows.extend(
            [
                "</ol>",
                "<p>"
                "Example: "
                "<code>2 My new instruction</code>"
                "</p>",
            ]
        )

        body = "".join(rows)

    await edit_menu(
        callback,
        rich_back_close(body),
    )


@router.callback_query(F.data == "memory_forget")
async def handle_memory_forget(
    callback: CallbackQuery,
):

    if not callback_belongs_to_user(callback):
        await callback.answer(
            "This memory menu belongs to another user.",
            show_alert=True,
        )
        return

    memories = await get_memories(
        str(callback.from_user.id),
    )

    await set_interaction(
        callback.from_user.id,
        "forget",
    )

    await callback.answer()

    if not memories:

        body = (
            "<h2>Forget memories</h2>"
            "<p>"
            "Your memory list is already empty."
            "</p>"
        )

    else:

        rows = [
            "<h2>Forget memories</h2>",
            "<p>"
            "Send one number or several separated by "
            "<code>,,</code>."
            "</p>",
            "<ol>",
        ]

        for memory in memories:
            rows.append(
                f"<li>{html.escape(memory)}</li>"
            )

        rows.extend(
            [
                "</ol>",
                "<p>"
                "Example: "
                "<code>1,, 3,, 5</code>"
                "</p>",
            ]
        )

        body = "".join(rows)

    await edit_menu(
        callback,
        rich_back_close(body),
    )


@router.callback_query(F.data == "memory_forget_all")
async def handle_memory_forget_all(
    callback: CallbackQuery,
):

    if not callback_belongs_to_user(callback):
        await callback.answer(
            "This memory menu belongs to another user.",
            show_alert=True,
        )
        return

    await clear_interaction(
        callback.from_user.id,
    )

    await callback.answer()

    await edit_menu(
        callback,
        rich_forget_all_confirm(),
    )


@router.callback_query(
    F.data == "memory_confirm_forget_all"
)
async def handle_confirm_forget_all(
    callback: CallbackQuery,
):

    if not callback_belongs_to_user(callback):
        await callback.answer(
            "This memory menu belongs to another user.",
            show_alert=True,
        )
        return

    user_id = callback.from_user.id
    user_id_str = str(user_id)

    chat_id = (
        callback.message.chat.id
        if callback.message
        else 0
    )

    # Delete ONLY this user's memory list.
    await redis_client.delete(
        f"memory_list:{user_id_str}",
        f"chat_history:{chat_id}:{user_id_str}",
        interaction_key(user_id),
    )

    await callback.answer(
        "All memories cleared."
    )

    await edit_menu(
        callback,
        rich_back_close(
            "<h2>Memories cleared</h2>"
            "<p>"
            "Everything saved for you in this conversation "
            "has been removed."
            "</p>"
        ),
    )


@router.callback_query(F.data == "memory_back")
async def handle_memory_back(
    callback: CallbackQuery,
):

    if not callback_belongs_to_user(callback):
        await callback.answer(
            "This memory menu belongs to another user.",
            show_alert=True,
        )
        return

    await clear_interaction(
        callback.from_user.id,
    )

    await callback.answer()

    await edit_menu(
        callback,
        rich_main_menu(),
    )


@router.callback_query(F.data == "memory_close")
async def handle_memory_close(
    callback: CallbackQuery,
):

    if not callback_belongs_to_user(callback):
        await callback.answer(
            "This memory menu belongs to another user.",
            show_alert=True,
        )
        return

    await callback.answer(
        "Closed"
    )

    await close_menu(callback)


# ============================================================
# Memory text input
# ============================================================

async def process_memory_text(
    message: Message,
    action: str,
) -> bool:

    if (
        not message.text
        or message.text.startswith("/")
    ):
        return False

    user_id = message.from_user.id
    user_id_str = str(user_id)

    # --------------------------------------------------------
    # ADD
    # --------------------------------------------------------

    if action == "add":

        parts = [
            p.strip()[:200]
            for p in message.text.split(",,")
            if p.strip()
        ]

        for part in parts[:10]:

            try:

                if (
                    await redis_client.lpos(
                        f"memory_list:{user_id_str}",
                        part,
                    )
                    is None
                ):
                    await redis_client.rpush(
                        f"memory_list:{user_id_str}",
                        part,
                    )

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
            user_id,
        )

        try:
            await message.delete()
        except Exception:
            pass

        return True

    # --------------------------------------------------------
    # EDIT
    # --------------------------------------------------------

    if action == "edit_number":

        parts = (
            message.text
            .strip()
            .split(" ", 1)
        )

        if (
            len(parts) != 2
            or not parts[0].isdigit()
        ):
            return True

        index = int(parts[0]) - 1
        new_value = parts[1].strip()[:200]

        raw = await redis_client.lrange(
            f"memory_list:{user_id_str}",
            0,
            -1,
        )

        if 0 <= index < len(raw):

            await redis_client.lset(
                f"memory_list:{user_id_str}",
                index,
                new_value,
            )

        await clear_interaction(
            user_id,
        )

        try:
            await message.delete()
        except Exception:
            pass

        return True

    # --------------------------------------------------------
    # FORGET
    # --------------------------------------------------------

    if action == "forget":

        indices = [
            int(n.strip()) - 1
            for n in message.text.split(",,")
            if n.strip().isdigit()
        ]

        raw = await redis_client.lrange(
            f"memory_list:{user_id_str}",
            0,
            -1,
        )

        memories = [
            x.decode("utf-8")
            if isinstance(x, bytes)
            else str(x)
            for x in raw
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

        await clear_interaction(
            user_id,
        )

        try:
            await message.delete()
        except Exception:
            pass

        return True

    return False


# ============================================================
# Delete command
# ============================================================

@router.message(Command("delete", "del"))
async def handle_delete(
    message: Message,
):

    if message.from_user.id != OWNER_ID:
        return

    if (
        message.reply_to_message
        and BOT_INFO
        and message.reply_to_message.from_user
    ):

        if (
            message.reply_to_message.from_user.id
            == BOT_INFO.id
        ):

            try:
                await bot.delete_message(
                    message.chat.id,
                    message.reply_to_message.message_id,
                )
            except Exception:
                pass

    try:
        await message.delete()
    except Exception:
        pass


# ============================================================
# Web search
# ============================================================

async def free_web_search(
    query: str,
) -> str:

    headers = {
        "User-Agent":
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64)",
        "Accept": "application/json",
    }

    try:

        async with httpx.AsyncClient() as client:

            response = await client.get(
                SEARXNG_URL,
                params={
                    "q": query,
                    "format": "json",
                },
                headers=headers,
                timeout=8.0,
            )

        if response.status_code != 200:

            print(
                f"SearXNG HTTP "
                f"{response.status_code}: "
                f"{response.text[:300]}"
            )

            return ""

        results = response.json().get(
            "results",
            [],
        )[:10]

        return "\n\n".join(
            f"Title: {x.get('title', '')}\n"
            f"Content: {x.get('content', '')}\n"
            f"URL: {x.get('url', '')}"
            for x in results
            if (
                x.get("title")
                or x.get("content")
            )
        )

    except Exception as e:

        print(
            f"SearXNG error: {e}"
        )

        return ""


# ============================================================
# Audio
# ============================================================

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

        cached = await redis_client.get(
            f"audio_cache:{key}"
        )

        reply_to = (
            None
            if is_private
            else msg_id
        )

        if cached:

            audio = (
                cached.decode("utf-8")
                if isinstance(cached, bytes)
                else cached
            )

        elif os.path.exists(file_path):

            audio = FSInputFile(
                file_path
            )

        else:

            return

        try:

            sent = await bot.send_audio(
                chat_id=chat_id,
                audio=audio,
                title=title,
                performer=performer,
                reply_to_message_id=reply_to,
            )

        except Exception as e:

            if (
                "message to be replied not found"
                not in str(e).lower()
            ):
                raise

            sent = await bot.send_audio(
                chat_id=chat_id,
                audio=audio,
                title=title,
                performer=performer,
            )

        if (
            sent.audio
            and sent.audio.file_id
        ):

            await redis_client.set(
                f"audio_cache:{key}",
                sent.audio.file_id,
            )

    except Exception as e:

        print(
            f"Audio error ({key}): {e}"
        )


# ============================================================
# Main conversation
# ============================================================

@router.message(
    F.text
    | F.caption
    | F.voice
    | F.audio
)
async def handle_conversation(
    message: Message,
):

    # --------------------------------------------------------
    # Memory interaction takes priority.
    # --------------------------------------------------------

    action = await get_interaction(
        message.from_user.id
    )

    if (
        action
        and message.text
        and not message.text.startswith("/")
    ):

        if await process_memory_text(
            message,
            action,
        ):
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

    # --------------------------------------------------------
    # Audio triggers
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Bot addressing
    # --------------------------------------------------------

    bot_username = (
        f"@{BOT_INFO.username}"
        if BOT_INFO and BOT_INFO.username
        else ""
    )

    is_private = (
        message.chat.type == "private"
    )

    is_tagged = (
        bool(bot_username)
        and bot_username.lower()
        in text_no_html.lower()
    )

    is_tagged = (
        is_tagged
        or "@gemini"
        in text_no_html.lower()
    )

    is_reply_to_bot = bool(
        message.reply_to_message
        and BOT_INFO
        and message.reply_to_message.from_user
        and (
            message.reply_to_message.from_user.id
            == BOT_INFO.id
        )
    )

    if not (
        is_tagged
        or is_reply_to_bot
        or is_private
        or message.content_type
        in {"voice", "audio"}
    ):
        return

    user_id_str = str(
        message.from_user.id
    )

    chat_id = message.chat.id
    msg_id = message.message_id

    # --------------------------------------------------------
    # Clean prompt
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Cooldown
    # --------------------------------------------------------

    cooldown_key = (
        f"cooldown:{user_id_str}"
    )

    if await redis_client.exists(
        cooldown_key
    ):

        warning = (
            "Slow down, request limit reached."
        )

        if is_private:

            await message.answer(
                warning
            )

        else:

            await message.answer(
                warning,
                reply_to_message_id=msg_id,
            )

        return

    await redis_client.set(
        cooldown_key,
        "1",
        ex=4,
    )

    # --------------------------------------------------------
    # Reply context
    # --------------------------------------------------------

    replied_context = ""

    if message.reply_to_message:

        replied_context = (
            message.reply_to_message.text
            or message.reply_to_message.caption
            or ""
        )

    # --------------------------------------------------------
    # Audio input
    # --------------------------------------------------------

    audio_bytes = None
    audio_mime = "audio/ogg"

    if message.content_type in {
        "voice",
        "audio",
    }:

        audio_obj = (
            message.voice
            or message.audio
        )

        if audio_obj:

            file_info = await bot.get_file(
                audio_obj.file_id
            )

            stream = await bot.download_file(
                file_info.file_path
            )

            if stream:
                audio_bytes = stream.read()

            if getattr(
                audio_obj,
                "mime_type",
                None,
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

    # ========================================================
    # Gemini
    # ========================================================

    try:

        # ----------------------------------------------------
        # User memory
        # ----------------------------------------------------

        raw_mem = await redis_client.lrange(
            f"memory_list:{user_id_str}",
            0,
            -1,
        )

        saved_facts = [
            x.decode("utf-8")
            if isinstance(x, bytes)
            else str(x)
            for x in raw_mem
        ]

        # ----------------------------------------------------
        # Chat history
        # ----------------------------------------------------

        history_key = (
            f"chat_history:{chat_id}:{user_id_str}"
        )

        raw_hist = await redis_client.lrange(
            history_key,
            0,
            -1,
        )

        chat_history = [
            x.decode("utf-8")
            if isinstance(x, bytes)
            else str(x)
            for x in raw_hist
        ]

        # ----------------------------------------------------
        # Search
        # ----------------------------------------------------

        search_words = {
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
            for word in search_words
        )

        search_query = clean_prompt

        if (
            explicit_search
            and len(clean_prompt.split()) <= 4
        ):

            if replied_context:

                search_query = (
                    replied_context
                )

            elif chat_history:

                for old in reversed(
                    chat_history
                ):

                    if (
                        old.startswith("User: ")
                        and len(old.split()) > 2
                    ):

                        search_query = (
                            old[6:].strip()
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

        # ----------------------------------------------------
        # Context
        # ----------------------------------------------------

        context = []

        if replied_context:

            context.append(
                "Message User is Replying To:\n"
                f"\"{replied_context}\""
            )

        if chat_history:

            context.append(
                "Recent Conversation Context:\n"
                + "\n".join(chat_history)
            )

        if search_context:

            context.append(
                "Web Search Context:\n"
                + search_context
            )

        final_prompt = (
            clean_prompt
            or "Process and answer this voice note."
        )

        if context:

            final_prompt = (
                "\n\n".join(context)
                + "\n\nUser Question: "
                + final_prompt
            )

        # ----------------------------------------------------
        # Gemini instructions
        # ----------------------------------------------------

        today = datetime.now(
            timezone.utc
        ).strftime(
            "%A, %B %d, %Y"
        )

        instructions = (
            f"Today's date is {today}.\n"
            "Never use standard AI pleasantries.\n"
            "Keep casual replies brief, but expand when asked for detail.\n"
            "If the user changes subject, immediately follow the new subject.\n"
            "If joking or sarcastic, match the energy.\n"
            "If you do not know, say exactly: "
            "'I don't have enough details to answer that accurately' "
            "without guessing.\n"
            "Do not assume personal details unless explicitly present "
            "in the memory list.\n\n"

            "OUTPUT FORMAT: Use Telegram Rich HTML.\n"
            "Use <h1>-<h6>, <p>, <b>, <i>, <u>, <s>, "
            "<code>, <pre>, <table>, <details>, "
            "<a href=\"URL\">text</a>, "
            "<tg-button-row>, and other supported rich HTML "
            "where useful.\n"

            "Do not use Markdown asterisks for formatting.\n"
            "Do not use Markdown pipe tables.\n"
            "Return clean HTML suitable for Telegram sendRichMessage."
        )

        if search_context:

            instructions += (
                "\nWhen using Web Search Context, state the "
                "information directly. "
                "If the user asks for links or sources, "
                "use HTML links. "
                "If they do not ask for sources, "
                "do not include URLs."
            )

        if chat_history:

            instructions += (
                "\nUse Recent Conversation Context for "
                "continuity, but do not repeat it."
            )

        if saved_facts:

            instructions += (
                "\n\nUser memory directives:\n"
                + "\n".join(
                    f"- {x}"
                    for x in saved_facts
                )
            )

        # ----------------------------------------------------
        # Gemini safety
        # ----------------------------------------------------

        safety = [
            types.SafetySetting(
                category="HARM_CATEGORY_HATE_SPEECH",
                threshold="BLOCK_NONE",
            ),
            types.SafetySetting(
                category="HARM_CATEGORY_HARASSMENT",
                threshold="BLOCK_NONE",
            ),
            types.SafetySetting(
                category="HARM_CATEGORY_SEXUALLY_EXPLICIT",
                threshold="BLOCK_NONE",
            ),
            types.SafetySetting(
                category="HARM_CATEGORY_DANGEROUS_CONTENT",
                threshold="BLOCK_NONE",
            ),
        ]

        # ----------------------------------------------------
        # Generate
        # ----------------------------------------------------

        if audio_bytes:

            response = (
                await gemini_client.aio.models.generate_content(
                    model="gemini-3.5-flash-lite",
                    contents=[
                        types.Part.from_bytes(
                            data=audio_bytes,
                            mime_type=audio_mime,
                        ),
                        final_prompt,
                    ],
                    config=types.GenerateContentConfig(
                        system_instruction=instructions,
                        safety_settings=safety,
                    ),
                )
            )

        else:

            response = (
                await gemini_client.aio.models.generate_content(
                    model="gemini-3.5-flash-lite",
                    contents=final_prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=instructions,
                        safety_settings=safety,
                    ),
                )
            )

        response_text = (
            response.text
            or "I didn't receive a response."
        )

        response_text = (
            response_text
            .replace("```html", "")
            .replace("```", "")
            .strip()
        )

        # ----------------------------------------------------
        # Send Rich Message
        # ----------------------------------------------------

        if is_private:

            await bot.send_rich_message(
                chat_id=chat_id,
                rich_message=InputRichMessage(
                    html=response_text,
                ),
            )

        else:

            await bot.send_rich_message(
                chat_id=chat_id,
                rich_message=InputRichMessage(
                    html=response_text,
                ),
                reply_parameters=ReplyParameters(
                    message_id=msg_id,
                ),
            )

        # ----------------------------------------------------
        # Save history
        # ----------------------------------------------------

        clean_history = re.sub(
            r"<[^>]+>",
            "",
            response_text,
        )

        await redis_client.rpush(
            history_key,
            f"User: {clean_prompt or 'Voice Note'}",
            f"Bot: {clean_history}",
        )

        await redis_client.ltrim(
            history_key,
            -10,
            -1,
        )

    except Exception as e:

        print(
            f"Gemini API error: {e}"
        )

        error = (
            "Whoa, I'm getting a little overwhelmed! "
            "Let me catch my breath for a minute."
            if "429" in str(e)
            else
            "I am currently broken right now, "
            "the owner needs to fix me."
        )

        if is_private:

            await message.answer(
                error
            )

        else:

            await message.answer(
                error,
                reply_to_message_id=msg_id,
            )


# ============================================================
# Health check
# ============================================================

async def health_check(
    request,
):
    return web.Response(
        text="200 OK - Bot is running.",
        status=200,
    )


# ============================================================
# Telegram command configuration
# ============================================================

async def configure_commands() -> None:

    commands = [
        BotCommand(
            command="memories",
            description="Open your private memory menu",
            is_ephemeral=True,
        ),
        BotCommand(
            command="help",
            description="Open your private memory menu",
            is_ephemeral=True,
        ),
    ]

    await bot.set_my_commands(
        commands,
        scope=BotCommandScopeAllGroupChats(),
    )

    await bot.set_my_commands(
        commands,
        scope=BotCommandScopeAllPrivateChats(),
    )


# ============================================================
# Main
# ============================================================

async def main():

    global BOT_INFO

    try:

        print(
            "Clearing conflicting webhooks "
            "from Telegram servers..."
        )

        await bot.delete_webhook(
            drop_pending_updates=True,
        )

        BOT_INFO = await bot.get_me()

        print(
            f"Bot authenticated as "
            f"@{BOT_INFO.username}"
        )

        await configure_commands()

        print(
            "Ephemeral /memories and /help "
            "commands configured."
        )

    except Exception as e:

        print(
            f"Startup Telegram configuration error: {e}"
        )

    # --------------------------------------------------------
    # Healthcheck server
    # --------------------------------------------------------

    app = web.Application()

    app.router.add_get(
        "/",
        health_check,
    )

    app.router.add_get(
        "/health",
        health_check,
    )

    runner = web.AppRunner(app)

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
        f"Healthcheck server listening "
        f"on port {port}"
    )

    print(
        f"SearXNG endpoint: {SEARXNG_URL}"
    )

    # --------------------------------------------------------
    # Polling
    # --------------------------------------------------------

    try:

        await dp.start_polling(
            bot,
        )

    finally:

        # Cancel memory timers.
        for task in list(
            menu_tasks.values()
        ):

            task.cancel()

        await bot.session.close()

        await redis_client.aclose()

        await runner.cleanup()

        print(
            "Cleanup complete. "
            "Process exiting."
        )


# ============================================================
# Entrypoint
# ============================================================

if __name__ == "__main__":
    asyncio.run(main())
