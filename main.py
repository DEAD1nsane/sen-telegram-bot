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
# CONFIGURATION
# ============================================================

redis_url = os.environ.get("REDIS_URL", "")

if not redis_url:
    host = os.environ.get("REDISHOST", "localhost")
    port = os.environ.get("REDISPORT", "6379")
    password = os.environ.get("REDISPASSWORD", "")

    redis_url = (
        f"redis://default:{password}@{host}:{port}"
        if password
        else f"redis://{host}:{port}"
    )

if "upstash" in redis_url.lower() and redis_url.startswith("redis://"):
    redis_url = redis_url.replace("redis://", "rediss://", 1)

redis_client = redis.from_url(
    redis_url,
    ssl_cert_reqs=None if redis_url.startswith("rediss://") else "required",
)

API_TOKEN = os.getenv("BOT_TOKEN", "")

if not API_TOKEN:
    raise ValueError(
        "CRITICAL CONFIGURATION ERROR: 'BOT_TOKEN' missing."
    )

SEARXNG_URL = os.getenv(
    "SEARXNG_URL",
    "http://searxng.railway.internal:8080/search",
).rstrip("/")

gemini_api_key = os.getenv("GEMINI_API_KEY", "")

if not gemini_api_key:
    raise ValueError(
        "CRITICAL CONFIGURATION ERROR: 'GEMINI_API_KEY' missing."
    )

gemini_client = genai.Client(api_key=gemini_api_key)

OWNER_ID = int(os.getenv("OWNER_ID", "0"))


# ============================================================
# TELEGRAM
# ============================================================

bot = Bot(
    token=API_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML"),
)

dp = Dispatcher()
router = Router()
dp.include_router(router)

BOT_INFO = None


# ============================================================
# MEMORY MENU SETTINGS
# ============================================================

# Menu disappears after 30 seconds of inactivity.
MENU_TTL = 30

# How long an add/edit/forget interaction remains active.
INTERACTION_TTL = 300

# Redis ownership records live slightly longer than the menu.
MENU_OWNER_TTL = MENU_TTL + 60

# (chat_id, user_id) -> asyncio.Task
menu_tasks: dict[tuple[int, int], asyncio.Task] = {}


# ============================================================
# USER / MEMORY HELPERS
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


def user_display_name(user) -> str:
    """
    Return a safely escaped display name for Rich Message HTML.
    """
    name = (user.full_name or "").strip()

    if not name:
        name = (user.username or "").strip()

    if not name:
        name = "Your"

    return html.escape(name, quote=False)


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


async def get_formatted_memories(
    user_id_str: str,
    user_name: str,
) -> str:

    memories = await get_memories(user_id_str)

    safe_name = html.escape(
        user_name,
        quote=False,
    )

    if not memories:
        return (
            f"<h2>{safe_name}'s Instructions</h2>"
            "<p>You don't have any saved instructions yet.</p>"
        )

    lines = [
        f"<h2>{safe_name}'s Instructions</h2>",
        "<ol>",
    ]

    for memory in memories:
        lines.append(
            f"<li>{html.escape(memory)}</li>"
        )

    lines.append("</ol>")

    return "".join(lines)


# ============================================================
# MEMORY MENU OWNERSHIP
# ============================================================

def menu_owner_key(
    chat_id: int,
    menu_id: int,
) -> str:
    return f"memory_menu_owner:{chat_id}:{menu_id}"


async def set_menu_owner(
    chat_id: int,
    menu_id: int,
    user_id: int,
) -> None:
    """
    Store exactly which Telegram user owns this menu.
    """
    await redis_client.set(
        menu_owner_key(chat_id, menu_id),
        str(user_id),
        ex=MENU_OWNER_TTL,
    )


async def get_menu_owner(
    chat_id: int,
    menu_id: int,
) -> int | None:

    value = await redis_client.get(
        menu_owner_key(chat_id, menu_id)
    )

    if value is None:
        return None

    if isinstance(value, bytes):
        value = value.decode("utf-8")

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def clear_menu_owner(
    chat_id: int,
    menu_id: int,
) -> None:

    await redis_client.delete(
        menu_owner_key(chat_id, menu_id)
    )


