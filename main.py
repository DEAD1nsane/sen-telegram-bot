import re
import os
import requests
from fastapi import FastAPI, Request, Response
import telebot
import redis
from google import genai
from google.genai import types

# Robust Redis connection fallback
redis_url = os.environ.get("REDIS_URL")
if not redis_url:
    host = os.environ.get("REDISHOST", "localhost")
    port = os.environ.get("REDISPORT", "6379")
    password = os.environ.get("REDISPASSWORD", "")
    redis_url = f"redis://default:{password}@{host}:{port}" if password else f"redis://{host}:{port}"

redis_client = redis.from_url(redis_url)

API_TOKEN = os.getenv("BOT_TOKEN")
bot = telebot.TeleBot(API_TOKEN)
app = FastAPI()

BOT_INFO = None
try:
    BOT_INFO = bot.get_me()
except Exception as e:
    print(f"Failed to fetch bot info: {e}")

gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

OWNER_ID = 6293437261

CACHED_FILE_IDS = {
    "sen": None,
    "magic": None
}

def free_web_search(query: str) -> str:
    try:
        url = "https://html.duckduckgo.com/html/"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        res = requests.post(url, data={"q": query}, headers=headers, timeout=10)
        
        snippets = re.findall(r'<a class="result__snippet[^">]*>(.*?)</a>', res.text, re.DOTALL)
        clean_snippets = [re.sub(r'<[^>]+>', '', s).strip() for s in snippets[:3]]
        
        if clean_snippets:
            return "\n".join(clean_snippets)
    except Exception as e:
        print(f"Search fetch error: {e}")
    return ""

@app.get("/")
def home_check():
    return {"status": "ok", "message": "Bot webhook server is running."}

@app.post("/getWebhookInfo")
@app.get("/getWebhookInfo")
def webhook_info_check():
    return {"status": "ok"}

