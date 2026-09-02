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

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
if not GEMINI_API_KEY:
    raise ValueError("CRITICAL CONFIGURATION ERROR: 'GEMINI_API_KEY' missing.")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

bot = Bot(
    token=API_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML"),
)
dp = Dispatcher()
router = Router()
dp.include_router(router)
BOT_INFO = None

MENU_TTL = 30
INTERACTION_TTL = 300
menu_tasks: dict[tuple[int, int], asyncio.Task] = {}


# ==========================================
# Redis state
# ==========================================

def interaction_key(chat_id: int, user_id: int) -> str:
    return f"memory_interaction:{chat_id}:{user_id}"


async def set_interaction(chat_id: int, user_id: int, action: str) -> None:
    await redis_client.set(interaction_key(chat_id, user_id), action, ex=INTERACTION_TTL)


async def get_interaction(chat_id: int, user_id: int) -> str | None:
    value = await redis_client.get(interaction_key(chat_id, user_id))
    return value.decode("utf-8") if isinstance(value, bytes) else value


async def clear_interaction(chat_id: int, user_id: int) -> None:
    await redis_client.delete(interaction_key(chat_id, user_id))


def menu_identity_key(chat_id: int, user_id: int) -> str:
    return f"memory_menu_identity:{chat_id}:{user_id}"


async def register_menu_identity(chat_id: int, user_id: int, message_id: int) -> None:
    await redis_client.set(menu_identity_key(chat_id, user_id), str(message_id), ex=MENU_TTL + 60)


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


async def get_memories(user_id_str: str) -> list[str]:
    try:
        raw = await redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
        return [x.decode("utf-8") if isinstance(x, bytes) else str(x) for x in raw]
    except Exception as e:
        print(f"Memory read error: {e}")
        return []


async def get_formatted_memories(user_id_str: str) -> str:
    memories = await get_memories(user_id_str)
    if not memories:
        return "<p>No instructed memories stored.</p>"
    return "<ol>" + "".join(f"<li>{html.escape(x)}</li>" for x in memories) + "</ol>"


# ==========================================
# Rich memory UI
# ==========================================

def rich_main_menu() -> str:
    return """
<h2>Sen Bot's Memory</h2>
<p>Manage your personal instructed memory.</p>
<tg-button-row align="center">
  <tg-button type="callback_data" style="primary" data="memory_view">Memories</tg-button>
  <tg-button type="callback_data" style="success" data="memory_add">New memory</tg-button>
</tg-button-row>
<tg-button-row align="center">
  <tg-button type="callback_data" style="link" data="memory_close">Close</tg-button>
</tg-button-row>
""".strip()


def rich_memory_menu(memories_html: str) -> str:
    return f"""
<h2>Sen Bot's Instructed Memories</h2>
{memories_html}
<tg-button-row align="center">
  <tg-button type="callback_data" style="primary" data="memory_edit">Edit</tg-button>
  <tg-button type="callback_data" data="memory_forget">Forget</tg-button>
</tg-button-row>
<tg-button-row align="center">
  <tg-button type="callback_data" style="danger" data="memory_forget_all">Forget all</tg-button>
</tg-button-row>
<tg-button-row align="center">
  <tg-button type="callback_data" style="link" data="memory_back">Back</tg-button>
  <tg-button type="callback_data" style="link" data="memory_close">Close</tg-button>
</tg-button-row>
""".strip()


def rich_back_close(prompt: str) -> str:
    return f"""
{prompt}
<tg-button-row align="center">
  <tg-button type="callback_data" style="link" data="memory_back">Back</tg-button>
  <tg-button type="callback_data" style="link" data="memory_close">Close</tg-button>
</tg-button-row>
""".strip()


def rich_forget_all_confirm() -> str:
    return """
<h2>Forget everything?</h2>
<p>This permanently clears all saved memories and this chat's stored conversation history.</p>
<p><b>This cannot be undone.</b></p>
<tg-button-row align="center">
  <tg-button type="callback_data" style="danger" data="memory_confirm_forget_all">Delete everything</tg-button>
</tg-button-row>
<tg-button-row align="center">
  <tg-button type="callback_data" style="link" data="memory_back">Cancel</tg-button>
  <tg-button type="callback_data" style="link" data="memory_close">Close</tg-button>
</tg-button-row>
""".strip()


# ==========================================
# Menu lifecycle
# ==========================================

