"""Web search via SearXNG, intent detection, and source link formatting."""

from __future__ import annotations

import contextlib
import html
import re
from contextvars import ContextVar
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import SEARXNG_URL, SEARCH_CACHE_TTL, redis_client, search_cache_key

_SEARCH_STATE: ContextVar[tuple[str, str]] = ContextVar("sen_search_state", default=("", ""))


def get_search_state() -> tuple[str, str]:
    return _SEARCH_STATE.get()


def set_search_state(query: str, result: str) -> None:
    _SEARCH_STATE.set((query, result))


def clean_url(url: str) -> str:
    """Strip query params and fragment from a URL."""
    if not url:
        return ""
    try:
        p = urlsplit(url)
        return urlunsplit((p.scheme, p.netloc, p.path, "", ""))
    except Exception:
        return url


def normalize_search_query(query: str) -> str:
    """Strip conversational filler from a search query."""
    query = re.sub(r"\s+", " ", (query or "")).strip()
    query = re.sub(
        r"^\s*(?:please\s+)?(?:google\s+)?(?:search|look\s+up|lookup|find(?:\s+out)?|search\s+the\s+web)\s*(?:for|about|on)?\s*",
        "",
        query,
        flags=re.I,
    )
    query = re.sub(
        r"^\s*(?:please\s+)?(?:send|give|show|fetch|get)\s+(?:me\s+)?(?:some\s+|the\s+)?",
        "",
        query,
        flags=re.I,
    )
    query = re.sub(
        r"\s*,?\s*(?:in|inside)\s+(?:collapsible|expandable)\s+(?:sections?|blocks?).*$",
        "",
        query,
        flags=re.I | re.S,
    )
    query = re.sub(
        r"\s+(?:and\s+)?(?:format|present|display|organize|put)\s+(?:it|them|the\s+results?)\s+.*$",
        "",
        query,
        flags=re.I | re.S,
    )
    return re.sub(r"\s+", " ", query).strip(" ,.-")


_EXPLICIT_SEARCH_MARKERS = (
    "search",
    "google",
    "look up",
    "lookup",
    "find out",
    "search the web",
    "browse",
    "web search",
    "internet",
    "online",
    "news",
    "headlines",
    "latest",
    "newest",
    "recent",
    "tonight",
    "this week",
    "right now",
    "currently",
    "what happened",
    "who won",
    "score",
    "price",
    "release date",
    "schedule",
    "status",
    "update",
    "source",
    "sources",
    "song",
    "songs",
    "youtube",
    "youtu.be",
    "video",
    "link",
    # Recency / office-holder signals. Short questions like "is Biden still
    # president" carry no other trigger, so without these the model answers
    # from stale training data instead of searching.
    "still",
    "as of",
    "up to date",
    "so far",
    "nowadays",
    "these days",
    "this year",
    "who is the",
    "who's the",
    "president",
    "prime minister",
    "who won",
    "who won the",
    "still in office",
    "current officeholder",
)

_IMPLICIT_QUESTION_WORDS = (
    "who",
    "what",
    "when",
    "where",
    "why",
    "how",
)


def detect_search_intent(text: str) -> bool:
    """Return True if the text likely warrants a web search."""
    t = re.sub(r"\s+", " ", (text or "")).strip().lower()
    if not t:
        return False
    if any(marker in t for marker in _EXPLICIT_SEARCH_MARKERS):
        return True
    question = re.search(
        r"\b(?:" + "|".join(_IMPLICIT_QUESTION_WORDS) + r")\b",
        t,
    )
    return bool(question and len(t.split()) >= 5)


def detect_explicit_search_intent(text: str) -> bool:
    """Return True only for explicit search keywords (not implicit questions)."""
    t = re.sub(r"\s+", " ", (text or "")).strip().lower()
    if not t:
        return False
    return any(marker in t for marker in _EXPLICIT_SEARCH_MARKERS)


