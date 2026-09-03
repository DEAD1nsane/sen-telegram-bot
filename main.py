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
# Configuration
# ==========================================
redis_url = os.environ.get("REDIS_URL", "")
if not redis_url:
    host = os.environ.get("REDISHOST", "localhost")
    port = os.environ.get("REDISPORT", "6379")
    password = os.environ.get("REDISPASSWORD", "")
    redis_url = (
        f"redis://default:{password}@{host}:{port}"
        if password else f"redis://{host}:{port}"
    )
if "upstash" in redis_url.lower() and redis_url.startswith("redis://"):
    redis_url = redis_url.replace("redis://", "rediss://", 1)

redis_client = redis.from_url(
    redis_url,
    ssl_cert_reqs=None if redis_url.startswith("rediss://") else "required",
)

API_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or ""
if not API_TOKEN:
    raise ValueError("CRITICAL CONFIGURATION ERROR: 'BOT_TOKEN' missing. Set BOT_TOKEN in Railway Variables.")

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
    default=DefaultBotProperties(parse_mode="HTML"),
)
dp = Dispatcher()
router = Router()
dp.include_router(router)
BOT_INFO = None

INTERACTION_TTL = 300
MENU_TTL = 30

# ==========================================
# Redis state
# ==========================================
def interaction_key(chat_id: int, user_id: int) -> str:
    return f"memory_interaction:{chat_id}:{user_id}"

async def set_interaction(chat_id: int, user_id: int, action: str) -> None:
    await redis_client.set(interaction_key(chat_id, user_id), action, ex=INTERACTION_TTL)

async def get_interaction(chat_id: int, user_id: int) -> str | None:
    value = await redis_client.get(interaction_key(chat_id, user_id))
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value

async def clear_interaction(chat_id: int, user_id: int) -> None:
    await redis_client.delete(interaction_key(chat_id, user_id))

def menu_identity_key(chat_id: int, user_id: int) -> str:
    return f"memory_menu_identity:{chat_id}:{user_id}"

async def register_menu_identity(chat_id: int, user_id: int, message_id: int) -> None:
    await redis_client.set(menu_identity_key(chat_id, user_id), str(message_id), ex=MENU_TTL + 5)

async def get_menu_identity(chat_id: int, user_id: int) -> int | None:
    value = await redis_client.get(menu_identity_key(chat_id, user_id))
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

async def clear_menu_identity(chat_id: int, user_id: int) -> None:
    await redis_client.delete(menu_identity_key(chat_id, user_id))

async def expire_memory_menu(chat_id: int, user_id: int, menu_id: int) -> None:
    try:
        await asyncio.sleep(MENU_TTL)
        if await get_menu_identity(chat_id, user_id) != menu_id:
            return
        try:
            if chat_id != user_id:
                await bot.delete_ephemeral_message(
                    chat_id=chat_id,
                    receiver_user_id=user_id,
                    ephemeral_message_id=menu_id,
                )
            else:
                await bot.delete_message(chat_id=chat_id, message_id=menu_id)
        except Exception as e:
            print(f"Automatic memory menu expiry error: {e}")
        finally:
            await clear_menu_identity(chat_id, user_id)
            await clear_interaction(chat_id, user_id)
    except asyncio.CancelledError:
        return
    except Exception as e:
        print(f"Memory menu expiry task error: {e}")

def schedule_menu_expiry(chat_id: int, user_id: int, menu_id: int) -> None:
    asyncio.create_task(expire_memory_menu(chat_id, user_id, menu_id))

async def get_memories(user_id_str: str) -> list[str]:
    try:
        raw = await redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
        return [x.decode("utf-8") if isinstance(x, bytes) else str(x) for x in raw]
    except Exception as e:
        print(f"Memory read error: {e}")
        return []

# ==========================================
# Formatting helpers
# ==========================================
def get_user_display_name(user) -> str:
    if not user:
        return "User"
    first = (getattr(user, "first_name", None) or "").strip()
    last = (getattr(user, "last_name", None) or "").strip()
    name = " ".join(x for x in (first, last) if x).strip()
    if name:
        return name
    username = (getattr(user, "username", None) or "").strip()
    return username or "User"

