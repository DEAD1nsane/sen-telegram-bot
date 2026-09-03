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
    BotCommandScopeAllChatAdministrators,
    ReplyParameters,
    InputRichMessage,
    InputRichBlockParagraph,
    InputRichBlockSectionHeading,
    InputRichBlockButtons,
    InputRichBlockList,
    InputRichBlockListItem,
    RichMessageButton,
    RichTextBold,
    RichTextItalic,
    RichTextUnderline,
    RichTextStrikethrough,
    RichTextCode,
    EphemeralMessageParameters,
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

    redis_url = (
        f"redis://default:{password}@{host}:{port}"
        if password
        else f"redis://{host}:{port}"
    )

if "upstash" in redis_url.lower() and redis_url.startswith("redis://"):
    redis_url = redis_url.replace(
        "redis://",
        "rediss://",
        1,
    )

redis_client = redis.from_url(
    redis_url,
    ssl_cert_reqs=None
    if redis_url.startswith("rediss://")
    else "required",
)


# ==========================================
# Telegram token
# ==========================================

API_TOKEN = (
    os.getenv("BOT_TOKEN")
    or os.getenv("TELEGRAM_BOT_TOKEN")
    or ""
)

if not API_TOKEN:
    raise ValueError(
        "CRITICAL CONFIGURATION ERROR: "
        "'BOT_TOKEN' missing. "
        "Set BOT_TOKEN in Railway Variables."
    )


# ==========================================
# Other configuration
# ==========================================

SEARXNG_URL = os.getenv(
    "SEARXNG_URL",
    "http://searxng.railway.internal:8080/search",
).rstrip("/")

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
    api_key=gemini_api_key
)

OWNER_ID = int(
    os.getenv("OWNER_ID", "0")
)


# ==========================================
# Telegram
# ==========================================

bot = Bot(
    token=API_TOKEN,
    default=DefaultBotProperties(
        parse_mode="HTML"
    ),
)

dp = Dispatcher()
router = Router()

dp.include_router(router)

BOT_INFO = None


# ==========================================
# Memory menu configuration
# ==========================================

INTERACTION_TTL = 300

# Telegram ephemeral messages are temporary.
# This controls our own cleanup task.
MENU_TTL = 30


# ==========================================
# Memory interaction state
# ==========================================

def interaction_key(
    chat_id: int,
    user_id: int,
) -> str:
    return f"memory_interaction:{chat_id}:{user_id}"


async def set_interaction(
    chat_id: int,
    user_id: int,
    action: str,
) -> None:
    await redis_client.set(
        interaction_key(chat_id, user_id),
        action,
        ex=INTERACTION_TTL,
    )


async def get_interaction(
    chat_id: int,
    user_id: int,
) -> str | None:

    value = await redis_client.get(
        interaction_key(chat_id, user_id)
    )

    if isinstance(value, bytes):
        return value.decode("utf-8")

    return value


async def clear_interaction(
    chat_id: int,
    user_id: int,
) -> None:

    await redis_client.delete(
        interaction_key(chat_id, user_id)
    )


# ==========================================
# Ephemeral menu identity
# ==========================================

def menu_identity_key(
    chat_id: int,
    user_id: int,
) -> str:

    return (
        f"memory_menu_identity:"
        f"{chat_id}:{user_id}"
    )


async def register_menu_identity(
    chat_id: int,
    user_id: int,
    ephemeral_message_id: int,
) -> None:

    await redis_client.set(
        menu_identity_key(
            chat_id,
            user_id,
        ),
        str(ephemeral_message_id),
        ex=MENU_TTL + 5,
    )


async def get_menu_identity(
    chat_id: int,
    user_id: int,
) -> int | None:

    value = await redis_client.get(
        menu_identity_key(
            chat_id,
            user_id,
        )
    )

    if value is None:
        return None

    if isinstance(value, bytes):
        value = value.decode("utf-8")

    try:
        return int(value)

    except (TypeError, ValueError):
        return None


async def clear_menu_identity(
    chat_id: int,
    user_id: int,
) -> None:

    await redis_client.delete(
        menu_identity_key(
            chat_id,
            user_id,
        )
    )


# ==========================================
# Menu expiry
# ==========================================

async def expire_memory_menu(
    chat_id: int,
    user_id: int,
    menu_id: int,
) -> None:

    try:

        await asyncio.sleep(MENU_TTL)

        current_id = await get_menu_identity(
            chat_id,
            user_id,
        )

        # A newer menu replaced this one.
        if current_id != menu_id:
            return

        is_group = (
            chat_id != user_id
        )

        try:

            if is_group:

                await bot.delete_ephemeral_message(
                    chat_id=chat_id,
                    receiver_user_id=user_id,
                    ephemeral_message_id=menu_id,
                )

            else:

                await bot.delete_message(
                    chat_id=chat_id,
                    message_id=menu_id,
                )

        except Exception as e:

            print(
                "Automatic memory menu expiry error: "
                f"{e}"
            )

        finally:

            await clear_menu_identity(
                chat_id,
                user_id,
            )

            await clear_interaction(
                chat_id,
                user_id,
            )

    except asyncio.CancelledError:

        return

    except Exception as e:

        print(
            "Memory menu expiry task error: "
            f"{e}"
        )


def schedule_menu_expiry(
    chat_id: int,
    user_id: int,
    menu_id: int,
) -> None:

    asyncio.create_task(
        expire_memory_menu(
            chat_id,
            user_id,
            menu_id,
        )
    )


# ==========================================
# Memory helpers
# ==========================================

