import asyncio
import os
import re
from datetime import datetime, timezone

import httpx
import redis.asyncio as redis
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import MessageEntityType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message, MessageEntity
from aiogram.utils.formatting import Bold, Text, as_list
from aiohttp import web
from google import genai
from google.genai import types
from redis.exceptions import RedisError

# ==========================================
# Environment & Config
# ==========================================
redis_url = os.environ.get("REDIS_URL")
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

if redis_url.startswith("rediss://"):
    redis_client = redis.from_url(redis_url, ssl_cert_reqs=None)
else:
    redis_client = redis.from_url(redis_url)

API_TOKEN = os.getenv("BOT_TOKEN")
SEARXNG_URL = os.getenv("SEARXNG_URL", "https://railway.app")

bot = Bot(token=API_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)
BOT_INFO = None

gemini_api_key = os.getenv("GEMINI_API_KEY")
if not gemini_api_key:
    raise ValueError("CRITICAL CONFIGURATION ERROR: 'GEMINI_API_KEY' missing.")

gemini_client = genai.Client(api_key=gemini_api_key)
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
app = web.Application()


# ==========================================
# Native Block Parser using MessageEntities
# ==========================================
def parse_text_to_entities(text: str) -> tuple[str, list[MessageEntity]]:
    full_text = ""
    text_entities = []
    lines = text.split("\n")
    in_code_block = False
    code_buffer = []

    for line in lines:
        if line.strip().startswith("```"):
            if in_code_block:
                in_code_block = False
                clean_code = "\n".join(code_buffer)
                start_offset = len(full_text)
                display_text = f"Code Snippet:\n{clean_code}\n\n"
                full_text += display_text
                text_entities.append(
                    MessageEntity(
                        type=MessageEntityType.PRE,
                        offset=start_offset + 14,
                        length=len(clean_code),
                        language="python",
                    )
                )
                code_buffer.clear()
            else:
                in_code_block = True
            continue

        if in_code_block:
            code_buffer.append(line)
            continue

        if line.strip().startswith("$$") and line.strip().endswith("$$"):
            expr = line.strip().strip("$")
            start_offset = len(full_text)
            full_text += f"{expr}\n\n"
            text_entities.append(
                MessageEntity(
                    type=MessageEntityType.CODE, offset=start_offset, length=len(expr)
                )
            )
            continue

        heading_match = re.match(r"^(#{1,3})\s+(.*)", line)
        if heading_match:
            content = heading_match.group(2).strip()
            start_offset = len(full_text)
            full_text += f"{content}\n\n"
            text_entities.append(
                MessageEntity(
                    type=MessageEntityType.BOLD,
                    offset=start_offset,
                    length=len(content),
                )
            )
            text_entities.append(
                MessageEntity(
                    type=MessageEntityType.UNDERLINE,
                    offset=start_offset,
                    length=len(content),
                )
            )
            continue

        if line.strip().startswith("|") and line.strip().endswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.match(r"^\-+$", c) for c in cells):
                continue
            row_str = " | ".join(cells)
            start_offset = len(full_text)
            full_text += f"| {row_str} |\n"
            text_entities.append(
                MessageEntity(
                    type=MessageEntityType.PRE,
                    offset=start_offset,
                    length=len(row_str) + 4,
                    language="text",
                )
            )
            continue

        list_match = re.match(r"^(\d+\.|\-|\*)\s+(.*)", line)
        if list_match:
            item_content = list_match.group(2).strip()
            bullet_prefix = (
                "• "
                if not list_match.group(1).replace(".", "").isdigit()
                else f"{list_match.group(1)} "
            )
            full_text += f"{bullet_prefix}{item_content}\n"
            continue

        if not line.strip():
            if not full_text.endswith("\n\n") and full_text:
                full_text += "\n"
            continue

        full_text += f"{line}\n"

    return full_text.strip(), text_entities


