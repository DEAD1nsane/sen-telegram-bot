import os
import re
import asyncio
import html
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from aiohttp import web
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery, BotCommand,
    BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats,
    BotCommandScopeAllChatAdministrators, ReplyParameters,
    InputRichMessage, InputRichBlockParagraph,
    InputRichBlockSectionHeading, InputRichBlockButtons, InputRichBlockList,
    InputRichBlockListItem,
    RichMessageButton, RichTextBold, RichTextItalic, RichTextUnderline,
    RichTextStrikethrough, RichTextCode, EphemeralMessageParameters,
)
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
import redis.asyncio as redis
import httpx
from google import genai
from google.genai import types

redis_url = os.environ.get("REDIS_URL", "")
if not redis_url:
    host = os.environ.get("REDISHOST", "localhost")
    port = os.environ.get("REDISPORT", "6379")
    password = os.environ.get("REDISPASSWORD", "")
    redis_url = f"redis://default:{password}@{host}:{port}" if password else f"redis://{host}:{port}"
if "upstash" in redis_url.lower() and redis_url.startswith("redis://"):
    redis_url = redis_url.replace("redis://", "rediss://", 1)
redis_client = redis.from_url(redis_url, ssl_cert_reqs=None if redis_url.startswith("rediss://") else "required")

API_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or ""
if not API_TOKEN:
    raise ValueError("CRITICAL CONFIGURATION ERROR: 'BOT_TOKEN' missing. Set BOT_TOKEN in Railway Variables.")
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://searxng.railway.internal:8080/search").rstrip("/")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
if not GEMINI_API_KEY:
    raise ValueError("CRITICAL CONFIGURATION ERROR: 'GEMINI_API_KEY' missing.")
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
router = Router()
dp.include_router(router)
BOT_INFO = None

INTERACTION_TTL = 300
MENU_TTL = 30
SEARCH_CACHE_TTL = 60
MENTION_ONLY_RE = re.compile(r"^(?:@[A-Za-z0-9_]{5,32}\s*)+$")


def interaction_key(chat_id, user_id): return f"memory_interaction:{chat_id}:{user_id}"
def menu_identity_key(chat_id, user_id): return f"memory_menu_identity:{chat_id}:{user_id}"
def search_cache_key(query, news): return f"web_search:{'news' if news else 'general'}:{query.strip().lower()}"

async def set_interaction(chat_id, user_id, action):
    await redis_client.set(interaction_key(chat_id, user_id), action, ex=INTERACTION_TTL)

async def get_interaction(chat_id, user_id):
    value = await redis_client.get(interaction_key(chat_id, user_id))
    return value.decode() if isinstance(value, bytes) else value

async def clear_interaction(chat_id, user_id): await redis_client.delete(interaction_key(chat_id, user_id))

async def register_menu_identity(chat_id, user_id, message_id):
    await redis_client.set(menu_identity_key(chat_id, user_id), str(message_id), ex=MENU_TTL + 5)

async def get_menu_identity(chat_id, user_id):
    value = await redis_client.get(menu_identity_key(chat_id, user_id))
    if value is None: return None
    try: return int(value.decode() if isinstance(value, bytes) else value)
    except (TypeError, ValueError): return None

async def clear_menu_identity(chat_id, user_id): await redis_client.delete(menu_identity_key(chat_id, user_id))

async def expire_memory_menu(chat_id, user_id, menu_id):
    try:
        await asyncio.sleep(MENU_TTL)
        if await get_menu_identity(chat_id, user_id) != menu_id: return
        try:
            if chat_id != user_id:
                await bot.delete_ephemeral_message(chat_id=chat_id, receiver_user_id=user_id, ephemeral_message_id=menu_id)
            else:
                await bot.delete_message(chat_id=chat_id, message_id=menu_id)
        except Exception as e: print(f"Automatic memory menu expiry error: {e}")
        finally:
            await clear_menu_identity(chat_id, user_id); await clear_interaction(chat_id, user_id)
    except asyncio.CancelledError: pass
    except Exception as e: print(f"Memory menu expiry task error: {e}")

def schedule_menu_expiry(chat_id, user_id, menu_id): asyncio.create_task(expire_memory_menu(chat_id, user_id, menu_id))

async def get_memories(user_id_str):
    try:
        raw = await redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
        return [x.decode() if isinstance(x, bytes) else str(x) for x in raw]
    except Exception as e:
        print(f"Memory read error: {e}"); return []

def get_user_display_name(user):
    if not user: return "User"
    name = " ".join(x for x in ((getattr(user, "first_name", "") or "").strip(), (getattr(user, "last_name", "") or "").strip()) if x)
    return name or (getattr(user, "username", "") or "User").strip() or "User"

