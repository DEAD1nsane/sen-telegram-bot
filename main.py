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

gemini_api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
gemini_client = genai.Client(api_key=gemini_api_key) if gemini_api_key else None
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

        # Pull text from standard message or image captions
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

        # 1. Check Audio Triggers FIRST and return early
        is_sen = bool(re.search(r'\bsen\b', text, re.IGNORECASE))
        is_magic = bool(re.search(r'\bmagic(?:al|ally)?\b', text, re.IGNORECASE))

        if is_sen:
            background_tasks.add_task(send_audio_track, chat_id, msg_id, "sen", "Devin_The_Dude_Anythang.mp3", "Anythang", "Devin The Dude", is_private)
            return Response(status_code=200)

        if is_magic:
            background_tasks.add_task(send_audio_track, chat_id, msg_id, "magic", "Do You Believe In Magic.mp3", "Do You Believe In Magic", "The Lovin' Spoonful", is_private)
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
                    msg_text = "Cleared all your saved memories."
                except Exception as r_err:
                    print(f"Redis forget all error: {r_err}")
                    msg_text = "Error clearing memories."
                bot.send_message(chat_id, msg_text) if is_private else bot.reply_to(update.message, msg_text)
                return Response(status_code=200)

            if clean_prompt.lower().startswith("forget "):
                raw_nums = clean_prompt[7:].strip()
                try:
                    indices = [int(n.strip()) - 1 for n in raw_nums.split(",") if n.strip().isdigit()]
                    raw_items = redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
                    if raw_items and indices:
                        memories = [item.decode('utf-8') for item in raw_items]
                        for i in sorted(set(indices), reverse=True):
                            if 0 <= i < len(memories):
                                memories.pop(i)
                        redis_client.delete(f"memory_list:{user_id_str}")
                        for m in memories:
                            redis_client.rpush(f"memory_list:{user_id_str}", m)
                        msg_text = get_formatted_memories(user_id_str)
                    else:
                        msg_text = "No valid memory numbers specified.\n\n" + get_formatted_memories(user_id_str)
                except Exception as r_err:
                    print(f"Redis forget error: {r_err}")
                    msg_text = "Error removing memory."
                bot.send_message(chat_id, msg_text) if is_private else bot.reply_to(update.message, msg_text)
                return Response(status_code=200)

            replied_context = ""
            if update.message.reply_to_message:
                replied_context = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""

            if clean_prompt or replied_context:
                if not clean_prompt and replied_context:
                    clean_prompt = "What are your thoughts on this?"

                try:
                    if not gemini_client:
                        raise ValueError("GEMINI_API_KEY environment variable is missing or invalid.")

                    saved_facts = []
                    try:
                        raw_items = redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
                        if raw_items:
                            saved_facts = [item.decode('utf-8') for item in raw_items]
                    except Exception as r_err:
                        print(f"Redis memory fetch error: {r_err}")

                    history_key = f"chat_history:{chat_id}"
                    chat_history = []
                    try:
                        raw_hist = redis_client.lrange(history_key, 0, -1)
                        if raw_hist:
                            chat_history = [h.decode('utf-8') for h in raw_hist]
                    except Exception as r_err:
                        print(f"Redis history fetch error: {r_err}")

                    search_context = await free_web_search(clean_prompt)
                    
                    context_parts = []
                    if saved_facts:
                        context_parts.append("Saved Instructions:\n" + "\n".join(f"  {f}" for f in saved_facts))
                    if replied_context:
                        context_parts.append(f"Message User is Replying To:\n\"{replied_context}\"")
                    if chat_history:
                        context_parts.append("Recent Conversation Context:\n" + "\n".join(chat_history))
                    if search_context:
                        context_parts.append(f"Web Search Context:\n{search_context}")
                    
                    final_prompt = clean_prompt
                    if context_parts:
                        final_prompt = "\n\n".join(context_parts) + f"\n\nUser Question: {clean_prompt}"
                        
                    today_str = datetime.now().strftime("%A, %B %d, %Y")
                    response = gemini_client.models.generate_content(
                        model='gemini-2.0-flash',
                        contents=final_prompt,
                        config=types.GenerateContentConfig(
                            system_instruction=f"Today's date is {today_str}. Return plain text only without markdown formatting."
                        )
                    )
                    clean_text = re.sub(r'[*_#`]', '', response.text or "")
                    bot.send_message(chat_id, clean_text) if is_private else bot.send_message(chat_id, clean_text, reply_to_message_id=msg_id)

                    try:
                        redis_client.rpush(history_key, f"User: {clean_prompt}")
                        redis_client.rpush(history_key, f"Bot: {clean_text}")
                        redis_client.ltrim(history_key, -10, -1)
                    except Exception as r_err:
                        print(f"Redis history save error: {r_err}")

                except Exception as ai_err:
                    print(f"Gemini API error: {ai_err}")
                    error_text = "Sorry, I had trouble processing that request."
                    bot.send_message(chat_id, error_text) if is_private else bot.send_message(chat_id, error_text, reply_to_message_id=msg_id)
            return Response(status_code=200)

        return Response(status_code=200)
    except Exception as e:
        print(f"Webhook error: {e}")
        return Response(status_code=200)

def send_audio_track(chat_id, msg_id, key, file_path, title, performer, is_private):
    try:
        kwargs = {"title": title, "performer": performer, "timeout": 60}
        if not is_private:
            kwargs["reply_to_message_id"] = msg_id
            
        cached_id = None
        try:
            cached_id = redis_client.get(f"audio_cache:{key}")
        except Exception as cache_err:
            print(f"Redis cache fetch error ({key}): {cache_err}")

        def attempt_send(audio_payload):
            try:
                return bot.send_audio(chat_id, audio_payload, **kwargs)
            except telebot.apihelper.ApiTelegramException as e:
                if "message to be replied not found" in str(e).lower():
                    kwargs.pop("reply_to_message_id", None)
                    return bot.send_audio(chat_id, audio_payload, **kwargs)
                raise e

        if cached_id:
            attempt_send(cached_id.decode('utf-8'))
        else:
            if os.path.exists(file_path):
                with open(file_path, "rb") as audio:
                    msg = attempt_send(audio)
                    try:
                        redis_client.set(f"audio_cache:{key}", msg.audio.file_id)
                    except Exception as set_err:
                        print(f"Redis cache set error ({key}): {set_err}")
            else:
                print(f"Audio file not found at path: {file_path}")
    except Exception as send_err:
        print(f"Error sending audio ({key}): {send_err}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
