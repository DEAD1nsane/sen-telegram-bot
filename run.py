"""Railway entrypoint for Sen.

The /help menu must never fall back to a public Rich Message in a group.
Telegram only guarantees a private response to a normal group command when
that command was delivered as an ephemeral command, so this wrapper replaces
the old fallback handler with a strict ephemeral-only handler.

A Redis-backed singleton lock prevents Railway from ever allowing two Sen
workers to poll Telegram simultaneously during overlapping restarts/deploys.
"""

import asyncio
import os
import uuid

import main


POLLING_LOCK_KEY = "sen:telegram:getupdates:lock"
POLLING_LOCK_TTL = 120
POLLING_LOCK_REFRESH = 30


async def private_help(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    await main.clear_interaction(user_id)

    # Private chats do not need Telegram's ephemeral-message machinery.
    if message.chat.type == "private":
        await main.send_menu(chat_id, user_id, main.rich_main_menu())
        try:
            await message.delete()
        except Exception:
            pass
        return

    # In groups, /help must arrive as an ephemeral command. Never send a
    # public fallback, because that defeats the privacy requirement.
    if message.ephemeral_message_id is None:
        print(
            f"Ignoring non-ephemeral /help from user {user_id} in chat {chat_id}."
        )
        try:
            await message.delete()
        except Exception:
            pass
        return

    await main.send_menu(
        chat_id,
        user_id,
        main.rich_main_menu(),
        reply_to_ephemeral_id=message.ephemeral_message_id,
    )


async def guarded_edit_menu(callback, rich_html):
    message = callback.message
    if (
        message
        and message.chat.type != "private"
        and message.ephemeral_message_id is None
    ):
        await callback.answer("This private menu has expired.", show_alert=True)
        return
    await main._original_edit_menu(callback, rich_html)


async def guarded_close_menu(callback):
    message = callback.message
    if (
        message
        and message.chat.type != "private"
        and message.ephemeral_message_id is None
    ):
        await callback.answer("This private menu has expired.", show_alert=True)
        return
    await main._original_close_menu(callback)


# Replace the registered /help callback before polling starts.
for handler in main.router.message.handlers:
    if getattr(handler.callback, "__name__", "") == "handle_help":
        handler.callback = private_help
        break

# Keep every Rich Message callback from ever mutating a public group message.
main._original_edit_menu = main.edit_menu
main._original_close_menu = main.close_menu
main.edit_menu = guarded_edit_menu
main.close_menu = guarded_close_menu


async def acquire_polling_lock() -> str | None:
    """Acquire a Redis singleton lock for Telegram long polling."""
    token = uuid.uuid4().hex
    acquired = await main.redis_client.set(
        POLLING_LOCK_KEY,
        token,
        ex=POLLING_LOCK_TTL,
        nx=True,
    )
    if not acquired:
        return None
    return token


async def refresh_polling_lock(token: str) -> None:
    """Refresh the lock only if this process still owns it."""
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
                script,
                1,
                POLLING_LOCK_KEY,
                token,
                POLLING_LOCK_TTL,
            )
            if result == 0:
                print("WARNING: Telegram polling lock was lost.")
                return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"Polling lock refresh error: {e}")


async def release_polling_lock(token: str) -> None:
    """Release the lock only if this process still owns it."""
    script = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
        return redis.call('del', KEYS[1])
    end
    return 0
    """
    try:
        await main.redis_client.eval(
            script,
            1,
            POLLING_LOCK_KEY,
            token,
        )
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