def rich_text_from_markup(text):
    pattern = re.compile(r"(<b>.*?</b>|<strong>.*?</strong>|<i>.*?</i>|<em>.*?</em>|<u>.*?</u>|<ins>.*?</ins>|<s>.*?</s>|<strike>.*?</strike>|<del>.*?</del>|<code>.*?</code>)", re.I | re.S)
    parts, position = [], 0
    for match in pattern.finditer(text):
        if match.start() > position: parts.append(html.unescape(text[position:match.start()]))
        token, low = match.group(0), match.group(0).lower()
        if low.startswith(("<b>", "<strong>")):
            inner = re.sub(r"^<(?:b|strong)>|</(?:b|strong)>$", "", token, flags=re.I | re.S); parts.append(RichTextBold(text=html.unescape(inner)))
        elif low.startswith(("<i>", "<em>")):
            inner = re.sub(r"^<(?:i|em)>|</(?:i|em)>$", "", token, flags=re.I | re.S); parts.append(RichTextItalic(text=html.unescape(inner)))
        elif low.startswith(("<u>", "<ins>")):
            inner = re.sub(r"^<(?:u|ins)>|</(?:u|ins)>$", "", token, flags=re.I | re.S); parts.append(RichTextUnderline(text=html.unescape(inner)))
        elif low.startswith(("<s>", "<strike>", "<del>")):
            inner = re.sub(r"^<(?:s|strike|del)>|</(?:s|strike|del)>$", "", token, flags=re.I | re.S); parts.append(RichTextStrikethrough(text=html.unescape(inner)))
        else:
            inner = re.sub(r"^<code>|</code>$", "", token, flags=re.I | re.S); parts.append(RichTextCode(text=html.unescape(inner)))
        position = match.end()
    if position < len(text): parts.append(html.unescape(text[position:]))
    parts = [p for p in parts if p != ""]
    return parts[0] if len(parts) == 1 else parts

def get_memory_rich_message(text, menu_type="main", memories=None):
    blocks = []
    heading = re.match(r"^\s*<b>(.*?)</b>\s*(?:\n|$)", text, re.I | re.S)
    if heading:
        blocks.append(InputRichBlockSectionHeading(text=html.unescape(heading.group(1)), size=2))
        rest = text[heading.end():].strip()
        if rest: blocks.append(InputRichBlockParagraph(text=rich_text_from_markup(rest)))
    else: blocks.append(InputRichBlockParagraph(text=rich_text_from_markup(text)))
    if memories:
        blocks.append(InputRichBlockList(items=[
            InputRichBlockListItem(blocks=[InputRichBlockParagraph(text=rich_text_from_markup(m))], value=i, type="1")
            for i, m in enumerate(memories, 1)
        ]))
    if menu_type == "main":
        blocks += [
            InputRichBlockButtons(buttons=[RichMessageButton(text="🧠 View Memories", callback_data="memory_view", style="primary")], align="center"),
            InputRichBlockButtons(buttons=[RichMessageButton(text="➕ New Memory", callback_data="memory_add", style="success")], align="center"),
            InputRichBlockButtons(buttons=[RichMessageButton(text="❌ Close", callback_data="memory_close", style="danger")], align="center"),
        ]
    elif menu_type == "view":
        blocks += [
            InputRichBlockButtons(buttons=[RichMessageButton(text="📝 Edit", callback_data="memory_edit", style="primary"), RichMessageButton(text="🗑️ Remove", callback_data="memory_forget", style="danger")], align="center"),
            InputRichBlockButtons(buttons=[RichMessageButton(text="🫯 Clear All", callback_data="memory_forget_all", style="danger")], align="center"),
            InputRichBlockButtons(buttons=[RichMessageButton(text="📢 Share to Group", callback_data="memory_share", style="success")], align="center"),
            InputRichBlockButtons(buttons=[RichMessageButton(text="↩️ Back", callback_data="memory_back"), RichMessageButton(text="❌ Close", callback_data="memory_close", style="danger")], align="center"),
        ]
    elif menu_type == "confirm_forget_all":
        blocks += [
            InputRichBlockButtons(buttons=[RichMessageButton(text="⚠️ Yes, Clear Everything", callback_data="memory_confirm_forget_all", style="danger")], align="center"),
            InputRichBlockButtons(buttons=[RichMessageButton(text="✖️ Cancel", callback_data="memory_back"), RichMessageButton(text="❌ Close", callback_data="memory_close", style="danger")], align="center"),
        ]
    else: blocks.append(InputRichBlockButtons(buttons=[RichMessageButton(text="↩️ Back", callback_data="memory_back"), RichMessageButton(text="❌ Close", callback_data="memory_close", style="danger")], align="center"))
    return InputRichMessage(blocks=blocks)

async def send_memory_menu(chat_id, user_id, text, menu_type="main", source_ephemeral_id=None, memories=None):
    rich = get_memory_rich_message(text, menu_type, memories)
    if chat_id != user_id:
        if source_ephemeral_id is None: raise RuntimeError("Missing ephemeral message id.")
        message = await bot.send_rich_message(chat_id=chat_id, rich_message=rich, reply_parameters=ReplyParameters(ephemeral_message_id=source_ephemeral_id), ephemeral_message_parameters=EphemeralMessageParameters(receiver_user_id=user_id))
        mid = getattr(message, "ephemeral_message_id", None)
    else:
        message = await bot.send_rich_message(chat_id=chat_id, rich_message=rich); mid = message.message_id
    if mid is None: raise RuntimeError("Telegram did not return a message id.")
    await register_menu_identity(chat_id, user_id, mid); schedule_menu_expiry(chat_id, user_id, mid); return message

