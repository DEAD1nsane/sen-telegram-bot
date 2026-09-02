"""Railway entrypoint for Sen.

Keeps Telegram polling singleton protection and installs the private Rich
Message /help flow. The /help command is configured as an ephemeral command
by main.py, so group replies can be private without requiring the bot to be an
administrator.
"""

import asyncio
import contextvars
import uuid

import main
from aiogram.types import EphemeralMessageParameters, InputRichMessage, ReplyParameters


POLLING_LOCK_KEY = "sen:telegram:getupdates:lock"
POLLING_LOCK_TTL = 120
POLLING_LOCK_REFRESH = 30

_current_menu_user: contextvars.ContextVar[object] = contextvars.ContextVar(
    "current_menu_user", default=None
)


def _display_name(user) -> str:
    name = str(
        getattr(user, "first_name", None)
        or getattr(user, "username", None)
        or "User"
    ).strip()
    return name or "User"


def personalized_rich_main_menu() -> str:
    user = _current_menu_user.get()
    name = _display_name(user) if user is not None else "Your"
    possessive = f"{name}'" if name.endswith("s") else f"{name}'s"
    return f"""
<h2>{possessive} Instructions</h2>
<p>Manage your personal memory without cluttering the chat.</p>
<tg-button-row align="center">
  <tg-button type="callback_data" style="primary" data="memory_view">Memories</tg-button>
  <tg-button type="callback_data" style="success" data="memory_add">New memory</tg-button>
</tg-button-row>
<tg-button-row align="center">
  <tg-button type="callback_data" style="link" data="memory_close">Close</tg-button>
</tg-button-row>
""".strip()


main.rich_main_menu = personalized_rich_main_menu


async def private_help(message):
    """Send /help as a Rich Message, privately in groups."""
    user = message.from_user
    if user is None:
        return

    user_id = user.id
    chat_id = message.chat.id
    token = _current_menu_user.set(user)

    try:
        await main.clear_interaction(user_id)
        rich_message = InputRichMessage(html=main.rich_main_menu())

        if message.chat.type == "private":
            sent = await main.bot.send_rich_message(
                chat_id=chat_id,
                rich_message=rich_message,
            )
        else:
            ephemeral_id = getattr(message, "ephemeral_message_id", None)
            if ephemeral_id is None:
                # This should only happen if Telegram delivered /help as a
                # normal command instead of the configured ephemeral command.
                # Do not leak a private menu into the group.
                print(
                    f"/help ignored: no ephemeral_message_id for user={user_id} chat={chat_id}"
                )
                return

            # A reply to an incoming ephemeral command is itself ephemeral.
            # Explicitly providing both fields avoids relying on client/server
            # inference and keeps the Rich Message private to this user.
            sent = await main.bot.send_rich_message(
                chat_id=chat_id,
                rich_message=rich_message,
                reply_parameters=ReplyParameters(
                    ephemeral_message_id=ephemeral_id,
                ),
                ephemeral_message_parameters=EphemeralMessageParameters(
                    receiver_user_id=user_id,
                ),
            )

        sent_ephemeral_id = getattr(sent, "ephemeral_message_id", None)
        await main.schedule_menu_delete(
            chat_id,
            user_id,
            sent_ephemeral_id,
            None if sent_ephemeral_id is not None else getattr(sent, "message_id", None),
        )
    except Exception as e:
        print(f"/help Rich Message error: {type(e).__name__}: {e}")
    finally:
        _current_menu_user.reset(token)


# Replace the original /help handler instead of merely mutating its callback.
# This makes the replacement deterministic with aiogram's HandlerObject list.
main.router.message.handlers = [
    handler
    for handler in main.router.message.handlers
    if getattr(handler.callback, "__name__", "") != "handle_help"
]
main.router.message.register(private_help, main.Command("help"))


async def _wrap_callback(callback, original):
    token = _current_menu_user.set(callback.from_user)
    try:
        return await original(callback)
    finally:
        _current_menu_user.reset(token)


# Personalize callbacks that can return to the main menu, especially Back.
for handler in main.router.callback_query.handlers:
    original = handler.callback
    if getattr(original, "__name__", "") in {
        "handle_memory_view",
        "handle_memory_add",
        "handle_memory_edit",
        "handle_memory_forget",
        "handle_memory_forget_all",
        "handle_confirm_forget_all",
        "handle_memory_back",
        "handle_memory_close",
    }:
        async def wrapped(callback, _original=original):
            return await _wrap_callback(callback, _original)

        handler.callback = wrapped


# Public Rich menus are not expected anymore. Keep a safety guard so an
# accidentally public menu cannot be edited or closed by another user.
main._original_edit_menu = main.edit_menu
main._original_close_menu = main.close_menu


async def guarded_edit_menu(callback, rich_html):
    message = callback.message
    if (
        message
        and getattr(message.chat, "type", None) != "private"
        and getattr(message, "ephemeral_message_id", None) is None
    ):
        await callback.answer(
            "This private menu has expired or is no longer available.",
            show_alert=True,
        )
        return
    await main._original_edit_menu(callback, rich_html)


async def guarded_close_menu(callback):
    message = callback.message
    if (
        message
        and getattr(message.chat, "type", None) != "private"
        and getattr(message, "ephemeral_message_id", None) is None
    ):
        await callback.answer(
            "This private menu has expired or is no longer available.",
            show_alert=True,
        )
        return
    await main._original_close_menu(callback)


main.edit_menu = guarded_edit_menu
main.close_menu = guarded_close_menu


async def acquire_polling_lock() -> str | None:
    token = uuid.uuid4().hex
    acquired = await main.redis_client.set(
        POLLING_LOCK_KEY,
        token,
        ex=POLLING_LOCK_TTL,
        nx=True,
    )
    return token if acquired else None


async def refresh_polling_lock(token: str) -> None:
    script = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
        return redis.call('expire', KEYS[1], ARGV[2])
    end
    return 0
    """
    while True:
        await asyncio.sleep(POLLING_LOCK_REFRESH)
        try:
            result = await main.redis_client.eval(
                script, 1, POLLING_LOCK_KEY, token, POLLING_LOCK_TTL
            )
            if result == 0:
                print("WARNING: Telegram polling lock was lost.")
                return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"Polling lock refresh error: {e}")


async def release_polling_lock(token: str) -> None:
    script = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
        return redis.call('del', KEYS[1])
    end
    return 0
    """
    try:
        await main.redis_client.eval(script, 1, POLLING_LOCK_KEY, token)
    except Exception as e:
        print(f"Polling lock release error: {e}")


async def run() -> None:
    token = await acquire_polling_lock()
    if token is None:
        print(
            "Another Sen worker already owns the Telegram polling lock. "
            "Refusing to start a second getUpdates consumer."
        )
        return

    print("Telegram polling singleton lock acquired.")
    refresh_task = asyncio.create_task(refresh_polling_lock(token))
    try:
        await main.main()
    finally:
        refresh_task.cancel()
        try:
            await refresh_task
        except asyncio.CancelledError:
            pass
        await release_polling_lock(token)
        print("Telegram polling singleton lock released.")


if __name__ == "__main__":
    asyncio.run(run())
