"""Memory system: menus, callbacks, and CRUD operations."""

from __future__ import annotations

import asyncio
import html
import re
from typing import TYPE_CHECKING

from aiogram.types import (
    CallbackQuery,
    EphemeralMessageParameters,
    InputRichBlockButtons,
    InputRichBlockList,
    InputRichBlockListItem,
    InputRichBlockParagraph,
    InputRichBlockSectionHeading,
    InputRichMessage,
    ReplyParameters,
    RichMessageButton,
    RichTextBold,
    RichTextCode,
    RichTextItalic,
    RichTextStrikethrough,
    RichTextUnderline,
)

from .config import MENU_TTL, redis_client, interaction_key
from .storage import (
    clear_interaction,
    clear_menu_identity,
    get_interaction,
    get_memories,
    get_menu_identity,
    register_menu_identity,
    set_interaction,
)

if TYPE_CHECKING:
    from aiogram import Bot


def get_user_display_name(user) -> str:
    """Return a display name for a Telegram user."""
    if not user:
        return "User"
    name = " ".join(
        x
        for x in (
            (getattr(user, "first_name", "") or "").strip(),
            (getattr(user, "last_name", "") or "").strip(),
        )
        if x
    )
    return name or (getattr(user, "username", "") or "User").strip() or "User"


def rich_text_from_markup(text: str):
    """Convert Telegram HTML markup to RichMessage text objects."""
    pattern = re.compile(
        r"(<b>.*?</b>|<strong>.*?</strong>|<i>.*?</i>|<em>.*?</em>"
        r"|<u>.*?</u>|<ins>.*?</ins>|<s>.*?</s>|<strike>.*?</strike>"
        r"|<del>.*?</del>|<code>.*?</code>)",
        re.I | re.S,
    )
    parts: list = []
    position = 0
    for match in pattern.finditer(text):
        if match.start() > position:
            parts.append(html.unescape(text[position : match.start()]))
        token = match.group(0)
        low = token.lower()
        if low.startswith(("<b>", "<strong>")):
            inner = re.sub(r"^<(?:b|strong)>|</(?:b|strong)>$", "", token, flags=re.I | re.S)
            parts.append(RichTextBold(text=html.unescape(inner)))
        elif low.startswith(("<i>", "<em>")):
            inner = re.sub(r"^<(?:i|em)>|</(?:i|em)>$", "", token, flags=re.I | re.S)
            parts.append(RichTextItalic(text=html.unescape(inner)))
        elif low.startswith(("<u>", "<ins>")):
            inner = re.sub(r"^<(?:u|ins)>|</(?:u|ins)>$", "", token, flags=re.I | re.S)
            parts.append(RichTextUnderline(text=html.unescape(inner)))
        elif low.startswith(("<s>", "<strike>", "<del>")):
            inner = re.sub(r"^<(?:s|strike|del)>|</(?:s|strike|del)>$", "", token, flags=re.I | re.S)
            parts.append(RichTextStrikethrough(text=html.unescape(inner)))
        else:
            inner = re.sub(r"^<code>|</code>$", "", token, flags=re.I | re.S)
            parts.append(RichTextCode(text=html.unescape(inner)))
        position = match.end()
    if position < len(text):
        parts.append(html.unescape(text[position:]))
    parts = [p for p in parts if p != ""]
    return parts[0] if len(parts) == 1 else parts