async def cancel_menu_timer(key: tuple[int, int]) -> None:
    task = menu_tasks.pop(key, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def schedule_menu_delete(chat_id: int, user_id: int, ephemeral_id: int) -> None:
    key = (chat_id, user_id)
    await cancel_menu_timer(key)

    async def expire() -> None:
        try:
            await asyncio.sleep(MENU_TTL)
            current = await get_menu_identity(chat_id, user_id)
            if current == ephemeral_id:
                try:
                    await bot.delete_ephemeral_message(
                        chat_id=chat_id,
                        receiver_user_id=user_id,
                        ephemeral_message_id=ephemeral_id,
                    )
                except Exception as e:
                    print(f"Ephemeral menu expiry delete error: {e}")
                finally:
                    await clear_menu_identity(chat_id, user_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"Menu expiry error: {e}")
        finally:
            if menu_tasks.get(key) is asyncio.current_task():
                menu_tasks.pop(key, None)

    menu_tasks[key] = asyncio.create_task(expire())


async def schedule_private_menu_delete(chat_id: int, user_id: int, message_id: int) -> None:
    key = (chat_id, user_id)
    await cancel_menu_timer(key)

    async def expire() -> None:
        try:
            await asyncio.sleep(MENU_TTL)
            if await get_menu_identity(chat_id, user_id) == message_id:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=message_id)
                except Exception as e:
                    print(f"Private menu expiry delete error: {e}")
                finally:
                    await clear_menu_identity(chat_id, user_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"Private menu expiry error: {e}")
        finally:
            if menu_tasks.get(key) is asyncio.current_task():
                menu_tasks.pop(key, None)

    menu_tasks[key] = asyncio.create_task(expire())


async def send_menu(
    chat_id: int,
    user_id: int,
    rich_html: str,
    source_message: Message | None = None,
) -> Message:
    """
    Sends the private memory UI.

    The important Bot API 10.2+ detail is that an ephemeral command must be
    answered through ReplyParameters(ephemeral_message_id=...). That keeps
    the response tied to the exact user/client that invoked the command.
    """
    is_group = chat_id != user_id

    if is_group:
        kwargs = {
            "chat_id": chat_id,
            "rich_message": InputRichMessage(html=rich_html),
        }

        incoming_ephemeral_id = getattr(source_message, "ephemeral_message_id", None)
        if incoming_ephemeral_id is not None:
            # MUST be sent immediately. Telegram only permits an ephemeral
            # reply for 15 seconds after the incoming ephemeral message.
            kwargs["reply_parameters"] = ReplyParameters(
                ephemeral_message_id=incoming_ephemeral_id,
            )
        else:
            # This also makes manually typed /memories private in groups.
            kwargs["ephemeral_message_parameters"] = EphemeralMessageParameters(
                receiver_user_id=user_id,
            )

        message = await bot.send_rich_message(**kwargs)
        ephemeral_id = getattr(message, "ephemeral_message_id", None)
        if ephemeral_id is None:
            raise RuntimeError("Telegram did not return an ephemeral_message_id for the memory menu.")

        receiver = getattr(message, "receiver_user", None)
        receiver_id = getattr(receiver, "id", None)
        if receiver_id is not None and receiver_id != user_id:
            raise RuntimeError("Telegram returned the wrong ephemeral receiver.")

        await register_menu_identity(chat_id, user_id, ephemeral_id)
        await schedule_menu_delete(chat_id, user_id, ephemeral_id)
        return message

    message = await bot.send_rich_message(
        chat_id=chat_id,
        rich_message=InputRichMessage(html=rich_html),
    )
    await register_menu_identity(chat_id, user_id, message.message_id)
    await schedule_private_menu_delete(chat_id, user_id, message.message_id)
    return message


# ==========================================
# Callback security and editing
# ==========================================

async def authorize_memory_callback(callback: CallbackQuery) -> bool:
    message = callback.message
    if not message:
        await callback.answer("This memory menu is no longer available.", show_alert=True)
        return False

    user_id = callback.from_user.id
    chat_id = message.chat.id
    is_group = message.chat.type in {"group", "supergroup"}

    if is_group:
        ephemeral_id = getattr(message, "ephemeral_message_id", None)
        if ephemeral_id is None:
            await callback.answer("This private memory menu is invalid.", show_alert=True)
            return False

        receiver = getattr(message, "receiver_user", None)
        receiver_id = getattr(receiver, "id", None)
        if receiver_id is not None and receiver_id != user_id:
            await callback.answer("This memory menu belongs to another user.", show_alert=True)
            return False

        current = await get_menu_identity(chat_id, user_id)
        if current != ephemeral_id:
            await callback.answer("This memory menu has expired.", show_alert=True)
            return False
        return True

    current = await get_menu_identity(chat_id, user_id)
    if current != message.message_id:
        await callback.answer("This memory menu has expired.", show_alert=True)
        return False
    return True


