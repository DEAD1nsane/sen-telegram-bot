import os
import re
import io
import urllib.parse
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

# Handle SSL for Upstash rediss:// URLs
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

gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

async def free_web_search(query: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    # 1. Try SearXNG Primary
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

    # 2. Fallback to DuckDuckGo HTML
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

async def fetch_real_image_bytes(query: str):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        url = "https://commons.wikimedia.org/w/api.php"
        params = {
            "action": "query",
            "generator": "search",
            "gsrsearch": f"file:{query}",
            "gsrnamespace": 6,
            "gsrlimit": 5,
            "prop": "imageinfo",
            "iiprop": "url",
            "format": "json"
        }
        async with httpx.AsyncClient(follow_redirects=True) as client:
            res = await client.get(url, params=params, headers=headers, timeout=8.0)
            if res.status_code == 200:
                data = res.json()
                pages = data.get("query", {}).get("pages", {})
                for page_id, page_data in pages.items():
                    image_info = page_data.get("imageinfo", [])
                    if image_info and "url" in image_info[0]:
                        img_url = image_info[0]["url"]
                        if img_url.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                            # Stream download bytes directly to bypass Telegram URL blocks
                            img_res = await client.get(img_url, headers=headers, timeout=10.0)
                            if img_res.status_code == 200:
                                return img_res.content
    except Exception as e:
        print(f"Wikimedia image search error ({query}): {e}")

    # Fallback download from Unsplash
    try:
        fallback_url = f"https://source.unsplash.com/1600x900/?{urllib.parse.quote(query)}"
        async with httpx.AsyncClient(follow_redirects=True) as client:
            img_res = await client.get(fallback_url, headers=headers, timeout=10.0)
            if img_res.status_code == 200:
                return img_res.content
    except Exception as e:
        print(f"Unsplash fallback error ({query}): {e}")

    return None

async def send_web_image(chat_id, msg_id, prompt, is_private):
    try:
        kwargs = {"reply_to_message_id": msg_id} if not is_private else {}
        
        clean_query = re.sub(
            r'\b(send|me|a|an|real|legit|actual|image|picture|photo|of|from)\b', 
            '', prompt, flags=re.IGNORECASE
        ).strip()
        if not clean_query:
            clean_query = prompt

        image_bytes = await fetch_real_image_bytes(clean_query)
        if image_bytes:
            image_stream = io.BytesIO(image_bytes)
            image_stream.name = "image.jpg"
            try:
                bot.send_photo(chat_id, image_stream, **kwargs)
            except telebot.apihelper.ApiTelegramException as e:
                if "message to be replied not found" in str(e).lower():
                    kwargs.pop("reply_to_message_id", None)
                    image_stream.seek(0)
                    bot.send_photo(chat_id, image_stream, **kwargs)
                else:
                    raise e
        else:
            bot.send_message(chat_id, "Couldn't find an image for that.")
    except Exception as err:
        print(f"Error sending web image: {err}")

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

        text = update.message.text or ""
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
            if normalized_prompt in ["help", "how do you remember", "commands"]:
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

            if clean_prompt.lower().startswith("remember "):
                raw_content = clean_prompt[9:].strip()
                if raw_content:
                    parts = [p.strip()[:200] for p in raw_content.split(",") if p.strip()]
                    for part in parts[:10]:
                        try:
                            redis_client.rpush(f"memory_list:{user_id_str}", part)
                        except Exception as r_err:
                            print(f"Redis memory save error: {r_err}")
                msg_text = "Got it, I've added those items to your memory list."
                bot.send_message(chat_id, msg_text) if is_private else bot.reply_to(update.message, msg_text)
                return Response(status_code=200)

            # Real Image Web Search Trigger
            if re.search(r'\b(image|picture|photo)\b', clean_prompt, re.IGNORECASE):
                background_tasks.add_task(send_web_image, chat_id, msg_id, clean_prompt, is_private)
                return Response(status_code=200)

            if clean_prompt:
                try:
                    saved_facts = []
                    try:
                        raw_items = redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
                        if raw_items:
                            saved_facts = [item.decode('utf-8') for item in raw_items]
                    except Exception as r_err:
                        print(f"Redis memory fetch error: {r_err}")

                    search_context = await free_web_search(clean_prompt)
                    
                    context_parts = []
                    if saved_facts:
                        context_parts.append("Saved Memories:\n" + "\n".join(f"  {f}" for f in saved_facts))
                    if search_context:
                        context_parts.append(f"Web Search Context:\n{search_context}")
                    
                    final_prompt = clean_prompt
                    if context_parts:
                        final_prompt = "\n\n".join(context_parts) + f"\n\nUser Question: {clean_prompt}"
                        
                    today_str = datetime.now().strftime("%A, %B %d, %Y")
                    response = gemini_client.models.generate_content(
                        model='gemini-3.1-flash-lite',
                        contents=final_prompt,
                        config=types.GenerateContentConfig(
                            system_instruction=f"Today's date is {today_str}. Return plain text only without markdown formatting."
                        )
                    )
                    clean_text = re.sub(r'[*_#`]', '', response.text or "")
                    bot.send_message(chat_id, clean_text) if is_private else bot.send_message(chat_id, clean_text, reply_to_message_id=msg_id)
                except Exception as ai_err:
                    print(f"Gemini API error: {ai_err}")
                    error_text = "Sorry, I had trouble processing that request."
                    bot.send_message(chat_id, error_text) if is_private else bot.send_message(chat_id, error_text, reply_to_message_id=msg_id)
            return Response(status_code=200)

        # Audio Track Trigger Logic via Background Tasks
        if re.search(r'\bsen\b', text, re.IGNORECASE):
            background_tasks.add_task(send_audio_track, chat_id, msg_id, "sen", "Devin_The_Dude_Anythang.mp3", "Anythang", "Devin The Dude", is_private)
        if re.search(r'\bmagic(?:al)?\b', text, re.IGNORECASE):
            background_tasks.add_task(send_audio_track, chat_id, msg_id, "magic", "Do You Believe In Magic.mp3", "Do You Believe In Magic", "The Lovin' Spoonful", is_private)

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