async def authorize_memory_callback(callback):
    message = callback.message
    if not message:
        await callback.answer("This memory menu is no longer available.", show_alert=True); return False
    user_id, chat_id = callback.from_user.id, message.chat.id
    mid = getattr(message, "ephemeral_message_id", None) if message.chat.type in {"group", "supergroup"} else message.message_id
    if mid is None or await get_menu_identity(chat_id, user_id) != mid:
        await callback.answer("This memory menu is no longer active.", show_alert=True); return False
    receiver = getattr(message, "receiver_user", None)
    if receiver and getattr(receiver, "id", user_id) != user_id:
        await callback.answer("This memory menu belongs to another user.", show_alert=True); return False
    return True

async def edit_memory_menu(callback, text, menu_type="main", memories=None):
    message = callback.message
    if not message: return
    chat_id, user_id = message.chat.id, callback.from_user.id
    rich = get_memory_rich_message(text, menu_type, memories)
    if message.chat.type in {"group", "supergroup"}:
        mid = getattr(message, "ephemeral_message_id", None)
        if mid is None or await get_menu_identity(chat_id, user_id) != mid: return
        await bot.edit_ephemeral_message_text(chat_id=chat_id, receiver_user_id=user_id, ephemeral_message_id=mid, rich_message=rich)
        await register_menu_identity(chat_id, user_id, mid); schedule_menu_expiry(chat_id, user_id, mid)
    else:
        await bot.edit_message_text(chat_id=chat_id, message_id=message.message_id, rich_message=rich)
        await register_menu_identity(chat_id, user_id, message.message_id); schedule_menu_expiry(chat_id, user_id, message.message_id)

async def close_menu(callback):
    message = callback.message
    if not message: return
    chat_id, user_id = message.chat.id, callback.from_user.id
    await clear_interaction(chat_id, user_id)
    try:
        if message.chat.type in {"group", "supergroup"}:
            mid = getattr(message, "ephemeral_message_id", None)
            if mid: await bot.delete_ephemeral_message(chat_id=chat_id, receiver_user_id=user_id, ephemeral_message_id=mid)
        else: await bot.delete_message(chat_id=chat_id, message_id=message.message_id)
    except Exception as e: print(f"Menu close error: {e}")
    finally: await clear_menu_identity(chat_id, user_id)

@router.message(Command("memories"))
async def handle_memories(message):
    uid, cid = message.from_user.id, message.chat.id
    incoming = getattr(message, "ephemeral_message_id", None)
    await clear_interaction(cid, uid); await clear_menu_identity(cid, uid)
    name = html.escape(get_user_display_name(message.from_user))
    text = f"<b>Memory Center</b>\n\nWelcome, {name}.\n\nKeep track of the details and instructions you've asked Sen to remember. Changes here affect how Sen responds to you."
    if message.chat.type in {"group", "supergroup"}:
        if incoming is None: return
        try: await send_memory_menu(cid, uid, text, "main", incoming)
        except Exception as e: print(f"Memory menu send error: {e}")
    else:
        try: await send_memory_menu(cid, uid, text)
        except Exception as e: print(f"Private memory menu error: {e}")

@router.callback_query(F.data == "memory_view")
async def handle_memory_view(callback):
    if not await authorize_memory_callback(callback): return
    await callback.answer(); memories = await get_memories(str(callback.from_user.id))
    body = "<b>What Sen Remembers</b>\n\nThese are the saved instructions and details currently available to Sen."
    if not memories: body += "\n\nNothing has been saved yet."
    await edit_memory_menu(callback, body, "view", memories)

@router.callback_query(F.data == "memory_share")
async def handle_memory_share(callback):
    if not await authorize_memory_callback(callback): return
    memories = await get_memories(str(callback.from_user.id))
    if not memories:
        await callback.answer("Your memory list is empty! Nothing to share.", show_alert=True); return
    first_name = (getattr(callback.from_user, "first_name", "") or "").strip() or "User"
    safe_first_name = html.escape(first_name)
    share_blocks = [InputRichBlockSectionHeading(text=f"What Sen Remembers for {safe_first_name}", size=3)]
    share_blocks.append(InputRichBlockList(items=[
        InputRichBlockListItem(blocks=[InputRichBlockParagraph(text=rich_text_from_markup(memory))], value=i, type="1")
        for i, memory in enumerate(memories, 1)
    ]))
    try:
        await callback.bot.send_rich_message(chat_id=callback.message.chat.id, rich_message=InputRichMessage(blocks=share_blocks))
        await callback.answer("Memories shared with the group!", show_alert=True)
    except Exception as e:
        print(f"Memory share error: {e}"); await callback.answer("Failed to share memories to group.", show_alert=True)

@router.callback_query(F.data == "memory_add")
async def handle_memory_add(callback):
    if not await authorize_memory_callback(callback): return
    await set_interaction(callback.message.chat.id, callback.from_user.id, "add"); await callback.answer()
    await edit_memory_menu(callback, "<b>Add a Memory</b>\n\nTell Sen what you'd like to keep in mind for future conversations.\n\nYou can add several items at once by separating them with <code>,,</code>.", "back_close")

@router.callback_query(F.data == "memory_edit")
async def handle_memory_edit(callback):
    if not await authorize_memory_callback(callback): return
    memories = await get_memories(str(callback.from_user.id)); await set_interaction(callback.message.chat.id, callback.from_user.id, "edit_number"); await callback.answer()
    body = "<b>Edit a Memory</b>\n\nThere aren't any saved memories to edit yet." if not memories else "<b>Edit a Memory</b>\n\nSend the memory number followed by the replacement text.\n\n" + "\n".join(f"{i}. {html.escape(m)}" for i,m in enumerate(memories,1)) + "\n\nExample: <code>2 My new instruction</code>"
    await edit_memory_menu(callback, body, "back_close")

