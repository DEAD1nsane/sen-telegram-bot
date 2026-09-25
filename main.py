"""Sen Telegram Bot - Entry point."""

from __future__ import annotations

import asyncio
import os

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from sen.config import API_TOKEN, gemini_client, redis_client
import sen.config as _cfg
from sen.handlers import register_handlers

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


async def health_check(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "bot": _cfg.BOT_INFO.username if _cfg.BOT_INFO else None})


# ---------------------------------------------------------------------------
# Command configuration
# ---------------------------------------------------------------------------


async def configure_commands() -> None:
    from aiogram.types import (
        BotCommand,
        BotCommandScopeAllChatAdministrators,
        BotCommandScopeAllGroupChats,
        BotCommandScopeAllPrivateChats,
    )

    group = [
        BotCommand(command="memories", description="Open your private memory menu", is_ephemeral=True),
        BotCommand(command="del", description="Delete a bot message", is_ephemeral=True),
        BotCommand(command="play", description="Play the Minesweeper web game"),
        BotCommand(command="scores", description="Show Minesweeper high scores"),
        BotCommand(command="mines", description="Play Minesweeper in chat using Python"),
        BotCommand(command="onion", description="Open the Tor onion browser"),
    ]
    private = [
        BotCommand(command="memories", description="Manage your instructed memories"),
        BotCommand(command="del", description="Delete a bot message"),
        BotCommand(command="play", description="Play the Minesweeper web game"),
        BotCommand(command="scores", description="Show Minesweeper high scores"),
        BotCommand(command="mines", description="Play Minesweeper in chat using Python"),
        BotCommand(command="onion", description="Open the Tor onion browser"),
    ]
    admin = [
        BotCommand(command="memories", description="Open your private memory menu", is_ephemeral=True),
        BotCommand(command="del", description="Delete a bot message", is_ephemeral=True),
        BotCommand(command="play", description="Play the Minesweeper web game"),
        BotCommand(command="scores", description="Show Minesweeper high scores"),
        BotCommand(command="mines", description="Play Minesweeper in chat using Python"),
        BotCommand(command="onion", description="Open the Tor onion browser"),
    ]
    try:
        await bot.set_my_commands(admin, scope=BotCommandScopeAllChatAdministrators())
    except Exception as e:
        print(f"Could not set administrator command scope: {e}")
    await bot.set_my_commands(group, scope=BotCommandScopeAllGroupChats())
    await bot.set_my_commands(private, scope=BotCommandScopeAllPrivateChats())
    print("Configured group commands: /memories=ephemeral /del=ephemeral")
    try:
        g = await bot.get_my_commands(scope=BotCommandScopeAllGroupChats())
        print("Telegram group commands: " + str([(x.command, getattr(x, "is_ephemeral", None)) for x in g]))
    except Exception as e:
        print(f"Could not verify Telegram commands: {e}")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