async def is_menu_owner(callback: CallbackQuery) -> bool:
    """
    SECURITY CHECK.

    A callback is allowed to operate on a memory menu only if
    the user pressing the button is the same user who owns it.
    """

    message = callback.message

    if not message:
        return False

    chat_id = message.chat.id

    # Ephemeral messages have a dedicated ephemeral_message_id.
    # Normal messages use message_id.
    menu_id = message.ephemeral_message_id

    if menu_id is None:
        menu_id = message.message_id

    owner_id = await get_menu_owner(
        chat_id,
        menu_id,
    )

    if owner_id is None:
        return False

    return owner_id == callback.from_user.id


async def reject_unauthorized_callback(
    callback: CallbackQuery,
) -> bool:
    """
    Returns True when the callback is NOT authorized.

    This deliberately performs no memory operation.
    """

    if await is_menu_owner(callback):
        return False

    try:
        await callback.answer(
            "This memory menu belongs to another user.",
            show_alert=True,
        )
    except Exception as e:
        print(f"Unauthorized callback answer error: {e}")

    print(
        "Blocked unauthorized memory-menu callback: "
        f"user={callback.from_user.id}"
    )

    return True


# ============================================================
# RICH MESSAGE MENUS
# ============================================================

def rich_main_menu(user_name: str) -> str:
    safe_name = html.escape(
        user_name,
        quote=False,
    )

    return f"""
<h2>{safe_name}'s Instructions</h2>
<p>Manage your personal memories and instructions.</p>

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


def rich_memory_menu(
    memories_html: str,
) -> str:

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
This permanently clears all saved memories and this
conversation's stored history.
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
# MENU TIMER
# ============================================================

async def cancel_menu_timer(
    key: tuple[int, int],
) -> None:

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
    ephemeral_message_id: int | None,
    message_id: int | None,
) -> None:

    key = (chat_id, user_id)

    await cancel_menu_timer(key)

    async def expire() -> None:
        try:
            await asyncio.sleep(MENU_TTL)

            if ephemeral_message_id is not None:

                await bot.delete_ephemeral_message(
                    chat_id=chat_id,
                    receiver_user_id=user_id,
                    ephemeral_message_id=ephemeral_message_id,
                )

                await clear_menu_owner(
                    chat_id,
                    ephemeral_message_id,
                )

            elif message_id is not None:

                await bot.delete_message(
                    chat_id=chat_id,
                    message_id=message_id,
                )

                await clear_menu_owner(
                    chat_id,
                    message_id,
                )

        except asyncio.CancelledError:
            raise

        except Exception as e:
            print(f"Menu expiry error: {e}")

        finally:
            if menu_tasks.get(key) is asyncio.current_task():
                menu_tasks.pop(key, None)

    menu_tasks[key] = asyncio.create_task(expire())


# ============================================================
# SEND MENU
# ============================================================

async def send_menu(
    chat_id: int,
    user_id: int,
    rich_html: str,
    *,
    reply_to_ephemeral_id: int | None = None,
) -> Message:

    kwargs = {
        "chat_id": chat_id,
        "rich_message": InputRichMessage(
            html=rich_html
        ),
    }

    if reply_to_ephemeral_id is not None:

        kwargs["reply_parameters"] = ReplyParameters(
            ephemeral_message_id=reply_to_ephemeral_id
        )

    elif chat_id == user_id:

        # Private chats don't need ephemeral parameters.
        pass

    else:

        # This branch is only used when Telegram has not supplied
        # an ephemeral command context.
        #
        # We intentionally do NOT attempt to create a direct
        # ephemeral group message here because Telegram can reject
        # that with BOT_NOT_ADMIN.
        kwargs["ephemeral_message_parameters"] = (
            EphemeralMessageParameters(
                receiver_user_id=user_id,
            )
        )

    message = await bot.send_rich_message(
        **kwargs
    )

    ephemeral_id = getattr(
        message,
        "ephemeral_message_id",
        None,
    )

    actual_message_id = (
        None
        if ephemeral_id is not None
        else message.message_id
    )

    # --------------------------------------------------------
    # CRITICAL SECURITY STEP
    # --------------------------------------------------------
    #
    # The menu ID is stored against the user who opened it.
    # Every callback later checks this Redis record.
    #
    menu_id = (
        ephemeral_id
        if ephemeral_id is not None
        else actual_message_id
    )

    if menu_id is not None:

        await set_menu_owner(
            chat_id,
            menu_id,
            user_id,
        )

    await schedule_menu_delete(
        chat_id,
        user_id,
        ephemeral_id,
        actual_message_id,
    )

    return message


# ============================================================
# EDIT MENU
# ============================================================

async def edit_menu(
    callback: CallbackQuery,
    rich_html: str,
) -> None:

    # SECOND SECURITY BOUNDARY.
    #
    # Even if a callback handler accidentally forgets to check
    # ownership, this function refuses to edit another user's menu.
    if await reject_unauthorized_callback(callback):
        return

    message = callback.message

    if not message:
        return

    chat_id = message.chat.id
    user_id = callback.from_user.id

    ephemeral_id = message.ephemeral_message_id

    await cancel_menu_timer(
        (chat_id, user_id)
    )

    if ephemeral_id is not None:

        await bot.edit_ephemeral_message_text(
            chat_id=chat_id,
            receiver_user_id=user_id,
            ephemeral_message_id=ephemeral_id,
            rich_message=InputRichMessage(
                html=rich_html
            ),
        )

        # Ownership remains with the same user.
        await set_menu_owner(
            chat_id,
            ephemeral_id,
            user_id,
        )

        await schedule_menu_delete(
            chat_id,
            user_id,
            ephemeral_id,
            None,
        )

    else:

        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message.message_id,
            rich_message=InputRichMessage(
                html=rich_html
            ),
        )

        await set_menu_owner(
            chat_id,
            message.message_id,
            user_id,
        )

        await schedule_menu_delete(
            chat_id,
            user_id,
            None,
            message.message_id,
        )


# ============================================================
# CLOSE MENU
# ============================================================

async def close_menu(
    callback: CallbackQuery,
) -> None:

    # SECOND SECURITY BOUNDARY.
    if await reject_unauthorized_callback(callback):
        return

    message = callback.message

    if not message:
        return

    chat_id = message.chat.id
    user_id = callback.from_user.id

    ephemeral_id = message.ephemeral_message_id

    await clear_interaction(user_id)

    await cancel_menu_timer(
        (chat_id, user_id)
    )

    try:

        if ephemeral_id is not None:

            await bot.delete_ephemeral_message(
                chat_id=chat_id,
                receiver_user_id=user_id,
                ephemeral_message_id=ephemeral_id,
            )

            await clear_menu_owner(
                chat_id,
                ephemeral_id,
            )

        else:

            await bot.delete_message(
                chat_id=chat_id,
                message_id=message.message_id,
            )

            await clear_menu_owner(
                chat_id,
                message.message_id,
            )

    except Exception as e:
        print(f"Menu close error: {e}")


# ============================================================
# /MEMORIES
# ============================================================

async def show_memories(
    message: Message,
) -> None:

    if not message.from_user:
        return

    user = message.from_user

    user_id = user.id
    chat_id = message.chat.id

    user_name = user_display_name(user)

    await clear_interaction(user_id)

    # --------------------------------------------------------
    # TELEGRAM EPHEMERAL COMMAND
    # --------------------------------------------------------

    if message.ephemeral_message_id is not None:

        await send_menu(
            chat_id,
            user_id,
            rich_main_menu(user_name),
            reply_to_ephemeral_id=message.ephemeral_message_id,
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
            chat_id,
            user_id,
            rich_main_menu(user_name),
        )

        return

    # --------------------------------------------------------
    # NORMAL GROUP MESSAGE
    # --------------------------------------------------------
    #
    # A manually typed group command does NOT contain Telegram's
    # ephemeral command context.
    #
    # Do not create a public memory menu.
    # Do not attempt EphemeralMessageParameters here because that
    # can require admin privileges and produces BOT_NOT_ADMIN.
    #
    # Delete the typed command and wait for the Telegram command
    # picker to invoke the ephemeral version.
    # --------------------------------------------------------

    try:
        await message.delete()
    except Exception:
        pass


@router.message(Command("memories"))
async def handle_memories(
    message: Message,
):
    await show_memories(message)


# ============================================================
# MEMORY CALLBACKS
# ============================================================

@router.callback_query(F.data == "memory_view")
async def handle_memory_view(
    callback: CallbackQuery,
):

    if await reject_unauthorized_callback(callback):
        return

    await callback.answer()

    user = callback.from_user

    memories_html = await get_formatted_memories(
        str(user.id),
        user_display_name(user),
    )

    try:

        await edit_menu(
            callback,
            rich_memory_menu(memories_html),
        )

    except Exception as e:
        print(f"Memory view error: {e}")


@router.callback_query(F.data == "memory_add")
async def handle_memory_add(
    callback: CallbackQuery,
):

    if await reject_unauthorized_callback(callback):
        return

    await callback.answer(
        "Send the memory you want me to save."
    )

    await set_interaction(
        callback.from_user.id,
        "add",
    )

    try:

        await edit_menu(
            callback,
            rich_back_close(
                "<h2>New memory</h2>"
                "<p>"
                "Send the fact or instruction you want me "
                "to remember."
                "</p>"
                "<p>"
                "You can send multiple items separated by "
                "<code>,,</code>."
                "</p>"
            ),
        )

    except Exception as e:
        print(f"Memory add menu error: {e}")


@router.callback_query(F.data == "memory_edit")
async def handle_memory_edit(
    callback: CallbackQuery,
):

    if await reject_unauthorized_callback(callback):
        return

    await callback.answer()

    user_id = callback.from_user.id

    memories = await get_memories(
        str(user_id)
    )

    await set_interaction(
        user_id,
        "edit_number",
    )

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
            "<p>",
            "Send the number and replacement text.",
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
                "<p>",
                "Example: "
                "<code>2 My new instruction</code>",
                "</p>",
            ]
        )

        body = "".join(rows)

    try:

        await edit_menu(
            callback,
            rich_back_close(body),
        )

    except Exception as e:
        print(f"Memory edit menu error: {e}")


@router.callback_query(F.data == "memory_forget")
async def handle_memory_forget(
    callback: CallbackQuery,
):

    if await reject_unauthorized_callback(callback):
        return

    await callback.answer()

    user_id = callback.from_user.id

    memories = await get_memories(
        str(user_id)
    )

    await set_interaction(
        user_id,
        "forget",
    )

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
            "<p>",
            "Send one number or several separated by "
            "<code>,,</code>.",
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
                "<p>",
                "Example: "
                "<code>1,, 3,, 5</code>",
                "</p>",
            ]
        )

        body = "".join(rows)

    try:

        await edit_menu(
            callback,
            rich_back_close(body),
        )

    except Exception as e:
        print(f"Memory forget menu error: {e}")


@router.callback_query(F.data == "memory_forget_all")
async def handle_memory_forget_all(
    callback: CallbackQuery,
):

    if await reject_unauthorized_callback(callback):
        return

    await clear_interaction(
        callback.from_user.id
    )

    await callback.answer()

    try:

        await edit_menu(
            callback,
            rich_forget_all_confirm(),
        )

    except Exception as e:
        print(
            f"Forget-all confirmation error: {e}"
        )


@router.callback_query(
    F.data == "memory_confirm_forget_all"
)
async def handle_confirm_forget_all(
    callback: CallbackQuery,
):

    if await reject_unauthorized_callback(callback):
        return

    user_id = callback.from_user.id
    user_id_str = str(user_id)

    chat_id = (
        callback.message.chat.id
        if callback.message
        else 0
    )

    # IMPORTANT:
    # Everything here is derived from callback.from_user.id.
    # There is no user-supplied ID in the callback data.
    await redis_client.delete(
        f"memory_list:{user_id_str}",
        f"chat_history:{chat_id}:{user_id_str}",
        interaction_key(user_id),
    )

    await callback.answer(
        "All memories cleared."
    )

    try:

        await edit_menu(
            callback,
            rich_back_close(
                "<h2>Memories cleared</h2>"
                "<p>"
                "Everything saved for this user and "
                "conversation has been removed."
                "</p>"
            ),
        )

    except Exception as e:
        print(
            f"Forget-all completion error: {e}"
        )


@router.callback_query(F.data == "memory_back")
async def handle_memory_back(
    callback: CallbackQuery,
):

    if await reject_unauthorized_callback(callback):
        return

    await clear_interaction(
        callback.from_user.id
    )

    await callback.answer()

    user_name = user_display_name(
        callback.from_user
    )

    try:

        await edit_menu(
            callback,
            rich_main_menu(user_name),
        )

    except Exception as e:
        print(f"Memory back error: {e}")


@router.callback_query(F.data == "memory_close")
async def handle_memory_close(
    callback: CallbackQuery,
):

    if await reject_unauthorized_callback(callback):
        return

    await callback.answer("Closed")

    await close_menu(callback)


# ============================================================
# MEMORY TEXT INPUT
# ============================================================

async def process_memory_text(
    message: Message,
    action: str,
) -> bool:

    if not message.text:
        return False

    if message.text.startswith("/"):
        return False

    if not message.from_user:
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

        await clear_interaction(user_id)

        try:
            await message.delete()
        except Exception:
            pass

        return True

    # --------------------------------------------------------
    # EDIT
    # --------------------------------------------------------

    if action == "edit_number":

        parts = message.text.strip().split(
            " ",
            1,
        )

        if (
            len(parts) != 2
            or not parts[0].isdigit()
        ):
            return True

        index = int(parts[0]) - 1

        new_value = (
            parts[1]
            .strip()
            [:200]
        )

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

        await clear_interaction(user_id)

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

        await clear_interaction(user_id)

        try:
            await message.delete()
        except Exception:
            pass

        return True

    return False


# ============================================================
# DELETE COMMAND
# ============================================================

@router.message(Command("delete", "del"))
async def handle_delete(
    message: Message,
):

    if not message.from_user:
        return

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
# WEB SEARCH
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
                "SearXNG HTTP "
                f"{response.status_code}: "
                f"{response.text[:300]}"
            )

            return ""

        results = response.json().get(
            "results",
            [],
        )[:10]

        return "\n\n".join(
            (
                f"Title: {x.get('title', '')}\n"
                f"Content: {x.get('content', '')}\n"
                f"URL: {x.get('url', '')}"
            )
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
# AUDIO
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

            print(
                f"Audio file not found: "
                f"{file_path}"
            )

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
                "message to be replied "
                "not found"
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
# CONVERSATION HANDLER
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

    if not message.from_user:
        return

    user_id = message.from_user.id

    # --------------------------------------------------------
    # MEMORY INTERACTION
    # --------------------------------------------------------

    action = await get_interaction(
        user_id
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

    # --------------------------------------------------------
    # NORMAL BOT CONVERSATION
    # --------------------------------------------------------

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

    # Audio triggers.
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

    bot_username = (
        f"@{BOT_INFO.username}"
        if BOT_INFO
        and BOT_INFO.username
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
            message.reply_to_message
            .from_user.id
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

    # --------------------------------------------------------
    # PROMPT CLEANUP
    # --------------------------------------------------------

    clean_prompt = text_no_html

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
    )

    clean_prompt = clean_prompt.strip()

    if not clean_prompt:
        clean_prompt = (
            "Please respond to the user's message."
        )

    # --------------------------------------------------------
    # USER MEMORY
    # --------------------------------------------------------

    saved_facts = await get_memories(
        str(user_id)
    )

    # --------------------------------------------------------
    # CHAT HISTORY
    # --------------------------------------------------------

    history_key = (
        f"chat_history:"
        f"{message.chat.id}:"
        f"{user_id}"
    )

    try:

        history_raw = await redis_client.lrange(
            history_key,
            -10,
            -1,
        )

        history = [
            item.decode("utf-8")
            if isinstance(item, bytes)
            else str(item)
            for item in history_raw
        ]

    except Exception as e:

        print(
            f"History read error: {e}"
        )

        history = []

    # --------------------------------------------------------
    # SYSTEM INSTRUCTIONS
    # --------------------------------------------------------

    instructions = """
