import os
import re
import asyncio
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, Header, HTTPException, BackgroundTasks
from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ContentType, ChatType
from aiogram.filters import Command
from aiogram.types import Message, Update, LinkPreviewOptions, FSInputFile
from aiogram.exceptions import TelegramBadRequest, TelegramAPIError

import redis.asyncio as redis
import httpx
from google import genai
from google.genai import types as genai_types
from telegramify_markdown import convert

# ==========================================
# Environment & Redis Config
# ==========================================
redis_url = os.environ.get("REDIS_URL")
if not redis_url:
    host = os.environ.get("REDISHOST", "localhost")
    port = os.environ.get("REDISPORT", "6379")
    password = os.environ.get("REDISPASSWORD", "")
    redis_url = f"redis://default:{password}@{host}:{port}" if password else f"redis://{host}:{port}"

if "upstash" in redis_url.lower() and redis_url.startswith("redis://"):
    redis_url = redis_url.replace("redis://", "rediss://", 1)

if redis_url.startswith("rediss://"):
    redis_client = redis.from_url(redis_url, ssl_cert_reqs=None)
else:
    redis_client = redis.from_url(redis_url)

API_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
SEARXNG_URL = os.getenv("SEARXNG_URL", "https://searxng-railway-production-3252.up.railway.app/search")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

gemini_api_key = os.getenv("GEMINI_API_KEY")
if not gemini_api_key:
    raise ValueError("CRITICAL CONFIGURATION ERROR: 'GEMINI_API_KEY' missing.")

gemini_client = genai.Client(api_key=gemini_api_key)

