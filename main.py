import os
import re
from datetime import datetime
from fastapi import FastAPI, Request, Response, Header, HTTPException, BackgroundTasks
import telebot
import redis
import httpx
from google import genai
from google.genai import types

# Environment & Config
redis_url = os.environ.get("REDIS_URL")
if not redis_url:
    host = os.environ.get("REDISHOST", "localhost")
    port = os.environ.get("REDISPORT", "6379")
    password = os.environ.get("REDISPASSWORD", "")
    redis_url = f"redis://default:{password}@{host}:{port}" if password else f"redis://{host}:{port}"

# Force secure SSL for Upstash even if REDIS_URL was set as standard redis://
if "upstash" in redis_url.lower() and redis_url.startswith("redis://"):
    redis_url = redis_url.replace("redis://", "rediss://", 1)

# Handle SSL context for Upstash / secure rediss:// URLs
if redis_url.startswith("rediss://"):
    redis_client = redis.from_url(redis_url, ssl_cert_reqs=None)
else:
    redis_client = redis.from_url(redis_url)

API_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")  # Optional header verification
SEARXNG_URL = os.getenv("SEARXNG_URL", "https://searxng-railway-production-3252.up.railway.app/search")

bot = telebot.TeleBot(API_TOKEN)
app = FastAPI()

BOT_INFO = None
try:
    BOT_INFO = bot.get_me()
except Exception as e:
    print(f"Failed to fetch bot info: {e}")

# Secure configuration check to prevent falling back to GCP credentials if the key is missing
gemini_api_key = os.getenv("GEMINI_API_KEY")
if not gemini_api_key:
    raise ValueError(
        "CRITICAL CONFIGURATION ERROR: The 'GEMINI_API_KEY' environment variable is missing or empty. "
        "Please set GEMINI_API_KEY in your hosting provider's dashboard."
    )

gemini_client = genai.Client(api_key=gemini_api_key)
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

async def free_web_search(query: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    try:
        params = {"q": query, "format": "json"}
        async with httpx.AsyncClient() as client:
            res = await client.get(SEARXNG_URL, params=params, headers=headers, timeout=8.0)
            if res.status_code == 200:
                data = res.json()
                results = data.get("results", [])[:3]
                snippets = [
                    f"{item.get('title', '').strip()}: {item.get('content', '').strip()}"
                    for item in results if item.get('title') or item.get('content')
                ]
                if snippets:
                    return "\n".join(snippets)
    except Exception as e:
        print(f"SearXNG search error, trying fallback: {e}")

    try:
        ddg_url = "https://html.duckduckgo.com/html/"
        async with httpx.AsyncClient() as client:
            res = await client.post(ddg_url, data={"q": query}, headers=headers, timeout=8.0)
            raw_snippets = re.findall(r'<a class="result__snippet[^">]*>(.*?)</a>', res.text, re.DOTALL)
            clean_snippets = [re.sub(r'<[^>]+>', '', s).strip() for s in raw_snippets[:3] if s.strip()]
            if clean_snippets:
                return "\n".join(clean_snippets)
    except Exception as e:
        print(f"DuckDuckGo fallback search error: {e}")

    return ""

def get_formatted_memories(user_id_str: str) -> str:
    try:
        raw_items = redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
        if not raw_items:
            return "Your memory list is currently empty."
        memories = [item.decode('utf-8') for item in raw_items]
        formatted_list = "\n".join(f"{i+1}. {mem}" for i, mem in enumerate(memories))
        return f"Here is what I remember for you:\n\n{formatted_list}"
    except Exception as e:
        print(f"Error fetching memory list format: {e}")
        return "Could not retrieve memory list."

@app.get("/")
def home_check():
    return {"status": "ok", "message": "Bot webhook server is running."}

@app.post("/webhook")
async def handle_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: str = Header(None)
):
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Unauthorized webhook source")

    json_data = await request.json()
    try:
        update = telebot.types.Update.de_json(json_data)
        if not update or not update.message:
            return Response(status_code=200)

        # Pull text from caption if it's an image
        text = update.message.text or update.message.caption or ""
        user_id = update.message.from_user.id
        user_id_str = str(user_id)
        chat_id = update.message.chat.id
        msg_id = update.message.message_id
        is_private = update.message.chat.type == "private"

        if text.startswith(("/delete", "/del")):
            if user_id == OWNER_ID:
                reply_msg = update.message.reply_to_message
                if reply_msg and BOT_INFO and reply_msg.from_user.id == BOT_INFO.id:
                    try:
                        bot.delete_message(chat_id, reply_msg.message_id)
                    except Exception as del_err:
                        print(f"Error deleting bot message: {del_err}")

                try:
                    bot.delete_message(chat_id, msg_id)
                except Exception:
                    pass
            return Response(status_code=