def get_memory_rich_message(
    text: str, menu_type: str = "main", memories: list[str] | None = None
) -> InputRichMessage:
    """Build a RichMessage for the memory menu."""
    import re

    blocks = []
    heading = re.match(r"^\s*<b>(.*?)</b>\s*(?:\n|$)", text, re.I | re.S)
    if heading:
        blocks.append(InputRichBlockSectionHeading(text=html.unescape(heading.group(1)), size=2))
        rest = text[heading.end() :].strip()
        if rest:
            blocks.append(InputRichBlockParagraph(text=rich_text_from_markup(rest)))
    else:
        blocks.append(InputRichBlockParagraph(text=rich_text_from_markup(text)))
    if memories:
        blocks.append(
            InputRichBlockList(
                items=[
                    InputRichBlockListItem(
                        blocks=[InputRichBlockParagraph(text=rich_text_from_markup(m))],
                        value=i,
                        type="1",
                    )
                    for i, m in enumerate(memories, 1)
                ]
            )
        )
    if menu_type == "main":
        blocks += [
            InputRichBlockButtons(buttons=[RichMessageButton(text="🧠 View Memories", callback_data="memory_view", style="primary")], align="center"),
            InputRichBlockButtons(buttons=[RichMessageButton(text="➕ New Memory", callback_data="memory_add", style="success")], align="center"),
            InputRichBlockButtons(buttons=[RichMessageButton(text="❌ Close", callback_data="memory_close", style="danger")], align="center"),
        ]
    elif menu_type == "view":
        blocks += [
            InputRichBlockButtons(buttons=[RichMessageButton(text="📝 Edit", callback_data="memory_edit", style="primary"), RichMessageButton(text="🗑️ Remove", callback_data="memory_forget", style="danger")], align="center"),
            InputRichBlockButtons(buttons=[RichMessageButton(text="🫯 Clear All", callback_data="memory_forget_all", style="danger")], align="center"),
            InputRichBlockButtons(buttons=[RichMessageButton(text="📢 Share to Group", callback_data="memory_share", style="success")], align="center"),
            InputRichBlockButtons(buttons=[RichMessageButton(text="↩️ Back", callback_data="memory_back"), RichMessageButton(text="❌ Close", callback_data="memory_close", style="danger")], align="center"),
        ]
    elif menu_type == "confirm_forget_all":
        blocks += [
            InputRichBlockButtons(buttons=[RichMessageButton(text="⚠️ Yes, Clear Everything", callback_data="memory_confirm_forget_all", style="danger")], align="center"),
            InputRichBlockButtons(buttons=[RichMessageButton(text="✖️ Cancel", callback_data="memory_back"), RichMessageButton(text="❌ Close", callback_data="memory_close", style="danger")], align="center"),
        ]
    else:
        blocks.append(InputRichBlockButtons(buttons=[RichMessageButton(text="↩️ Back", callback_data="memory_back"), RichMessageButton(text="❌ Close", callback_data="memory_close", style="danger")], align="center"))
    return InputRichMessage(blocks=blocks)


async def send_memory_menu(
    bot: "Bot",
    chat_id: int,
    user_id: int,
    text: str,
    menu_type: str = "main",
    source_ephemeral_id: int | None = None,
    memories: list[str] | None = None,
):
    """Send or update a memory menu message."""
    rich = get_memory_rich_message(text, menu_type, memories)
    if chat_id != user_id:
        if source_ephemeral_id is None:
            raise RuntimeError("Missing ephemeral message id.")
        message = await bot.send_rich_message(
            chat_id=chat_id,
            rich_message=rich,
            reply_parameters=ReplyParameters(ephemeral_message_id=source_ephemeral_id),
            ephemeral_message_parameters=EphemeralMessageParameters(receiver_user_id=user_id),
        )
        mid = getattr(message, "ephemeral_message_id", None)
    else:
        message = await bot.send_rich_message(chat_id=chat_id, rich_message=rich)
        mid = message.message_id
    if mid is None:
        raise RuntimeError("Telegram did not return a message id.")
    await register_menu_identity(chat_id, user_id, mid)
    schedule_menu_expiry(bot, chat_id, user_id, mid)
    return message


async def authorize_memory_callback(callback: CallbackQuery, bot: "Bot") -> bool:
    """Check that a callback is from the active memory menu owner."""
    message = callback.message
    if not message:
        await callback.answer("This memory menu is no longer available.", show_alert=True)
        return False
    user_id, chat_id = callback.from_user.id, message.chat.id
    mid = (
        getattr(message, "ephemeral_message_id", None)
        if message.chat.type in {"group", "supergroup"}
        else message.message_id
    )
    if mid is None or await get_menu_identity(chat_id, user_id) != mid:
        await callback.answer("This memory menu is no longer active.", show_alert=True)
        return False
    receiver = getattr(message, "receiver_user", None)
    if receiver and getattr(receiver, "id", user_id) != user_id:
        await callback.answer("This memory menu belongs to another user.", show_alert=True)
        return False
    return True