async def edit_menu(callback: CallbackQuery, rich_html: str) -> None:
    message = callback.message
    if not message:
        return

    chat_id = message.chat.id
    user_id = callback.from_user.id
    is_group = message.chat.type in {"group", "supergroup"}
    await cancel_menu_timer((chat_id, user_id))

    if is_group:
        ephemeral_id = getattr(message, "ephemeral_message_id", None)
        if ephemeral_id is None or await get_menu_identity(chat_id, user_id) != ephemeral_id:
            raise RuntimeError("Ephemeral memory menu identity does not match.")
        await bot.edit_ephemeral_message_text(
            chat_id=chat_id,
            receiver_user_id=user_id,
            ephemeral_message_id=ephemeral_id,
            rich_message=InputRichMessage(html=rich_html),
        )
        await register_menu_identity(chat_id, user_id, ephemeral_id)
        await schedule_menu_delete(chat_id, user_id, ephemeral_id)
        return

    if await get_menu_identity(chat_id, user_id) != message.message_id:
        raise RuntimeError("Private memory menu identity does not match.")
    await bot.edit_message_text(
        chat_id=chat_id,
        message_id=message.message_id,
        rich_message=InputRichMessage(html=rich_html),
    )
    await register_menu_identity(chat_id, user_id, message.message_id)
    await schedule_private_menu_delete(chat_id, user_id, message.message_id)


async def close_menu(callback: CallbackQuery) -> None:
    message = callback.message
    if not message:
        return
    chat_id = message.chat.id
    user_id = callback.from_user.id
    await clear_interaction(chat_id, user_id)
    await cancel_menu_timer((chat_id, user_id))
    try:
        if message.chat.type in {"group", "supergroup"}:
            ephemeral_id = getattr(message, "ephemeral_message_id", None)
            if ephemeral_id is not None:
                await bot.delete_ephemeral_message(
                    chat_id=chat_id,
                    receiver_user_id=user_id,
                    ephemeral_message_id=ephemeral_id,
                )
        else:
            await bot.delete_message(chat_id=chat_id, message_id=message.message_id)
    except Exception as e:
        print(f"Menu close error: {e}")
    finally:
        await clear_menu_identity(chat_id, user_id)


# ==========================================
# /memories
# ==========================================

def is_memories_command(message: Message) -> bool:
    text = (message.text or "").strip()
    if not text:
        return False
    first = text.split(maxsplit=1)[0]
    if not first.startswith("/"):
        return False
    command, _, username = first.partition("@")
    if command.lower() != "/memories":
        return False
    if username and BOT_INFO and BOT_INFO.username:
        return username.lower() == BOT_INFO.username.lower()
    return True


async def show_memories_command(message: Message) -> None:
    user_id = message.from_user.id
    chat_id = message.chat.id
    await clear_interaction(chat_id, user_id)
    await cancel_menu_timer((chat_id, user_id))
    await clear_menu_identity(chat_id, user_id)

    try:
        # The response is sent immediately so the 15-second ephemeral reply
        # window is never missed.
        await send_menu(chat_id, user_id, rich_main_menu(), source_message=message)
    except Exception as e:
        print(f"Memory menu send error: {e}")
        # Never leak a memory menu into a group as a public message.
        if message.chat.type in {"group", "supergroup"}:
            return
        try:
            await message.answer("I couldn't open the memory menu.")
        except Exception:
            pass


@router.message(F.text.func(is_memories_command))
async def handle_memories(message: Message):
    await show_memories_command(message)


# ==========================================
# Memory callbacks
# ==========================================

@router.callback_query(F.data == "memory_view")
async def handle_memory_view(callback: CallbackQuery):
    if not await authorize_memory_callback(callback):
        return
    await callback.answer()
    try:
        await edit_menu(callback, rich_memory_menu(await get_formatted_memories(str(callback.from_user.id))))
    except Exception as e:
        print(f"Memory view error: {e}")