You are SenAnythangBot.

Be helpful, conversational, accurate, and concise.

When the user asks for current information, use the available
web search context when provided.

The user may have personal instructions or memories stored in
Redis. These are private to that user and must only be applied
to that user's conversations.

Do not reveal, infer, or fabricate another user's memories,
instructions, conversation history, or private information.

Telegram Rich Messages are supported. When appropriate, return
clean HTML suitable for Telegram Rich Messages.

Do not wrap the response in Markdown code fences unless the user
specifically requests code.

Supported basic formatting includes:
<b>bold</b>
<i>italic</i>
<code>code</code>
<s>strikethrough</s>
"""

    if saved_facts:

        instructions += (
            "\n\nYou must strictly follow these "
            "User Instructions:\n"
            + "\n".join(
                f"- {fact}"
                for fact in saved_facts
            )
        )

    # --------------------------------------------------------
    # WEB SEARCH
    # --------------------------------------------------------

    search_context = ""

    search_triggers = (
        "latest",
        "today",
        "current",
        "news",
        "weather",
        "price",
        "recent",
        "right now",
    )

    if any(
        trigger in clean_prompt.lower()
        for trigger in search_triggers
    ):

        search_context = await free_web_search(
            clean_prompt
        )

    # --------------------------------------------------------
    # GEMINI PROMPT
    # --------------------------------------------------------

    prompt_parts = []

    if history:

        prompt_parts.append(
            "Recent conversation:\n"
            + "\n".join(history)
        )

    if search_context:

        prompt_parts.append(
            "Web search results:\n"
            + search_context
        )

    prompt_parts.append(
        "User message:\n"
        + clean_prompt
    )

    final_prompt = "\n\n".join(
        prompt_parts
    )

    # --------------------------------------------------------
    # GEMINI
    # --------------------------------------------------------

    try:

        response = (
            await gemini_client
            .aio
            .models
            .generate_content(
                model="gemini-3.5-flash-lite",
                contents=final_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=instructions,
                ),
            )
        )

        response_text = (
            response.text
            or "I didn't get a response."
        )

        # Remove accidental Markdown fences.
        response_text = re.sub(
            r"^```(?:html)?\s*",
            "",
            response_text.strip(),
            flags=re.IGNORECASE,
        )

        response_text = re.sub(
            r"\s*```$",
            "",
            response_text.strip(),
        )

        # ----------------------------------------------------
        # SEND AS RICH MESSAGE
        # ----------------------------------------------------

        if is_private:

            await bot.send_rich_message(
                chat_id=message.chat.id,
                rich_message=InputRichMessage(
                    html=response_text
                ),
            )

        else:

            await bot.send_rich_message(
                chat_id=message.chat.id,
                rich_message=InputRichMessage(
                    html=response_text
                ),
                reply_parameters=ReplyParameters(
                    message_id=message.message_id
                ),
            )

        # ----------------------------------------------------
        # SAVE HISTORY
        # ----------------------------------------------------

        await redis_client.rpush(
            history_key,
            f"User: {clean_prompt}",
            f"Bot: {response_text}",
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

        error_text = (
            "I am currently broken right now, "
            "the owner needs to fix me."
        )

        if "429" in str(ai_err):

            error_text = (
                "Whoa, I'm getting a little "
                "overwhelmed! Let me catch my "
                "breath for a minute."
            )

        try:

            if is_private:

                await bot.send_rich_message(
                    chat_id=message.chat.id,
                    rich_message=InputRichMessage(
                        html=(
                            f"<p>{html.escape(error_text)}</p>"
                        )
                    ),
                )

            else:

                await bot.send_rich_message(
                    chat_id=message.chat.id,
                    rich_message=InputRichMessage(
                        html=(
                            f"<p>{html.escape(error_text)}</p>"
                        )
                    ),
                    reply_parameters=ReplyParameters(
                        message_id=message.message_id
                    ),
                )

        except Exception as send_error:

            print(
                "Failed to send Gemini error "
                f"message: {send_error}"
            )


# ============================================================
# TELEGRAM COMMAND CONFIGURATION
# ============================================================

async def configure_commands() -> None:

    commands = [
        BotCommand(
            command="memories",
            description="Open your private memory menu",
            is_ephemeral=True,
        ),
    ]

    # Remove the old /help command from these scopes.
    try:

        await bot.delete_my_commands(
            scope=BotCommandScopeAllGroupChats()
        )

        await bot.delete_my_commands(
            scope=BotCommandScopeAllPrivateChats()
        )

    except Exception as e:

        print(
            f"Command cleanup warning: {e}"
        )

    await bot.set_my_commands(
        commands,
        scope=BotCommandScopeAllGroupChats(),
    )

    await bot.set_my_commands(
        commands,
        scope=BotCommandScopeAllPrivateChats(),
    )


# ============================================================
# HEALTH CHECK
# ============================================================

async def health_check(
    request: web.Request,
) -> web.Response:

    return web.json_response(
        {
            "status": "ok",
            "bot": (
                f"@{BOT_INFO.username}"
                if BOT_INFO
                else "starting"
            ),
        }
    )


# ============================================================
# STARTUP
# ============================================================

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
            f"Bot authenticated as "
            f"@{BOT_INFO.username}"
        )

        await configure_commands()

        print(
            "Ephemeral /memories command configured."
        )

    except Exception as e:

        print(
            "Startup Telegram configuration "
            f"error: {e}"
        )

    # --------------------------------------------------------
    # RAILWAY HEALTH SERVER
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
        os.getenv("PORT", "8080")
    )

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port,
    )

    await site.start()

    print(
        f"Health server listening on port {port}"
    )

    # --------------------------------------------------------
    # POLLING
    # --------------------------------------------------------

    try:

        print(
            "Starting Telegram polling..."
        )

        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )

    finally:

        print(
            "Shutting down..."
        )

        try:
            await runner.cleanup()
        except Exception:
            pass

        try:
            await bot.session.close()
        except Exception:
            pass

        try:
            await redis_client.aclose()
        except Exception:
            pass


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    asyncio.run(main())