@app.post("/webhook")
async def handle_webhook(request: Request):
    try:
        json_data = await request.json()
        update = telebot.types.Update.de_json(json_data)
        
        if update and update.message:
            user_id = update.message.from_user.id
            user_id_str = str(user_id)
            chat_id = update.message.chat.id
            msg_id = update.message.message_id
            text = update.message.text or ""
            is_private = update.message.chat.type == "private"

            if text.startswith(("/delete", "/del")):
                if user_id == OWNER_ID:
                    reply_msg = update.message.reply_to_message
                    if reply_msg and reply_msg.from_user.is_bot:
                        try:
                            bot.delete_message(chat_id, reply_msg.message_id)
                            bot.delete_message(chat_id, msg_id)
                        except Exception as del_err:
                            print(f"Error deleting message: {del_err}")
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
                    clean_prompt = re.sub(rf'{re.escape(bot_username)}', '', clean_prompt, flags=re.IGNORECASE)
                clean_prompt = re.sub(r'@gemini', '', clean_prompt, flags=re.IGNORECASE).strip()
                
                normalized_prompt = clean_prompt.rstrip("?").lower()

                if normalized_prompt in ["help", "how do you remember", "commands"]:
                    bot_uname_str = BOT_INFO.username if BOT_INFO else "SenAnythangBot"
                    help_text = (
                        "• Remember rule, rule, rule:\n"
                        "  • splits items by commas and adds each as its own rule... as many as you'd like.. add more any time.\n\n"
                        "• What do you remember?:\n"
                        "  • displays your rules in a numbered list format.\n\n"
                        "• Edit #:\n"
                        "  • edit a specific rule by it's number.\n\n"
                        "• Forget #, #, #:\n"
                        "  • removes specific memories by separating its number by commas... as many as you like in any order.\n\n"
                        "• Forget all:\n"
                        "  • self explanatory.\n\n"
                        f"Must mention @gemini or @{bot_uname_str} or direct reply to my messages without needing to mention followed by the command.\n\n"
                        "or private message me (which doesn't require reply or tag)."
                    )
                    if is_private:
                        bot.send_message(chat_id, help_text)
                    else:
                        bot.reply_to(update.message, help_text)
                    return Response(status_code=200)

                if clean_prompt.startswith("remember "):
                    raw_content = clean_prompt.replace("remember ", "", 1).strip()
                    if raw_content:
                        parts = [p.strip() for p in raw_content.split(",") if p.strip()]
                        for part in parts:
                            redis_client.rpush(f"memory_list:{user_id_str}", part)
                    if is_private:
                        bot.send_message(chat_id, "Got it, I've added those items to your memory list.")
                    else:
                        bot.reply_to(update.message, "Got it, I've added those items to your memory list.")
                    return Response(status_code=200)
                elif clean_prompt.lower().startswith("edit "):
                    edit_content = clean_prompt.replace("edit", "", 1).strip()
                    parts = edit_content.split(" ", 1)
                    if len(parts) == 2 and parts[0].isdigit():
                        index = int(parts[0]) - 1
                        new_fact = parts[1].strip()
                        raw_items = redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
                        if 0 <= index < len(raw_items) and new_fact:
                            redis_client.lset(f"memory_list:{user_id_str}", index, new_fact)
                            msg_text = f"Updated memory #{index + 1}."
                        else:
                            msg_text = "Invalid memory number or empty replacement text."
                    else:
                        msg_text = "Usage: edit [number] [new fact]"
                    
                    if is_private:
                        bot.send_message(chat_id, msg_text)
                    else:
                        bot.reply_to(update.message, msg_text)
                    return Response(status_code=200)
                elif clean_prompt.rstrip("?").lower() in ["forget all", "forget what you remember"]:
                    redis_client.delete(f"memory_list:{user_id_str}")
                    if is_private:
                        bot.send_message(chat_id, "I've forgotten everything I had saved.")
                    else:
                        bot.reply_to(update.message, "I've forgotten everything I had saved.")
                    return Response(status_code=200)
                elif clean_prompt.lower().startswith("forget "):
                    target_str = clean_prompt.replace("forget", "", 1).strip().rstrip("?")
                    raw_items = redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
                    items = [item.decode('utf-8') for item in raw_items] if raw_items else []
                    
                    targets = [p.strip() for p in target_str.split(",") if p.strip()]
                    removed_indices = []
                    invalid = False
                    
                    for t in targets:
                        if t.isdigit():
                            idx = int(t) - 1
                            if 0 <= idx < len(items):
                                removed_indices.append(idx)
                            else:
                                invalid = True
                        else:
                            invalid = True

                    if removed_indices and not invalid:
                        removed_indices = sorted(list(set(removed_indices)), reverse=True)
                        for idx in removed_indices:
                            redis_client.lset(f"memory_list:{user_id_str}", idx, "__DELETED__")
                        redis_client.lrem(f"memory_list:{user_id_str}", 0, "__DELETED__")
                        msg_text = f"Removed memory/memories: {', '.join(str(idx + 1) for idx in sorted([i for i in removed_indices]))}."
                    else:
                        msg_text = "Invalid memory numbers. Use 'forget [number, number]' or 'forget all'."

                    if is_private:
                        bot.send_message(chat_id, msg_text)
                    else:
                        bot.reply_to(update.message, msg_text)
                    return Response(status_code=200)
                elif normalized_prompt == "what do you remember":
                    raw_items = redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
                    if raw_items:
                        items = [item.decode('utf-8') for item in raw_items]
                        msg_text = "I remember:\n" + "\n".join(f"{i+1}. {item}" for i, item in enumerate(items))
                    else:
                        msg_text = "I don't remember anything yet."
                    
                    if is_private:
                        bot.send_message(chat_id, msg_text)
                    else:
                        bot.reply_to(update.message, msg_text)
                    return Response(status_code=200)

                if clean_prompt:
                    try:
                        raw_items = redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
                        saved_facts = [item.decode('utf-8') for item in raw_items] if raw_items else []
                        search_context = free_web_search(clean_prompt)
                        
                        final_prompt = clean_prompt
                        context_parts = []
                        if saved_facts:
                            context_parts.append("Saved Memories:\n" + "\n".join(f"• {f}" for f in saved_facts))
                        if search_context:
                            context_parts.append(f"Web Search Context:\n{search_context}")
                        
                        if context_parts:
                            final_prompt = "\n\n".join(context_parts) + f"\n\nUser Question: {clean_prompt}"

                        response = gemini_client.models.generate_content(
                            model='gemini-2.5-flash',
                            contents=final_prompt,
                            config=types.GenerateContentConfig(
                                system_instruction="Use the web search context and saved memories provided if relevant. Do not use markdown formatting such as bold (**), headers (#), or italics (*). Return plain text only."
                            )
                        )
                        raw_text = response.text or ""
                        clean_text = re.sub(r'[*_#`]', '', raw_text)
                        
                        if is_private:
                            bot.send_message(chat_id, clean_text)
                        else:
                            bot.send_message(chat_id, clean_text, reply_to_message_id=msg_id)
                    except Exception as ai_err:
                        print(f"Gemini API error: {ai_err}")
                        error_text = "Sorry, I had trouble processing that request."
                        if is_private:
                            bot.send_message(chat_id, error_text)
                        else:
                            bot.send_message(chat_id, error_text, reply_to_message_id=msg_id)
                return Response(status_code=200)

            entities = update.message.entities or []
            for entity in sorted(entities, key=lambda e: e.offset, reverse=True):
                if entity.type in ["code", "pre"]:
                    start = entity.offset
                    end = start + entity.length
                    text = text[:start] + (" " * entity.length) + text[end:]

            if re.search(r'\bsen\b', text, re.IGNORECASE):
                send_audio_track(chat_id, msg_id, "sen", "Devin_The_Dude_Anythang.mp3", "Anythang", "Devin The Dude", is_private)

            if re.search(r'\bmagic(?:al)?\b', text, re.IGNORECASE):
                send_audio_track(chat_id, msg_id, "magic", "Do You Believe In Magic.mp3", "Do You Believe In Magic", "The Lovin' Spoonful", is_private)

        return Response(status_code=200)
    except Exception as e:
        print(f"Webhook error: {e}")
        return Response(status_code=200)

def send_audio_track(chat_id, msg_id, key, file_path, title, performer, is_private):
    try:
        kwargs = {
            "title": title,
            "performer": performer,
            "timeout": 60
        }
        if not is_private:
            kwargs["reply_to_message_id"] = msg_id

        if CACHED_FILE_IDS.get(key):
            bot.send_audio(chat_id, CACHED_FILE_IDS[key], **kwargs)
        else:
            with open(file_path, "rb") as audio:
                msg = bot.send_audio(chat_id, audio, **kwargs)
                CACHED_FILE_IDS[key] = msg.audio.file_id
    except Exception as send_err:
        print(f"Error sending audio ({key}): {send_err}")