async def main() -> None:
    _cfg.BOT_INFO = await bot.get_me()
    print(f"Logged in successfully as @{_cfg.BOT_INFO.username}")
    await configure_commands()

    # Install raw-update capture for advanced editor media
    from sen.media import _RAW_UPDATE

    original_feed_update = getattr(dp, "feed_update", None)
    if original_feed_update is not None:

        async def feed_update_with_capture(bot, update, **kwargs):
            token = _RAW_UPDATE.set(update)
            try:
                return await original_feed_update(bot, update, **kwargs)
            finally:
                _RAW_UPDATE.reset(token)

        dp.feed_update = feed_update_with_capture

    original_feed_raw_update = getattr(dp, "feed_raw_update", None)
    if original_feed_raw_update is not None:

        async def feed_raw_update_with_capture(bot, update, **kwargs):
            token = _RAW_UPDATE.set(update)
            try:
                return await original_feed_raw_update(bot, update, **kwargs)
            finally:
                _RAW_UPDATE.reset(token)

        dp.feed_raw_update = feed_raw_update_with_capture

    # Install no-media Gemini guard
    from sen.config import gemini_client
    import re

    gemini_models = getattr(getattr(gemini_client, "aio", None), "models", None)
    original_generate_content = getattr(gemini_models, "generate_content", None) if gemini_models is not None else None
    if original_generate_content is not None:
        from sen.config import TEMPORARY_MEDIA_LABEL_RE

        async def generate_content_with_media_guard(*args, **kwargs):
            contents = kwargs.get("contents")
            if isinstance(contents, str):
                contents = TEMPORARY_MEDIA_LABEL_RE.sub("", contents)
                contents = (
                    "MEDIA AVAILABILITY RULE: No actual media attachment was recovered for this request. "
                    "Do not claim to have seen, heard, watched, or inspected media. "
                    "Do not infer that the user supplied media from Telegram reply-preview labels or wording. "
                    "Answer only from the text and other context actually supplied.\n\n" + contents
                )
                kwargs["contents"] = contents
            return await original_generate_content(*args, **kwargs)

        gemini_models.generate_content = generate_content_with_media_guard
        print("Installed no-media Gemini guard")

    # Register handlers
    from sen.handlers import register_handlers

    register_handlers(dp, bot)

    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

    webhook_url = os.environ.get("WEBHOOK_URL", "https://sen-telegram-bot-production.up.railway.app/webhook")
    try:
        await bot.set_webhook(
            url=webhook_url,
            drop_pending_updates=True,
            allowed_updates=[
                "message",
                "edited_message",
                "channel_post",
                "edited_channel_post",
                "callback_query",
            ],
        )
        print(f"Webhook set to {webhook_url}")
    except Exception as e:
        print(f"Webhook setup error: {e}")

    async def on_shutdown(app):
        await bot.session.close()
        await redis_client.aclose()

    _CORS = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }

    async def handle_score_options(request: web.Request) -> web.Response:
        return web.Response(headers=_CORS)

    async def handle_score(request: web.Request) -> web.Response:
        """Accept a finished-game score from the web game and record it."""
        import hashlib
        import hmac as hmac_mod

        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400, headers=_CORS)
        try:
            uid, cid, mid, score = int(data["uid"]), int(data["chat"]), int(data["mid"]), int(data["score"])
            sig = str(data["sig"])
        except Exception:
            return web.json_response({"ok": False, "error": "bad fields"}, status=400, headers=_CORS)
        if not (0 <= score <= 10000):
            return web.json_response({"ok": False, "error": "score out of range"}, status=400, headers=_CORS)
        expect = hmac_mod.new(API_TOKEN.encode(), f"{uid}:{cid}:{mid}".encode(), hashlib.sha256).hexdigest()
        if not hmac_mod.compare_digest(expect, sig):
            return web.json_response({"ok": False, "error": "bad signature"}, status=403, headers=_CORS)
        try:
            await bot.set_game_score(user_id=uid, score=score, chat_id=cid, message_id=mid)
        except Exception as e:
            print(f"[SCORE] set failed: {type(e).__name__}: {e}")
            return web.json_response({"ok": False, "error": "telegram rejected"}, status=502, headers=_CORS)
        print(f"[SCORE] recorded {score} for {uid} in {cid}:{mid}")
        return web.json_response({"ok": True}, headers=_CORS)

    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    app.router.add_route("OPTIONS", "/score", handle_score_options)
    app.router.add_post("/score", handle_score)

    # --- Onion browser (Telegram Web App, Tor stays server-side) ---
    import pathlib as _pl

    async def handle_browser(_request: web.Request) -> web.Response:
        page = (_pl.Path(__file__).parent / "web" / "onion.html").read_text()
        return web.Response(text=page, content_type="text/html")

    async def handle_onion_directory(_request: web.Request) -> web.Response:
        from sen.onion import DIRECTORY

        return web.json_response({"ok": True, "sites": [{"name": n, "url": u} for n, u in DIRECTORY]}, headers=_CORS)

    async def handle_onion_fetch(request: web.Request) -> web.Response:
        from sen import onion as _on

        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400, headers=_CORS)
        url = str(data.get("url", ""))
        if _on.should_refuse(url):
            return web.json_response({"ok": False, "error": "blocked"}, status=403, headers=_CORS)
        if not _on.normalize_onion_url(url):
            return web.json_response({"ok": False, "error": "not a valid .onion URL"}, status=400, headers=_CORS)
        try:
            status, raw, final = await _on.tor_get(url)
            return web.json_response(
                {
                    "ok": True,
                    "status": status,
                    "final_url": final,
                    "html": _on.extract_body_html(raw, final),
                    "text": _on.extract_text(raw),
                },
                headers=_CORS,
            )
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400, headers=_CORS)
        except Exception as e:
            print(f"[ONION] fetch failed: {type(e).__name__}: {e}")
            return web.json_response({"ok": False, "error": "Tor fetch failed"}, status=502, headers=_CORS)

    async def handle_onion_search(request: web.Request) -> web.Response:
        from sen import onion as _on
        from sen.search import searx_request

        q = (request.query.get("q", "") or "").strip()
        if not q:
            return web.json_response({"ok": False, "error": "empty query"}, status=400, headers=_CORS)
        if _on.should_refuse(q):
            return web.json_response({"ok": False, "error": "blocked"}, status=403, headers=_CORS)
        try:
            results = await searx_request(q, "general", None, 1, 10)
        except Exception as e:
            print(f"[ONION] search failed: {type(e).__name__}: {e}")
            return web.json_response({"ok": False, "error": "search failed"}, status=502, headers=_CORS)
        out = []
        for r in results:
            url = str(r.get("url", ""))
            if not _on.is_onion_url(url):
                continue
            out.append({"title": r.get("title") or url, "url": url, "snippet": (r.get("content") or "")[:300]})
            if len(out) >= 8:
                break
        return web.json_response({"ok": True, "results": out}, headers=_CORS)

    app.router.add_get("/browser", handle_browser)
    app.router.add_get("/api/onion/directory", handle_onion_directory)
    app.router.add_post("/api/onion/fetch", handle_onion_fetch)
    app.router.add_get("/api/onion/search", handle_onion_search)

    async def handle_onion_img(request: web.Request) -> web.Response:
        """Proxy an .onion image through Tor so the client can render it."""
        from sen import onion as _on

        url = (request.query.get("u", "") or "").strip()
        if _on.should_refuse(url):
            return web.Response(status=403, text="blocked")
        if not _on.is_onion_url(url):
            return web.Response(status=400, text="not an onion image")
        try:
            ctype, data = await _on.tor_get_bytes(url)
        except ValueError as e:
            return web.Response(status=400, text=str(e))
        except Exception as e:
            print(f"[ONION] img failed: {type(e).__name__}: {e}")
            return web.Response(status=502, text="Tor fetch failed")
        if not ctype.startswith("image/"):
            return web.Response(status=415, text="not an image")
        return web.Response(body=data, content_type=ctype)

    app.router.add_get("/api/onion/img", handle_onion_img)
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    app.on_cleanup.append(on_shutdown)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(__import__("os").environ.get("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Webhook server running on port {port}")

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
