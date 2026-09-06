"""Web search via SearXNG, intent detection, and source link formatting."""

from __future__ import annotations

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
        r"^\s*(?:please\s+)?(?:send|give|show|fetch|get)\s+me\s+(?:some\s+|the\s+)?",
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
    "search", "google", "look up", "lookup", "find out", "search the web",
    "browse", "web search", "internet", "online", "news", "headlines",
    "latest", "newest", "recent", "tonight", "this week",
    "right now", "currently", "what happened", "who won", "score", "price",
    "release date", "schedule", "status", "update", "source", "sources",
)

_IMPLICIT_QUESTION_WORDS = (
    "who", "what", "when", "where", "why", "how",
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
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36", "Accept": "application/json"}
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
    """Build a collapsible source footnote section with numbered links."""
    entries = source_entries(search_context)
    if not entries:
        return ""
    lines = ["<details><summary>Sources</summary>"]
    for i, (title, url) in enumerate(entries, 1):
        safe_title = html.escape(title, quote=False)
        safe_url = html.escape(url, quote=True)
        domain = re.sub(r"https?://(www\.)?", "", url).split("/")[0]
        lines.append(f'<p>[<a href="{safe_url}">{i}</a>] {safe_title} - <code>{domain}</code></p>')
    lines.append("</details>")
    return "\n\n" + "\n".join(lines)


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
        if re.match(r"^[-•]\s+\S.+(?:blog|comparison|guide|vs\.?|difference|202\d|full|key|what|which|should|10\+|underrated|battle|head.to.head)", stripped, re.I):
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
