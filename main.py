import os
import re
import asyncio
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, Header, HTTPException, BackgroundTasks
from telebot.async_telebot import AsyncTeleBot
import telebot.types
import redis.asyncio as redis
import httpx
from google import genai
from google.genai import types
from telegramify_markdown import convert

### Environment & Config
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

bot = AsyncTeleBot(API_TOKEN)
BOT_INFO = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global BOT_INFO
    try:
        BOT_INFO = await bot.get_me()
    except Exception as e:
        print(f"Failed to fetch bot info: {e}")

app = FastAPI(lifespan=lifespan)

gemini_api_key = os.getenv("GEMINI_API_KEY")
if not gemini_api_key:
    raise ValueError("CRITICAL CONFIGURATION ERROR: 'GEMINI_API_KEY' missing.")

gemini_client = genai.Client(api_key=gemini_api_key)
oses unclosed codeblocks to prevent broken Telegram formatting."""
    if text.count("") % 2 != 0:
                return text + "\n"
                return textWNER_ID = int(os.getenv("OWNER_ID", "0"))

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
                if snippets:
                    return "\n\n".join(snippets)
    except Exception as e:
        print(f"SearXNG error: {e}")

async def get_formatted_memories(user_id_str: str) -> str:
    try:
        raw_items = await redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
        if not raw_items:
            return "Your memory list is currently empty."
        memories = [item.decode('utf-8') if isinstance(item, bytes) else item for item in raw_items]
        formatted_list = "\n".join(f"{i+1}. {mem}" for i, mem in enumerate(memories))
        return f"<b>Active Memory Directives:</b>\n──────────────────────────\n{formatted_list}"
    except Exception as e:
        print(f"Error fetching memory list format: {e}")
        return "Could not retrieve memory list."

async def send_gemini_formatted_response(chat_id: int, prompt: str):
    """Fetches Gemini output and sends it using entity-based formatting."""
    try:
        # 1. Fetch raw Markdown response from Gemini
        response = gemini_client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt
        )
        raw_markdown = response.text
        
        # 2. Convert markdown and unpack the tuple directly
        clean_text, text_entities = convert(raw_markdown)
        
        # 3. Transmit the clean string and array of message formatting elements
        # Note: parse_mode is strictly omitted here
        await bot.send_message(
            chat_id=chat_id, 
            text=clean_text, 
            entities=text_entities
        )
    except Exception as e:
        print(f"Error generating formatted response: {e}")
        await bot.send_message(chat_id, "Sorry, I encountered an error formatting that response.")

@app.get("/")
def home_check():
    return {"status": "ok", "message": "Bot webhook server is running."}

@app.post("/webhook")
async def handle_webhook(request: Request, background_tasks: BackgroundTasks, x_telegram_bot_api_secret_token: str = Header(None)):
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Unauthorized")
        
    update_data = await request.json()
    update = telebot.types.Update.de_json(update_data)
    
    # Properly pass the update back into pyTelegramBotAPI's internal handler routing
    await bot.process_new_updates([update])
    return {"status": "ok"}

@bot.message_handler(func=lambda message: True)
async def handle_all_messages(message):
    """Intercepts messages and triggers the entity-based generation."""
    # Execute generation without blocking the main event loop
    asyncio.create_task(send_gemini_formatted_response(message.chat.id, message.text))

async def collapse_message(chat_id: int, message_id: int, delay: int):
    """Waits for the delay (in seconds), then edits the message to a collapsed state."""
    await asyncio.sleep(delay)
    collapsed_text = "<blockquote>Active Memories Collapsed</blockquote>"
    try:
        await bot.edit_message_text(
            chat_id=chat_id, 
            message_id=message_id, 
            text=collapsed_text, 
            parse_mode="HTML"
        )
    except Exception as e:
        print(f"Error collapsing memory list: {e}")

async def send_audio_track(chat_id, msg_id, key, file_path, title, performer, is_private):
    try:
        kwargs = {"title": title, "performer": performer, "timeout": 60}
        if not is_private:
            kwargs["reply_to_message_id"] = msg_id
    except Exception as e:
        pass

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