async def edit_memory_menu(
    bot: "Bot",
    callback: CallbackQuery,
    text: str,
    menu_type: str = "main",
    memories: list[str] | None = None,
) -> None:
    """Edit an existing memory menu in place."""
    message = callback.message
    if not message:
        return
    chat_id, user_id = message.chat.id, callback.from_user.id
    rich = get_memory_rich_message(text, menu_type, memories)
    if message.chat.type in {"group", "supergroup"}:
        mid = getattr(message, "ephemeral_message_id", None)
        if mid is None or await get_menu_identity(chat_id, user_id) != mid:
            return
        await bot.edit_ephemeral_message_text(
            chat_id=chat_id,
            receiver_user_id=user_id,
            ephemeral_message_id=mid,
            rich_message=rich,
        )
        await register_menu_identity(chat_id, user_id, mid)
        schedule_menu_expiry(bot, chat_id, user_id, mid)
    else:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message.message_id,
            rich_message=rich,
        )
        await register_menu_identity(chat_id, user_id, message.message_id)
        schedule_menu_expiry(bot, chat_id, user_id, message.message_id)


async def close_menu(bot: "Bot", callback: CallbackQuery) -> None:
    """Close and delete a memory menu."""
    message = callback.message
    if not message:
        return
    chat_id, user_id = message.chat.id, callback.from_user.id
    await clear_interaction(chat_id, user_id)
    try:
        if message.chat.type in {"group", "supergroup"}:
            mid = getattr(message, "ephemeral_message_id", None)
            if mid:
                await bot.delete_ephemeral_message(
                    chat_id=chat_id, receiver_user_id=user_id, ephemeral_message_id=mid
                )
        else:
            await bot.delete_message(chat_id=chat_id, message_id=message.message_id)
    except Exception as e:
        print(f"Menu close error: {e}")
    finally:
        await clear_menu_identity(chat_id, user_id)


async def expire_memory_menu(
    bot: "Bot", chat_id: int, user_id: int, menu_id: int
) -> None:
    """Auto-delete a memory menu after TTL."""
    try:
        await asyncio.sleep(MENU_TTL)
        if await get_menu_identity(chat_id, user_id) != menu_id:
            return
        try:
            if chat_id != user_id:
                await bot.delete_ephemeral_message(
                    chat_id=chat_id,
                    receiver_user_id=user_id,
                    ephemeral_message_id=menu_id,
                )
            else:
                await bot.delete_message(chat_id=chat_id, message_id=menu_id)
        except Exception as e:
            print(f"Automatic memory menu expiry error: {e}")
        finally:
            await clear_menu_identity(chat_id, user_id)
            await clear_interaction(chat_id, user_id)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"Memory menu expiry task error: {e}")


def schedule_menu_expiry(bot: "Bot", chat_id: int, user_id: int, menu_id: int) -> None:
    """Fire-and-forget task to expire a memory menu."""
    asyncio.create_task(expire_memory_menu(bot, chat_id, user_id, menu_id))


async def process_memory_text(message, action: str, temp_forget: bool = False) -> bool:
    """Handle text input for memory add/edit/forget. Returns True if consumed."""
    import re

    if not message.text or message.text.startswith("/"):
        return False
    uid = message.from_user.id
    cid = message.chat.id
    key = f"memory_list:{uid}"
    if action == "add":
        for part in [re.sub(r"<[^>]+>", "", p.strip())[:200] for p in message.text.split(",,") if p.strip()][:10]:
            if await redis_client.lpos(key, part) is None:
                await redis_client.rpush(key, part)
        await redis_client.ltrim(key, -25, -1)
        await clear_interaction(cid, uid)
    elif action == "edit_number":
        parts = message.text.strip().split(" ", 1)
        if len(parts) != 2 or not parts[0].isdigit():
            return True
        idx = int(parts[0]) - 1
        raw = await redis_client.lrange(key, 0, -1)
        if 0 <= idx < len(raw):
            await redis_client.lset(key, idx, re.sub(r"<[^>]+>", "", parts[1].strip())[:200])
        await clear_interaction(cid, uid)
    elif action == "forget":
        memories = await get_memories(str(uid), temp_forget)
        for idx in sorted(
            {int(n.strip()) - 1 for n in message.text.split(",,") if n.strip().isdigit()},
            reverse=True,
        ):
            if 0 <= idx < len(memories):
                memories.pop(idx)
        await redis_client.delete(key)
        if memories:
            await redis_client.rpush(key, *memories)
        await clear_interaction(cid, uid)
    else:
        return False
    try:
        await message.delete()
    except Exception:
        pass
    return True