async def get_memories(
    user_id_str: str,
) -> list[str]:

    try:

        raw = await redis_client.lrange(
            f"memory_list:{user_id_str}",
            0,
            -1,
        )

        return [
            x.decode("utf-8")
            if isinstance(x, bytes)
            else str(x)
            for x in raw
        ]

    except Exception as e:

        print(
            f"Memory read error: {e}"
        )

        return []


# ==========================================
# User display name
# ==========================================

def get_user_display_name(
    user,
) -> str:

    if not user:
        return "User"

    first_name = (
        getattr(
            user,
            "first_name",
            None,
        )
        or ""
    ).strip()

    last_name = (
        getattr(
            user,
            "last_name",
            None,
        )
        or ""
    ).strip()

    display_name = " ".join(
        part
        for part in (
            first_name,
            last_name,
        )
        if part
    ).strip()

    if display_name:
        return display_name

    username = (
        getattr(
            user,
            "username",
            None,
        )
        or ""
    ).strip()

    if username:
        return username

    return "User"


# ==========================================
# Rich Text Parser
# ==========================================

def rich_text_from_markup(
    text: str,
):

    pattern = re.compile(
        r"(<b>.*?</b>|"
        r"<strong>.*?</strong>|"
        r"<i>.*?</i>|"
        r"<em>.*?</em>|"
        r"<u>.*?</u>|"
        r"<ins>.*?</ins>|"
        r"<s>.*?</s>|"
        r"<strike>.*?</strike>|"
        r"<del>.*?</del>|"
        r"<code>.*?</code>)",
        re.IGNORECASE | re.DOTALL,
    )

    parts = []
    position = 0

    for match in pattern.finditer(text):

        if match.start() > position:

            plain = text[
                position:match.start()
            ]

            if plain:
                parts.append(
                    html.unescape(plain)
                )

        token = match.group(0)
        lowered = token.lower()

        if (
            lowered.startswith("<b>")
            or lowered.startswith("<strong>")
        ):

            inner = re.sub(
                r"^<(?:b|strong)>|"
                r"</(?:b|strong)>$",
                "",
                token,
                flags=re.IGNORECASE | re.DOTALL,
            )

            parts.append(
                RichTextBold(
                    text=html.unescape(inner)
                )
            )

        elif (
            lowered.startswith("<i>")
            or lowered.startswith("<em>")
        ):

            inner = re.sub(
                r"^<(?:i|em)>|"
                r"</(?:i|em)>$",
                "",
                token,
                flags=re.IGNORECASE | re.DOTALL,
            )

            parts.append(
                RichTextItalic(
                    text=html.unescape(inner)
                )
            )

        elif (
            lowered.startswith("<u>")
            or lowered.startswith("<ins>")
        ):

            inner = re.sub(
                r"^<(?:u|ins)>|"
                r"</(?:u|ins)>$",
                "",
                token,
                flags=re.IGNORECASE | re.DOTALL,
            )

            parts.append(
                RichTextUnderline(
                    text=html.unescape(inner)
                )
            )

        elif (
            lowered.startswith("<s>")
            or lowered.startswith("<strike>")
            or lowered.startswith("<del>")
        ):

            inner = re.sub(
                r"^<(?:s|strike|del)>|"
                r"</(?:s|strike|del)>$",
                "",
                token,
                flags=re.IGNORECASE | re.DOTALL,
            )

            parts.append(
                RichTextStrikethrough(
                    text=html.unescape(inner)
                )
            )

        elif lowered.startswith("<code>"):

            inner = re.sub(
                r"^<code>|</code>$",
                "",
                token,
                flags=re.IGNORECASE | re.DOTALL,
            )

            parts.append(
                RichTextCode(
                    text=html.unescape(inner)
                )
            )

        position = match.end()

    if position < len(text):

        parts.append(
            html.unescape(
                text[position:]
            )
        )

    parts = [
        part
        for part in parts
        if part != ""
    ]

    if not parts:
        return ""

    if len(parts) == 1:
        return parts[0]

    return parts


# ==========================================
# Rich Memory Menu
# ==========================================