_MUSIC_NOUN_RE = re.compile(r"\b(?:songs?|tracks?|music|album|single|playlist|ep)\b", re.I)
_STRONG_MUSIC_VERB_RE = re.compile(r"\b(?:listen\s+to|put\s+on|throw\s+on|stream)\b", re.I)
_PLAY_RE = re.compile(r"\bplay\b", re.I)
_BY_ARTIST_RE = re.compile(r"\bby\s+\S", re.I)


def is_music_request(prompt: str) -> bool:
    """True when the user is asking to be played something.

    "play wait and bleed by slipknot" names no keyword the media-link path used
    to look for, so without this it reached the model with no search at all and
    the model improvised a video id from memory. Bot commands are excluded so
    /play (Minesweeper) never becomes a music request, and bare "play" needs an
    artist so "play minesweeper" stays a game command.
    """
    t = (prompt or "").strip()
    if not t or t.startswith("/"):
        return False
    if _MUSIC_NOUN_RE.search(t) or _STRONG_MUSIC_VERB_RE.search(t):
        return True
    return bool(_PLAY_RE.search(t) and _BY_ARTIST_RE.search(t))


async def searx_request(
    query: str,
    category: str = "general",
    time_range: str | None = None,
    page: int = 1,
    limit: int = 10,
) -> list[dict]:
    """Execute a single SearXNG search request."""
    params: dict = {
        "q": query,
        "format": "json",
        "categories": category,
        "language": "en",
        "pageno": page,
        "safesearch": 1,
    }
    if time_range:
        params["time_range"] = time_range
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(follow_redirects=True, timeout=12.0) as client:
        r = await client.get(SEARXNG_URL, params=params, headers=headers)
        if r.status_code != 200:
            raise RuntimeError(f"SearXNG HTTP {r.status_code}")
        return r.json().get("results", []) or []


async def free_web_search(query: str, news: bool = False) -> str:
    """Search the web and return formatted results text."""
    search_query = normalize_search_query(query)
    if not search_query:
        return ""
    cache_key = search_cache_key(search_query, news)
    try:
        cached = await redis_client.get(cache_key)
        if cached:
            return cached.decode() if isinstance(cached, bytes) else str(cached)
    except Exception as e:
        print(f"Web search cache read failure: {e}")
    try:
        if news:
            results = await searx_request(search_query, "news", "day", 1, 8)
            if not results:
                results = await searx_request(search_query, "news", "week", 1, 8)
            if not results:
                results = await searx_request(search_query, "general", "month", 1, 8)
        else:
            results = await searx_request(search_query, "general", None, 1, 8)
        seen: set = set()
        out: list[str] = []
        for result in results:
            title = (result.get("title") or "").strip()
            content = (result.get("content") or result.get("snippet") or "").strip()
            url = clean_url(result.get("url", ""))
            published = (result.get("publishedDate") or result.get("published_date") or "").strip()
            source = (result.get("engine") or result.get("source") or "").strip()
            image_url = (result.get("img_src") or result.get("image") or result.get("thumbnail") or "").strip()
            key = url.lower() if url else (title.lower(), content[:120].lower())
            if key in seen or not (title or content or url):
                continue
            seen.add(key)
            lines = []
            if title:
                lines.append(f"Title: {title}")
            if source:
                lines.append(f"Source: {source}")
            if published:
                lines.append(f"Published: {published}")
            if content:
                lines.append(f"Content: {content}")
            if url:
                lines.append(f"URL: {url}")
            if image_url:
                lines.append(f"Image: {image_url}")
            out.append("\n".join(lines))
        result_text = "\n\n".join(out)
        if result_text:
            try:
                await redis_client.set(cache_key, result_text, ex=SEARCH_CACHE_TTL)
            except Exception as e:
                print(f"Web search cache write failure: {e}")
        set_search_state(query or "", result_text)
        return result_text
    except Exception as e:
        print(f"Web contextual search failure: {e}")
        return ""


