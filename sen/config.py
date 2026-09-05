"""Bot configuration and constants."""

import os
import re

import redis.asyncio as redis
from google import genai

# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------

redis_url = os.environ.get("REDIS_URL", "")
if not redis_url:
    host = os.environ.get("REDISHOST", "localhost")
    port = os.environ.get("REDISPORT", "6379")
    password = os.environ.get("REDISPASSWORD", "")
    redis_url = (
        f"redis://default:{password}@{host}:{port}"
        if password
        else f"redis://{host}:{port}"
    )
if "upstash" in redis_url.lower() and redis_url.startswith("redis://"):
    redis_url = redis_url.replace("redis://", "rediss://", 1)
redis_client = redis.from_url(redis_url)

# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

API_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or ""
if not API_TOKEN:
    raise ValueError(
        "CRITICAL CONFIGURATION ERROR: 'BOT_TOKEN' missing. "
        "Set BOT_TOKEN in Railway Variables."
    )
SEARXNG_URL = os.getenv(
    "SEARXNG_URL", "http://searxng.railway.internal:8080/search"
).rstrip("/")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
if not GEMINI_API_KEY:
    raise ValueError("CRITICAL CONFIGURATION ERROR: 'GEMINI_API_KEY' missing.")
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

BOT_INFO = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INTERACTION_TTL = 300
MENU_TTL = 30
SEARCH_CACHE_TTL = 300
AUDIO_CACHE_TTL = 60 * 60 * 24 * 30

MENTION_ONLY_RE = re.compile(r"^(?:@[A-Za-z0-9_]{5,32}\s*)+$")
TEMPORARY_FORGET_RE = re.compile(
    r"\btemporarily\s+(?:forget|ignore)\s+(?:all\s+)?(?:your\s+)?"
    r"(?:saved\s+)?memories?\b",
    re.I,
)
TEMPORARY_MEDIA_LABEL_RE = re.compile(
    r'Message User is Replying To:\s*"(?:Video|GIF|Animation|Photo|Audio|'
    r'Voice message|Video message|Document)"\s*',
    re.I,
)

TRIGGER_AUDIO_FILES: dict[str, str] = {
    "sen": "Anythang.mp3",
    "magic": "Do You Believe In Magic.mp3",
    "magical": "Do You Believe In Magic.mp3",
}

AUDIO_METADATA: dict[str, tuple[str | None, str | None]] = {
    "Anythang.mp3": ("Anythang", "Devin The Dude"),
    "Do You Believe In Magic.mp3": ("Do You Believe In Magic", "The Lovin' Spoonful"),
}

# ---------------------------------------------------------------------------
# Redis key helpers
# ---------------------------------------------------------------------------


def interaction_key(chat_id: int, user_id: int) -> str:
    return f"memory_interaction:{chat_id}:{user_id}"


def menu_identity_key(chat_id: int, user_id: int) -> str:
    return f"memory_menu_identity:{chat_id}:{user_id}"


def search_cache_key(query: str, news: bool) -> str:
    return f"web_search:{'news' if news else 'general'}:{query.strip().lower()}"


def audio_cache_key(filename: str) -> str:
    return f"keyword_audio_file_id:{filename}"
