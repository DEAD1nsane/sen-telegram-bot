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
    redis_url = f"redis://default:{password}@{host}:{port}" if password else f"redis://{host}:{port}"
if "upstash" in redis_url.lower() and redis_url.startswith("redis://"):
    redis_url = redis_url.replace("redis://", "rediss://", 1)
redis_client = redis.from_url(redis_url)

# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

API_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or ""
if not API_TOKEN:
    raise ValueError("CRITICAL CONFIGURATION ERROR: 'BOT_TOKEN' missing. Set BOT_TOKEN in Railway Variables.")
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://searxng.railway.internal:8080/search").rstrip("/")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
if not GEMINI_API_KEY:
    raise ValueError("CRITICAL CONFIGURATION ERROR: 'GEMINI_API_KEY' missing.")
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# Chats where the bot reads and summarises photos on its own, without being
# tagged: a photo album or a ZIP of images is summarised as one paced reply.
# Comma separated chat ids; empty disables the feature entirely.
PHOTO_BATCH_CHAT_IDS = {
    int(part) for part in os.getenv("PHOTO_BATCH_CHAT_IDS", "").split(",") if part.strip().lstrip("-").isdigit()
}

BOT_INFO = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INTERACTION_TTL = 300
MENU_TTL = 300
SEARCH_CACHE_TTL = 300
AUDIO_CACHE_TTL = 60 * 60 * 24 * 30

# Telegram refuses bot downloads above 20 MB, which is also well under the
# Gemini inline-data ceiling, so everything a document path fetches can be inlined.
TELEGRAM_DOWNLOAD_MAX_BYTES = 20 * 1024 * 1024

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

#: Single source of truth for keyword-triggered audio. To add a trigger:
#: drop the mp3 in the music/ directory, then add one line here —
#:   key: (word-boundary regex, filename, title, performer).
#: Insertion order sets send order; several keys may share one filename
#: (deduped downstream). Title/performer must always be filled in: Telegram
#: only shows the music-card metadata because we pass it on every send.
#: Matching is case-insensitive and ignores inline code spans (`code`, `pre`, backticks).
TRIGGER_AUDIO_TRACKS: dict[str, tuple[str, str, str | None, str | None]] = {
    # sen, tip -> Teeeeep (order: sen first to match test expectations)
    "sen": (r"\bsen\b", "Teeeeep.mp3", "Teeeeep", "Shinigami In Name Only"),
    "tip": (r"\btip\b", "Teeeeep.mp3", "Teeeeep", "Shinigami In Name Only"),
    # magic, magical -> Do You Believe In Magic (order: 2nd for tests)
    "magic": (
        r"\bmagic(?:al)?\b",
        "Do You Believe In Magic.mp3",
        "Do You Believe In Magic",
        "The Lovin' Spoonful",
    ),
    # dust -> Dust In A Baggie (order: 3rd for tests)
    "dust": (r"\bdust\b", "Dust In A Baggie.mp3", "Dust In A Baggie", "Billy Strings"),
    # trump -> I Hate U B
    "trump": (r"\btrump\b", "I Hate U B.mp3", "I Hate U B", "Princess Nokia"),
    # smd, idgaf, happy, rish, rich, hoe, ho, bitch -> Joy
    "smd": (r"\bsmd\b", "Joy.mp3", "Joy", "Joy Again"),
    "idgaf": (r"\bidgaf\b", "Joy.mp3", "Joy", "Joy Again"),
    "happy": (r"\bhappy\b", "Joy.mp3", "Joy", "Joy Again"),
    "rish": (r"\brish\b", "Joy.mp3", "Joy", "Joy Again"),
    "rich": (r"\brich\b", "Joy.mp3", "Joy", "Joy Again"),
    "hoe": (r"\bhoe\b", "Joy.mp3", "Joy", "Joy Again"),
    "ho": (r"\bho(?:s|es)?\b", "Joy.mp3", "Joy", "Joy Again"),
    "bitch": (r"\bbitch(?:es)?\b", "Joy.mp3", "Joy", "Joy Again"),
}

#: Derived views kept for delivery code and tests.
TRIGGER_AUDIO_FILES: dict[str, str] = {key: track[1] for key, track in TRIGGER_AUDIO_TRACKS.items()}
AUDIO_METADATA: dict[str, tuple[str | None, str | None]] = {
    track[1]: (track[2], track[3]) for track in TRIGGER_AUDIO_TRACKS.values()
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