def get_memory_rich_message(
    text: str,
    menu_type: str,
    memories: list[str] | None = None,
) -> InputRichMessage:

    blocks = []

    # --------------------------------------
    # Native Telegram heading
    # --------------------------------------

    heading_match = re.match(
        r"^\s*<b>(.*?)</b>\s*(?:\n|$)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if heading_match:

        heading_text = html.unescape(
            heading_match.group(1)
        )

        remaining_text = text[
            heading_match.end():
        ].strip()

        blocks.append(
            InputRichBlockSectionHeading(
                text=heading_text,
                size=2,
            )
        )

        if remaining_text:

            blocks.append(
                InputRichBlockParagraph(
                    text=rich_text_from_markup(
                        remaining_text
                    )
                )
            )

    else:

        blocks.append(
            InputRichBlockParagraph(
                text=rich_text_from_markup(
                    text
                )
            )
        )

    # --------------------------------------
    # Memory list
    # --------------------------------------

    if memories:

        blocks.append(
            InputRichBlockList(
                items=[
                    InputRichBlockListItem(
                        blocks=[
                            InputRichBlockParagraph(
                                text=html.escape(
                                    memory
                                )
                            )
                        ],
                        value=index,
                        type="1",
                    )
                    for index, memory
                    in enumerate(
                        memories,
                        1,
                    )
                ]
            )
        )

    # --------------------------------------
    # Main menu
    # --------------------------------------

    if menu_type == "main":

        blocks.extend([

            InputRichBlockButtons(
                buttons=[
                    RichMessageButton(
                        text="🧠 View Memories",
                        callback_data="memory_view",
                        style="primary",
                    ),
                ],
                align="center",
            ),

            InputRichBlockButtons(
                buttons=[
                    RichMessageButton(
                        text="➕ New Memory",
                        callback_data="memory_add",
                        style="success",
                    ),
                ],
                align="center",
            ),

            InputRichBlockButtons(
                buttons=[
                    RichMessageButton(
                        text="❌ Close",
                        callback_data="memory_close",
                        style="danger",
                    ),
                ],
                align="center",
            ),
        ])

    # --------------------------------------
    # View menu
    # --------------------------------------

    elif menu_type == "view":

        blocks.extend([

            InputRichBlockButtons(
                buttons=[
                    RichMessageButton(
                        text="📝 Edit",
                        callback_data="memory_edit",
                        style="primary",
                    ),
                    RichMessageButton(
                        text="🗑️ Remove",
                        callback_data="memory_forget",
                        style="danger",
                    ),
                ],
                align="center",
            ),

            InputRichBlockButtons(
                buttons=[
                    RichMessageButton(
                        text="🫯 Clear All",
                        callback_data="memory_forget_all",
                        style="danger",
                    ),
                ],
                align="center",
            ),

            InputRichBlockButtons(
                buttons=[
                    RichMessageButton(
                        text="↩️ Back",
                        callback_data="memory_back",
                    ),
                    RichMessageButton(
                        text="❌ Close",
                        callback_data="memory_close",
                        style="danger",
                    ),
                ],
                align="center",
            ),
        ])

    # --------------------------------------
    # Confirm clear all
    # --------------------------------------

    elif menu_type == "confirm_forget_all":

        blocks.extend([

            InputRichBlockButtons(
                buttons=[
                    RichMessageButton(
                        text="⚠️ Yes, Clear Everything",
                        callback_data="memory_confirm_forget_all",
                        style="danger",
                    ),
                ],
                align="center",
            ),

            InputRichBlockButtons(
                buttons=[
                    RichMessageButton(
                        text="✖️ Cancel",
                        callback_data="memory_back",
                    ),
                    RichMessageButton(
                        text="❌ Close",
                        callback_data="memory_close",
                        style="danger",
                    ),
                ],
                align="center",
            ),
        ])

    # --------------------------------------
    # Add/edit/delete state
    # --------------------------------------

    else:

        blocks.append(
            InputRichBlockButtons(
                buttons=[
                    RichMessageButton(
                        text="↩️ Back",
                        callback_data="memory_back",
                    ),
                    RichMessageButton(
                        text="❌ Close",
                        callback_data="memory_close",
                        style="danger",
                    ),
                ],
                align="center",
            )
        )

    return InputRichMessage(
        blocks=blocks
    )


# ==========================================
# Outbound Memory Menu
# ==========================================

async def send_memory_menu(
    chat_id: int,
    user_id: int,
    text: str,
    menu_type: str = "main",
    source_ephemeral_id: int | None = None,
    memories: list[str] | None = None,
) -> Message:

    is_group = (
        chat_id != user_id
    )

    rich_message = get_memory_rich_message(
        text,
        menu_type,
        memories=memories,
    )

    # --------------------------------------
    # Group / supergroup
    # --------------------------------------

    if is_group:

        if source_ephemeral_id is None:

            raise RuntimeError(
                "Cannot reply to a group memory "
                "command without ephemeral_message_id."
            )

        # IMPORTANT:
        #
        # A reply to an ephemeral message MUST
        # itself be ephemeral.
        #
        # Telegram gives us only a short window
        # to reply to the incoming ephemeral
        # command, so this call must happen
        # immediately from the command handler.
        message = await bot.send_rich_message(
            chat_id=chat_id,
            rich_message=rich_message,
            reply_parameters=ReplyParameters(
                ephemeral_message_id=source_ephemeral_id,
            ),
            ephemeral_message_parameters=EphemeralMessageParameters(
                receiver_user_id=user_id,
            ),
        )

        ephemeral_id = getattr(
            message,
            "ephemeral_message_id",
            None,
        )

        if ephemeral_id is None:

            raise RuntimeError(
                "Telegram did not return "
                "ephemeral_message_id."
            )

        await register_menu_identity(
            chat_id,
            user_id,
            ephemeral_id,
        )

        schedule_menu_expiry(
            chat_id,
            user_id,
            ephemeral_id,
        )

        return message

    # --------------------------------------
    # Private chat
    # --------------------------------------

    message = await bot.send_rich_message(
        chat_id=chat_id,
        rich_message=rich_message,
    )

    await register_menu_identity(
        chat_id,
        user_id,
        message.message_id,
    )

    schedule_menu_expiry(
        chat_id,
        user_id,
        message.message_id,
    )

    return message


# ==========================================
# Edit Memory Menu
# ==========================================

async def edit_memory_menu(
    callback: CallbackQuery,
    text: str,
    menu_type: str = "main",
    memories: list[str] | None = None,
) -> None:

    message = callback.message

    if not message:
        return

    chat_id = message.chat.id
    user_id = callback.from_user.id

    is_group = (
        message.chat.type
        in {
            "group",
            "supergroup",
        }
    )

    rich_message = get_memory_rich_message(
        text,
        menu_type,
        memories=memories,
    )

    # --------------------------------------
    # Ephemeral group message
    # --------------------------------------

    if is_group:

        ephemeral_id = getattr(
            message,
            "ephemeral_message_id",
            None,
        )

        if ephemeral_id is None:

            await callback.answer(
                "This private memory menu is unavailable.",
                show_alert=True,
            )

            return

        current_id = await get_menu_identity(
            chat_id,
            user_id,
        )

        if (
            current_id is None
            or current_id != ephemeral_id
        ):

            await callback.answer(
                "This memory menu is no longer active.",
                show_alert=True,
            )

            return

        await bot.edit_ephemeral_message_text(
            chat_id=chat_id,
            receiver_user_id=user_id,
            ephemeral_message_id=ephemeral_id,
            rich_message=rich_message,
        )

        # Refresh our identity TTL.
        await register_menu_identity(
            chat_id,
            user_id,
            ephemeral_id,
        )

        # Refresh our local cleanup timer.
        schedule_menu_expiry(
            chat_id,
            user_id,
            ephemeral_id,
        )

        return

    # --------------------------------------
    # Private chat message
    # --------------------------------------

    current_id = await get_menu_identity(
        chat_id,
        user_id,
    )

    if (
        current_id is None
        or current_id != message.message_id
    ):

        raise RuntimeError(
            "Context mapping mismatch in personal chat."
        )

    await bot.edit_message_text(
        chat_id=chat_id,
        message_id=message.message_id,
        rich_message=rich_message,
    )

    await register_menu_identity(
        chat_id,
        user_id,
        message.message_id,
    )

    schedule_menu_expiry(
        chat_id,
        user_id,
        message.message_id,
    )


# ==========================================
# Callback authorization
# ==========================================

async def authorize_memory_callback(
    callback: CallbackQuery,
) -> bool:

    message = callback.message

    if not message:

        await callback.answer(
            "This memory menu is no longer available.",
            show_alert=True,
        )

        return False

    user_id = callback.from_user.id
    chat_id = message.chat.id

    is_group = (
        message.chat.type
        in {
            "group",
            "supergroup",
        }
    )

    # --------------------------------------
    # Group / supergroup ephemeral menu
    # --------------------------------------

    if is_group:

        ephemeral_id = getattr(
            message,
            "ephemeral_message_id",
            None,
        )

        if ephemeral_id is None:

            await callback.answer(
                "This private memory menu is invalid.",
                show_alert=True,
            )

            return False

        receiver_user = getattr(
            message,
            "receiver_user",
            None,
        )

        receiver_id = getattr(
            receiver_user,
            "id",
            None,
        )

        if (
            receiver_id is not None
            and receiver_id != user_id
        ):

            await callback.answer(
                "This memory menu belongs to another user.",
                show_alert=True,
            )

            return False

        stored_id = await get_menu_identity(
            chat_id,
            user_id,
        )

        if (
            stored_id is None
            or stored_id != ephemeral_id
        ):

            await callback.answer(
                "This memory menu is no longer active.",
                show_alert=True,
            )

            return False

        return True

    # --------------------------------------
    # Private chat
    # --------------------------------------

    current_id = await get_menu_identity(
        chat_id,
        user_id,
    )

    if current_id is None:

        await callback.answer(
            "This memory menu is no longer active.",
            show_alert=True,
        )

        return False

    if message.message_id != current_id:

        await callback.answer(
            "This memory menu is no longer active.",
            show_alert=True,
        )

        return False

    return True


# ==========================================
# Close menu
# ==========================================

async def close_menu(
    callback: CallbackQuery,
) -> None:

    message = callback.message

    if not message:
        return

    chat_id = message.chat.id
    user_id = callback.from_user.id

    is_group = (
        message.chat.type
        in {
            "group",
            "supergroup",
        }
    )

    await clear_interaction(
        chat_id,
        user_id,
    )

    try:

        if is_group:

            ephemeral_id = getattr(
                message,
                "ephemeral_message_id",
                None,
            )

            if ephemeral_id is not None:

                await bot.delete_ephemeral_message(
                    chat_id=chat_id,
                    receiver_user_id=user_id,
                    ephemeral_message_id=ephemeral_id,
                )

        else:

            await bot.delete_message(
                chat_id=chat_id,
                message_id=message.message_id,
            )

    except Exception as e:

        print(
            f"Menu close error: {e}"
        )

    finally:

        await clear_menu_identity(
            chat_id,
            user_id,
        )


# ==========================================
# /memories Command
# ==========================================

@router.message(
    Command("memories")
)
async def handle_memories(
    message: Message,
):

    user_id = message.from_user.id
    chat_id = message.chat.id

    incoming_ephemeral_id = getattr(
        message,
        "ephemeral_message_id",
        None,
    )

    print(
        "[/memories] "
        f"chat_type={message.chat.type!r} "
        f"user_id={user_id} "
        f"message_id={message.message_id} "
        f"ephemeral_message_id={incoming_ephemeral_id}"
    )

    await clear_interaction(
        chat_id,
        user_id,
    )

    await clear_menu_identity(
        chat_id,
        user_id,
    )

    display_name = get_user_display_name(
        message.from_user
    )

    safe_name = html.escape(
        display_name
    )

    text = (
        "<b>Memory Center</b>\n\n"
        f"Welcome, {safe_name}.\n\n"
        "Keep track of the details and "
        "instructions you've asked Sen to remember. "
        "Changes here affect how Sen responds to you."
    )

    # --------------------------------------
    # GROUP / SUPERGROUP
    # --------------------------------------

    if message.chat.type in {
        "group",
        "supergroup",
    }:

        # ----------------------------------
        # This is the important case.
        #
        # /memories is declared with
        # is_ephemeral=True in BotCommand.
        #
        # Therefore Telegram should deliver
        # the incoming command with an
        # ephemeral_message_id.
        # ----------------------------------

        if incoming_ephemeral_id is None:

            print(
                "[/memories] WARNING: command arrived "
                "without ephemeral_message_id."
            )

            # DO NOT send a public fallback.
            #
            # Doing so would defeat the entire
            # purpose of the ephemeral command.
            #
            # This can happen if someone manually
            # constructs/sends the command through
            # an API/client path that does not invoke
            # Telegram's ephemeral-command mechanism.
            return

        try:

            await send_memory_menu(
                chat_id=chat_id,
                user_id=user_id,
                text=text,
                menu_type="main",
                source_ephemeral_id=incoming_ephemeral_id,
            )

            print(
                "[/memories] Private ephemeral "
                "memory menu created successfully."
            )

        except Exception as e:

            print(
                f"Memory menu send error: {e}"
            )

        return

    # --------------------------------------
    # PRIVATE CHAT
    # --------------------------------------

    try:

        await send_memory_menu(
            chat_id=chat_id,
            user_id=user_id,
            text=text,
            menu_type="main",
        )

    except Exception as e:

        print(
            f"Private memory menu error: {e}"
        )

        try:

            await message.answer(
                "I couldn't open the memory configuration window."
            )

        except Exception:
            pass


# ==========================================
# Memory View
# ==========================================

@router.callback_query(
    F.data == "memory_view"
)
async def handle_memory_view(
    callback: CallbackQuery,
):

    if not await authorize_memory_callback(
        callback
    ):
        return

    await callback.answer()

    user_id_str = str(
        callback.from_user.id
    )

    memories = await get_memories(
        user_id_str
    )

    body = (
        "<b>What Sen Remembers</b>\n\n"
        "These are the saved instructions and details "
        "currently available to Sen."
    )

    if not memories:

        body += (
            "\n\nNothing has been saved yet."
        )

    try:

        await edit_memory_menu(
            callback,
            body,
            menu_type="view",
            memories=memories,
        )

    except Exception as e:

        print(
            f"Memory view navigation error: {e}"
        )


# ==========================================
# Add Memory
# ==========================================

@router.callback_query(
    F.data == "memory_add"
)
async def handle_memory_add(
    callback: CallbackQuery,
):

    if not await authorize_memory_callback(
        callback
    ):
        return

    chat_id = callback.message.chat.id

    await set_interaction(
        chat_id,
        callback.from_user.id,
        "add",
    )

    await callback.answer()

    body = (
        "<b>Add a Memory</b>\n\n"
        "Tell Sen what you'd like to keep in mind "
        "for future conversations.\n\n"
        "You can add several items at once by separating "
        "them with <code>,,</code>."
    )

    try:

        await edit_memory_menu(
            callback,
            body,
            menu_type="back_close",
        )

    except Exception as e:

        print(
            f"Memory addition transition error: {e}"
        )


# ==========================================
# Edit Memory
# ==========================================

@router.callback_query(
    F.data == "memory_edit"
)
async def handle_memory_edit(
    callback: CallbackQuery,
):

    if not await authorize_memory_callback(
        callback
    ):
        return

    user_id_str = str(
        callback.from_user.id
    )

    memories = await get_memories(
        user_id_str
    )

    chat_id = callback.message.chat.id

    await set_interaction(
        chat_id,
        callback.from_user.id,
        "edit_number",
    )

    await callback.answer()

    if not memories:

        body = (
            "<b>Edit a Memory</b>\n\n"
            "There aren't any saved memories to edit yet."
        )

    else:

        rows = [
            "<b>Edit a Memory</b>\n\n"
            "Send the memory number followed by "
            "the replacement text.\n"
        ]

        for i, memory in enumerate(
            memories,
            1,
        ):

            rows.append(
                f"{i}. {html.escape(memory)}"
            )

        rows.append(
            "\nExample: "
            "<code>2 My new instruction</code>"
        )

        body = "\n".join(rows)

    try:

        await edit_memory_menu(
            callback,
            body,
            menu_type="back_close",
        )

    except Exception as e:

        print(
            f"Memory editor menu error: {e}"
        )


# ==========================================
# Forget Memory
# ==========================================

@router.callback_query(
    F.data == "memory_forget"
)
async def handle_memory_forget(
    callback: CallbackQuery,
):

    if not await authorize_memory_callback(
        callback
    ):
        return

    user_id_str = str(
        callback.from_user.id
    )

    memories = await get_memories(
        user_id_str
    )

    chat_id = callback.message.chat.id

    await set_interaction(
        chat_id,
        callback.from_user.id,
        "forget",
    )

    await callback.answer()

    if not memories:

        body = (
            "<b>Remove Memories</b>\n\n"
            "There's nothing saved here to remove."
        )

    else:

        rows = [
            "<b>Remove Memories</b>\n\n"
            "Send one or more memory numbers, separated "
            "with <code>,,</code>, to remove them.\n"
        ]

        for i, memory in enumerate(
            memories,
            1,
        ):

            rows.append(
                f"{i}. {html.escape(memory)}"
            )

        rows.append(
            "\nExample: <code>1,, 3</code>"
        )

        body = "\n".join(rows)

    try:

        await edit_memory_menu(
            callback,
            body,
            menu_type="back_close",
        )

    except Exception as e:

        print(
            f"Memory wipe menu error: {e}"
        )


# ==========================================
# Forget All
# ==========================================

@router.callback_query(
    F.data == "memory_forget_all"
)
async def handle_memory_forget_all(
    callback: CallbackQuery,
):

    if not await authorize_memory_callback(
        callback
    ):
        return

    chat_id = callback.message.chat.id

    await clear_interaction(
        chat_id,
        callback.from_user.id,
    )

    await callback.answer()

    body = (
        "<b>Clear All Memories?</b>\n\n"
        "This will remove every saved memory for your "
        "account and clear the conversation context "
        "associated with this chat.\n\n"
        "<b>This cannot be undone.</b>"
    )

    try:

        await edit_memory_menu(
            callback,
            body,
            menu_type="confirm_forget_all",
        )

    except Exception as e:

        print(
            f"Confirm all menu error: {e}"
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

    if not await authorize_memory_callback(
        callback
    ):
        return

    user_id = callback.from_user.id
    user_id_str = str(user_id)

    chat_id = (
        callback.message.chat.id
        if callback.message
        else 0
    )

    await redis_client.delete(
        f"memory_list:{user_id_str}",
        f"chat_history:{chat_id}:{user_id_str}",
        interaction_key(
            chat_id,
            user_id,
        ),
    )

    await callback.answer(
        "All saved memory has been cleared.",
        show_alert=True,
    )

    body = (
        "<b>Memory Cleared</b>\n\n"
        "Your saved memories and local conversation "
        "context have been removed."
    )

    try:

        await edit_memory_menu(
            callback,
            body,
            menu_type="back_close",
        )

    except Exception as e:

        print(
            f"Post wipe layout error: {e}"
        )


# ==========================================
# Back
# ==========================================

@router.callback_query(
    F.data == "memory_back"
)
async def handle_memory_back(
    callback: CallbackQuery,
):

    if not await authorize_memory_callback(
        callback
    ):
        return

    chat_id = callback.message.chat.id

    await clear_interaction(
        chat_id,
        callback.from_user.id,
    )

    await callback.answer()

    display_name = get_user_display_name(
        callback.from_user
    )

    safe_name = html.escape(
        display_name
    )

    body = (
        "<b>Memory Center</b>\n\n"
        f"Welcome, {safe_name}.\n\n"
        "Keep track of the details and "
        "instructions you've asked Sen to remember. "
        "Changes here affect how Sen responds to you."
    )

    try:

        await edit_memory_menu(
            callback,
            body,
            menu_type="main",
        )

    except Exception as e:

        print(
            f"Back button routing error: {e}"
        )


# ==========================================
# Close
# ==========================================

@router.callback_query(
    F.data == "memory_close"
)
async def handle_memory_close(
    callback: CallbackQuery,
):

    if not await authorize_memory_callback(
        callback
    ):
        return

    await callback.answer(
        "Closed"
    )

    await close_menu(
        callback
    )


# ==========================================
# Direct Memory Processing
# ==========================================

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
    chat_id = message.chat.id

    # --------------------------------------
    # ADD
    # --------------------------------------

    if action == "add":

        parts = [
            p.strip()[:200]
            for p in message.text.split(",,")
            if p.strip()
        ]

        for part in parts[:10]:

            try:

                if await redis_client.lpos(
                    f"memory_list:{user_id_str}",
                    part,
                ) is None:

                    await redis_client.rpush(
                        f"memory_list:{user_id_str}",
                        part,
                    )

            except Exception as e:

                print(
                    f"Memory listing error: {e}"
                )

        await redis_client.ltrim(
            f"memory_list:{user_id_str}",
            -25,
            -1,
        )

        await clear_interaction(
            chat_id,
            user_id,
        )

        try:

            await message.delete()

        except Exception:
            pass

        return True

    # --------------------------------------
    # EDIT
    # --------------------------------------

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
            chat_id,
            user_id,
        )

        try:

            await message.delete()

        except Exception:
            pass

        return True

    # --------------------------------------
    # FORGET
    # --------------------------------------

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
            chat_id,
            user_id,
        )

        try:

            await message.delete()

        except Exception:
            pass

        return True

    return False