def rich_text_from_markup(text: str):
    pattern = re.compile(
        r"(<b>.*?</b>|<strong>.*?</strong>|<i>.*?</i>|<em>.*?</em>|"
        r"<u>.*?</u>|<ins>.*?</ins>|<s>.*?</s>|<strike>.*?</strike>|"
        r"<del>.*?</del>|<code>.*?</code>)",
        re.IGNORECASE | re.DOTALL,
    )
    parts = []
    position = 0
    for match in pattern.finditer(text):
        if match.start() > position:
            plain = text[position:match.start()]
            if plain:
                parts.append(html.unescape(plain))
        token = match.group(0)
        lowered = token.lower()
        if lowered.startswith(("<b>", "<strong>")):
            inner = re.sub(r"^<(?:b|strong)>|</(?:b|strong)>$", "", token, flags=re.I | re.S)
            parts.append(RichTextBold(text=html.unescape(inner)))
        elif lowered.startswith(("<i>", "<em>")):
            inner = re.sub(r"^<(?:i|em)>|</(?:i|em)>$", "", token, flags=re.I | re.S)
            parts.append(RichTextItalic(text=html.unescape(inner)))
        elif lowered.startswith(("<u>", "<ins>")):
            inner = re.sub(r"^<(?:u|ins)>|</(?:u|ins)>$", "", token, flags=re.I | re.S)
            parts.append(RichTextUnderline(text=html.unescape(inner)))
        elif lowered.startswith(("<s>", "<strike>", "<del>")):
            inner = re.sub(r"^<(?:s|strike|del)>|</(?:s|strike|del)>$", "", token, flags=re.I | re.S)
            parts.append(RichTextStrikethrough(text=html.unescape(inner)))
        elif lowered.startswith("<code>"):
            inner = re.sub(r"^<code>|</code>$", "", token, flags=re.I | re.S)
            parts.append(RichTextCode(text=html.unescape(inner)))
        position = match.end()
    if position < len(text):
        parts.append(html.unescape(text[position:]))
    parts = [p for p in parts if p != ""]
    if not parts:
        return ""
    return parts[0] if len(parts) == 1 else parts

def get_memory_rich_message(text: str, menu_type: str, memories: list[str] | None = None) -> InputRichMessage:
    blocks = []
    heading_match = re.match(r"^\s*<b>(.*?)</b>\s*(?:\n|$)", text, re.I | re.S)
    if heading_match:
        blocks.append(InputRichBlockSectionHeading(
            text=html.unescape(heading_match.group(1)), size=2
        ))
        remaining = text[heading_match.end():].strip()
        if remaining:
            blocks.append(InputRichBlockParagraph(text=rich_text_from_markup(remaining)))
    else:
        blocks.append(InputRichBlockParagraph(text=rich_text_from_markup(text)))

    if memories:
        blocks.append(InputRichBlockList(items=[
            InputRichBlockListItem(
                blocks=[InputRichBlockParagraph(text=html.escape(memory))],
                value=index,
                type="1",
            )
            for index, memory in enumerate(memories, 1)
        ]))

    if menu_type == "main":
        blocks.extend([
            InputRichBlockButtons(buttons=[RichMessageButton(text="🧠 View Memories", callback_data="memory_view", style="primary")], align="center"),
            InputRichBlockButtons(buttons=[RichMessageButton(text="➕ New Memory", callback_data="memory_add", style="success")], align="center"),
            InputRichBlockButtons(buttons=[RichMessageButton(text="❌ Close", callback_data="memory_close", style="danger")], align="center"),
        ])
    elif menu_type == "view":
        blocks.extend([
            InputRichBlockButtons(buttons=[
                RichMessageButton(text="📝 Edit", callback_data="memory_edit", style="primary"),
                RichMessageButton(text="🗑️ Remove", callback_data="memory_forget", style="danger"),
            ], align="center"),
            InputRichBlockButtons(buttons=[RichMessageButton(text="🫯 Clear All", callback_data="memory_forget_all", style="danger")], align="center"),
            InputRichBlockButtons(buttons=[
                RichMessageButton(text="↩️ Back", callback_data="memory_back"),
                RichMessageButton(text="❌ Close", callback_data="memory_close", style="danger"),
            ], align="center"),
        ])
    elif menu_type == "confirm_forget_all":
        blocks.extend([
            InputRichBlockButtons(buttons=[RichMessageButton(text="⚠️ Yes, Clear Everything", callback_data="memory_confirm_forget_all", style="danger")], align="center"),
            InputRichBlockButtons(buttons=[
                RichMessageButton(text="✖️ Cancel", callback_data="memory_back"),
                RichMessageButton(text="❌ Close", callback_data="memory_close", style="danger"),
            ], align="center"),
        ])
    else:
        blocks.append(InputRichBlockButtons(buttons=[
            RichMessageButton(text="↩️ Back", callback_data="memory_back"),
            RichMessageButton(text="❌ Close", callback_data="memory_close", style="danger"),
        ], align="center"))
    return InputRichMessage(blocks=blocks)