@router.callback_query(F.data == "memory_add")
async def handle_memory_add(callback: CallbackQuery):
    if not await authorize_memory_callback(callback):
        return
    chat_id = callback.message.chat.id
    await set_interaction(chat_id, callback.from_user.id, "add")
    await callback.answer("Send the memory you want me to save.")
    try:
        await edit_menu(callback, rich_back_close(
            "<h2>New memory</h2><p>Send the fact or instruction you want me to remember.</p>"
            "<p>You can send multiple items separated with <code>,,</code>.</p>"
        ))
    except Exception as e:
        print(f"Memory add menu error: {e}")


@router.callback_query(F.data == "memory_edit")
async def handle_memory_edit(callback: CallbackQuery):
    if not await authorize_memory_callback(callback):
        return
    memories = await get_memories(str(callback.from_user.id))
    chat_id = callback.message.chat.id
    await set_interaction(chat_id, callback.from_user.id, "edit_number")
    await callback.answer()
    if not memories:
        body = "<h2>Edit memory</h2><p>You don't have any saved memories yet.</p>"
    else:
        body = (
            "<h2>Edit memory</h2><p>Send the number and replacement text.</p><ol>"
            + "".join(f"<li>{html.escape(x)}</li>" for x in memories)
            + "</ol><p>Example: <code>2 My new instruction</code></p>"
        )
    try:
        await edit_menu(callback, rich_back_close(body))
    except Exception as e:
        print(f"Memory edit menu error: {e}")


@router.callback_query(F.data == "memory_forget")
async def handle_memory_forget(callback: CallbackQuery):
    if not await authorize_memory_callback(callback):
        return
    memories = await get_memories(str(callback.from_user.id))
    chat_id = callback.message.chat.id
    await set_interaction(chat_id, callback.from_user.id, "forget")
    await callback.answer()
    if not memories:
        body = "<h2>Forget memories</h2><p>Your instructed memory list is already empty.</p>"
    else:
        body = (
            "<h2>Forget memories</h2><p>Send one number or several separated by <code>,,</code>.</p><ol>"
            + "".join(f"<li>{html.escape(x)}</li>" for x in memories)
            + "</ol><p>Example: <code>1,, 3,, 5</code></p>"
        )
    try:
        await edit_menu(callback, rich_back_close(body))
    except Exception as e:
        print(f"Memory forget menu error: {e}")


@router.callback_query(F.data == "memory_forget_all")
async def handle_memory_forget_all(callback: CallbackQuery):
    if not await authorize_memory_callback(callback):
        return
    await clear_interaction(callback.message.chat.id, callback.from_user.id)
    await callback.answer()
    try:
        await edit_menu(callback, rich_forget_all_confirm())
    except Exception as e:
        print(f"Forget-all confirmation error: {e}")


@router.callback_query(F.data == "memory_confirm_forget_all")
async def handle_confirm_forget_all(callback: CallbackQuery):
    if not await authorize_memory_callback(callback):
        return
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    await redis_client.delete(
        f"memory_list:{user_id}",
        f"chat_history:{chat_id}:{user_id}",
        interaction_key(chat_id, user_id),
    )
    await callback.answer("All memories cleared.")
    try:
        await edit_menu(callback, rich_back_close(
            "<h2>Memories cleared</h2><p>Everything saved for this user and conversation has been removed.</p>"
        ))
    except Exception as e:
        print(f"Forget-all completion error: {e}")


@router.callback_query(F.data == "memory_back")
async def handle_memory_back(callback: CallbackQuery):
    if not await authorize_memory_callback(callback):
        return
    await clear_interaction(callback.message.chat.id, callback.from_user.id)
    await callback.answer()
    try:
        await edit_menu(callback, rich_main_menu())
    except Exception as e:
        print(f"Memory back error: {e}")


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
    user_key = f"memory_list:{user_id}"
    chat_id = message.chat.id

    if action == "add":
        parts = [p.strip()[:200] for p in message.text.split(",,") if p.strip()]
        for part in parts[:10]:
            try:
                if await redis_client.lpos(user_key, part) is None:
                    await redis_client.rpush(user_key, part)
            except Exception as e:
                print(f"Memory save error: {e}")
        await redis_client.ltrim(user_key, -25, -1)
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
        raw = await redis_client.lrange(user_key, 0, -1)
        if 0 <= index < len(raw):
            await redis_client.lset(user_key, index, new_value)
        await clear_interaction(chat_id, user_id)
        try:
            await message.delete()
        except Exception:
            pass
        return True

    if action == "forget":
        indices = [int(n.strip()) - 1 for n in message.text.split(",,") if n.strip().isdigit()]
        raw = await redis_client.lrange(user_key, 0, -1)
        memories = [x.decode("utf-8") if isinstance(x, bytes) else str(x) for x in raw]
        for index in sorted(set(indices), reverse=True):
            if 0 <= index < len(memories):
                memories.pop(index)
        await redis_client.delete(user_key)
        if memories:
            await redis_client.rpush(user_key, *memories)
        await clear_interaction(chat_id, user_id)
        try:
            await message.delete()
        except Exception:
            pass
        return True

    return False