@router.callback_query(F.data == "memory_forget")
async def handle_memory_forget(callback):
    if not await authorize_memory_callback(callback): return
    memories = await get_memories(str(callback.from_user.id)); await set_interaction(callback.message.chat.id, callback.from_user.id, "forget"); await callback.answer()
    body = "<b>Remove Memories</b>\n\nThere's nothing saved here to remove." if not memories else "<b>Remove Memories</b>\n\nSend one or more memory numbers, separated with <code>,,</code>, to remove them.\n\n" + "\n".join(f"{i}. {html.escape(m)}" for i,m in enumerate(memories,1)) + "\n\nExample: <code>1,, 3</code>"
    await edit_memory_menu(callback, body, "back_close")

@router.callback_query(F.data == "memory_forget_all")
async def handle_memory_forget_all(callback):
    if not await authorize_memory_callback(callback): return
    await clear_interaction(callback.message.chat.id, callback.from_user.id); await callback.answer()
    await edit_memory_menu(callback, "<b>Clear All Memories?</b>\n\nThis will remove every saved memory for your account and clear the conversation context associated with this chat.\n\n<b>This cannot be undone.</b>", "confirm_forget_all")

@router.callback_query(F.data == "memory_confirm_forget_all")
async def handle_confirm_forget_all(callback):
    if not await authorize_memory_callback(callback): return
    uid, cid = callback.from_user.id, callback.message.chat.id
    await redis_client.delete(f"memory_list:{uid}", f"chat_history:{cid}:{uid}", interaction_key(cid,uid))
    await callback.answer("All saved memory has been cleared.", show_alert=True)
    await edit_memory_menu(callback, "<b>Memory Cleared</b>\n\nYour saved memories and local conversation context have been removed.", "back_close")

@router.callback_query(F.data == "memory_back")
async def handle_memory_back(callback):
    if not await authorize_memory_callback(callback): return
    await clear_interaction(callback.message.chat.id, callback.from_user.id); await callback.answer()
    name = html.escape(get_user_display_name(callback.from_user))
    await edit_memory_menu(callback, f"<b>Memory Center</b>\n\nWelcome, {name}.\n\nKeep track of the details and instructions you've asked Sen to remember. Changes here affect how Sen responds to you.", "main")

@router.callback_query(F.data == "memory_close")
async def handle_memory_close(callback):
    if not await authorize_memory_callback(callback): return
    await callback.answer("Closed"); await close_menu(callback)

async def process_memory_text(message, action):
    if not message.text or message.text.startswith("/"): return False
    uid, cid, key = message.from_user.id, message.chat.id, f"memory_list:{message.from_user.id}"
    if action == "add":
        for part in [p.strip()[:200] for p in message.text.split(",,") if p.strip()][:10]:
            if await redis_client.lpos(key, part) is None: await redis_client.rpush(key, part)
        await redis_client.ltrim(key, -25, -1); await clear_interaction(cid, uid)
    elif action == "edit_number":
        parts = message.text.strip().split(" ",1)
        if len(parts) != 2 or not parts[0].isdigit(): return True
        idx = int(parts[0])-1; raw = await redis_client.lrange(key,0,-1)
        if 0 <= idx < len(raw): await redis_client.lset(key, idx, parts[1].strip()[:200])
        await clear_interaction(cid, uid)
    elif action == "forget":
        memories = await get_memories(str(uid))
        for idx in sorted({int(n.strip())-1 for n in message.text.split(",,") if n.strip().isdigit()}, reverse=True):
            if 0 <= idx < len(memories): memories.pop(idx)
        await redis_client.delete(key)
        if memories: await redis_client.rpush(key,*memories)
        await clear_interaction(cid,uid)
    else: return False
    try: await message.delete()
    except Exception: pass
    return True

@router.message(Command("del"))
async def handle_delete(message):
    if not message.from_user or message.from_user.id != OWNER_ID: return
    if message.reply_to_message and BOT_INFO and message.reply_to_message.from_user and message.reply_to_message.from_user.id == BOT_INFO.id:
        try: await bot.delete_message(message.chat.id, message.reply_to_message.message_id)
        except Exception as e: print(f"/del target deletion error: {e}")
    print(f"[/del] user_id={message.from_user.id} chat_id={message.chat.id} ephemeral_message_id={getattr(message,'ephemeral_message_id',None)}")

# ---------------- Free SearXNG search ----------------

def clean_url(url):
    if not url: return ""
    try:
        p = urlsplit(url); return urlunsplit((p.scheme, p.netloc, p.path, "", ""))
    except Exception: return url

def normalize_search_query(query):
    query = re.sub(r"\s+", " ", (query or "")).strip()
    query = re.sub(r"^\s*(?:please\s+)?(?:google\s+)?(?:search|look\s+up|lookup|find(?:\s+out)?|search\s+the\s+web)\s*(?:for|about|on)?\s*", "", query, flags=re.I)
    query = re.sub(r"^\s*(?:please\s+)?(?:send|give|show|fetch|get)\s+me\s+(?:some\s+|the\s+)?", "", query, flags=re.I)
    query = re.sub(r"\s*,?\s*(?:in|inside)\s+(?:collapsible|expandable)\s+(?:sections?|blocks?).*$", "", query, flags=re.I | re.S)
    query = re.sub(r"\s+(?:and\s+)?(?:format|present|display|organize|put)\s+(?:it|them|the\s+results?)\s+.*$", "", query, flags=re.I | re.S)
    return re.sub(r"\s+", " ", query).strip(" ,.-")