# ==========================================
# Memory menu delivery
# ==========================================
async def send_memory_menu(chat_id: int, user_id: int, text: str, menu_type: str = "main", source_ephemeral_id: int | None = None, memories: list[str] | None = None) -> Message:
    rich_message = get_memory_rich_message(text, menu_type, memories)
    if chat_id != user_id:
        if source_ephemeral_id is None:
            raise RuntimeError("Cannot reply to a group memory command without ephemeral_message_id.")
        message = await bot.send_rich_message(
            chat_id=chat_id,
            rich_message=rich_message,
            reply_parameters=ReplyParameters(ephemeral_message_id=source_ephemeral_id),
            ephemeral_message_parameters=EphemeralMessageParameters(receiver_user_id=user_id),
        )
        ephemeral_id = getattr(message, "ephemeral_message_id", None)
        if ephemeral_id is None:
            raise RuntimeError("Telegram did not return ephemeral_message_id.")
        await register_menu_identity(chat_id, user_id, ephemeral_id)
        schedule_menu_expiry(chat_id, user_id, ephemeral_id)
        return message
    message = await bot.send_rich_message(chat_id=chat_id, rich_message=rich_message)
    await register_menu_identity(chat_id, user_id, message.message_id)
    schedule_menu_expiry(chat_id, user_id, message.message_id)
    return message

async def edit_memory_menu(callback: CallbackQuery, text: str, menu_type: str = "main", memories: list[str] | None = None) -> None:
    message = callback.message
    if not message:
        return
    chat_id = message.chat.id
    user_id = callback.from_user.id
    rich_message = get_memory_rich_message(text, menu_type, memories)
    if message.chat.type in {"group", "supergroup"}:
        ephemeral_id = getattr(message, "ephemeral_message_id", None)
        if ephemeral_id is None:
            await callback.answer("This private memory menu is unavailable.", show_alert=True)
            return
        if await get_menu_identity(chat_id, user_id) != ephemeral_id:
            await callback.answer("This memory menu is no longer active.", show_alert=True)
            return
        await bot.edit_ephemeral_message_text(
            chat_id=chat_id,
            receiver_user_id=user_id,
            ephemeral_message_id=ephemeral_id,
            rich_message=rich_message,
        )
        await register_menu_identity(chat_id, user_id, ephemeral_id)
        schedule_menu_expiry(chat_id, user_id, ephemeral_id)
    else:
        if await get_menu_identity(chat_id, user_id) != message.message_id:
            raise RuntimeError("Context mapping mismatch in personal chat.")
        await bot.edit_message_text(chat_id=chat_id, message_id=message.message_id, rich_message=rich_message)
        await register_menu_identity(chat_id, user_id, message.message_id)
        schedule_menu_expiry(chat_id, user_id, message.message_id)

async def authorize_memory_callback(callback: CallbackQuery) -> bool:
    message = callback.message
    if not message:
        await callback.answer("This memory menu is no longer available.", show_alert=True)
        return False
    user_id = callback.from_user.id
    chat_id = message.chat.id
    if message.chat.type in {"group", "supergroup"}:
        ephemeral_id = getattr(message, "ephemeral_message_id", None)
        if ephemeral_id is None or await get_menu_identity(chat_id, user_id) != ephemeral_id:
            await callback.answer("This memory menu is no longer active.", show_alert=True)
            return False
        receiver_user = getattr(message, "receiver_user", None)
        receiver_id = getattr(receiver_user, "id", None)
        if receiver_id is not None and receiver_id != user_id:
            await callback.answer("This memory menu belongs to another user.", show_alert=True)
            return False
        return True
    if await get_menu_identity(chat_id, user_id) != message.message_id:
        await callback.answer("This memory menu is no longer active.", show_alert=True)
        return False
    return True

async def close_menu(callback: CallbackQuery) -> None:
    message = callback.message
    if not message:
        return
    chat_id = message.chat.id
    user_id = callback.from_user.id
    await clear_interaction(chat_id, user_id)
    try:
        if message.chat.type in {"group", "supergroup"}:
            ephemeral_id = getattr(message, "ephemeral_message_id", None)
            if ephemeral_id is not None:
                await bot.delete_ephemeral_message(chat_id=chat_id, receiver_user_id=user_id, ephemeral_message_id=ephemeral_id)
        else:
            await bot.delete_message(chat_id=chat_id, message_id=message.message_id)
    except Exception as e:
        print(f"Menu close error: {e}")
    finally:
        await clear_menu_identity(chat_id, user_id)

