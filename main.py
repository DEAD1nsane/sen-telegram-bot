import os
import re
import asyncio
from datetime import datetime
from fastapi import FastAPI, Request, Response, Header, HTTPException, BackgroundTasks
from telebot.async_telebot import AsyncTeleBot
import telebot.types
import redis.asyncio as redis
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
app = FastAPI()

BOT_INFO = None

@app.on_event("startup")
async def startup_event():
    global BOT_INFO
    try:
        BOT_INFO = await bot.get_me()
    except Exception as e:
        print(f"Failed to fetch bot info: {e}")

gemini_api_key = os.getenv("GEMINI_API_KEY")
if not gemini_api_key:
    raise ValueError("CRITICAL CONFIGURATION ERROR: 'GEMINI_API_KEY' missing.")

gemini_client = genai.Client(api_key=gemini_api_key)
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

async def free_web_search(query: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        params = {"q": query, "format": "json"}
        async with httpx.AsyncClient() as client:
            res = await client.get(SEARXNG_URL, params=params, headers=headers, timeout=8.0)
            if res.status_code == 200:
                results = res.json().get("results", [])[:3]
                snippets = [f"{item.get('title', '')}: {item.get('content', '')}" for item in results if item.get('title') or item.get('content')]
                if snippets: return "\n".join(snippets)
    except Exception as e:
        print(f"SearXNG error: {e}")

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post("https://html.duckduckgo.com/html/", data={"q": query}, headers=headers, timeout=8.0)
            raw = re.findall(r'<a class="result__snippet[^">]*>(.*?)</a>', res.text, re.DOTALL)
            clean = [re.sub(r'<[^>]+>', '', s).strip() for s in raw[:3] if s.strip()]
            if clean: return "\n".join(clean)
    except Exception as e:
        print(f"DuckDuckGo error: {e}")
    return ""

async def get_formatted_memories(user_id_str: str) -> str:
    try:
        raw_items = await redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
        if not raw_items: return "Your memory list is currently empty."
        memories = [item.decode('utf-8') if isinstance(item, bytes) else item for item in raw_items]
        formatted_list = "\n".join(f"{i+1}. {mem}" for i, mem in enumerate(memories))
        return f"Here is what I remember for you:\n\n{formatted_list}"
    except Exception as e:
        print(f"Error fetching memory list format: {e}")
        return "Could not retrieve memory list."

@app.get("/")
def home_check():
    return {"status": "ok", "message": "Bot webhook server is running."}

@app.post("/webhook")
async def handle_webhook(request: Request, background_tasks: BackgroundTasks, x_telegram_bot_api_secret_token: str = Header(None)):
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Unauthorized")

    json_data = await request.json()
    try:
        update = telebot.types.Update.de_json(json_data)
        if not update or not update.message: return Response(status_code=200)

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
                    try: await bot.delete_message(chat_id, reply_msg.message_id)
                    except Exception: pass
                try: await bot.delete_message(chat_id, msg_id)
                except Exception: pass
            return Response(status_code=200)

        bot_username = f"@{BOT_INFO.username}" if BOT_INFO else ""
        is_tagged = (bot_username and bot_username.lower() in text.lower()) or "@gemini" in text.lower()
        is_reply_to_bot = bool(update.message.reply_to_message and BOT_INFO and update.message.reply_to_message.from_user.id == BOT_INFO.id)

        if is_tagged or is_reply_to_bot or is_private:
            clean_prompt = text.replace(bot_username, "").replace(bot_username.lower(), "").replace("@gemini", "").replace("@Gemini", "").strip()
            normalized_prompt = clean_prompt.rstrip("?").lower()

            if normalized_prompt in ["help", "commands"]:
                help_text = (
                    "Remember rule:\n  remember [item] - adds items to memory list.\n\n"
                    "What do you remember:\n  displays your rules in a numbered list format.\n\n"
                    "Edit #:\n  edit [number] [new fact] - edits a specific rule.\n\n"
                    "Forget #:\n  forget [number] - removes specific memories.\n\n"
                    "Forget all:\n  clears all memory."
                )
                if is_private:
                    await bot.send_message(chat_id, help_text)
                else:
                    await bot.reply_to(update.message, help_text)
                return Response(status_code=200)

            if normalized_prompt in ["what do you remember", "how do you remember"]:
                msg_text = await get_formatted_memories(user_id_str)
                if is_private:
                    await bot.send_message(chat_id, msg_text)
                else:
                    await bot.reply_to(update.message, msg_text)
                return Response(status_code=200)

            if clean_prompt.lower().startswith("remember "):
                parts = [p.strip()[:200] for p in clean_prompt[9:].split(",") if p.strip()]
                for part in parts[:10]:
                    try: await redis_client.rpush(f"memory_list:{user_id_str}", part)
                    except Exception: pass
                msg_text = await get_formatted_memories(user_id_str)
                if is_private:
                    await bot.send_message(chat_id, msg_text)
                else:
                    await bot.reply_to(update.message, msg_text)
                return Response(status_code=200)

            if clean_prompt.lower().startswith("edit "):
                parts = clean_prompt[5:].strip().split(" ", 1)
                if len(parts) == 2 and parts.isdigit():
                    idx, new_val = int(parts) - 1, parts[15].strip()
                    raw_items = await redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
                    if 0 <= idx < len(raw_items):
                        await redis_client.lset(f"memory_list:{user_id_str}", idx, new_val)
                        msg_text = await get_formatted_memories(user_id_str)
                    else:
                        msg_text = "Invalid memory number.\n\n" + await get_formatted_memories(user_id_str)
                else:
                    msg_text = "Usage: edit [number] [new text]"
                
                if is_private:
                    await bot.send_message(chat_id, msg_text)
                else:
                    await bot.reply_to(update.message, msg_text)
                return Response(status_code=200)

            if normalized_prompt == "forget all":
                await redis_client.delete(f"memory_list:{user_id_str}", f"chat_history:{chat_id}:{user_id_str}")
                msg_text = "Cleared all your saved memories."
                if is_private:
                    await bot.send_message(chat_id, msg_text)
                else:
                    await bot.reply_to(update.message, msg_text)
                return Response(status_code=200)

            if clean_prompt.lower().startswith("forget "):
                try:
                    indices = [int(n.strip()) - 1 for n in clean_prompt[7:].split(",") if n.strip().isdigit()]
                    raw_items = await redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
                    if raw_items and indices:
                        memories = [i.decode('utf-8') if isinstance(i, bytes) else i for i in raw_items]
                        for i in sorted(set(indices), reverse=True):
                            if 0 <= i < len(memories): memories.pop(i)
                        await redis_client.delete(f"memory_list:{user_id_str}")
                        if memories: await redis_client.rpush(f"memory_list:{user_id_str}", *memories)
                        msg_text = await get_formatted_memories(user_id_str)
                    else:
                        msg_text = "No valid memory numbers specified.\n\n" + await get_formatted_memories(user_id_str)
                except Exception:
                    msg_text = "Error removing memory."
                
                if is_private:
                    await bot.send_message(chat_id, msg_text)
                else:
                    await bot.reply_to(update.message, msg_text)
                return Response(status_code=200)

            replied = update.message.reply_to_message
            replied_context = replied.text or replied.caption or "" if replied else ""
            if not clean_prompt and replied_context: clean_prompt = "What are your thoughts on this?"

            if clean_prompt or replied_context:
                try:
                    raw_mem = await redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
                    saved_facts = [i.decode('utf-8') if isinstance(i, bytes) else i for i in raw_mem]

                    history_key = f"chat_history:{chat_id}:{user_id_str}"
                    raw_hist = await redis_client.lrange(history_key, 0, -1)
                    chat_history = [h.decode('utf-8') if isinstance(h, bytes) else h for h in raw_hist]

                    search_context = await free_web_search(clean_prompt)

                    context_parts = []
                    if saved_facts: context_parts.append("Saved Instructions:\n  " + "\n  ".join(saved_facts))
                    if replied_context: context_parts.append(f"Message User is Replying To:\n\"{replied_context}\"")
                    if chat_history: context_parts.append("Recent Conversation Context:\n" + "\n".join(chat_history))
                    if search_context: context_parts.append(f"Web Search Context:\n{search_context}")

                    final_prompt = clean_prompt
                    if context_parts: final_prompt = "\n\n".join(context_parts) + f"\n\nUser Question: {clean_prompt}"

                    today_str = datetime.now().strftime("%A, %B %d, %Y")
                    bot_instructions = (
                        f"Today's date is {today_str}. Return plain text only without markdown formatting. "
                        "Never ask follow-up questions. Never give suggestions unless explicitly requested. "
                        "Always respond short and brief. Talk like a degenerate."
                    )

                    response = await gemini_client.aio.models.generate_content(
                        model='gemini-1.5-flash-latest', # or 'gemini-1.5-flash-002'
                        contents=final_prompt,
                        config=types.GenerateContentConfig(system_instruction=bot_instructions)
                    )

                    clean_text = re.sub(r'[*_#`]', '', response.text or "")
                    if is_private:
                        await bot.send_message(chat_id, clean_text)
                    else:
                        await bot.send_message(chat_id, clean_text, reply_to_message_id=msg_id)

                    await redis_client.rpush(history_key, f"User: {clean_prompt}", f"Bot: {clean_text}")
                    await redis_client.ltrim(history_key, -10, -1)

                except Exception as ai_err:
                    print(f"Gemini API error: {ai_err}")
                    error_text = "Sorry, I had trouble processing that request."
                    if is_private:
                        await bot.send_message(chat_id, error_text)
                    else:
                        await bot.send_message(chat_id, error_text, reply_to_message_id=msg_id)

            return Response(status_code=200)

        if re.search(r'\bsen\b', text, re.IGNORECASE):
            background_tasks.add_task(send_audio_track, chat_id, msg_id, "sen", "Devin_The_Dude_Anythang.mp3", "Anythang", "Devin The Dude", is_private)
        if re.search(r'\bmagic(?:al|ally)?\b', text, re.IGNORECASE):
            background_tasks.add_task(send_audio_track, chat_id, msg_id, "magic", "Do You Believe In Magic.mp3", "Do You Believe In Magic", "The Lovin' Spoonful", is_private)

    except Exception as e:
        print(f"Webhook error: {e}")
    return Response(status_code=200)

async def send_audio_track(chat_id, msg_id, key, file_path, title, performer, is_private):
    try:
        kwargs = {"title": title, "performer": performer, "timeout": 60}
        if not is_private: kwargs["reply_to_message_id"] = msg_id

        cached_id = await redis_client.get(f"audio_cache:{key}")

        async def attempt_send(audio_payload):
            try:
                return await bot.send_audio(chat_id, audio_payload, **kwargs)
            except telebot.apihelper.ApiTelegramException as e:
                if "message to be replied not found" in str(e).lower():
                    kwargs.pop("reply_to_message_id", None)
                    return await bot.send_audio(chat_id, audio_payload, **kwargs)
                raise e

        if cached_id:
            await attempt_send(cached_id.decode('utf-8') if isinstance(cached_id, bytes) else cached_id)
        elif os.path.exists(file_path):
            with open(file_path, "rb") as audio:
                msg = await attempt_send(audio)
                await redis_client.set(f"audio_cache:{key}", msg.audio.file_id)
        else:
            print(f"Audio file not found: {file_path}")
    except Exception as send_err:
        print(f"Error sending audio ({key}): {send_err}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port)