# ==========================================
# Helpers
# ==========================================
async def free_web_search(query: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        params = {"q": query, "format": "json"}
        async with httpx.AsyncClient() as client:
            res = await client.get(
                SEARXNG_URL, params=params, headers=headers, timeout=8.0
            )
            if res.status_code == 200:
                results = res.json().get("results", [])[:15]
                snippets = []
                for item in results:
                    title = item.get("title", "")
                    content = item.get("content", "")
                    url = item.get("url", "")
                    if title or content:
                        snippets.append(
                            f"Title: {title}\nContent: {content}\nURL: {url}"
                        )
                if snippets:
                    return "\n\n".join(snippets)
    except Exception as e:  # noqa: BLE001
        print(f"SearXNG error: {e}")
    return ""


async def auto_delete_message(
    chat_id: int, bot_msg_id: int, user_msg_id: int, delay: int
):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=bot_msg_id)
    except TelegramAPIError:
        pass
    try:
        await bot.delete_message(chat_id=chat_id, message_id=user_msg_id)
    except TelegramAPIError:
        pass


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
        cached_id = await redis_client.get(f"audio_cache:{key}")
        reply_to = None if is_private else msg_id

        async def attempt_send(audio_payload):
            if isinstance(audio_payload, str):
                return await bot.send_audio(
                    chat_id=chat_id,
                    audio=audio_payload,
                    title=title,
                    performer=performer,
                    reply_to_message_id=reply_to,
                )
            else:
                return await bot.send_audio(
                    chat_id=chat_id,
                    audio=FSInputFile(audio_payload),
                    title=title,
                    performer=performer,
                    reply_to_message_id=reply_to,
                )

        if cached_id:
            try:
                await attempt_send(
                    cached_id.decode("utf-8")
                    if isinstance(cached_id, bytes)
                    else cached_id
                )
            except Exception as e:  # noqa: BLE001
                if "message to be replied not found" in str(e).lower():
                    await bot.send_audio(
                        chat_id=chat_id,
                        audio=(
                            cached_id.decode("utf-8")
                            if isinstance(cached_id, bytes)
                            else cached_id
                        ),
                        title=title,
                        performer=performer,
                    )
                else:
                    raise
        elif os.path.exists(file_path):
            msg = await attempt_send(file_path)
            if msg and msg.audio and msg.audio.file_id:
                await redis_client.set(f"audio_cache:{key}", msg.audio.file_id)
    except Exception as send_err:  # noqa: BLE001
        print(f"Error sending audio ({key}): {send_err}")


async def send_formatted_memories_as_blocks(message: Message, user_id_str: str):
    """Builds the text using pure Aiogram formatting objects, bypassing parse_mode entirely."""
    try:
        raw_items = await redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
        if not raw_items:
            content = Text("Your memory list is currently empty.")
        else:
            formatted_items = []
            for i, item in enumerate(raw_items):
                mem_text = item.decode("utf-8") if isinstance(item, bytes) else item
                formatted_items.append(Text(Bold(f"[{i+1}] "), mem_text))

            content = as_list(
                Bold("Active Memory Directives:"), "", *formatted_items, sep="\n\n"
            )

        kwargs = content.as_kwargs()

        if message.chat.type == "private":
            sent_msg = await message.answer(**kwargs)
        else:
            sent_msg = await message.answer(
                **kwargs, reply_to_message_id=message.message_id
            )

        if sent_msg:
            asyncio.create_task(
                auto_delete_message(
                    message.chat.id, sent_msg.message_id, message.message_id, 60
                )
            )
    except Exception as e:  # noqa: BLE001
        print(f"Error rendering structured memory blocks: {e}")
        await message.answer("⚠️ Error loading active memory layout.")


# ==========================================
# Handlers
# ==========================================


@router.message(Command("delete", "del"))
async def handle_delete(message: Message):
    if message.from_user.id == OWNER_ID:
        chat_id = message.chat.id
        reply_msg = message.reply_to_message
        if reply_msg and BOT_INFO and reply_msg.from_user.id == BOT_INFO.id:
            try:
                await bot.delete_message(chat_id, reply_msg.message_id)
            except TelegramAPIError:
                pass
        try:
            await bot.delete_message(chat_id, message.message_id)
        except TelegramAPIError:
            pass


@router.message(Command("help", "commands"))
async def handle_help(message: Message):
    content = as_list(
        Bold("Sen Bot Command Hub"),
        "",
        "• remember [item],, [item2] - Adds items to memory",
        "• what do you remember - Displays rules in a formatted list",
        "• edit [number] [new fact] - Edits a specific rule",
        "• forget [number],, [number2] - Removes memories",
        "• forget all - Clears all memory",
        sep="\n",
    )
    kwargs = content.as_kwargs()
    if message.chat.type == "private":
        sent_msg = await message.answer(**kwargs)
    else:
        sent_msg = await message.answer(
            **kwargs, reply_to_message_id=message.message_id
        )

    if sent_msg:
        asyncio.create_task(
            auto_delete_message(
                message.chat.id, sent_msg.message_id, message.message_id, 60
            )
        )


def text_in(options: set):
    return lambda message: message.text and message.text.lower() in options


def text_startswith(prefix: str):
    return lambda message: message.text and message.text.lower().startswith(prefix)


@router.message(text_in({"what do you remember", "how do you remember"}))
async def handle_what_remember(message: Message):
    await send_formatted_memories_as_blocks(message, str(message.from_user.id))


