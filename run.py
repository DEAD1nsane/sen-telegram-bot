"""Railway entrypoint for Sen.

The /help menu must never fall back to a public Rich Message in a group.
Telegram only guarantees a private response to a normal group command when
that command was delivered as an ephemeral command, so this wrapper replaces
the old fallback handler with a strict ephemeral-only handler.
"""

import asyncio

import main


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
    # public fallback, because that defeats the entire privacy requirement.
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


if __name__ == "__main__":
    asyncio.run(main.main())
