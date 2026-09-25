"""Dark web browsing via Railway Tor proxy (OSINT/research only).

Tor stays server-side: the bot fetches .onion pages through the
tor-proxy SOCKS listener and renders a sanitized readable view in the
Telegram Web App. The Telegram client never touches Tor directly.
"""

from __future__ import annotations

import html as _html
import os
import re
from urllib.parse import urljoin, urlsplit

import httpx

TOR_SOCKS = os.environ.get("TOR_PROXY", "socks5h://tor-proxy.railway.internal:9050")

ONION_RE = re.compile(r"^(?:http://|https://)?[a-z2-7]{16,56}\.onion(?:/[^\s]*)?$", re.I)
ONION_HOST_RE = re.compile(r"[a-z2-7]{16,56}\.onion", re.I)

# Curated legit starting points — only addresses verified in widespread use.
DIRECTORY = [
    ("DuckDuckGo (onion)", "http://duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagvbroqkxad.onion"),
    ("ProPublica (onion)", "http://p53lf57qovyuvwsc6xnrppyply3vtqm7l6pcobkmyqsiofyeznfu5uqad.onion"),
]

# Refuse to facilitate these — handler checks before fetching.
REFUSAL_RE = re.compile(
    r"\b(child\s?porn|child abuse|loli|buy drugs|buy coke|buy heroin|buy meth|fentanyl vendor|"
    r"buy gun|buy weapon|hitman|murder for hire|hire a hacker|carding|stolen cards|"
    r"counterfeit money|fake passport|fake id vendor)\b",
    re.I,
)

_FETCH_TIMEOUT = 25.0
_MAX_BYTES = 1_500_000


def normalize_onion_url(raw: str) -> str | None:
    raw = (raw or "").strip().strip("<>").strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    if not ONION_RE.match(raw):
        return None
    return raw


def should_refuse(text: str) -> bool:
    return bool(REFUSAL_RE.search(text or ""))


async def tor_get(url: str) -> tuple[int, str, str]:
    """Fetch an .onion URL through Tor SOCKS. Returns (status, html, final_url)."""
    norm = normalize_onion_url(url)
    if not norm:
        raise ValueError("not an .onion URL")
    async with httpx.AsyncClient(
        proxy=TOR_SOCKS,
        timeout=_FETCH_TIMEOUT,
        follow_redirects=True,
        max_redirects=3,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) SenOnionBrowser/1.0"},
    ) as client:
        r = await client.get(norm)
        body = r.text
        if len(body.encode("utf-8", "ignore")) > _MAX_BYTES:
            body = body[:_MAX_BYTES]
        final = str(r.url)
        if not ONION_HOST_RE.search(final):
            raise ValueError("redirect left Tor network — blocked")
        return r.status_code, body, final


def sanitize_for_viewer(raw_html: str, base_url: str) -> str:
    """Strip dangerous markup; rewrite .onion links to browser deep-links."""
    text = raw_html or ""
    text = re.sub(r"(?is)<script.*?</script>", "", text)
    text = re.sub(r"(?is)<style.*?</style>", "", text)
    text = re.sub(r"(?is)<iframe.*?</iframe>", "", text)
    text = re.sub(r"(?is)<form.*?</form>", "", text)
    text = re.sub(r"(?is)<(input|button|select|textarea)[^>]*>", "", text)

    def _link(m: re.Match) -> str:
        href = (m.group(1) or "").strip()
        label = m.group(2) or href
        abs_url = urljoin(base_url, href)
        if ONION_HOST_RE.search(abs_url):
            return f'<a href="/browser?url={_html.escape(abs_url, quote=True)}">{label}</a>'
        return label

    text = re.sub(r'(?is)<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', _link, text)
    text = re.sub(r"(?is)<img[^>]*>", "[image — not loaded over Tor in viewer]", text)
    return text[:200_000]


def extract_text(raw_html: str, limit: int = 4000) -> str:
    text = re.sub(r"(?is)<script.*?</script>", " ", raw_html or "")
    text = re.sub(r"(?is)<style.*?</style>", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = _html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]