def detect_search_intent(text):
    t = re.sub(r"\s+", " ", (text or "")).strip().lower()
    if not t: return False
    explicit_markers = ("search", "google", "look up", "lookup", "find out", "search the web", "browse", "web search", "internet", "online", "news", "headlines", "latest", "newest", "recent", "today", "tonight", "this week", "right now", "currently", "what happened", "who won", "score", "price", "release date", "schedule", "status", "update", "source", "sources")
    if any(marker in t for marker in explicit_markers): return True
    question = re.search(r"\b(?:who|what|when|where|why|how|does|do|did|is|are|can|could|will|has|have)\b", t)
    return bool(question and len(t.split()) >= 5)

async def searx_request(query, category="general", time_range=None, page=1, limit=10):
    params = {"q": query, "format": "json", "categories": category, "language": "en", "pageno": page, "safesearch": 1}
    if time_range: params["time_range"] = time_range
    headers = {"User-Agent": "Mozilla/5.0 (Telegram Sen Bot)", "Accept": "application/json"}
    async with httpx.AsyncClient(follow_redirects=True, timeout=12.0) as client:
        r = await client.get(SEARXNG_URL, params=params, headers=headers)
        if r.status_code != 200: raise RuntimeError(f"SearXNG HTTP {r.status_code}: {r.text[:200]}")
        return (r.json().get("results", []) or [])

async def free_web_search(query, news=False):
    search_query = normalize_search_query(query)
    if not search_query: return ""
    cache_key = search_cache_key(search_query, news)
    try:
        cached = await redis_client.get(cache_key)
        if cached: return cached.decode() if isinstance(cached, bytes) else str(cached)
    except Exception as e: print(f"Web search cache read failure: {e}")
    try:
        if news:
            results = await searx_request(search_query, "news", "day", 1, 8)
            if not results: results = await searx_request(search_query, "news", "week", 1, 8)
            if not results: results = await searx_request(search_query, "general", "month", 1, 8)
        else: results = await searx_request(search_query, "general", None, 1, 8)
        seen, out = set(), []
        for result in results:
            title = (result.get("title") or "").strip(); content = (result.get("content") or result.get("snippet") or "").strip(); url = clean_url(result.get("url", "")); published = (result.get("publishedDate") or result.get("published_date") or "").strip(); source = (result.get("engine") or result.get("source") or "").strip()
            image_url = (result.get("img_src") or result.get("image") or result.get("thumbnail") or "").strip()
            key = url.lower() if url else (title.lower(), content[:120].lower())
            if key in seen or not (title or content or url): continue
            seen.add(key); lines = []
            if title: lines.append(f"Title: {title}")
            if source: lines.append(f"Source: {source}")
            if published: lines.append(f"Published: {published}")
            if content: lines.append(f"Content: {content}")
            if url: lines.append(f"URL: {url}")
            if image_url: lines.append(f"Image: {image_url}")
            out.append("\n".join(lines))
        result_text = "\n\n".join(out)
        if result_text:
            try: await redis_client.set(cache_key, result_text, ex=SEARCH_CACHE_TTL)
            except Exception as e: print(f"Web search cache write failure: {e}")
        return result_text
    except Exception as e:
        print(f"Web contextual search failure: {e}"); return ""

def render_math_markup(text):
    if not text: return text
    protected = []
    def protect(match): protected.append(match.group(0)); return f"\x00MATH{len(protected)-1}\x00"
    text = re.sub(r"<tg-math>.*?</tg-math>|<tg-math-block>.*?</tg-math-block>|<pre>.*?</pre>|<code>.*?</code>", protect, text, flags=re.I | re.S)
    text = re.sub(r"\$\$(.+?)\$\$", lambda m: f"<tg-math-block>{html.escape(m.group(1).strip())}</tg-math-block>", text, flags=re.S)
    text = re.sub(r"\\\[(.+?)\\\]", lambda m: f"<tg-math-block>{html.escape(m.group(1).strip())}</tg-math-block>", text, flags=re.S)
    text = re.sub(r"\\\((.+?)\\\)", lambda m: f"<tg-math>{html.escape(m.group(1).strip())}</tg-math>", text, flags=re.S)
    for i, value in enumerate(protected): text = text.replace(f"\x00MATH{i}\x00", value)
    return text