# ==========================================
# /memories
# ==========================================
@router.message(Command("memories"))
async def handle_memories(message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    incoming_ephemeral_id = getattr(message, "ephemeral_message_id", None)
    print(f"[/memories] chat_type={message.chat.type!r} user_id={user_id} message_id={message.message_id} ephemeral_message_id={incoming_ephemeral_id}")
    await clear_interaction(chat_id, user_id)
    await clear_menu_identity(chat_id, user_id)
    safe_name = html.escape(get_user_display_name(message.from_user))
    text = (
        f"<b>Memory Center</b>\n\nWelcome, {safe_name}.\n\n"
        "Keep track of the details and instructions you've asked Sen to remember. "
        "Changes here affect how Sen responds to you."
    )
    if message.chat.type in {"group", "supergroup"}:
        if incoming_ephemeral_id is None:
            print("[/memories] WARNING: command arrived without ephemeral_message_id.")
            return
        try:
            await send_memory_menu(chat_id, user_id, text, "main", incoming_ephemeral_id)
        except Exception as e:
            print(f"Memory menu send error: {e}")
        return
    try:
        await send_memory_menu(chat_id, user_id, text)
    except Exception as e:
        print(f"Private memory menu error: {e}")
        try:
            await message.answer("I couldn't open the memory configuration window.")
        except Exception:
            pass

@router.callback_query(F.data == "memory_view")
async def handle_memory_view(callback: CallbackQuery):
    if not await authorize_memory_callback(callback):
        return
    await callback.answer()
    memories = await get_memories(str(callback.from_user.id))
    body = "<b>What Sen Remembers</b>\n\nThese are the saved instructions and details currently available to Sen."
    if not memories:
        body += "\n\nNothing has been saved yet."
    try:
        await edit_memory_menu(callback, body, "view", memories)
    except Exception as e:
        print(f"Memory view navigation error: {e}")

@router.callback_query(F.data == "memory_add")
async def handle_memory_add(callback: CallbackQuery):
    if not await authorize_memory_callback(callback):
        return
    chat_id = callback.message.chat.id
    await set_interaction(chat_id, callback.from_user.id, "add")
    await callback.answer()
    body = "<b>Add a Memory</b>\n\nTell Sen what you'd like to keep in mind for future conversations.\n\nYou can add several items at once by separating them with <code>,,</code>."
    try:
        await edit_memory_menu(callback, body, "back_close")
    except Exception as e:
        print(f"Memory addition transition error: {e}")

@router.callback_query(F.data == "memory_edit")
async def handle_memory_edit(callback: CallbackQuery):
    if not await authorize_memory_callback(callback):
        return
    memories = await get_memories(str(callback.from_user.id))
    chat_id = callback.message.chat.id
    await set_interaction(chat_id, callback.from_user.id, "edit_number")
    await callback.answer()
    if not memories:
        body = "<b>Edit a Memory</b>\n\nThere aren't any saved memories to edit yet."
    else:
        rows = ["<b>Edit a Memory</b>\n\nSend the memory number followed by the replacement text.\n"]
        rows.extend(f"{i}. {html.escape(memory)}" for i, memory in enumerate(memories, 1))
        rows.append("\nExample: <code>2 My new instruction</code>")
        body = "\n".join(rows)
    try:
        await edit_memory_menu(callback, body, "back_close")
    except Exception as e:
        print(f"Memory editor menu error: {e}")

@router.callback_query(F.data == "memory_forget")
async def handle_memory_forget(callback: CallbackQuery):
    if not await authorize_memory_callback(callback):
        return
    memories = await get_memories(str(callback.from_user.id))
    chat_id = callback.message.chat.id
    await set_interaction(chat_id, callback.from_user.id, "forget")
    await callback.answer()
    if not memories:
        body = "<b>Remove Memories</b>\n\nThere's nothing saved here to remove."
    else:
        rows = ["<b>Remove Memories</b>\n\nSend one or more memory numbers, separated with <code>,,</code>, to remove them.\n"]
        rows.extend(f"{i}. {html.escape(memory)}" for i, memory in enumerate(memories, 1))
        rows.append("\nExample: <code>1,, 3</code>")
        body = "\n".join(rows)
    try:
        await edit_memory_menu(callback, body, "back_close")
    except Exception as e:
        print(f"Memory wipe menu error: {e}")

@router.callback_query(F.data == "memory_forget_all")
async def handle_memory_forget_all(callback: CallbackQuery):
    if not await authorize_memory_callback(callback):
        return
    await clear_interaction(callback.message.chat.id, callback.from_user.id)
    await callback.answer()
    body = "<b>Clear All Memories?</b>\n\nThis will remove every saved memory for your account and clear the conversation context associated with this chat.\n\n<b>This cannot be undone.</b>"
    try:
        await edit_memory_menu(callback, body, "confirm_forget_all")
    except Exception as e:
        print(f"Confirm all menu error: {e}")

@router.callback_query(F.data == "memory_confirm_forget_all")
async def handle_confirm_forget_all(callback: CallbackQuery):
    if not await authorize_memory_callback(callback):
        return
    user_id = callback.from_user.id
    user_id_str = str(user_id)
    chat_id = callback.message.chat.id
    await redis_client.delete(
        f"memory_list:{user_id_str}",
        f"chat_history:{chat_id}:{user_id_str}",
        interaction_key(chat_id, user_id),
    )
    await callback.answer("All saved memory has been cleared.", show_alert=True)
    try:
        await edit_memory_menu(callback, "<b>Memory Cleared</b>\n\nYour saved memories and local conversation context have been removed.", "back_close")
    except Exception as e:
        print(f"Post wipe layout error: {e}")

@router.callback_query(F.data == "memory_back")
async def handle_memory_back(callback: CallbackQuery):
    if not await authorize_memory_callback(callback):
        return
    chat_id = callback.message.chat.id
    await clear_interaction(chat_id, callback.from_user.id)
    await callback.answer()
    safe_name = html.escape(get_user_display_name(callback.from_user))
    body = f"<b>Memory Center</b>\n\nWelcome, {safe_name}.\n\nKeep track of the details and instructions you've asked Sen to remember. Changes here affect how Sen responds to you."
    try:
        await edit_memory_menu(callback, body, "main")
    except Exception as e:
        print(f"Back button routing error: {e}")

@router.callback_query(F.data == "memory_close")
async def handle_memory_close(callback: CallbackQuery):
    if not await authorize_memory_callback(callback):
        return
    await callback.answer("Closed")
    await close_menu(callback)

# ==========================================
# Memory text input
# ==========================================
async def process_memory_text(message: Message, action: str) -> bool:
    if not message.text or message.text.startswith("/"):
        return False
    user_id = message.from_user.id
    user_id_str = str(user_id)
    chat_id = message.chat.id
    key = f"memory_list:{user_id_str}"
    if action == "add":
        parts = [p.strip()[:200] for p in message.text.split(",,") if p.strip()]
        for part in parts[:10]:
            try:
                if await redis_client.lpos(key, part) is None:
                    await redis_client.rpush(key, part)
            except Exception as e:
                print(f"Memory listing error: {e}")
        await redis_client.ltrim(key, -25, -1)
        await clear_interaction(chat_id, user_id)
        try:
            await message.delete()
        except Exception:
            pass
        return True
    if action == "edit_number":
        parts = message.text.strip().split(" ", 1)
        if len(parts) != 2 or not parts[0].isdigit():
            return True
        index = int(parts[0]) - 1
        new_value = parts[1].strip()[:200]
        raw = await redis_client.lrange(key, 0, -1)
        if 0 <= index < len(raw):
            await redis_client.lset(key, index, new_value)
        await clear_interaction(chat_id, user_id)
        try:
            await message.delete()
        except Exception:
            pass
        return True
    if action == "forget":
        indices = [int(n.strip()) - 1 for n in message.text.split(",,") if n.strip().isdigit()]
        memories = await get_memories(user_id_str)
        for index in sorted(set(indices), reverse=True):
            if 0 <= index < len(memories):
                memories.pop(index)
        await redis_client.delete(key)
        if memories:
            await redis_client.rpush(key, *memories)
        await clear_interaction(chat_id, user_id)
        try:
            await message.delete()
        except Exception:
            pass
        return True
    return False

# ==========================================
# /del
# ==========================================
@router.message(Command("del"))
async def handle_delete(message: Message):
    if not message.from_user or message.from_user.id != OWNER_ID:
        return
    if message.reply_to_message and BOT_INFO and message.reply_to_message.from_user and message.reply_to_message.from_user.id == BOT_INFO.id:
        try:
            await bot.delete_message(message.chat.id, message.reply_to_message.message_id)
        except Exception as e:
            print(f"/del target deletion error: {e}")
    print(f"[/del] user_id={message.from_user.id} chat_id={message.chat.id} ephemeral_message_id={getattr(message, 'ephemeral_message_id', None)}")

# ==========================================
# Web search
# ==========================================
async def free_web_search(query: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(SEARXNG_URL, params={"q": query, "format": "json"}, headers=headers, timeout=8.0)
        if response.status_code != 200:
            print(f"SearXNG Pipeline HTTP {response.status_code}: {response.text[:300]}")
            return ""
        results = response.json().get("results", [])[:10]
        return "\n\n".join(
            f"Title: {x.get('title', '')}\nContent: {x.get('content', '')}\nURL: {x.get('url', '')}"
            for x in results if x.get("title") or x.get("content")
        )
    except Exception as e:
        print(f"Web contextual search failure: {e}")
        return ""

# ==========================================
# Audio
# ==========================================
async def send_audio_track(chat_id: int, msg_id: int, key: str, file_path: str, title: str, performer: str, is_private: bool):
    try:
        cached = await redis_client.get(f"audio_cache:{key}")
        reply_to = None if is_private else msg_id
        if cached:
            audio = cached.decode("utf-8") if isinstance(cached, bytes) else cached
        elif os.path.exists(file_path):
            audio = FSInputFile(file_path)
        else:
            return
        try:
            sent = await bot.send_audio(chat_id=chat_id, audio=audio, title=title, performer=performer, reply_to_message_id=reply_to)
        except Exception as e:
            if "message to be replied not found" not in str(e).lower():
                raise
            sent = await bot.send_audio(chat_id=chat_id, audio=audio, title=title, performer=performer)
        if sent.audio and sent.audio.file_id:
            await redis_client.set(f"audio_cache:{key}", sent.audio.file_id)
    except Exception as e:
        print(f"Audio media delivery fault record ({key}): {e}")

@router.message(F.community_chat_added)
async def handle_community_added(message: Message):
    print(f"Community binding topology registered: {message.chat.id}")

@router.message(F.community_chat_removed)
async def handle_community_removed(message: Message):
    print(f"Community dropping context safely absorbed: {message.chat.id}")

# ==========================================
# AI response formatting
# ==========================================
def clean_ai_output(text: str) -> str:
    text = (text or "I didn't receive a response.").strip()
    text = re.sub(r"^```(?:html)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()

async def send_ai_response(chat_id: int, msg_id: int, response_text: str, is_private: bool) -> Message:
    """Send Gemini output through Telegram's Rich Message API.

    Gemini is instructed to return Rich HTML, including <p>, headings,
    tables and <details>. Those are Rich HTML features, not legacy
    parse_mode=HTML tags. Sending them through send_message(parse_mode=HTML)
    causes Telegram's 'Unsupported start tag p' error.
    """
    rich_message = InputRichMessage(html=response_text)
    if is_private:
        return await bot.send_rich_message(
            chat_id=chat_id,
            rich_message=rich_message,
        )
    return await bot.send_rich_message(
        chat_id=chat_id,
        rich_message=rich_message,
        reply_parameters=ReplyParameters(message_id=msg_id),
    )

# ==========================================
# Primary AI core
# ==========================================
@router.message(F.text | F.caption | F.voice)
async def handle_conversation(message: Message):
    if message.audio is not None:
        return

    action = await get_interaction(message.chat.id, message.from_user.id)
    if action and message.text and not message.text.startswith("/"):
        if await process_memory_text(message, action):
            return

    text = message.text or message.caption or ""
    text_no_html = re.sub(r"<[^>]+>", "", text)

    if re.search(r"\bsen\b", text_no_html, re.I):
        asyncio.create_task(send_audio_track(message.chat.id, message.message_id, "sen", "Devin_The_Dude_Anythang.mp3", "Anythang", "Devin The Dude", message.chat.type == "private"))
    if re.search(r"\bmagic(?:al|ally)?\b", text_no_html, re.I):
        asyncio.create_task(send_audio_track(message.chat.id, message.message_id, "magic", "Do You Believe In Magic.mp3", "Do You Believe In Magic", "The Lovin' Spoonful", message.chat.type == "private"))

    bot_username = f"@{BOT_INFO.username}" if BOT_INFO and BOT_INFO.username else ""
    is_private = message.chat.type == "private"
    lower_text = text_no_html.lower()
    is_tagged = bool(bot_username) and bot_username.lower() in lower_text
    is_tagged = is_tagged or "@gemini" in lower_text
    is_reply_to_bot = bool(message.reply_to_message and BOT_INFO and message.reply_to_message.from_user and message.reply_to_message.from_user.id == BOT_INFO.id)

    if message.voice is not None:
        if not (is_tagged or is_reply_to_bot):
            return
    elif not (is_tagged or is_reply_to_bot or is_private):
        return

    user_id_str = str(message.from_user.id)
    chat_id = message.chat.id
    msg_id = message.message_id
    clean_prompt = text
    if bot_username:
        clean_prompt = re.sub(re.escape(bot_username), "", clean_prompt, flags=re.I)
    clean_prompt = re.sub(r"@gemini\b", "", clean_prompt, flags=re.I).strip()

    cooldown_key = f"cooldown:{user_id_str}"
    if await redis_client.exists(cooldown_key):
        warning = "Slow down, request limit reached."
        if is_private:
            await message.answer(warning)
        else:
            await message.answer(warning, reply_to_message_id=msg_id)
        return
    await redis_client.set(cooldown_key, "1", ex=4)

    replied_context = ""
    if message.reply_to_message:
        replied_context = message.reply_to_message.text or message.reply_to_message.caption or ""

    audio_bytes = None
    audio_mime = "audio/ogg"
    if message.voice is not None:
        voice_obj = message.voice
        file_info = await bot.get_file(voice_obj.file_id)
        stream = await bot.download_file(file_info.file_path)
        if stream:
            audio_bytes = stream.read()
        if getattr(voice_obj, "mime_type", None):
            audio_mime = voice_obj.mime_type

    if not clean_prompt and replied_context:
        clean_prompt = "What are your thoughts on this?"
    if not (clean_prompt or replied_context or audio_bytes):
        return

    try:
        saved_facts = await get_memories(user_id_str)
        history_key = f"chat_history:{chat_id}:{user_id_str}"
        raw_hist = await redis_client.lrange(history_key, 0, -1)
        chat_history = [x.decode("utf-8") if isinstance(x, bytes) else str(x) for x in raw_hist]

        search_words = {"search", "google", "look up", "lookup", "find", "show", "show me", "table", "list", "info"}
        explicit_search = any(word in clean_prompt.lower() for word in search_words)
        search_query = clean_prompt
        if explicit_search and len(clean_prompt.split()) <= 4:
            if replied_context:
                search_query = replied_context
            elif chat_history:
                for old in reversed(chat_history):
                    if old.startswith("User: ") and len(old.split()) > 2:
                        search_query = old[6:].strip()
                        break

        search_context = await free_web_search(search_query) if explicit_search and search_query else ""
        context = []
        if replied_context:
            context.append(f'Message User is Replying To:\n"{replied_context}"')
        if chat_history:
            context.append("Recent Conversation Context:\n" + "\n".join(chat_history))
        if search_context:
            context.append("Web Search Context:\n" + search_context)

        final_prompt = clean_prompt or "Process and answer this voice note."
        if context:
            final_prompt = "\n\n".join(context) + "\n\nUser Question: " + final_prompt

        today = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
        instructions = (
            f"Today's date is {today}.\n"
            "Never use standard AI pleasantries.\n"
            "Keep casual replies brief, but expand when asked for detail.\n"
            "If the user changes subject, immediately follow the new subject.\n"
            "If joking or sarcastic, match the energy.\n"
            "If you do not know, say exactly: 'I don't have enough details to answer that accurately' without guessing.\n"
            "Do not assume personal details unless explicitly present in the memory list.\n\n"
            "OUTPUT FORMAT: Return Telegram Rich HTML, not legacy Telegram HTML. "
            "Rich HTML supports <h1>-<h6>, <p>, <b>, <i>, <u>, <s>, <code>, <pre>, "
            "<table>, <details>, <a href=\"URL\">text</a>, lists, blockquotes and other documented Rich HTML features. "
            "Do not use Markdown asterisks for formatting. Do not use Markdown pipe tables. "
            "Return clean Rich HTML suitable for Telegram's sendRichMessage API."
        )
        if search_context:
            instructions += "\nWhen using Web Search Context, state the information directly. If links are requested, use HTML. Otherwise, do not include URLs."
        if chat_history:
            instructions += "\nUse Recent Conversation Context for continuity, but do not repeat it."
        if saved_facts:
            instructions += "\n\nUser memory directives:\n" + "\n".join(f"- {x}" for x in saved_facts)

        safety = [
            types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
        ]

        if audio_bytes:
            response = await gemini_client.aio.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=[types.Part.from_bytes(data=audio_bytes, mime_type=audio_mime), final_prompt],
                config=types.GenerateContentConfig(system_instruction=instructions, safety_settings=safety),
            )
        else:
            response = await gemini_client.aio.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=final_prompt,
                config=types.GenerateContentConfig(system_instruction=instructions, safety_settings=safety),
            )

        response_text = clean_ai_output(response.text)

        # FIX: Rich HTML must go through sendRichMessage.
        # The old send_message(parse_mode="HTML") path rejected <p>
        # and caused every valid Gemini response to fall into the
        # "owner needs to fix me" error handler.
        try:
            await send_ai_response(chat_id, msg_id, response_text, is_private)
        except Exception as rich_error:
            print(f"Rich response delivery error: {rich_error}")
            # Safe fallback for malformed/unsupported Rich HTML.
            fallback = re.sub(r"<p\s*>|</p\s*>", "\n", response_text, flags=re.I)
            fallback = re.sub(r"<h[1-6]\s*>", "<b>", fallback, flags=re.I)
            fallback = re.sub(r"</h[1-6]\s*>", "</b>\n", fallback, flags=re.I)
            fallback = re.sub(r"<details[^>]*>|</details>|<summary>|</summary>", "", fallback, flags=re.I)
            fallback = re.sub(r"<(?:table|thead|tbody|tr|th|td)[^>]*>", "", fallback, flags=re.I)
            fallback = re.sub(r"</(?:table|thead|tbody|tr|th|td)>", "\n", fallback, flags=re.I)
            fallback = re.sub(r"<[^>]+>", "", fallback)
            fallback = html.unescape(fallback).strip()
            if not fallback:
                fallback = "I didn't receive a response."
            if is_private:
                await bot.send_message(chat_id=chat_id, text=fallback)
            else:
                await bot.send_message(chat_id=chat_id, text=fallback, reply_parameters=ReplyParameters(message_id=msg_id))

        clean_history = re.sub(r"<[^>]+>", "", response_text)
        clean_history = html.unescape(clean_history).strip()
        await redis_client.rpush(history_key, f"User: {clean_prompt or 'Voice Note'}", f"Bot: {clean_history}")
        await redis_client.ltrim(history_key, -10, -1)

    except Exception as e:
        print(f"Gemini AI processing error: {e}")
        error = "Whoa, I'm getting a little overwhelmed! Let me catch my breath." if "429" in str(e) else "I am currently broken right now, the owner needs to fix me."
        if is_private:
            await message.answer(error)
        else:
            await message.answer(error, reply_to_message_id=msg_id)

# ==========================================
# Health server
# ==========================================
async def health_check(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "bot": BOT_INFO.username if BOT_INFO else None})