# ==========================================
# Owner delete command
# ==========================================

@router.message(F.text.regexp(r"^/(?:delete|del)(?:@\w+)?(?:\s|$)"))
async def handle_delete(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    if message.reply_to_message and BOT_INFO and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.id == BOT_INFO.id:
            try:
                await bot.delete_message(message.chat.id, message.reply_to_message.message_id)
            except Exception:
                pass
    try:
        await message.delete()
    except Exception:
        pass


# ==========================================
# Search and audio
# ==========================================

async def free_web_search(query: str) -> str:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                SEARXNG_URL,
                params={"q": query, "format": "json"},
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Accept": "application/json",
                },
                timeout=8.0,
            )
        if response.status_code != 200:
            print(f"SearXNG HTTP {response.status_code}: {response.text[:300]}")
            return ""
        results = response.json().get("results", [])[:10]
        return "\n\n".join(
            f"Title: {x.get('title', '')}\nContent: {x.get('content', '')}\nURL: {x.get('url', '')}"
            for x in results if x.get("title") or x.get("content")
        )
    except Exception as e:
        print(f"SearXNG error: {e}")
        return ""


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
            sent = await bot.send_audio(
                chat_id=chat_id,
                audio=audio,
                title=title,
                performer=performer,
                reply_to_message_id=reply_to,
            )
        except Exception as e:
            if "message to be replied not found" not in str(e).lower():
                raise
            sent = await bot.send_audio(chat_id=chat_id, audio=audio, title=title, performer=performer)
        if sent.audio and sent.audio.file_id:
            await redis_client.set(f"audio_cache:{key}", sent.audio.file_id)
    except Exception as e:
        print(f"Audio error ({key}): {e}")


# ==========================================
# Primary chat
# ==========================================