def sanitize_rich_html(text):
    if not text: return text
    text = re.sub(r"<p\b[^>]*>", "", text, flags=re.I); text = re.sub(r"</p>", "\n\n", text, flags=re.I)
    text = re.sub(r"<h[1-6]\b[^>]*>(.*?)</h[1-6]>", r"<b>\1</b>\n\n", text, flags=re.I | re.S)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I); text = re.sub(r"<div\b[^>]*>", "", text, flags=re.I); text = re.sub(r"</div>", "\n", text, flags=re.I)
    text = re.sub(r"<section\b[^>]*>", "", text, flags=re.I); text = re.sub(r"</section>", "\n", text, flags=re.I); text = re.sub(r"<article\b[^>]*>", "", text, flags=re.I); text = re.sub(r"</article>", "\n", text, flags=re.I)
    text = re.sub(r"<ul\b[^>]*>|</ul>|<ol\b[^>]*>|</ol>", "", text, flags=re.I); text = re.sub(r"<li\b[^>]*>", "• ", text, flags=re.I); text = re.sub(r"</li>", "\n", text, flags=re.I)
    text = re.sub(r"\n{3,}", "\n\n", text); return text.strip()

async def send_ai_response(chat_id, msg_id, response_text, is_private):
    rich = InputRichMessage(html=sanitize_rich_html(render_math_markup(response_text)))
    kwargs = {"chat_id": chat_id, "rich_message": rich}
    if not is_private: kwargs["reply_parameters"] = ReplyParameters(message_id=msg_id)
    return await bot.send_rich_message(**kwargs)

def clean_ai_output(text):
    text = (text or "I didn't receive a response.").strip(); text = re.sub(r"^```(?:html)?\s*", "", text, flags=re.I); text = re.sub(r"\s*```$", "", text)
    return sanitize_rich_html(render_math_markup(text)).strip()

async def download_telegram_media(file_id):
    try:
        file_info = await bot.get_file(file_id)
        stream = await bot.download_file(file_info.file_path)
        return stream.read() if stream else None
    except Exception as e:
        print(f"Telegram media download error: {e}")
        return None

async def get_sticker_input(message):
    sticker = getattr(message, "sticker", None)
    if not sticker: return None, None, ""
    if not sticker.is_animated and not sticker.is_video:
        data = await download_telegram_media(sticker.file_id)
        return data, "image/webp", "Sticker"
    thumbnail = getattr(sticker, "thumbnail", None)
    if thumbnail:
        data = await download_telegram_media(thumbnail.file_id)
        return data, "image/webp", "Animated/video sticker thumbnail"
    return None, None, "Sticker (animated/video; no usable thumbnail)"

def get_replied_video_media(message):
    replied = getattr(message, "reply_to_message", None)
    if not replied: return None
    video = getattr(replied, "video", None)
    if video:
        return video.file_id, getattr(video, "mime_type", None) or "video/mp4", getattr(video, "file_size", None), "Replied-to video"
    video_note = getattr(replied, "video_note", None)
    if video_note:
        return video_note.file_id, "video/mp4", getattr(video_note, "file_size", None), "Replied-to video note"
    document = getattr(replied, "document", None)
    if document:
        mime = (getattr(document, "mime_type", None) or "").lower()
        name = (getattr(document, "file_name", None) or "").lower()
        if mime.startswith("video/") or name.endswith((".mp4", ".mov", ".webm", ".avi", ".mkv", ".mpeg", ".mpg", ".wmv", ".3gp")):
            return document.file_id, mime or "video/mp4", getattr(document, "file_size", None), "Replied-to video file"
    return None

async def generate_gemini_response(contents, config, max_attempts=4):
    retry_delays = (2, 4, 8)
    for attempt in range(max_attempts):
        try:
            return await gemini_client.aio.models.generate_content(model="gemini-3.5-flash-lite", contents=contents, config=config)
        except Exception as e:
            s = str(e).upper()
            retryable = "503" in s or "UNAVAILABLE" in s or "429" in s or "RESOURCE_EXHAUSTED" in s
            if not retryable or attempt >= len(retry_delays): raise
            delay = retry_delays[attempt]
            print(f"Gemini temporary failure ({str(e)[:180]}). Retrying in {delay}s, attempt {attempt + 2}/{max_attempts}.")
            await asyncio.sleep(delay)

@router.message(F.community_chat_added)
async def handle_community_added(message): print(f"Community binding topology registered: {message.chat.id}")
@router.message(F.community_chat_removed)
async def handle_community_removed(message): print(f"Community dropping context safely absorbed: {message.chat.id}")

