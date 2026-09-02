"""Railway entrypoint for Sen.

Keeps Telegram polling singleton protection and patches the Rich Message
memory menu so each user's menu is personalized.
"""

import asyncio
import contextvars
import uuid

import main

POLLING_LOCK_KEY = "sen:telegram:getupdates:lock"
POLLING_LOCK_TTL = 120
POLLING_LOCK_REFRESH = 30

_current_menu_user: contextvars.ContextVar[object] = contextvars.ContextVar(
    "current_menu_user", default=None
)


def _display_name(user) -> str:
    return str(
        getattr(user, "first_name", None)
        or getattr(user, "username", None)
        or "User"
    ).strip()


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
    user_id = message.from_user.id
    chat_id = message.chat.id
    token = _current_menu_user.set(message.from_user)
    try:
        await main.clear_interaction(user_id)

        if message.chat.type == "private":
            await main.send_menu(chat_id, user_id, main.rich_main_menu())
            try:
                await message.delete()
            except Exception:
                pass
            return

        if message.ephemeral_message_id is not None:
            await main.send_menu(
                chat_id,
                user_id,
                main.rich_main_menu(),
                reply_to_ephemeral_id=message.ephemeral_message_id,
            )
            return

        # Normal group /help is allowed again. Try the private Rich Message
        # path first. If Telegram rejects it because this bot is not an admin,
        # fall back to a working public Rich Message rather than silently
        # ignoring the command.
        try:
            await main.send_menu(chat_id, user_id, main.rich_main_menu())
        except Exception as e:
            print(f"Ephemeral /help failed: {e}")
            try:
                await main.bot.send_rich_message(
                    chat_id=chat_id,
                    rich_message=main.InputRichMessage(
                        html=main.rich_main_menu()
                    ),
                )
            except Exception as fallback_error:
                print(f"Public Rich /help fallback failed: {fallback_error}")

        try:
            await message.delete()
        except Exception:
            pass
    finally:
        _current_menu_user.reset(token)


# Replace the registered /help callback before polling starts.
for handler in main.router.message.handlers:
    if getattr(handler.callback, "__name__", "") == "handle_help":
        handler.callback = private_help
        break


async def _wrap_callback(callback, original):
    token = _current_menu_user.set(callback.from_user)
    try:
        return await original(callback)
    finally:
        _current_menu_user.reset(token)


# Personalize every callback that can display the main menu, especially Back.
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


# Keep Rich Message callbacks from mutating a public group message belonging
# to a different user.
main._original_edit_menu = main.edit_menu
main._original_close_menu = main.close_menu


async def guarded_edit_menu(callback, rich_html):
    message = callback.message
    if (
        message
        and message.chat.type != "private"
        and message.ephemeral_message_id is None
    ):
        await callback.answer(
            "This menu is public because Telegram could not make /help private.",
            show_alert=True,
        )
        return
    await main._original_edit_menu(callback, rich_html)


async def guarded_close_menu(callback):
    message = callback.message
    if (
        message
        and message.chat.type != "private"
        and message.ephemeral_message_id is None
    ):
        await callback.answer(
            "This menu is public and cannot be controlled privately.",
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