# ==========================================
# Aiogram Bot, Dispatcher & Router Setup
# ==========================================
bot = Bot(token=API_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

BOT_INFO = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global BOT_INFO
    try:
        BOT_INFO = await bot.get_me()
    except Exception as e:
        print(f"Failed to fetch bot info: {e}")
    
    yield
    
    try:
        await bot.session.close()
        await redis_client.aclose()
    except Exception as e:
        print(f"Error during shutdown: {e}")

app = FastAPI(lifespan=lifespan)

# ==========================================
# Helper Utilities
# ==========================================
def balance_codeblocks(text: str) -> str:
    """Auto-closes unclosed codeblocks to prevent broken Telegram formatting."""
    if text.count("```") % 2 != 0:
        return text + "\n```"
    return text

async def free_web_search(query: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        params = {"q": query, "format": "json"}
        async with httpx.AsyncClient() as client:
            res = await client.get(SEARXNG_URL, params=params, headers=headers, timeout=8.0)
            if res.status_code == 200:
                results = res.json().get("results", [])[:3]
                snippets = []
                for item in results:
                    title = item.get('title', '')
                    content = item.get('content', '')
                    url = item.get('url', '')
                    if title or content:
                        snippets.append(f"Title: {title}\nContent: {content}\nURL: {url}")
                if snippets: return "\n\n".join(snippets)
    except Exception as e:
        print(f"SearXNG error: {e}")

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post("https://html.duckduckgo.com/html/", data={"q": query}, headers=headers, timeout=8.0)
            raw = re.findall(r'<a class="result__snippet[^">]*>(.*?)</a>', res.text, re.DOTALL)
            urls = re.findall(r'href="(https?://[^"]+)"', res.text)
            clean = []
            for i, snippet in enumerate(raw[:3]):
                text_clean = re.sub(r'<[^>]+>', '', snippet).strip()
                link = urls[i] if i < len(urls) else ""
                if text_clean:
                    clean.append(f"Content: {text_clean}\nURL: {link}")
            if clean: return "\n\n".join(clean)
    except Exception as e:
        print(f"DuckDuckGo error: {e}")
    return ""

async def get_formatted_memories(user_id_str: str) -> str:
    try:
        raw_items = await redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
        if not raw_items: return "Your memory list is currently empty."
        memories = [item.decode('utf-8') if isinstance(item, bytes) else item for item in raw_items]
        formatted_list = "\n".join(f"{i+1}. {mem}" for i, mem in enumerate(memories))
        return f"**Active Memory Directives:**\n----------\n\n{formatted_list}"
    except Exception as e:
        print(f"Error fetching memory list format: {e}")
        return "Could not retrieve memory list."

async def collapse_message(bot_instance: Bot, chat_id: int, message_id: int, delay: int):
    """Waits for the delay (in seconds), then edits the message to a collapsed state."""
    await asyncio.sleep(delay)
    collapsed_text = "> Active Memories Collapsed"
    clean_text, text_entities = convert(collapsed_text)
    try:
        await bot_instance.edit_message_text(
            chat_id=chat_id, 
            message_id=message_id, 
            text=clean_text, 
            entities=text_entities
        )
    except Exception as e:
        print(f"Error collapsing memory list: {e}")

async def send_audio_track(bot_instance: Bot, chat_id: int, msg_id: int, key: str, file_path: str, title: str, performer: str, is_private: bool):
    try:
        reply_id = None if is_private else msg_id
        cached_id = await redis_client.get(f"audio_cache:{key}")

        async def attempt_send(audio_payload):
            try:
                return await bot_instance.send_audio(
                    chat_id=chat_id,
                    audio=audio_payload,
                    title=title,
                    performer=performer,
                    reply_to_message_id=reply_id,
                    request_timeout=60
                )
            except TelegramBadRequest as e:
                if "message to be replied not found" in str(e).lower():
                    return await bot_instance.send_audio(
                        chat_id=chat_id,
                        audio=audio_payload,
                        title=title,
                        performer=performer,
                        request_timeout=60
                    )
                raise e

        if cached_id:
            audio_id = cached_id.decode('utf-8') if isinstance(cached_id, bytes) else cached_id
            await attempt_send(audio_id)
        elif os.path.exists(file_path):
            audio_file = FSInputFile(file_path)
            msg = await attempt_send(audio_file)
            if msg and msg.audio and msg.audio.file_id:
                await redis_client.set(f"audio_cache:{key}", msg.audio.file_id)
        else:
            print(f"Audio file not found: {file_path}")
    except Exception as send_err:
        print(f"Error sending audio ({key}): {send_err}")

# ==========================================
# FastAPI Webhook Route
# ==========================================
@app.get("/")
def home_check():
    return {"status": "ok", "message": "Aiogram webhook server is running."}

@app.post("/webhook")
async def handle_webhook(
    request: Request, 
    background_tasks: BackgroundTasks, 
    x_telegram_bot_api_secret_token: str = Header(None)
):
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Unauthorized")

    json_data = await request.json()
    try:
        update = Update.model_validate(json_data, context={"bot": bot})
        await dp.feed_update(bot, update, background_tasks=background_tasks)
    except Exception as e:
        print(f"Webhook update processing error: {e}")
    return Response(status_code=200)

# ==========================================
# Aiogram Handlers & Magic Filters
# ==========================================

# Owner Message Deletion Handler
@router.message(Command("delete", "del"))
async def handle_delete_cmd(message: Message):
    if message.from_user and message.from_user.id == OWNER_ID:
        if message.reply_to_message and BOT_INFO and message.reply_to_message.from_user.id == BOT_INFO.id:
            try:
                await message.bot.delete_message(chat_id=message.chat.id, message_id=message.reply_to_message.message_id)
            except Exception:
                pass
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
        except Exception:
            pass

# Main Interactive Handler
@router.message(F.content_type.in_({
    ContentType.TEXT, ContentType.PHOTO, ContentType.AUDIO, 
    ContentType.VIDEO, ContentType.DOCUMENT, ContentType.VOICE
}))
async def process_incoming_message(message: Message, background_tasks: BackgroundTasks):
    text = message.text or message.caption or ""
    
    text_no_code = re.sub(r'(?s)```.*?```', '', text)
    text_no_code = re.sub(r'(?s)`.*?`', '', text_no_code)

    user_id = message.from_user.id if message.from_user else 0
    user_id_str = str(user_id)
    chat_id = message.chat.id
    msg_id = message.message_id
    is_private = message.chat.type == ChatType.PRIVATE

    bot_username = f"@{BOT_INFO.username}" if BOT_INFO else ""
    
    is_tagged = (bot_username and bot_username.lower() in text_no_code.lower()) or "@gemini" in text_no_code.lower()
    is_reply_to_bot = bool(message.reply_to_message and BOT_INFO and message.reply_to_message.from_user and message.reply_to_message.from_user.id == BOT_INFO.id)
    is_voice_or_audio = message.content_type in [ContentType.VOICE, ContentType.AUDIO]

    if is_tagged or is_reply_to_bot or is_private or is_voice_or_audio:
        clean_prompt = text.replace(bot_username, "").replace(bot_username.lower(), "").replace("@gemini", "").replace("@Gemini", "").strip()
        normalized_prompt = clean_prompt.rstrip("?").lower()

        # Rate Limit Cooldown
        cooldown_key = f"cooldown:{user_id_str}"
        if await redis_client.exists(cooldown_key):
            warn_msg = "Slow the fuck down, this ain't a god damn fuck-fest"
            if is_private:
                await message.answer(warn_msg)
            else:
                await message.reply(warn_msg)
            return

        await redis_client.setex(cooldown_key, 4, "1")

        # Command: help / commands
        if normalized_prompt in ["help", "commands"]:
            help_text = (
                "**Remember rule:**\n  remember [item],, [item2] - adds items to memory list (separate multiple with double commas).\n\n"
                "**What do you remember:**\n  displays your rules in a numbered list format.\n\n"
                "**Edit #:**\n  edit [number] [new fact] - edits a specific rule.\n\n"
                "**Forget #:**\n  forget [number],, [number2] - removes specific memories (separate multiple with double commas).\n\n"
                "**Forget all:**\n  clears all memory."
            )
            clean_text, text_entities = convert(help_text)
            if is_private:
                await message.answer(clean_text, entities=text_entities)
            else:
                await message.reply(clean_text, entities=text_entities)
            return

        # Command: what do you remember
        if normalized_prompt in ["what do you remember", "how do you remember"]:
            msg_text = await get_formatted_memories(user_id_str)
            clean_text, text_entities = convert(msg_text)
            if is_private:
                sent_msg = await message.answer(clean_text, entities=text_entities)
            else:
                sent_msg = await message.reply(clean_text, entities=text_entities)
            
            if sent_msg:
                background_tasks.add_task(collapse_message, message.bot, chat_id, sent_msg.message_id, 60)
            return

        # Command: remember
        if clean_prompt.lower().startswith("remember "):
            parts = [p.strip()[:200] for p in clean_prompt[9:].split(",,") if p.strip()]
            for part in parts[:10]:
                try:
                    pos = await redis_client.lpos(f"memory_list:{user_id_str}", part)
                    if pos is None:
                        await redis_client.rpush(f"memory_list:{user_id_str}", part)
                except Exception:
                    pass
            await redis_client.ltrim(f"memory_list:{user_id_str}", -25, -1)
            
            msg_text = await get_formatted_memories(user_id_str)
            clean_text, text_entities = convert(msg_text)
            if is_private:
                sent_msg = await message.answer(clean_text, entities=text_entities)
            else:
                sent_msg = await message.reply(clean_text, entities=text_entities)
            
            if sent_msg:
                background_tasks.add_task(collapse_message, message.bot, chat_id, sent_msg.message_id, 60)
            return

        # Command: edit
        if clean_prompt.lower().startswith("edit "):
            parts = clean_prompt[5:].strip().split(" ", 1)
            if len(parts) == 2 and parts[0].isdigit():
                idx, new_val = int(parts[0]) - 1, parts[1].strip()
                raw_items = await redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
                if 0 <= idx < len(raw_items):
                    await redis_client.lset(f"memory_list:{user_id_str}", idx, new_val)
                    msg_text = await get_formatted_memories(user_id_str)
                else:
                    msg_text = "Invalid memory number.\n\n" + await get_formatted_memories(user_id_str)
            else:
                msg_text = "Usage: edit [number] [new text]"
            
            clean_text, text_entities = convert(msg_text)
            if is_private:
                sent_msg = await message.answer(clean_text, entities=text_entities)
            else:
                sent_msg = await message.reply(clean_text, entities=text_entities)
            
            if sent_msg and "Active Memory Directives" in clean_text:
                background_tasks.add_task(collapse_message, message.bot, chat_id, sent_msg.message_id, 60)
            return

        # Command: forget all
        if normalized_prompt == "forget all":
            await redis_client.delete(f"memory_list:{user_id_str}", f"chat_history:{chat_id}:{user_id_str}")
            msg_text = "Cleared all your saved memories."
            clean_text, text_entities = convert(msg_text)
            if is_private:
                await message.answer(clean_text, entities=text_entities)
            else:
                await message.reply(clean_text, entities=text_entities)
            return

        # Command: forget
        if clean_prompt.lower().startswith("forget "):
            try:
                indices = [int(n.strip()) - 1 for n in clean_prompt[7:].split(",,") if n.strip().isdigit()]
                raw_items = await redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
                if raw_items and indices:
                    memories = [i.decode('utf-8') if isinstance(i, bytes) else i for i in raw_items]
                    for i in sorted(set(indices), reverse=True):
                        if 0 <= i < len(memories):
                            memories.pop(i)
                    await redis_client.delete(f"memory_list:{user_id_str}")
                    if memories:
                        await redis_client.rpush(f"memory_list:{user_id_str}", *memories)
                    msg_text = await get_formatted_memories(user_id_str)
                else:
                    msg_text = "No valid memory numbers specified.\n\n" + await get_formatted_memories(user_id_str)
            except Exception:
                msg_text = "Error removing memory."
            
            clean_text, text_entities = convert(msg_text)
            if is_private:
                sent_msg = await message.answer(clean_text, entities=text_entities)
            else:
                sent_msg = await message.reply(clean_text, entities=text_entities)
            
            if sent_msg and "Active Memory Directives" in clean_text:
                background_tasks.add_task(collapse_message, message.bot, chat_id, sent_msg.message_id, 60)
            return

        # Context & Gemini Processing
        replied = message.reply_to_message
        replied_context = replied.text or replied.caption or "" if replied else ""
        
        audio_bytes = None
        audio_mime = "audio/ogg"
        if is_voice_or_audio:
            audio_obj = message.voice or message.audio
            if audio_obj:
                file_info = await message.bot.get_file(audio_obj.file_id)
                downloaded_file = await message.bot.download_file(file_info.file_path)
                audio_bytes = downloaded_file.read()
                if hasattr(audio_obj, 'mime_type') and audio_obj.mime_type:
                    audio_mime = audio_obj.mime_type

        if not clean_prompt and replied_context:
            clean_prompt = "What are your thoughts on this?"

        if clean_prompt or replied_context or audio_bytes:
            try:
                raw_mem = await redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
                saved_facts = [i.decode('utf-8') if isinstance(i, bytes) else i for i in raw_mem]

                history_key = f"chat_history:{chat_id}:{user_id_str}"
                raw_hist = await redis_client.lrange(history_key, 0, -1)
                chat_history = [h.decode('utf-8') if isinstance(h, bytes) else h for h in raw_hist]

                search_context = await free_web_search(clean_prompt) if clean_prompt else ""

                context_parts = []
                if replied_context:
                    context_parts.append(f"Message User is Replying To:\n\"{replied_context}\"")
                if chat_history:
                    context_parts.append("Recent Conversation Context:\n" + "\n".join(chat_history))
                if search_context:
                    context_parts.append(f"Web Search Context:\n{search_context}")

                final_prompt = clean_prompt if clean_prompt else "Process and answer this voice note."
                if context_parts:
                    final_prompt = "\n\n".join(context_parts) + f"\n\nUser Question: {final_prompt}"

                today_str = datetime.now().strftime("%A, %B %d, %Y")
                bot_instructions = (
                    f"Today's date is {today_str}. "
                    "Never use standard AI pleasantries. Do not start responses with 'As an AI' or end with generic offers for help. "
                )
                
                bot_instructions += (
                    "Keep casual replies brief, but dynamically expand your response length when explicitly asked for details or when playing interactive games. "
                    "If the user changes the subject abruptly, drop the previous topic immediately and adapt to the new flow. "
                    "If the user is clearly joking or sarcastic, match their energy rather than taking the prompt literally. "
                )

                bot_instructions += (
                    "If you do not know the answer or the provided context is insufficient, state 'I don't have enough details to answer that accurately' directly without guessing. "
                    "Do not assume personal details about the user unless they are explicitly provided in your memory list. "
                )

                if search_context:
                    bot_instructions += (
                        "When referencing 'Web Search Context', state the information directly without saying 'According to my search' or 'I found this online'. "
                        "Never include or state URLs/links from the Web Search Context unless the user explicitly asks for links, sources, or URLs in their prompt. "
                    )

                if chat_history:
                    bot_instructions += "Use the 'Recent Conversation Context' to track pronouns and subjects, but never summarize or repeat the history back to the user. "

                if saved_facts:
                    bot_instructions += "\n\nYou must strictly follow these User Instructions. They override any baseline behavior and are your absolute highest priority:\n" + "\n".join(f"- {f}" for f in saved_facts)

                if audio_bytes:
                    contents = [
                        genai_types.Part.from_bytes(data=audio_bytes, mime_type=audio_mime),
                        final_prompt
                    ]
                    response = await gemini_client.aio.models.generate_content(
                        model='gemini-3.5-flash-lite',
                        contents=contents,
                        config=genai_types.GenerateContentConfig(system_instruction=bot_instructions)
                    )
                else:
                    try:
                        chat = gemini_client.aio.chats.create(
                            model='gemini-3.5-flash-lite',
                            config=genai_types.GenerateContentConfig(system_instruction=bot_instructions)
                        )
                        response = await chat.send_message(final_prompt)
                    except Exception as primary_err:
                        print(f"Primary model failed ({primary_err}), falling back to gemini-2.5-flash...")
                        chat = gemini_client.aio.chats.create(
                            model='gemini-2.5-flash',
                            config=genai_types.GenerateContentConfig(system_instruction=bot_instructions)
                        )
                        response = await chat.send_message(final_prompt)

                raw_markdown = response.text or ""
                raw_markdown = balance_codeblocks(raw_markdown)
                clean_text, text_entities = convert(raw_markdown)
                
                preview_opts = LinkPreviewOptions(is_disabled=False, prefer_small_media=True)
                
                if is_private:
                    await message.answer(clean_text, entities=text_entities, link_preview_options=preview_opts)
                else:
                    await message.reply(clean_text, entities=text_entities, link_preview_options=preview_opts)

                await redis_client.rpush(history_key, f"User: {clean_prompt or 'Voice Note'}", f"Bot: {clean_text}")
                await redis_client.ltrim(history_key, -10, -1)

            except Exception as ai_err:
                print(f"Gemini API error: {ai_err}")
                error_text = "I am currently broken right now, the owner needs to fix me."
                if "429" in str(ai_err):
                    error_text = "Whoa, I'm getting a little overwhelmed! Let me catch my breath for a minute."
                if is_private:
                    await message.answer(error_text)
                else:
                    await message.reply(error_text)

    # Keyword Audio Triggers
    if re.search(r'\bsen\b', text_no_code, re.IGNORECASE):
        background_tasks.add_task(send_audio_track, message.bot, chat_id, msg_id, "sen", "Devin_The_Dude_Anythang.mp3", "Anythang", "Devin The Dude", is_private)
    if re.search(r'\bmagic(?:al|ally)?\b', text_no_code, re.IGNORECASE):
        background_tasks.add_task(send_audio_track, message.bot, chat_id, msg_id, "magic", "Do You Believe In Magic.mp3", "Do You Believe In Magic", "The Lovin' Spoonful", is_private)

# ==========================================
# Execution Entry Point
# ==========================================
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port)