@router.message(F.text | F.caption | F.voice | F.photo | F.video)
async def handle_conversation(message):
    if message.audio is not None: return
    action = await get_interaction(message.chat.id, message.from_user.id)
    if action and message.text and not message.text.startswith("/"):
        if await process_memory_text(message, action): return

    text = message.text or message.caption or ""
    text_no_html = re.sub(r"<[^>]+>", "", text)
    is_private = message.chat.type == "private"
    bot_username = f"@{BOT_INFO.username}" if BOT_INFO and BOT_INFO.username else ""
    lower = text_no_html.lower()
    tagged = bool(bot_username) and bot_username.lower() in lower
    tagged = tagged or "@gemini" in lower
    reply_to_bot = bool(message.reply_to_message and BOT_INFO and message.reply_to_message.from_user and message.reply_to_message.from_user.id == BOT_INFO.id)
    replied_video_media = get_replied_video_media(message)
    replied_video = bool(replied_video_media)
    has_media_input = bool(message.photo or message.voice or replied_video)

    if message.voice is not None:
        if not (tagged or reply_to_bot): return
    elif message.video is not None:
        if not (replied_video and (tagged or reply_to_bot)): return
    elif not (tagged or reply_to_bot or is_private): return

    prompt = text
    if bot_username: prompt = re.sub(re.escape(bot_username), "", prompt, flags=re.I)
    prompt = re.sub(r"@gemini\b", "", prompt, flags=re.I).strip()
    if reply_to_bot and prompt and MENTION_ONLY_RE.fullmatch(prompt): return
    if not re.sub(r"```(?:\w+)?", "", prompt).strip() and not has_media_input and not message.reply_to_message: return

    uid, cid, mid = message.from_user.id, message.chat.id, message.message_id
    cooldown = f"cooldown:{uid}"
    if await redis_client.exists(cooldown):
        await message.answer("Slow down, request limit reached.", reply_to_message_id=None if is_private else mid); return
    await redis_client.set(cooldown, "1", ex=4)

    replied_context = ""
    if message.reply_to_message:
        replied_context = message.reply_to_message.text or message.reply_to_message.caption or ""
        if message.reply_to_message.sticker:
            replied_context += f"\n[Replied-to message contains a sticker: {message.reply_to_message.sticker.emoji or 'sticker'}]"
        if replied_video:
            replied_context += f"\n[Replied-to message contains {replied_video_media[3]}]"

    media_bytes, media_mime, media_description = None, None, ""
    if message.voice:
        media_bytes = await download_telegram_media(message.voice.file_id)
        media_mime = getattr(message.voice, "mime_type", None) or "audio/ogg"
        media_description = "Voice note"
    elif message.photo:
        media_bytes = await download_telegram_media(message.photo[-1].file_id)
        media_mime = "image/jpeg"
        media_description = "Photo"

    if message.reply_to_message and message.reply_to_message.sticker and not media_bytes:
        media_bytes, media_mime, media_description = await get_sticker_input(message.reply_to_message)

    if replied_video_media and not media_bytes:
        file_id, video_mime, video_size, video_description = replied_video_media
        if video_size and video_size > 20 * 1024 * 1024:
            await message.answer("That video is over Telegram's 20 MB bot download limit, so I can't inspect it.", reply_to_message_id=None if is_private else mid)
            return
        media_bytes = await download_telegram_media(file_id)
        media_mime = video_mime
        media_description = video_description
        if not media_bytes:
            await message.answer("I couldn't download that video to inspect it. Try sending the video again and reply to it.", reply_to_message_id=None if is_private else mid)
            return

    if not prompt and replied_context and not media_bytes: prompt = "What are your thoughts on this?"
    if not (prompt or replied_context or media_bytes): return

    try:
        saved = await get_memories(str(uid))
        history_key = f"chat_history:{cid}:{uid}"
        raw_hist = await redis_client.lrange(history_key, 0, -1)
        history = [x.decode() if isinstance(x, bytes) else str(x) for x in raw_hist]

        use_search = detect_search_intent(prompt)
        news = bool(re.search(r"\b(?:news|headlines|latest|today|breaking|recent)\b", prompt, re.I))
        search_query = normalize_search_query(prompt)
        search_context = await free_web_search(search_query, news=news) if use_search else ""

        context = []
        if replied_context: context.append(f'Message User is Replying To:\n"{replied_context}"')
        if history: context.append("Recent Conversation Context:\n" + "\n".join(history))
        if media_description: context.append(f"Incoming Media: {media_description}")
        if search_context: context.append("Web Search Context:\n" + search_context)
        elif use_search: context.append("Web Search Context:\nA web search was requested, but no usable results were returned. Do not pretend that a search result supports a claim.")
        final_prompt = "\n\n".join(context) + ("\n\n" if context else "") + (prompt or "Process and answer this media input.")

        today = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
        instructions = (f"Today's date is {today}.\n"
            "Never use standard AI pleasantries.\n"
            "Keep casual replies brief, but expand when asked for detail.\n"
            "If the user changes subject, immediately follow the new subject.\n"
            "If joking or sarcastic, match the energy.\n"
            "If you do not know, say exactly: 'I don't have enough details to answer that accurately' without guessing.\n"
            "Do not assume personal details unless explicitly present in the memory list.\n"
            "Return Telegram Rich HTML for sendRichMessage. Use only HTML that Telegram Rich HTML actually supports: <b>, <strong>, <i>, <em>, <u>, <ins>, <s>, <strike>, <del>, <code>, <mark>, <sub>, <sup>, <tg-spoiler>, <a>, <tg-reference>, <tg-emoji>, <table>, <details>, and <summary>.\n"
            "Do NOT use <p>, <h1>-<h6>, <div>, <section>, <article>, <ul>, <ol>, or <li> in Rich HTML. Use normal newlines for paragraphs and <details><summary>...</summary>...</details> for collapsible sections.\n"
            "For mathematical answers, prefer <tg-math-block> for standalone equations and <tg-math> for inline equations. Put raw LaTeX inside those tags. You may also use $$...$$, \\[...\\], or \\(...\\) when useful; the bot converts those delimiters to Telegram math rendering automatically.\n"
            "For current facts, news, prices, schedules, product information, Telegram features, or anything the user asks you to search/look up, use the supplied Web Search Context. Do not invent search results or claim a fact is current without supporting search context.\n"
            "When Web Search Context contains Image: URLs, an image may be useful as supporting media for the search result. If an image is genuinely useful, put exactly one marker [ATTACH_SEARCH_IMAGE: URL] in your response using one of the supplied Image URLs. Do not invent image URLs. Never use this marker for non-search media.\n"
            "Only search-result images may be sent as outgoing media. Do not generate slideshows, collages, presentations, images, videos, audio, or other media. If asked to create media, respond in text instead.\n"
            "Only show source links when the user explicitly asks for sources, citations, links, or URLs. When requested, put them at the very end as a compact rich-text footnote section using <details><summary>Sources</summary>...links...</details>. Otherwise do not display source URLs.\n"
            "Do not use Markdown formatting or Markdown tables.")
        if saved: instructions += "\nUser memory directives:\n" + "\n".join(f"- {x}" for x in saved)
        if search_context: instructions += "\nUse Web Search Context for current facts. Prefer retrieved sources over stale model knowledge."
        if use_search and not search_context: instructions += "\nA search was attempted but returned no usable results. Be explicit about that instead of fabricating sources or pretending to have searched."
        if history: instructions += "\nUse Recent Conversation Context for continuity without repeating it."

        safety = [types.SafetySetting(category=c, threshold="BLOCK_NONE") for c in ("HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")]
        if media_bytes:
            media_part = types.Part.from_bytes(data=media_bytes, mime_type=media_mime)
            contents = [media_part, final_prompt]
        else:
            contents = final_prompt

        response = await generate_gemini_response(contents, types.GenerateContentConfig(system_instruction=instructions, safety_settings=safety))
        response_text = clean_ai_output(response.text)

        search_image_url = None
        image_marker = re.search(r"\[ATTACH_SEARCH_IMAGE:\s*(https?://[^\]\s]+)\]", response_text, re.I)
        if image_marker and search_context:
            candidate = image_marker.group(1).rstrip(".,)")
            supplied_images = set(re.findall(r"Image:\s*(https?://\S+)", search_context, re.I))
            if candidate in supplied_images: search_image_url = candidate
            response_text = re.sub(r"\s*\[ATTACH_SEARCH_IMAGE:\s*https?://[^\]\s]+\]\s*", "\n", response_text, flags=re.I).strip()

        try:
            await send_ai_response(cid, mid, response_text, is_private)
        except Exception as rich_error:
            print(f"Rich response delivery error: {rich_error}")
            fallback = html.unescape(re.sub(r"<[^>]+>", "", response_text)).strip() or "I didn't receive a response."
            await message.answer(fallback, reply_to_message_id=None if is_private else mid)

        if search_image_url:
            try: await bot.send_photo(chat_id=cid, photo=search_image_url, reply_to_message_id=None if is_private else mid)
            except Exception as media_error: print(f"Search result image delivery error: {media_error}")

        clean_history = html.unescape(re.sub(r"<[^>]+>", "", response_text)).strip()
        await redis_client.rpush(history_key, f"User: {prompt or media_description or 'Media'}", f"Bot: {clean_history}")
        await redis_client.ltrim(history_key, -10, -1)
    except Exception as e:
        print(f"Gemini AI processing error: {e}")
        s = str(e).upper()
        if "503" in s or "UNAVAILABLE" in s:
            error = "Whoa, I'm getting a little overwhelmed! Let me catch my breath. Try again in about 15 seconds."
        elif "429" in s or "RESOURCE_EXHAUSTED" in s:
            error = "Whoa, I'm getting a little overwhelmed! Let me catch my breath. Try again in about 10 seconds."
        else:
            error = "I ran into an unexpected problem processing that request."
        await message.answer(error, reply_to_message_id=None if is_private else mid)

