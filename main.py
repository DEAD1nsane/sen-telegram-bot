import asyncio
import logging
import json
import os
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

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(
    "gemini-1.5-flash"
)  # Using standard flash for reliability


# ... (Helpers)
async def perform_search(query: str) -> str:
    async with aiohttp.ClientSession() as session:
        async with session.get(
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
@search_router.message(F.text.lower().contains("search"))
async def handle_search(message: types.Message):
    search_context = await perform_search(message.text)
    prompt = f"Context: {search_context}\n\nQuestion: {message.text}"
    response = model.generate_content(prompt)
    await message.answer(response.text)


dp.include_router(search_router)


# ... (Handlers)
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("<b>Sen Bot</b> initialized. Use commands or chat directly.")


@dp.message()
async def handle_message(message: types.Message):
    # Only if not handled by search_router
    response = model.generate_content(message.text)
    await message.answer(response.text)


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
