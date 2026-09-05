"""Sen Telegram Bot - Entry point."""

from __future__ import annotations

import asyncio

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
    ]
    private = [
        BotCommand(command="memories", description="Manage your instructed memories"),
        BotCommand(command="del", description="Delete a bot message"),
    ]
    try:
        await bot.delete_my_commands(scope=BotCommandScopeAllChatAdministrators())
    except Exception as e:
        print(f"Could not clear administrator command scope: {e}")
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
        async def feed_update_with_capture(bot_instance, update, **kwargs):
            token = _RAW_UPDATE.set(update)
            try:
                return await original_feed_update(bot_instance, update, **kwargs)
            finally:
                _RAW_UPDATE.reset(token)
        dp.feed_update = feed_update_with_capture

    original_feed_raw_update = getattr(dp, "feed_raw_update", None)
    if original_feed_raw_update is not None:
        async def feed_raw_update_with_capture(bot_instance, update, **kwargs):
            token = _RAW_UPDATE.set(update)
            try:
                return await original_feed_raw_update(bot_instance, update, **kwargs)
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
                contents = TEMPORARY_MEDIA_LABEL_RE.sub('', contents)
                contents = (
                    "MEDIA AVAILABILITY RULE: No actual media attachment was recovered for this request. "
                    "Do not claim to have seen, heard, watched, or inspected media. "
                    "Do not infer that the user supplied media from Telegram reply-preview labels or wording. "
                    "Answer only from the text and other context actually supplied.\n\n"
                    + contents
                )
                kwargs["contents"] = contents
            return await original_generate_content(*args, **kwargs)
        gemini_models.generate_content = generate_content_with_media_guard
        print("Installed no-media Gemini guard")

    # Register handlers
    from sen.handlers import register_handlers
    register_handlers(dp, bot)

    # Health check web server
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(__import__("os").environ.get("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Operational check dashboard running on port {port}")

    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

    webhook_url = f"https://sen-telegram-bot-production.up.railway.app/webhook"
    try:
        await bot.set_webhook(url=webhook_url, drop_pending_updates=True)
        print(f"Webhook set to {webhook_url}")
    except Exception as e:
        print(f"Webhook setup error: {e}")

    async def on_shutdown(app):
        await bot.session.close()
        await redis_client.aclose()

    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
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