async def health_check(request): return web.json_response({"status":"ok","bot":BOT_INFO.username if BOT_INFO else None})

async def configure_commands():
    group = [BotCommand(command="memories", description="Open your private memory menu", is_ephemeral=True), BotCommand(command="del", description="Delete a bot message", is_ephemeral=True)]
    private = [BotCommand(command="memories", description="Manage your instructed memories"), BotCommand(command="del", description="Delete a bot message")]
    try: await bot.delete_my_commands(scope=BotCommandScopeAllChatAdministrators())
    except Exception as e: print(f"Could not clear administrator command scope: {e}")
    await bot.set_my_commands(group, scope=BotCommandScopeAllGroupChats()); await bot.set_my_commands(private, scope=BotCommandScopeAllPrivateChats()); print("Configured group commands: /memories=ephemeral /del=ephemeral")
    try:
        g = await bot.get_my_commands(scope=BotCommandScopeAllGroupChats()); print("Telegram group commands: " + str([(x.command,getattr(x,"is_ephemeral",None)) for x in g]))
    except Exception as e: print(f"Could not verify Telegram commands: {e}")

async def main():
    global BOT_INFO
    BOT_INFO = await bot.get_me(); print(f"Logged in successfully as @{BOT_INFO.username}"); await configure_commands()
    app = web.Application(); app.router.add_get("/", health_check); app.router.add_get("/health", health_check); runner = web.AppRunner(app); await runner.setup(); port = int(os.environ.get("PORT","8080")); site = web.TCPSite(runner, "0.0.0.0", port); await site.start(); print(f"Operational check dashboard running on port {port}")
    try: await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e: print(f"Non-critical webhook clearance notice: {e}")
    try: await dp.start_polling(bot)
    finally: await bot.session.close(); await redis_client.aclose(); await runner.cleanup()

if __name__ == "__main__": asyncio.run(main())