# ==========================================
# /del Command
# ==========================================
#
# IMPORTANT:
#
# /del MUST remain in the Telegram command
# list with is_ephemeral=True.
#
# That causes Telegram to deliver the command
# privately in group chats instead of exposing
# it to the group timeline.
#
# The Python handler still performs the OWNER_ID
# authorization.
#
# Removing /del from BotFather / setMyCommands
# means Telegram no longer treats it as an
# ephemeral command. That is why removing it from
# the command list broke the behavior.
# ==========================================

@router.message(
    Command("del")
)
async def handle_delete(
    message: Message,
):

    if not message.from_user:

        return

    # Owner-only.
    if message.from_user.id != OWNER_ID:

        return

    # --------------------------------------
    # Delete the replied-to bot message.
    # --------------------------------------

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
                    chat_id=message.chat.id,
                    message_id=(
                        message
                        .reply_to_message
                        .message_id
                    ),
                )

            except Exception as e:

                print(
                    f"/del target deletion error: {e}"
                )

    # --------------------------------------
    # DO NOT send anything publicly.
    #
    # If the incoming command is ephemeral,
    # Telegram already keeps it private.
    #
    # We do not need to answer it.
    # --------------------------------------

    incoming_ephemeral_id = getattr(
        message,
        "ephemeral_message_id",
        None,
    )

    print(
        "[/del] "
        f"user_id={message.from_user.id} "
        f"chat_id={message.chat.id} "
        f"ephemeral_message_id="
        f"{incoming_ephemeral_id}"
    )

    # There is no public command response to
    # clean up. The ephemeral command itself is
    # managed by Telegram.
    return