@router.message(text_startswith("remember "))
async def handle_remember(message: Message):
    user_id_str = str(message.from_user.id)
    parts = [p.strip()[:200] for p in message.text.strip()[9:].split(",,") if p.strip()]
    for part in parts[:10]:
        try:
            if await redis_client.lpos(f"memory_list:{user_id_str}", part) is None:
                await redis_client.rpush(f"memory_list:{user_id_str}", part)
        except RedisError:
            pass
    await redis_client.ltrim(f"memory_list:{user_id_str}", -25, -1)
    await send_formatted_memories_as_blocks(message, user_id_str)


@router.message(text_startswith("edit "))
async def handle_edit(message: Message):
    user_id_str = str(message.from_user.id)
    parts = message.text.strip()[5:].strip().split(" ", 1)
    if len(parts) == 2 and parts[0].isdigit():
        idx, new_val = int(parts[0]) - 1, parts[1].strip()
        raw_items = await redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
        if 0 <= idx < len(raw_items):
            await redis_client.lset(f"memory_list:{user_id_str}", idx, new_val)
            await send_formatted_memories_as_blocks(message, user_id_str)
            return

    sent_msg = await message.answer("Usage: edit [number] [new text]")
    if sent_msg:
        asyncio.create_task(
            auto_delete_message(
                message.chat.id, sent_msg.message_id, message.message_id, 10
            )
        )


@router.message(F.text.lower() == "forget all")
async def handle_forget_all(message: Message):
    user_id_str = str(message.from_user.id)
    await redis_client.delete(
        f"memory_list:{user_id_str}", f"chat_history:{message.chat.id}:{user_id_str}"
    )
    reply_id = None if message.chat.type == "private" else message.message_id
    sent_msg = await bot.send_message(
        chat_id=message.chat.id,
        text="Cleared all your saved memories.",
        reply_to_message_id=reply_id,
    )
    if sent_msg:
        asyncio.create_task(
            auto_delete_message(
                message.chat.id, sent_msg.message_id, message.message_id, 10
            )
        )


@router.message(text_startswith("forget "))
async def handle_forget(message: Message):
    user_id_str = str(message.from_user.id)
    try:
        indices = [
            int(n.strip()) - 1
            for n in message.text.strip()[7:].split(",,")
            if n.strip().isdigit()
        ]
        raw_items = await redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
        if raw_items and indices:
            memories = [
                i.decode("utf-8") if isinstance(i, bytes) else i for i in raw_items
            ]
            for i in sorted(set(indices), reverse=True):
                if 0 <= i < len(memories):
                    memories.pop(i)
            await redis_client.delete(f"memory_list:{user_id_str}")
            if memories:
                await redis_client.rpush(f"memory_list:{user_id_str}", *memories)
    except RedisError:
        pass
    await send_formatted_memories_as_blocks(message, user_id_str)


# ==========================================
# Core Engine
# ==========================================