def source_entries(search_context: str) -> list[tuple[str, str]]:
    """Parse search context into (title, url) source entries."""
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for chunk in re.split(r"\n\s*\n", (search_context or "").strip()):
        title_match = re.search(r"^Title:\s*(.+)$", chunk, re.I | re.M)
        url_match = re.search(r"^URL:\s*(https?://\S+)$", chunk, re.I | re.M)
        if not url_match:
            continue
        url = url_match.group(1).rstrip(".,)")
        if url in seen:
            continue
        seen.add(url)
        title = title_match.group(1).strip() if title_match else url
        entries.append((title, url))
        if len(entries) >= 8:
            break
    return entries


def source_links(search_context: str) -> str:
    """Build a collapsible source section with domain names."""
    entries = source_entries(search_context)
    if not entries:
        return ""
    domains = []
    for title, url in entries:
        domain = re.sub(r"https?://(www\.)?", "", url).split("/")[0]
        if domain not in domains:
            domains.append(domain)
    return "\n\n" + "<details><summary>Sources</summary>" + " | ".join(domains) + "</details>"


def replace_model_source_blocks(text: str) -> str:
    """Remove model-generated source blocks from response text."""
    pattern = re.compile(r"<details\b[^>]*>.*?</details>", re.I | re.S)

    def _replace(match: re.Match) -> str:
        block = match.group(0)
        if re.search(
            r"\b(?:source|sources|citation|citations|footnote|footnotes|links?|urls?)\b",
            block,
            re.I,
        ):
            return ""
        return block

    text = pattern.sub(_replace, text or "")

    SOURCE_NAMES = re.compile(
        r"GeeksforGeeks|Digital Aptech|Treehouse|Tech Insider|Stanza|Coddy|Proxidize|Udacity|"
        r"Stanford|W3Schools|MDN|freeCodeCamp|Baeldung|Medium|Dev\.to|Stack Overflow|Reddit|"
        r"Javatpoint|TutorialsPoint|Guru99|Simplilearn|CodingNinjas|Intellipaat|Edureka|"
        r"Coursera|Udemy|Pluralsight|Educative|HackerRank|LeetCode|Codecademy|Khan Academy|"
        r"MIT OCW|Roadmap\.sh|Chudovo|Bacancy|Scaler|InterviewBit|KnowledgeHut|"
        r"mygreatlearning|Analytics Vidhya|Towards Data Science|KDnuggets|DataCamp|"
        r"CodeSignal|Exercism|The Odin Project|Full Stack Open|Boot\.dev|Wesionary|"
        r"Section\.io|YouTube|Google Developers|Apple Developer|Microsoft Learn|"
        r"AWS Docs|Cloudflare Docs|DigitalOcean|Linode|Heroku|Vercel|Netlify",
        re.I,
    )
    lines = text.split("\n")
    cleaned = []
    in_source_block = False
    for line in lines:
        stripped = line.strip()
        if re.match(
            r"^[-•]\s+\S.+(?:blog|comparison|guide|vs\.?|difference|202\d|full|key|what|which|should|10\+|underrated|battle|head.to.head)",
            stripped,
            re.I,
        ):
            in_source_block = True
            continue
        if in_source_block and re.match(r"^[-•]\s+\S", stripped):
            continue
        if in_source_block and not stripped:
            in_source_block = False
            continue
        if SOURCE_NAMES.search(stripped) and not re.search(r"[<>(){}]", stripped):
            continue
        cleaned.append(line)

    return "\n".join(cleaned)


def asked_for_sources(text: str) -> bool:
    """Return True if the user explicitly asked for sources."""
    return bool(
        re.search(
            r"\b(?:source|sources|citation|citations|reference|references|footnote|footnotes|links?|urls?|cite|cited|attribut|credit)\b",
            text or "",
            re.I,
        )
    )


# ---------------------------------------------------------------------------
# YouTube link validation
# ---------------------------------------------------------------------------

#: Any YouTube URL shape -> its video id. Covers watch?v=, youtu.be/, shorts/,
#: live/, embed/ and the music. subdomain, with or without a scheme.
_YOUTUBE_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/|v/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})",
    re.I,
)

_OEMBED_URL = "https://www.youtube.com/oembed"
_PLAYABLE_TTL = 60 * 60 * 24 * 7