# ==========================================
# Telegram commands
# ==========================================
async def configure_commands() -> None:
    group_commands = [
        BotCommand(command="memories", description="Open your private memory menu", is_ephemeral=True),
        BotCommand(command="del", description="Delete a bot message", is_ephemeral=True),
    ]
    private_commands = [
        BotCommand(command="memories", description="Manage your instructed memories"),
        BotCommand(command="del", description="Delete a bot message"),
    ]
    try:
        await bot.delete_my_commands(scope=BotCommandScopeAllChatAdministrators())
        print("Cleared administrator-specific command scope.")
    except Exception as e:
        print(f"Could not clear administrator command scope: {e}")
    await bot.set_my_commands(group_commands, scope=BotCommandScopeAllGroupChats())
    await bot.set_my_commands(private_commands, scope=BotCommandScopeAllPrivateChats())
    print("Configured group commands: /memories=ephemeral /del=ephemeral")
    try:
        stored_group_commands = await bot.get_my_commands(scope=BotCommandScopeAllGroupChats())
        print("Telegram group commands: " + str([(x.command, getattr(x, "is_ephemeral", None)) for x in stored_group_commands]))
        stored_admin_commands = await bot.get_my_commands(scope=BotCommandScopeAllChatAdministrators())
        print("Telegram administrator-specific commands: " + str([(x.command, getattr(x, "is_ephemeral", None)) for x in stored_admin_commands]))
        stored_private_commands = await bot.get_my_commands(scope=BotCommandScopeAllPrivateChats())
        print("Telegram private commands: " + str([(x.command, getattr(x, "is_ephemeral", None)) for x in stored_private_commands]))
    except Exception as e:
        print(f"Could not verify Telegram commands: {e}")

# ==========================================
# Main
# ==========================================
async def main():
    global BOT_INFO
    BOT_INFO = await bot.get_me()
    print(f"Logged in successfully as @{BOT_INFO.username}")
    await configure_commands()

    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Operational check dashboard running on port {port}")

    try:
        print("Clearing any existing webhook or active server blocks...")
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        print(f"Non-critical webhook clearance notice: {e}")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await redis_client.aclose()
        await runner.cleanup()
        print("Bot execution stack dropped cleanly.")

if __name__ == "__main__":
    asyncio.run(main())
