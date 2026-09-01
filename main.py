import asyncio
import logging
import json
import os
import re
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio import Redis
import google.generativeai as genai

# ... (Configuration)
BOT_TOKEN = os.getenv("BOT_TOKEN")
REDIS_URL = os.getenv("REDIS_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SEARXNG_URL = os.getenv(
    "SEARXNG_URL", "https://searxng-railway-production-3252.up.railway.app/search"
)

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Bot, Dispatcher, Redis, Gemini
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
redis = Redis.from_url(REDIS_URL)
storage = RedisStorage(redis=redis)
dp = Dispatcher(storage=storage)
search_router = Router()
# Persistent HTTP session
http_session = aiohttp.ClientSession()


genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(
    "gemini-1.5-flash"
)  # Using standard flash for reliability


# ... (Helpers)
async def perform_search(query: str) -> str:
    async with http_session.get(
        SEARXNG_URL, params={"q": query, "format": "json"}
    ) as resp:
        if resp.status == 200:
            data = await resp.json()
            snippets = [
                f"Title: {r.get('title')}\nURL: {r.get('url')}"
                for r in data.get("results", [])[:5]
            ]
            return "\n\n".join(snippets)
    return ""


# ... (Router setup)
# Helper to send Rich Messages
async def send_rich_message(chat_id, rich_message_data):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendRichMessage"
    payload = {"chat_id": chat_id, "rich_message": rich_message_data}
    async with http_session.post(url, json=payload) as resp:
        return await resp.json()


def extract_json(text):
    # Try to find the first '{' and last '}'
    match = re.search(r"\{.*?\}", text, re.DOTALL)
    if match:
        return match.group(0)
    return None


async def send_ai_response(message: types.Message, response_text: str):
    # Try to extract and parse JSON for RichMessage
    json_str = extract_json(response_text)
    if json_str:
        try:
            rich_data = json.loads(json_str)
            if "blocks" in rich_data:
                await send_rich_message(message.chat.id, rich_data)
                return
        except json.JSONDecodeError:
            pass

    # Fallback to plain text if not valid RichMessage JSON
    try:
        await message.answer(response_text)
    except Exception:
        await message.answer(response_text, parse_mode=None)


# ... (Router setup)
@search_router.message(F.text.lower().contains("search"))
async def handle_search(message: types.Message):
    search_context = await perform_search(message.text)
    prompt = (
        f"Context: {search_context}\n\nQuestion: {message.text}\n\n"
        "If you need to display a table or complex layout, output ONLY the valid JSON for a Telegram InputRichMessage "
        "(with 'blocks' array containing InputRichBlockTable etc). Do not include any introductory text."
    )
    response = model.generate_content(prompt)
    await send_ai_response(message, response.text)


dp.include_router(search_router)


# ... (Handlers)
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("<b>Sen Bot</b> initialized. Use commands or chat directly.")


@dp.message()
async def handle_message(message: types.Message):
    # Only if not handled by search_router
    prompt = (
        f"Question: {message.text}\n\n"
        "If you need to display a table or complex layout, output ONLY the valid JSON for a Telegram InputRichMessage "
        "(with 'blocks' array containing InputRichBlockTable etc). Do not include any introductory text."
    )
    response = model.generate_content(prompt)
    await send_ai_response(message, response.text)


# ==========================================
# Healthcheck & Startup
# ==========================================


async def health_check(request):
    return web.Response(text="OK", status=200)


async def run_http_server():
    app = web.Application()
    app.router.add_get("/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    logger.info("Healthcheck server running on port 8080")


async def main():
    # Start healthcheck server in background
    asyncio.create_task(run_http_server())

    # Start polling
    logger.info("Starting bot polling")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
