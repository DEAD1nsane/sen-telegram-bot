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

# Curated legit starting points — addresses verified against the
# operators' own published pages (DuckDuckGo CSP headers / Wikipedia,
# ProPublica's own Tor announcement + Wikipedia).
DIRECTORY = [
    ("DuckDuckGo (onion)", "http://duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagswzczad.onion"),
    ("ProPublica (onion)", "http://p53lf57qovyuvwsc6xnrppyply3vtqm7l6pcobkmyqsiofyeznfu5uqd.onion"),
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


def is_onion_url(url: str) -> bool:
    """True only if the URL's host is actually a .onion address."""
    try:
        host = (urlsplit(url).hostname or "").lower()
        return host.endswith(".onion") and bool(ONION_HOST_RE.fullmatch(host))
    except Exception:
        return False


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


# ---------------------------------------------------------------------------
# Mines-style rich cards (everything stays inside Telegram)
# ---------------------------------------------------------------------------


def browser_base() -> str:
    base = (
        (os.environ.get("WEBHOOK_URL", "https://sen-telegram-bot-production.up.railway.app/webhook") or "")
        .rsplit("/webhook", 1)[0]
        .rstrip("/")
    )
    if not base.startswith("https://"):
        base = "https://sen-telegram-bot-production.up.railway.app"
    return base


def _para(header: str):
    from aiogram.types import InputRichBlockParagraph

    from .memory import rich_text_from_markup

    return InputRichBlockParagraph(text=rich_text_from_markup(header))


def directory_card(browser_url: str):
    """Start card: directory buttons + full-browser link. Taps stay in chat."""
    from aiogram.types import InputRichBlockButtons, InputRichMessage, RichMessageButton

    blocks: list = [_para("<b>🧅 ONION BROWSER</b>  <code>research only</code>")]
    for i, (name, _url) in enumerate(DIRECTORY):
        blocks.append(
            InputRichBlockButtons(buttons=[RichMessageButton(text=f"🧅 {name}", callback_data=f"onion:site:{i}")])
        )
    blocks.append(
        InputRichBlockButtons(buttons=[RichMessageButton(text="🌐 Full browser (web view)", url=browser_url)])
    )
    return InputRichMessage(blocks=blocks)


def loading_card(label: str):
    from aiogram.types import InputRichMessage

    return InputRichMessage(blocks=[_para(f"<b>🧅 ONION BROWSER</b>\n{_html.escape(label)}")])


def page_card(title: str, url: str, text: str, browser_url: str):
    """Fetched-page card: text excerpt + Back + full-browser buttons."""
    from aiogram.types import InputRichBlockButtons, InputRichMessage, RichMessageButton

    safe_title = _html.escape((title or url)[:120])
    safe_text = _html.escape((text or "Empty page.")[:900])
    blocks: list = [_para(f"<b>🧅 {safe_title}</b>  <code>{_html.escape(url[:80])}</code>\n{safe_text}")]
    blocks.append(
        InputRichBlockButtons(
            buttons=[
                RichMessageButton(text="⬅️ Directory", callback_data="onion:dir"),
                RichMessageButton(text="🌐 Full view", url=browser_url),
            ]
        )
    )
    return InputRichMessage(blocks=blocks)


def results_card(query: str, results: list[dict], browser_url: str):
    """Search-result card: one tap-button per .onion hit."""
    from aiogram.types import InputRichBlockButtons, InputRichMessage, RichMessageButton

    blocks: list = [_para(f"<b>🧅 ONION RESULTS</b>  <code>{_html.escape(query[:60])}</code>")]
    for i, r in enumerate(results[:6]):
        label = str(r.get("title") or r.get("url") or "link")[:40]
        blocks.append(
            InputRichBlockButtons(buttons=[RichMessageButton(text=f"🧅 {label}", callback_data=f"onion:res:{i}")])
        )
    blocks.append(
        InputRichBlockButtons(
            buttons=[
                RichMessageButton(text="⬅️ Directory", callback_data="onion:dir"),
                RichMessageButton(text="🌐 Full view", url=browser_url),
            ]
        )
    )
    return InputRichMessage(blocks=blocks)


def error_card(message: str, browser_url: str):
    from aiogram.types import InputRichBlockButtons, InputRichMessage, RichMessageButton

    blocks: list = [_para(f"<b>🧅 ONION BROWSER</b>\n{_html.escape(message)}")]
    blocks.append(
        InputRichBlockButtons(
            buttons=[
                RichMessageButton(text="⬅️ Directory", callback_data="onion:dir"),
                RichMessageButton(text="🌐 Full view", url=browser_url),
            ]
        )
    )
    return InputRichMessage(blocks=blocks)