@router.message(F.text | F.caption | F.voice | F.audio)
async def handle_conversation(message: Message):
    text = message.text or message.caption or ""
    text_no_code = re.sub(r"(?s).*?", "", text)
    text_no_code = re.sub(r"(?s).*?", "", text_no_code)

    if re.search(r"\bsen\b", text_no_code, re.IGNORECASE):
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
    if re.search(r"\bmagic(?:al|ally)?\b", text_no_code, re.IGNORECASE):
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

    bot_username = f"@{BOT_INFO.username}" if BOT_INFO else ""
    is_tagged = (
        bot_username and bot_username.lower() in text_no_code.lower()
    ) or "@gemini" in text_no_code.lower()
    is_reply_to_bot = bool(
        message.reply_to_message
        and BOT_INFO
        and message.reply_to_message.from_user.id == BOT_INFO.id
    )
    is_private = message.chat.type == "private"

    if (
        is_tagged
        or is_reply_to_bot
        or is_private
        or message.content_type in ["voice", "audio"]
    ):
        user_id_str = str(message.from_user.id)
        chat_id = message.chat.id
        msg_id = message.message_id

        clean_prompt = (
            text.replace(bot_username, "")
            .replace(bot_username.lower(), "")
            .replace("@gemini", "")
            .replace("@Gemini", "")
            .strip()
        )
        cooldown_key = f"cooldown:{user_id_str}"
        reply_id = None if is_private else msg_id

        if await redis_client.exists(cooldown_key):
            await bot.send_message(
                chat_id=chat_id,
                text="Slow down, request limit reached.",
                reply_to_message_id=reply_id,
            )
            return
        await redis_client.setex(cooldown_key, 4, "1")

        if message.reply_to_message:
            replied_context = (
                message.reply_to_message.text or message.reply_to_message.caption or ""
            )
        else:
            replied_context = ""

        audio_bytes = None
        audio_mime = "audio/ogg"

        if message.content_type in ["voice", "audio"]:
            audio_obj = message.voice or message.audio
            if audio_obj:
                file_info = await bot.get_file(audio_obj.file_id)
                audio_stream = await bot.download_file(file_info.file_path)
                if audio_stream:
                    audio_bytes = audio_stream.read()
                if getattr(audio_obj, "mime_type", None):
                    audio_mime = audio_obj.mime_type

        if not clean_prompt and replied_context:
            clean_prompt = "What are your thoughts on this?"

        if clean_prompt or replied_context or audio_bytes:
            try:
                raw_mem = await redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
                saved_facts = [
                    i.decode("utf-8") if isinstance(i, bytes) else i for i in raw_mem
                ]

                history_key = f"chat_history:{chat_id}:{user_id_str}"
                raw_hist = await redis_client.lrange(history_key, 0, -1)
                chat_history = [
                    h.decode("utf-8") if isinstance(h, bytes) else h for h in raw_hist
                ]

                search_context = (
                    await free_web_search(clean_prompt)
                    if any(
                        w in clean_prompt.lower()
                        for w in ("search", "google", "look up", "find")
                    )
                    else ""
                )

                context_parts = []
                if replied_context:
                    context_parts.append(
                        f"Message User is Replying To:\n{replied_context}"
                    )
                if chat_history:
                    context_parts.append(
                        "Recent Conversation Context:\n" + "\n".join(chat_history)
                    )
                if search_context:
                    context_parts.append(f"Web Search Context:\n{search_context}")

                final_prompt = (
                    clean_prompt
                    if clean_prompt
                    else "Process and answer this voice note."
                )
                if context_parts:
                    final_prompt = (
                        "\n\n".join(context_parts)
                        + f"\n\nUser Question: {final_prompt}"
                    )

                today_str = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
                bot_instructions = (
                    f"Today's date is {today_str}. Keep responses structural using double line-breaks to separate ideas. "
                    "Never use standard AI pleasantries. Do not start responses with 'As an AI' or end with generic offers for help. "
                    "Keep casual replies brief, but dynamically expand your response length when explicitly asked for details. "
                )

                if saved_facts:
                    bot_instructions += (
                        "\n\nYou must strictly follow these overrides:\n"
                        + "\n".join(f"- {f}" for f in saved_facts)
                    )

                safety_overrides = [
                    types.SafetySetting(category=c, threshold="BLOCK_NONE")
                    for c in [
                        "HARM_CATEGORY_HATE_SPEECH",
                        "HARM_CATEGORY_HARASSMENT",
                        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                        "HARM_CATEGORY_DANGEROUS_CONTENT",
                    ]
                ]

                if audio_bytes:
                    contents = [
                        types.Part.from_bytes(data=audio_bytes, mime_type=audio_mime),
                        final_prompt,
                    ]
                    response = await gemini_client.aio.models.generate_content(
                        model="gemini-3.5-flash-lite",
                        contents=contents,
                        config=types.GenerateContentConfig(
                            system_instruction=bot_instructions,
                            safety_settings=safety_overrides,
                        ),
                    )
                else:
                    chat = gemini_client.aio.chats.create(
                        model="gemini-3.5-flash-lite",
                        config=types.GenerateContentConfig(
                            system_instruction=bot_instructions,
                            safety_settings=safety_overrides,
                        ),
                    )
                    response = await chat.send_message(final_prompt)

                raw_text = response.text or ""
                final_text, text_entities = parse_text_to_entities(raw_text)

                await bot.send_message(
                    chat_id=chat_id,
                    text=final_text,
                    entities=text_entities,
                    reply_to_message_id=reply_id,
                )
                await redis_client.rpush(
                    history_key,
                    f"User: {clean_prompt or 'Voice Note'}",
                    f"Bot: {raw_text}",
                )
                await redis_client.ltrim(history_key, -10, -1)

            except RedisError:
                await bot.send_message(
                    chat_id=chat_id,
                    text="Whoa, I'm getting a little overwhelmed! Let me catch my breath.",
                    reply_to_message_id=reply_id,
                )


# ==========================================
# Web Hooks & App Initialization
# ==========================================


async def health_check(request):
    return web.Response(text="200 OK - Bot is running.", status=200)


app.router.add_get("/", health_check)
app.router.add_get("/health", health_check)


async def on_startup(app_instance):
    global BOT_INFO
    try:
        print("Clearing conflicting webhooks from Telegram servers...")
        await bot.delete_webhook(drop_pending_updates=True)

        BOT_INFO = await bot.get_me()
        print(f"Bot authenticated as {BOT_INFO.username}")
    except Exception as e:  # noqa: BLE001
        print(f"Failed to fetch bot info: {e}")

    asyncio.create_task(dp.start_polling(bot))


async def on_shutdown(app_instance):
    await bot.session.close()
    await redis_client.aclose()


app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
