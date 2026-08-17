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
            return Response(status_code=200)

        bot_username = f"@{BOT_INFO.username}" if BOT_INFO else ""
        is_tagged = (bot_username and bot_username.lower() in text.lower()) or "@gemini" in text.lower()

        is_reply_to_bot = False
        if update.message.reply_to_message and update.message.reply_to_message.from_user and BOT_INFO:
            if update.message.reply_to_message.from_user.id == BOT_INFO.id:
                is_reply_to_bot = True

        if is_tagged or is_reply_to_bot or is_private:
            clean_prompt = text
            if bot_username:
                clean_prompt = clean_prompt.replace(bot_username, "").replace(bot_username.lower(), "")
            clean_prompt = clean_prompt.replace("@gemini", "").replace("@Gemini", "").strip()

            normalized_prompt = clean_prompt.rstrip("?").lower()
            if normalized_prompt in ["help", "commands"]:
                help_text = (
                    "Remember rule:\n"
                    "  remember [item, item2] - adds items to memory list.\n\n"
                    "What do you remember:\n"
                    "  displays your rules in a numbered list format.\n\n"
                    "Edit #:\n"
                    "  edit [number] [new fact] - edits a specific rule.\n\n"
                    "Forget #:\n"
                    "  forget [number, number] - removes specific memories.\n\n"
                    "Forget all:\n"
                    "  clears all memory."
                )
                bot.send_message(chat_id, help_text) if is_private else bot.reply_to(update.message, help_text)
                return Response(status_code=200)

            if normalized_prompt in ["what do you remember", "how do you remember"]:
                msg_text = get_formatted_memories(user_id_str)
                bot.send_message(chat_id, msg_text) if is_private else bot.reply_to(update.message, msg_text)
                return Response(status_code=200)

            if clean_prompt.lower().startswith("remember "):
                raw_content = clean_prompt[9:].strip()
                if raw_content:
                    parts = [p.strip()[:200] for p in raw_content.split(",") if p.strip()]
                    for part in parts[:10]:
                        try:
                            redis_client.rpush(f"memory_list:{user_id_str}", part)
                        except Exception as r_err:
                            print(f"Redis memory save error: {r_err}")
                msg_text = get_formatted_memories(user_id_str)
                bot.send_message(chat_id, msg_text) if is_private else bot.reply_to(update.message, msg_text)
                return Response(status_code=200)

            if clean_prompt.lower().startswith("edit "):
                parts = clean_prompt[5:].strip().split(" ", 1)
                if len(parts) == 2 and parts[0].isdigit():

                    idx = int(parts[0]) - 1
                    new_val = parts[1].strip()
                    try:
                        raw_items = redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
                        if 0 <= idx < len(raw_items):
                            redis_client.lset(f"memory_list:{user_id_str}", idx, new_val)
                            msg_text = get_formatted_memories(user_id_str)
                        else:
                            msg_text = "Invalid memory number.\n\n" + get_formatted_memories(user_id_str)
                    except Exception as r_err:
                        print(f"Redis edit error: {r_err}")
                        msg_text = "Error updating memory."
                else:
                    msg_text = "Usage: edit [number] [new text]"
                bot.send_message(chat_id, msg_text) if is_private else bot.reply_to(update.message, msg_text)
                return Response(status_code=200)

            if normalized_prompt == "forget all":
                try:
                    redis_client.delete(f"memory_list:{user_id_str}")
                    redis_client.delete(f"chat_history:{chat_id}")
                    msg_text = "Cleared all your saved memories."
                except Exception as r_err:
                    print(f"Redis forget all error: {r_err}")
                    msg_text = "Error clearing memories."
                bot.send_message(chat_id, msg_text) if is_