def youtube_video_id(url: str) -> str | None:
    """Return the 11-character video id, or None if this isn't a YouTube link."""
    m = _YOUTUBE_ID_RE.search(url or "")
    return m.group(1) if m else None


def as_music_url(url: str) -> str:
    """Rewrite a YouTube link to music.youtube.com.

    Telegram renders music.youtube.com as an audio card, which is what someone
    asking for a track actually wants, and the id is identical so the video
    behind it is unchanged.
    """
    vid = youtube_video_id(url)
    return f"https://music.youtube.com/watch?v={vid}" if vid else url


#: "music video", "official video", "the video" - only these want the watch page
#: rather than the audio card.
VIDEO_REQUEST_RE = re.compile(r"\bvideo\b", re.I)


#: A channel or title like VEVO, "Official Music Video" or an auto-generated
#: "- Topic" track is the label/artist upload rather than a fan rip.
_OFFICIAL_AUTHOR_RE = re.compile(r"\bvevo\b|official|\btopic\b|records|\blabel\b", re.I)
_OFFICIAL_TITLE_RE = re.compile(r"\bofficial\b|\bvevo\b|\blyric[s]?\s+video\b", re.I)


async def youtube_probe(url: str) -> tuple[bool, bool]:
    """Return (playable, official) for a YouTube id.

    A deleted, private or hallucinated id answers 404 on oEmbed, and Telegram
    builds its preview from the same metadata, so those links can never embed.
    The same response carries author_name and title, which is enough to tell a
    label upload from a fan rip without a second request. A transport failure
    reports playable-but-not-official so a YouTube outage degrades to today's
    behaviour instead of dropping the link.
    """
    vid = youtube_video_id(url)
    if not vid:
        return False, False
    key = f"yt_ok:{vid}"
    with contextlib.suppress(Exception):
        cached = await redis_client.get(key)
        if cached:
            val = cached.decode() if isinstance(cached, bytes) else str(cached)
            ok, _, official = val.partition(":")
            return ok == "1", official == "1"
    official = False
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=8.0) as client:
            r = await client.get(
                _OEMBED_URL, params={"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"}
            )
        ok = r.status_code == 200
        if ok:
            with contextlib.suppress(Exception):
                data = r.json()
                official = bool(
                    _OFFICIAL_AUTHOR_RE.search(data.get("author_name") or "")
                    or _OFFICIAL_TITLE_RE.search(data.get("title") or "")
                )
    except Exception as e:
        print(f"YouTube oEmbed check failed for {vid}: {type(e).__name__}")
        return True, False
    with contextlib.suppress(Exception):
        await redis_client.set(key, f"{'1' if ok else '0'}:{'1' if official else '0'}", ex=_PLAYABLE_TTL)
    return ok, official


async def pick_playable_youtube(
    search_context: str, want_video: bool = False, limit: int = 6
) -> tuple[str, str] | None:
    """Find the first YouTube link in search results that actually resolves.

    Returns (url, title) or None when every candidate is dead. Defaults to the
    music.youtube.com audio card, since a track is what people usually ask for;
    pass want_video for the ordinary watch page.
    """
    candidates: list[tuple[str, str]] = []
    for title, url in source_entries(search_context):
        vid = youtube_video_id(url)
        if not vid:
            continue
        candidates.append((vid, title))
        if len(candidates) >= limit:
            break
    if not candidates:
        return None

    import asyncio

    probes = await asyncio.gather(*(youtube_probe(f"https://www.youtube.com/watch?v={v}") for v, _ in candidates))
    host = "www.youtube.com" if want_video else "music.youtube.com"
    # Playability is the hard requirement; among links that actually embed, the
    # label/artist upload wins over a fan rip.
    playable = [(vid, title) for (vid, title), (ok, _) in zip(candidates, probes, strict=True) if ok]
    if not playable:
        return None
    chosen = next(
        ((vid, title) for (vid, title), (_, official) in zip(candidates, probes, strict=True) if official),
        playable[0],
    )
    return f"https://{host}/watch?v={chosen[0]}", chosen[1]