# ==========================================
# Search Pipeline
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
                "SearXNG Pipeline HTTP "
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
            if x.get("title")
            or x.get("content")
        )

    except Exception as e:

        print(
            f"Web contextual search failure: {e}"
        )

        return ""


# ==========================================
# Audio Track System
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
            f"Audio media delivery fault "
            f"record ({key}): {e}"
        )


# ==========================================
# Bot API Community Interceptors
# ==========================================

@router.message(
    F.community_chat_added
)
async def handle_community_added(
    message: Message,
):

    print(
        "Community binding topology registered: "
        f"{message.chat.id}"
    )

    return


@router.message(
    F.community_chat_removed
)
async def handle_community_removed(
    message: Message,
):

    print(
        "Community dropping context safely absorbed: "
        f"{message.chat.id}"
    )

    return


# ==========================================
# Primary AI Core
# ==========================================

@router.message(
    F.text | F.caption | F.voice
)
async def handle_conversation(
    message: Message,
):

    if message.audio is not None:

        return

    # --------------------------------------
    # Memory interaction state
    # --------------------------------------

    action = await get_interaction(
        message.chat.id,
        message.from_user.id,
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

    # --------------------------------------
    # Audio trigger: Sen
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

    # --------------------------------------
    # Audio trigger: Magic
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
                message.chat.type == "private",
            )
        )

    # --------------------------------------
    # Bot addressing
    # --------------------------------------

    bot_username = (
        f"@{BOT_INFO.username}"
        if (
            BOT_INFO
            and BOT_INFO.username
        )
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

    if message.voice is not None:

        if not (
            is_tagged
            or is_reply_to_bot
        ):

            return

    else:

        if not (
            is_tagged
            or is_reply_to_bot
            or is_private
        ):

            return

    user_id_str = str(
        message.from_user.id
    )

    chat_id = message.chat.id
    msg_id = message.message_id

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

    # --------------------------------------
    # Reply context
    # --------------------------------------

    replied_context = (
        (
            message.reply_to_message.text
            or message.reply_to_message.caption
            or ""
        )
        if message.reply_to_message
        else ""
    )

    # --------------------------------------
    # Voice processing
    # --------------------------------------

    audio_bytes = None
    audio_mime = "audio/ogg"

    if message.voice is not None:

        voice_obj = message.voice

        file_info = await bot.get_file(
            voice_obj.file_id
        )

        stream = await bot.download_file(
            file_info.file_path
        )

        if stream:

            audio_bytes = stream.read()

        if getattr(
            voice_obj,
            "mime_type",
            None,
        ):

            audio_mime = (
                voice_obj.mime_type
            )

    # --------------------------------------
    # Reply-only prompt
    # --------------------------------------

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

        # ----------------------------------
        # Saved memories
        # ----------------------------------

        saved_facts = await get_memories(
            user_id_str
        )

        # ----------------------------------
        # Chat history
        # ----------------------------------

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
            x.decode("utf-8")
            if isinstance(x, bytes)
            else str(x)
            for x in raw_hist
        ]

        # ----------------------------------
        # Search detection
        # ----------------------------------

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

        # ----------------------------------
        # Web search
        # ----------------------------------

        search_context = ""

        if (
            explicit_search
            and search_query
        ):

            search_context = await free_web_search(
                search_query
            )

        # ----------------------------------
        # Build context
        # ----------------------------------

        context = []

        if replied_context:

            context.append(
                "Message User is Replying To:\n"
                f'"{replied_context}"'
            )

        if chat_history:

            context.append(
                "Recent Conversation Context:\n"
                + "\n".join(
                    chat_history
                )
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

        # ----------------------------------
        # Date
        # ----------------------------------

        today = datetime.now(
            timezone.utc
        ).strftime(
            "%A, %B %d, %Y"
        )

        # ----------------------------------
        # Gemini instructions
        # ----------------------------------

        instructions = (
            f"Today's date is {today}.\n"
            "Never use standard AI pleasantries.\n"
            "Keep casual replies brief, but expand "
            "when asked for detail.\n"
            "If the user changes subject, immediately "
            "follow the new subject.\n"
            "If joking or sarcastic, match the energy.\n"
            "If you do not know, say exactly: "
            "'I don't have enough details to answer "
            "that accurately' without guessing.\n"
            "Do not assume personal details unless "
            "explicitly present in the memory list.\n\n"
            "OUTPUT FORMAT: Use Telegram Rich HTML. "
            "Use <h1>-<h6>, <p>, <b>, <i>, <u>, "
            "<s>, <code>, <pre>, <table>, <details>, "
            '<a href="URL">text</a>.\n'
            "Do not use Markdown asterisks for "
            "formatting. Do not use Markdown pipe "
            "tables. Return clean HTML suitable for "
            "Telegram."
        )

        if search_context:

            instructions += (
                "\nWhen using Web Search Context, "
                "state the information directly. "
                "If links are requested, use HTML. "
                "Otherwise, do not include URLs."
            )

        if chat_history:

            instructions += (
                "\nUse Recent Conversation Context "
                "for continuity, but do not repeat it."
            )

        if saved_facts:

            instructions += (
                "\n\nUser memory directives:\n"
                + "\n".join(
                    f"- {x}"
                    for x in saved_facts
                )
            )

        # ----------------------------------
        # Gemini safety
        # ----------------------------------

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

        # ----------------------------------
        # Gemini request
        # ----------------------------------

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

        # ----------------------------------
        # Response cleanup
        # ----------------------------------

        response_text = (
            response.text
            or "I didn't receive a response."
        )

        response_text = (
            response_text
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

        # ----------------------------------
        # Deliver response
        # ----------------------------------

        if is_private:

            await bot.send_message(
                chat_id=chat_id,
                text=response_text,
                parse_mode="HTML",
            )

        else:

            await bot.send_message(
                chat_id=chat_id,
                text=response_text,
                parse_mode="HTML",
                reply_parameters=ReplyParameters(
                    message_id=msg_id
                ),
            )

        # ----------------------------------
        # Save history
        # ----------------------------------

        clean_history = re.sub(
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
            f"Bot: {clean_history}",
        )

        await redis_client.ltrim(
            history_key,
            -10,
            -1,
        )

    except Exception as e:

        print(
            f"Gemini AI processing error: {e}"
        )

        error = (
            "Whoa, I'm getting a little overwhelmed! "
            "Let me catch my breath."
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


# ==========================================
# Health Server
# ==========================================

async def health_check(
    request: web.Request,
) -> web.Response:

    return web.json_response(
        {
            "status": "ok",
            "bot": (
                BOT_INFO.username
                if BOT_INFO
                else None
            ),
        }
    )


# ==========================================
# Telegram Commands
# ==========================================

async def configure_commands() -> None:

    # ======================================
    # GROUP COMMANDS
    # ======================================
    #
    # DO NOT REMOVE /del.
    #
    # The is_ephemeral=True property is what
    # tells Telegram that this command should
    # be delivered privately to the user.
    #
    # Both regular members and administrators
    # receive this same group command scope.
    # ======================================

    group_commands = [

        BotCommand(
            command="memories",
            description="Open your private memory menu",
            is_ephemeral=True,
        ),

        BotCommand(
            command="del",
            description="Delete a bot message",
            is_ephemeral=True,
        ),
    ]

    # ======================================
    # PRIVATE COMMANDS
    # ======================================

    private_commands = [

        BotCommand(
            command="memories",
            description="Manage your instructed memories",
        ),

        BotCommand(
            command="del",
            description="Delete a bot message",
        ),
    ]

    # ======================================
    # IMPORTANT:
    #
    # Remove any administrator-specific
    # command override.
    #
    # Telegram's command resolution will
    # therefore fall through to
    # AllGroupChats for administrators.
    # ======================================

    try:

        await bot.delete_my_commands(
            scope=BotCommandScopeAllChatAdministrators()
        )

        print(
            "Cleared administrator-specific "
            "command scope."
        )

    except Exception as e:

        print(
            "Could not clear administrator command "
            f"scope: {e}"
        )

    # ======================================
    # GROUP COMMANDS
    # ======================================

    await bot.set_my_commands(
        group_commands,
        scope=BotCommandScopeAllGroupChats(),
    )

    print(
        "Configured group commands:"
        " /memories=ephemeral"
        " /del=ephemeral"
    )

    # ======================================
    # PRIVATE COMMANDS
    # ======================================

    await bot.set_my_commands(
        private_commands,
        scope=BotCommandScopeAllPrivateChats(),
    )

    print(
        "Configured private commands."
    )

    # ======================================
    # VERIFY
    # ======================================

    try:

        stored_group_commands = (
            await bot.get_my_commands(
                scope=BotCommandScopeAllGroupChats()
            )
        )

        print(
            "Telegram group commands: "
            + str(
                [
                    (
                        command.command,
                        getattr(
                            command,
                            "is_ephemeral",
                            None,
                        ),
                    )
                    for command
                    in stored_group_commands
                ]
            )
        )

        stored_admin_commands = (
            await bot.get_my_commands(
                scope=BotCommandScopeAllChatAdministrators()
            )
        )

        print(
            "Telegram administrator-specific commands: "
            + str(
                [
                    (
                        command.command,
                        getattr(
                            command,
                            "is_ephemeral",
                            None,
                        ),
                    )
                    for command
                    in stored_admin_commands
                ]
            )
        )

        stored_private_commands = (
            await bot.get_my_commands(
                scope=BotCommandScopeAllPrivateChats()
            )
        )

        print(
            "Telegram private commands: "
            + str(
                [
                    (
                        command.command,
                        getattr(
                            command,
                            "is_ephemeral",
                            None,
                        ),
                    )
                    for command
                    in stored_private_commands
                ]
            )
        )

    except Exception as e:

        print(
            "Could not verify Telegram commands: "
            f"{e}"
        )


# ==========================================
# Main
# ==========================================

async def main():

    global BOT_INFO

    BOT_INFO = await bot.get_me()

    print(
        "Logged in successfully as "
        f"@{BOT_INFO.username}"
    )

    await configure_commands()

    # --------------------------------------
    # Health server
    # --------------------------------------

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
        "Operational check dashboard running "
        f"on port {port}"
    )

    # --------------------------------------
    # Clear webhook
    # --------------------------------------

    try:

        print(
            "Clearing any existing webhook "
            "or active server blocks..."
        )

        await bot.delete_webhook(
            drop_pending_updates=True
        )

    except Exception as e:

        print(
            f"Non-critical webhook clearance notice: {e}"
        )

    # --------------------------------------
    # Polling
    # --------------------------------------

    try:

        await dp.start_polling(
            bot
        )

    finally:

        await bot.session.close()

        await redis_client.aclose()

        await runner.cleanup()

        print(
            "Bot execution stack dropped cleanly."
        )


# ==========================================
# Entrypoint
# ==========================================

if __name__ == "__main__":

    asyncio.run(
        main()
    )
