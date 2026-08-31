import json
import re
from datetime import datetime, timezone
from js import fetch, Headers, Request, Response

# ==========================================
# Helpers & Web Search
# ==========================================

async def free_web_search(query: str, searxng_url: str) -> str:
    """Performs web search via native fetch."""
    headers = Headers.new({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}.items())
    
    # 1. Try SearXNG
    try:
        url = f"{searxng_url}?q={query}&format=json"
        req = Request.new(url, method="GET", headers=headers)
        res = await fetch(req)
        if res.status == 200:
            data = json.loads(await res.text())
            results = data.get("results", [])[:15]
            snippets = []
            for item in results:
                title = item.get("title", "")
                content = item.get("content", "")
                link = item.get("url", "")
                if title or content:
                    snippets.append(f"Title: {title}\nContent: {content}\nURL: {link}")
            if snippets:
                return "\n\n".join(snippets)
    except Exception as e:
        print(f"SearXNG error: {e}")

    # 2. Fallback to DuckDuckGo HTML
    try:
        ddg_url = f"https://html.duckduckgo.com/html/?q={query}"
        req = Request.new(ddg_url, method="POST", headers=headers)
        res = await fetch(req)
        html = await res.text()
        raw = re.findall(r'<a class="result__snippet[^">]*>(.*?)</a>', html, re.DOTALL)
        urls = re.findall(r'href="(https?://[^"]+)"', html)
        clean = []
        for i, snippet in enumerate(raw[:15]):
            text_clean = re.sub(r'<[^>]+>', '', snippet).strip()
            link = urls[i] if i < len(urls) else ""
            if text_clean:
                clean.append(f"Content: {text_clean}\nURL: {link}")
        if clean:
            return "\n\n".join(clean)
    except Exception as e:
        print(f"DuckDuckGo error: {e}")

    return ""

async def upstash_command(redis_url: str, *args):
    """Executes Redis commands via Upstash REST API."""
    try:
        host = redis_url.split("@")[-1].split(":")[0] if "@" in redis_url else redis_url
        token = redis_url.split("//")[1].split("@")[0].split(":")[-1] if "@" in redis_url else ""
        
        endpoint = f"https://{host}"
        headers = Headers.new({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }.items())
        
        url = f"{endpoint}/"
        req = Request.new(url, method="POST", headers=headers, body=json.dumps(args))
        res = await fetch(req)
        if res.status == 200:
            data = json.loads(await res.text())
            return data.get("result")
    except Exception as e:
        print(f"Upstash error: {e}")
    return None

async def send_telegram_message(bot_token: str, chat_id: int, text: str, reply_to_msg_id: int = None):
    """Sends HTML formatted response to Telegram Bot API."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_to_msg_id:
        payload["reply_to_message_id"] = reply_to_msg_id

    headers = Headers.new({"Content-Type": "application/json"}.items())
    req = Request.new(url, method="POST", headers=headers, body=json.dumps(payload))
    await fetch(req)

async def call_gemini_api(prompt: str, system_instruction: str, api_key: str) -> str:
    """Invokes Gemini 2.5 Flash via REST endpoint."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    payload = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": [{"parts": [{"text": prompt}]}]
    }
    headers = Headers.new({"Content-Type": "application/json"}.items())
    req = Request.new(url, method="POST", headers=headers, body=json.dumps(payload))
    res = await fetch(req)
    if res.status == 200:
        data = json.loads(await res.text())
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            return "I don't have enough details to answer that accurately."
    return "I am currently broken right now, the owner needs to fix me."

# ==========================================
# Worker Handler Entrypoint
# ==========================================

async def on_fetch(request, env):
    if request.method != "POST":
        return Response.new("OK - Worker Running", status=200)

    try:
        body_text = await request.text()
        update = json.loads(body_text)

        if "message" in update:
            msg = update["message"]
            chat_id = msg["chat"]["id"]
            user_id_str = str(msg["from"]["id"])
            chat_type = msg["chat"]["type"]
            msg_id = msg["message_id"]
            text = (msg.get("text") or msg.get("caption") or "").strip()

            bot_token = env.BOT_TOKEN
            gemini_key = env.GEMINI_API_KEY
            redis_url = env.REDIS_URL
            searxng_url = getattr(env, "SEARXNG_URL", "https://searxng-railway-production-3252.up.railway.app/search")

            # Rate Limiting via Upstash
            cooldown_key = f"cooldown:{user_id_str}"
            cooldown = await upstash_command(redis_url, "EXISTS", cooldown_key)
            if cooldown == 1:
                await send_telegram_message(bot_token, chat_id, "Slow down, request limit reached.", msg_id)
                return Response.new("OK", status=200)
            
            await upstash_command(redis_url, "SET", cooldown_key, "1", "EX", 4)

            # Commands
            if text.lower() in ["/help", "help", "/commands"]:
                content = (
                    "<b>Sen Bot Command Hub</b>\n\n"
                    "<ul>"
                    "<li><b>remember [item]</b> - Adds item to memory</li>"
                    "<li><b>what do you remember</b> - Displays rules</li>"
                    "<li><b>forget all</b> - Clears memory</li>"
                    "</ul>"
                )
                await send_telegram_message(bot_token, chat_id, content, msg_id)
                return Response.new("OK", status=200)

            if text.lower() == "forget all":
                await upstash_command(redis_url, "DEL", f"memory_list:{user_id_str}")
                await send_telegram_message(bot_token, chat_id, "Cleared all your saved memories.", msg_id)
                return Response.new("OK", status=200)

            # Perform Web Search if needed
            search_keywords = {"search", "google", "look up", "lookup", "find", "show"}
            explicit_search = any(word in text.lower() for word in search_keywords)
            search_context = await free_web_search(text, searxng_url) if explicit_search else ""

            today_str = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
            bot_instructions = (
                f"Today's date is {today_str}. Keep responses structural using double line-breaks. "
                "Never use standard AI pleasantries. Do not start responses with 'As an AI'. "
                "CRITICAL FORMATTING RULE: You must natively structure all of your output utilizing semantic HTML strings (e.g., <b> for bold, <ul> and <li> for lists)."
            )

            if search_context:
                bot_instructions += (
                    "\n\nWhen referencing 'Web Search Context', state the information directly without saying 'According to my search'."
                    f"\nWeb Search Context:\n{search_context}"
                )

            response_text = await call_gemini_api(text, bot_instructions, gemini_key)
            reply_id = None if chat_type == "private" else msg_id
            await send_telegram_message(bot_token, chat_id, response_text, reply_id)

    except Exception as err:
        print(f"Worker Error: {err}")

    return Response.new("OK", status=200)