@router.message(F.text | F.caption | F.voice | F.audio)
async def handle_conversation(message: Message):
    # /memories has its own handler above. This second guard prevents it from
    # ever reaching Gemini if Telegram/client routing changes unexpectedly.
    if is_memories_command(message):
        return

    action = await get_interaction(message.chat.id, message.from_user.id)
    if action and message.text and not message.text.startswith("/"):
        if await process_memory_text(message, action):
            return

    text = message.text or message.caption or ""
    text_no_html = re.sub(r"<[^>]+>", "", text)

    if re.search(r"\bsen\b", text_no_html, re.IGNORECASE):
        asyncio.create_task(send_audio_track(
            message.chat.id, message.message_id, "sen",
            "Devin_The_Dude_Anythang.mp3", "Anythang", "Devin The Dude",
            message.chat.type == "private",
        ))

    if re.search(r"\bmagic(?:al|ally)?\b", text_no_html, re.IGNORECASE):
        asyncio.create_task(send_audio_track(
            message.chat.id, message.message_id, "magic",
            "Do You Believe In Magic.mp3", "Do You Believe In Magic", "The Lovin' Spoonful",
            message.chat.type == "private",
        ))

    bot_username = f"@{BOT_INFO.username}" if BOT_INFO and BOT_INFO.username else ""
    is_private = message.chat.type == "private"
    is_tagged = bool(bot_username) and bot_username.lower() in text_no_html.lower()
    is_tagged = is_tagged or "@gemini" in text_no_html.lower()
    is_reply_to_bot = bool(
        message.reply_to_message and BOT_INFO and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == BOT_INFO.id
    )

    if not (is_tagged or is_reply_to_bot or is_private or message.content_type in {"voice", "audio"}):
        return

    user_id_str = str(message.from_user.id)
    chat_id = message.chat.id
    msg_id = message.message_id
    clean_prompt = text
    if bot_username:
        clean_prompt = re.sub(re.escape(bot_username), "", clean_prompt, flags=re.IGNORECASE)
    clean_prompt = re.sub(r"@gemini\b", "", clean_prompt, flags=re.IGNORECASE).strip()

    cooldown_key = f"cooldown:{user_id_str}"
    if await redis_client.exists(cooldown_key):
        warning = "Slow down, request limit reached."
        try:
            if is_private:
                await message.answer(warning)
            else:
                await message.answer(warning, reply_to_message_id=msg_id)
        except Exception:
            pass
        return
    await redis_client.set(cooldown_key, "1", ex=4)

    replied_context = ""
    if message.reply_to_message:
        replied_context = message.reply_to_message.text or message.reply_to_message.caption or ""

    audio_bytes = None
    audio_mime = "audio/ogg"
    if message.content_type in {"voice", "audio"}:
        audio_obj = message.voice or message.audio
        if audio_obj:
            file_info = await bot.get_file(audio_obj.file_id)
            stream = await bot.download_file(file_info.file_path)
            if stream:
                audio_bytes = stream.read()
            if getattr(audio_obj, "mime_type", None):
                audio_mime = audio_obj.mime_type

    if not clean_prompt and replied_context:
        clean_prompt = "What are your thoughts on this?"
    if not (clean_prompt or replied_context or audio_bytes):
        return

    try:
        raw_mem = await redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
        saved_facts = [x.decode("utf-8") if isinstance(x, bytes) else str(x) for x in raw_mem]

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
            "OUTPUT FORMAT: Use Telegram Rich HTML. Use <h1>-<h6>, <p>, <b>, <i>, <u>, <s>, <code>, <pre>, <table>, <details>, <a href=\"URL\">text</a>, and other supported rich HTML where useful.\n"
            "Do not use Markdown asterisks for formatting. Do not use Markdown pipe tables. Return clean HTML suitable for Telegram sendRichMessage."
        )
        if search_context:
            instructions += "\nWhen using Web Search Context, state the information directly. If the user asks for links or sources, use HTML links. If they do not ask for sources, do not include URLs."
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

        response_text = response.text or "I didn't receive a response."
        response_text = response_text.replace("```html", "").replace("```", "")

        if is_private:
            await bot.send_rich_message(chat_id=chat_id, rich_message=InputRichMessage(html=response_text))
        else:
            await bot.send_rich_message(
                chat_id=chat_id,
                rich_message=InputRichMessage(html=response_text),
                reply_parameters=ReplyParameters(message_id=msg_id),
            )

        clean_history = re.sub(r"<[^>]+>", "", response_text)
        await redis_client.rpush(history_key, f"User: {clean_prompt or 'Voice Note'}", f"Bot: {clean_history}")
        await redis_client.ltrim(history_key, -10, -1)

    except Exception as e:
        print(f"Gemini API error: {e}")
        error = (
            "Whoa, I'm getting a little overwhelmed! Let me catch my breath for a minute."
            if "429" in str(e)
            else "I am currently broken right now, the owner needs to fix me."
        )
        try:
            if is_private:
                await message.answer(error)
            else:
                await message.answer(error, reply_to_message_id=msg_id)
        except Exception:
            pass


# ==========================================
# Health and commands
# ==========================================

async def health_check(request):
    return web.Response(text="200 OK - Bot is running.", status=200)


async def configure_commands() -> None:
    commands = [
        BotCommand(
            command="memories",
            description="Open your private instructed memories",
            is_ephemeral=True,
        )
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())
    await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())


async def main():
    global BOT_INFO

    try:
        print("Clearing conflicting webhooks from Telegram servers...")
        await bot.delete_webhook(drop_pending_updates=True)
        BOT_INFO = await bot.get_me()
        print(f"Bot authenticated as @{BOT_INFO.username}")
        await configure_commands()
        print("Ephemeral /memories command configured.")
    except Exception as e:
        print(f"Startup Telegram configuration error: {e}")

    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Healthcheck server listening on port {port}")
    print(f"SearXNG endpoint: {SEARXNG_URL}")

    try:
        await dp.start_polling(bot)
    finally:
        for task in list(menu_tasks.values()):
            task.cancel()
        await bot.session.close()
        await redis_client.aclose()
        await runner.cleanup()
        print("Cleanup complete. Process exiting.")


if __name__ == "__main__":
    asyncio.run(main())
