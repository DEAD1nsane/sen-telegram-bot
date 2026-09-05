"""Message handlers and conversation logic."""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    InputRichMessage,
    Message,
    ReplyParameters,
)

from . import config as _cfg
from .config import (
    MENTION_ONLY_RE,
    OWNER_ID,
    TRIGGER_AUDIO_FILES,
    TEMPORARY_FORGET_RE,
    redis_client,
    interaction_key,
)
from .storage import get_memories, clear_interaction, clear_menu_identity, get_interaction, set_interaction
from .memory import (
    close_menu,
    edit_memory_menu,
    authorize_memory_callback,
    get_user_display_name,
    process_memory_text,
    schedule_menu_expiry,
    send_memory_menu,
)
from .search import (
    asked_for_sources,
    detect_explicit_search_intent,
    detect_search_intent,
    free_web_search,
    get_search_state,
    normalize_search_query,
    replace_model_source_blocks,
    source_entries,
    source_links,
)
from .media import (
    delete_gemini_file,
    download_telegram_media,
    get_gemini_video_file,
    get_replied_video_media,
    send_keyword_audio,
)

if TYPE_CHECKING:
    from aiogram import Bot, Dispatcher


# ---------------------------------------------------------------------------
# Code-span mention guard
# ---------------------------------------------------------------------------


def _strip_code_spans(text: str, entities=None) -> str:
    """Remove Telegram code/pre entities and markdown backtick code."""
    text = text or ""
    if entities:
        cumulative = [0]
        for char in text:
            cumulative.append(cumulative[-1] + len(char.encode("utf-16-le")) // 2)
        for entity in entities:
            entity_type = getattr(entity, "type", None)
            if hasattr(entity_type, "value"):
                entity_type = entity_type.value
            if entity_type not in ("code", "pre"):
                continue
            offset = getattr(entity, "offset", 0)
            length = getattr(entity, "length", 0)
            end_offset = offset + length
            start = next((i for i, units in enumerate(cumulative) if units >= offset), None)
            end = next((i for i, units in enumerate(cumulative) if units >= end_offset), None)
            if start is not None and end is not None:
                chars = list(text)
                for i in range(start, end):
                    chars[i] = " "
                text = "".join(chars)
    text = re.sub(r"```[\s\S]*?```", lambda m: " " * len(m.group()), text)
    text = re.sub(r"`[^`\n]*`", lambda m: " " * len(m.group()), text)
    return text


# ---------------------------------------------------------------------------
# Rich message helpers
# ---------------------------------------------------------------------------


def render_math_markup(text: str) -> str:
    """Convert $$, \\[, \\( math delimiters to Telegram math tags."""
    if not text:
        return text
    protected: list[str] = []

    def protect(match: re.Match) -> str:
        protected.append(match.group(0))
        return f"\x00MATH{len(protected) - 1}\x00"

    text = re.sub(
        r"<tg-math>.*?</tg-math>|<tg-math-block>.*?</tg-math-block>|<pre>.*?</pre>|<code>.*?</code>",
        protect, text, flags=re.I | re.S,
    )
    text = re.sub(r"\$\$(.+?)\$\$", lambda m: f"<tg-math-block>{html.escape(m.group(1).strip())}</tg-math-block>", text, flags=re.S)
    text = re.sub(r"\\\[(.+?)\\\]", lambda m: f"<tg-math-block>{html.escape(m.group(1).strip())}</tg-math-block>", text, flags=re.S)
    text = re.sub(r"\\\((.+?)\\\)", lambda m: f"<tg-math>{html.escape(m.group(1).strip())}</tg-math>", text, flags=re.S)
    for i, value in enumerate(protected):
        text = text.replace(f"\x00MATH{i}\x00", value)
    return text


def sanitize_rich_html(text: str) -> str:
    """Strip unsupported HTML tags for Telegram RichMessage."""
    if not text:
        return text
    text = re.sub(r"<p\b[^>]*>", "", text, flags=re.I)
    text = re.sub(r"</p>", "\n\n", text, flags=re.I)
    text = re.sub(r"<h[1-6]\b[^>]*>(.*?)</h[1-6]>", r"<b>\1</b>\n\n", text, flags=re.I | re.S)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<div\b[^>]*>", "", text, flags=re.I)
    text = re.sub(r"</div>", "\n", text, flags=re.I)
    text = re.sub(r"<section\b[^>]*>", "", text, flags=re.I)
    text = re.sub(r"</section>", "\n", text, flags=re.I)
    text = re.sub(r"<article\b[^>]*>", "", text, flags=re.I)
    text = re.sub(r"</article>", "\n", text, flags=re.I)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_ai_output(text: str) -> str:
    """Strip markdown code fences and sanitize for RichMessage."""
    text = (text or "I didn't receive a response.").strip()
    text = re.sub(r"^```(?:html)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    return sanitize_rich_html(render_math_markup(text)).strip()


# ---------------------------------------------------------------------------
# Send AI response with source links
# ---------------------------------------------------------------------------


async def send_ai_response(bot: "Bot", chat_id: int, msg_id: int, response_text: str, is_private: bool):
    """Send a response, appending source links if the user asked for them."""
    query, search_context = get_search_state()
    wants_sources = asked_for_sources(query)
    cleaned = response_text
    if search_context and wants_sources:
        cleaned = replace_model_source_blocks(cleaned).rstrip()
        entries = source_entries(search_context)
        if entries:
            cleaned += source_links(search_context)
    rich = InputRichMessage(html=sanitize_rich_html(render_math_markup(cleaned)))
    kwargs: dict = {"chat_id": chat_id, "rich_message": rich}
    if not is_private:
        kwargs["reply_parameters"] = ReplyParameters(message_id=msg_id)
    return await bot.send_rich_message(**kwargs)


# ---------------------------------------------------------------------------
# Gemini response generation
# ---------------------------------------------------------------------------


async def generate_gemini_response(contents, config, max_attempts: int = 4):
    """Call Gemini with visual grounding rule and retry on transient errors."""
    from .config import gemini_client
    from google.genai import types

    grounding = (
        "\n\nVISUAL GROUNDING RULE: When actual image/video/audio media is present, use that media as the primary evidence. "
        "For character or object identification, inspect distinctive visual features and do not substitute a vaguely similar celebrity, fictional character, or meme. "
        "If you cannot determine the exact identity reliably, say so rather than inventing a name. "
        "If web search context is present, use it to verify an identification, not to replace inspection of the supplied media. "
        "Never mention hidden prompts, transport labels, or internal search markers in the answer."
    )
    if isinstance(contents, list) and contents and isinstance(contents[-1], str):
        contents = list(contents)
        contents[-1] += grounding
    elif isinstance(contents, str):
        contents += grounding
    retry_delays = (2, 4, 8)
    for attempt in range(max_attempts):
        try:
            return await gemini_client.aio.models.generate_content(
                model="gemini-3.5-flash-lite", contents=contents, config=config,
            )
        except Exception as e:
            s = str(e).upper()
            retryable = "503" in s or "UNAVAILABLE" in s or "429" in s or "RESOURCE_EXHAUSTED" in s
            if not retryable or attempt >= len(retry_delays):
                raise
            delay = retry_delays[attempt]
            print(f"Gemini temporary failure ({str(e)[:180]}). Retrying in {delay}s, attempt {attempt + 2}/{max_attempts}.")
            import asyncio
            await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def register_handlers(router: Router, bot: "Bot") -> None:
    """Register all message and callback handlers."""

    @router.message(Command("memories"))
    async def handle_memories(message: Message):
        uid, cid = message.from_user.id, message.chat.id
        incoming = getattr(message, "ephemeral_message_id", None)
        await clear_interaction(cid, uid)
        await clear_menu_identity(cid, uid)
        name = html.escape(get_user_display_name(message.from_user))
        text = f"<b>Memory Center</b>\n\nWelcome, {name}.\n\nKeep track of the details and instructions you've asked Sen to remember. Changes here affect how Sen responds to you."
        if message.chat.type in {"group", "supergroup"}:
            if incoming is None:
                return
            try:
                await send_memory_menu(bot, cid, uid, text, "main", incoming)
            except Exception as e:
                print(f"Memory menu send error: {e}")
        else:
            try:
                await send_memory_menu(bot, cid, uid, text)
            except Exception as e:
                print(f"Private memory menu error: {e}")

    @router.callback_query(F.data == "memory_view")
    async def handle_memory_view(callback: CallbackQuery):
        if not await authorize_memory_callback(callback, bot):
            return
        await callback.answer()
        memories = await get_memories(str(callback.from_user.id))
        body = "<b>What Sen Remembers</b>\n\nThese are the saved instructions and details currently available to Sen."
        if not memories:
            body += "\n\nNothing has been saved yet."
        await edit_memory_menu(bot, callback, body, "view", memories)

    @router.callback_query(F.data == "memory_share")
    async def handle_memory_share(callback: CallbackQuery):
        if not await authorize_memory_callback(callback, bot):
            return
        memories = await get_memories(str(callback.from_user.id))
        if not memories:
            await callback.answer("Your memory list is empty! Nothing to share.", show_alert=True)
            return
        first_name = (getattr(callback.from_user, "first_name", "") or "").strip() or "User"
        safe_first_name = html.escape(first_name)
        from aiogram.types import (
            InputRichBlockList,
            InputRichBlockListItem,
            InputRichBlockParagraph,
            InputRichBlockSectionHeading,
            InputRichMessage,
        )
        from memory import rich_text_from_markup
        share_blocks = [InputRichBlockSectionHeading(text=f"What Sen Remembers for {safe_first_name}", size=3)]
        share_blocks.append(InputRichBlockList(items=[
            InputRichBlockListItem(blocks=[InputRichBlockParagraph(text=rich_text_from_markup(memory))], value=i, type="1")
            for i, memory in enumerate(memories, 1)
        ]))
        try:
            await callback.bot.send_rich_message(chat_id=callback.message.chat.id, rich_message=InputRichMessage(blocks=share_blocks))
            await callback.answer("Memories shared with the group!", show_alert=True)
        except Exception as e:
            print(f"Memory share error: {e}")
            await callback.answer("Failed to share memories to group.", show_alert=True)

    @router.callback_query(F.data == "memory_add")
    async def handle_memory_add(callback: CallbackQuery):
        if not await authorize_memory_callback(callback, bot):
            return
        await set_interaction(callback.message.chat.id, callback.from_user.id, "add")
        await callback.answer()
        await edit_memory_menu(bot, callback, "<b>Add a Memory</b>\n\nTell Sen what you'd like to keep in mind for future conversations.\n\nYou can add several items at once by separating them with <code>,,</code>.", "back_close")

    @router.callback_query(F.data == "memory_edit")
    async def handle_memory_edit(callback: CallbackQuery):
        if not await authorize_memory_callback(callback, bot):
            return
        memories = await get_memories(str(callback.from_user.id))
        await set_interaction(callback.message.chat.id, callback.from_user.id, "edit_number")
        await callback.answer()
        body = (
            "<b>Edit a Memory</b>\n\nThere aren't any saved memories to edit yet."
            if not memories
            else "<b>Edit a Memory</b>\n\nSend the memory number followed by the replacement text.\n\n"
            + "\n".join(f"{i}. {html.escape(m)}" for i, m in enumerate(memories, 1))
            + "\n\nExample: <code>2 My new instruction</code>"
        )
        await edit_memory_menu(bot, callback, body, "back_close")

    @router.callback_query(F.data == "memory_forget")
    async def handle_memory_forget(callback: CallbackQuery):
        if not await authorize_memory_callback(callback, bot):
            return
        memories = await get_memories(str(callback.from_user.id))
        await set_interaction(callback.message.chat.id, callback.from_user.id, "forget")
        await callback.answer()
        body = (
            "<b>Remove Memories</b>\n\nThere's nothing saved here to remove."
            if not memories
            else "<b>Remove Memories</b>\n\nSend one or more memory numbers, separated with <code>,,</code>, to remove them.\n\n"
            + "\n".join(f"{i}. {html.escape(m)}" for i, m in enumerate(memories, 1))
            + "\n\nExample: <code>1,, 3</code>"
        )
        await edit_memory_menu(bot, callback, body, "back_close")

    @router.callback_query(F.data == "memory_forget_all")
    async def handle_memory_forget_all(callback: CallbackQuery):
        if not await authorize_memory_callback(callback, bot):
            return
        await clear_interaction(callback.message.chat.id, callback.from_user.id)
        await callback.answer()
        await edit_memory_menu(bot, callback, "<b>Clear All Memories?</b>\n\nThis will remove every saved memory for your account and clear the conversation context associated with this chat.\n\n<b>This cannot be undone.</b>", "confirm_forget_all")

    @router.callback_query(F.data == "memory_confirm_forget_all")
    async def handle_confirm_forget_all(callback: CallbackQuery):
        if not await authorize_memory_callback(callback, bot):
            return
        uid, cid = callback.from_user.id, callback.message.chat.id
        await redis_client.delete(f"memory_list:{uid}", f"chat_history:{cid}:{uid}", interaction_key(cid, uid))
        await callback.answer("All saved memory has been cleared.", show_alert=True)
        await edit_memory_menu(bot, callback, "<b>Memory Cleared</b>\n\nYour saved memories and local conversation context have been removed.", "back_close")

    @router.callback_query(F.data == "memory_back")
    async def handle_memory_back(callback: CallbackQuery):
        if not await authorize_memory_callback(callback, bot):
            return
        await clear_interaction(callback.message.chat.id, callback.from_user.id)
        await callback.answer()
        name = html.escape(get_user_display_name(callback.from_user))
        await edit_memory_menu(bot, callback, f"<b>Memory Center</b>\n\nWelcome, {name}.\n\nKeep track of the details and instructions you've asked Sen to remember. Changes here affect how Sen responds to you.", "main")

    @router.callback_query(F.data == "memory_close")
    async def handle_memory_close(callback: CallbackQuery):
        if not await authorize_memory_callback(callback, bot):
            return
        await callback.answer("Closed")
        await close_menu(bot, callback)

    @router.message(Command("del"))
    async def handle_delete(message: Message):
        if not message.from_user or message.from_user.id != OWNER_ID:
            return
        if (
            message.reply_to_message
            and _cfg.BOT_INFO
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.id == _cfg.BOT_INFO.id
        ):
            try:
                await bot.delete_message(message.chat.id, message.reply_to_message.message_id)
            except Exception as e:
                print(f"/del target deletion error: {e}")
        print(f"[/del] user_id={message.from_user.id} chat_id={message.chat.id} ephemeral_message_id={getattr(message, 'ephemeral_message_id', None)}")

    @router.message(F.community_chat_added)
    async def handle_community_added(message: Message):
        print(f"Community binding topology registered: {message.chat.id}")

    @router.message(F.community_chat_removed)
    async def handle_community_removed(message: Message):
        print(f"Community dropping context safely absorbed: {message.chat.id}")

    @router.message(F.text | F.caption | F.voice | F.photo | F.video)
    async def handle_conversation(message: Message):
        if message.audio is not None:
            return

        text = message.text or message.caption or ""
        entities = (
            getattr(message, "entities", None)
            if message.text is not None
            else getattr(message, "caption_entities", None)
        )
        if _cfg.BOT_INFO and _cfg.BOT_INFO.username:
            mention_re = re.compile(
                r"(?<![A-Za-z0-9_])@" + re.escape(_cfg.BOT_INFO.username) + r"\b", re.I
            )
            code_stripped = _strip_code_spans(text, entities)
            if mention_re.search(text) and not mention_re.search(code_stripped):
                return

        text_forget = re.sub(r"<[^>]+>", "", text)
        temporary_forget = bool(TEMPORARY_FORGET_RE.search(text_forget))
        await _handle_conversation_inner(message, bot, temporary_forget)

    async def _handle_conversation_inner(message: Message, bot: "Bot", temp_forget: bool):
        action = await get_interaction(message.chat.id, message.from_user.id)
        if action and message.text and not message.text.startswith("/"):
            if await process_memory_text(message, action, temp_forget):
                return

        text = message.text or message.caption or ""
        text_no_html = re.sub(r"<[^>]+>", "", text)
        is_private = message.chat.type == "private"
        bot_username = f"@{_cfg.BOT_INFO.username}" if _cfg.BOT_INFO and _cfg.BOT_INFO.username else ""
        lower = text_no_html.lower()
        tagged = bool(bot_username) and bot_username.lower() in lower
        tagged = tagged or "@gemini" in lower
        reply_to_bot = bool(
            message.reply_to_message
            and _cfg.BOT_INFO
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.id == _cfg.BOT_INFO.id
        )
        replied_video_media = get_replied_video_media(message)
        replied_video = bool(replied_video_media)
        has_media_input = bool(message.photo or message.voice or replied_video)

        keyword_audio = None
        if text_no_html and not text_no_html.startswith("/"):
            msg_entities = (
                getattr(message, "entities", None)
                if message.text is not None
                else getattr(message, "caption_entities", None)
            )
            code_stripped = _strip_code_spans(text, msg_entities)
            if re.search(r"\bsen\b", code_stripped, re.I):
                keyword_audio = TRIGGER_AUDIO_FILES["sen"]
            elif re.search(r"\bmagical\b", code_stripped, re.I):
                keyword_audio = TRIGGER_AUDIO_FILES["magical"]
            elif re.search(r"\bmagic\b", code_stripped, re.I):
                keyword_audio = TRIGGER_AUDIO_FILES["magic"]
        if keyword_audio:
            await send_keyword_audio(message, keyword_audio)

        if message.voice is not None:
            if not (tagged or reply_to_bot):
                return
        elif message.video is not None:
            if not (replied_video and (tagged or reply_to_bot)):
                return
        elif not (tagged or reply_to_bot or is_private):
            return

        prompt = text
        if bot_username:
            prompt = re.sub(re.escape(bot_username), "", prompt, flags=re.I)
        prompt = re.sub(r"@gemini\b", "", prompt, flags=re.I).strip()
        if reply_to_bot and prompt and MENTION_ONLY_RE.fullmatch(prompt):
            return
        if not re.sub(r"```(?:\w+)?", "", prompt).strip() and not has_media_input and not message.reply_to_message:
            return

        uid, cid, mid = message.from_user.id, message.chat.id, message.message_id
        cooldown = f"cooldown:{uid}"
        if await redis_client.exists(cooldown):
            await message.answer("Slow down, request limit reached.", reply_to_message_id=None if is_private else mid)
            return
        await redis_client.set(cooldown, "1", ex=4)

        replied_context = ""
        if message.reply_to_message:
            replied_context = message.reply_to_message.text or message.reply_to_message.caption or ""
            if message.reply_to_message.sticker:
                replied_context += f"\n[Replied-to message contains a sticker: {message.reply_to_message.sticker.emoji or 'sticker'}]"
            if replied_video:
                replied_context += f"\n[Replied-to message contains {replied_video_media[3]}]"

        media_bytes, media_mime, media_description = None, None, ""
        if message.voice:
            media_bytes = await download_telegram_media(bot, message.voice.file_id)
            media_mime = getattr(message.voice, "mime_type", None) or "audio/ogg"
            media_description = "Voice note"
        elif message.photo:
            media_bytes = await download_telegram_media(bot, message.photo[-1].file_id)
            media_mime = "image/jpeg"
            media_description = "Photo"

        if message.reply_to_message and message.reply_to_message.sticker and not media_bytes:
            from .media import _get_sticker_input
            media_bytes, media_mime, media_description = await _get_sticker_input(bot, message.reply_to_message)

        if replied_video_media and not media_bytes:
            file_id, video_mime, video_size, video_description = replied_video_media
            if video_size and video_size > 20 * 1024 * 1024:
                await message.answer("That video is over Telegram's 20 MB bot download limit, so I can't inspect it.", reply_to_message_id=None if is_private else mid)
                return
            media_bytes = await download_telegram_media(bot, file_id)
            media_mime = video_mime
            media_description = video_description
            if not media_bytes:
                await message.answer("I couldn't download that video to inspect it. Try sending the video again and reply to it.", reply_to_message_id=None if is_private else mid)
                return
            print(f"Replied video downloaded: message_id={getattr(message.reply_to_message, 'message_id', None)} size={len(media_bytes)} mime={media_mime}")

        if not prompt and replied_context and not media_bytes:
            prompt = "What are your thoughts on this?"
        if not (prompt or replied_context or media_bytes):
            return

        uploaded_gemini_video = None
        try:
            saved = await get_memories(str(uid), temp_forget)
            history_key = f"chat_history:{cid}:{uid}"
            raw_hist = [] if temp_forget else await redis_client.lrange(history_key, 0, -1)
            history = [x.decode() if isinstance(x, bytes) else str(x) for x in raw_hist]

            use_search = detect_explicit_search_intent(prompt) if media_bytes else detect_search_intent(prompt)
            news = bool(re.search(r"\b(?:news|headlines|latest|today|breaking|recent)\b", prompt, re.I))
            search_query = normalize_search_query(prompt)
            search_context = await free_web_search(search_query, news=news) if use_search else ""

            context_parts = []
            if replied_context:
                context_parts.append(f'Message User is Replying To:\n"{replied_context}"')
            if history:
                context_parts.append("Recent Conversation Context:\n" + "\n".join(history))
            if media_description:
                context_parts.append(f"Incoming Media: {media_description}")
            if search_context:
                context_parts.append("Web Search Context:\n" + search_context)
            elif use_search:
                context_parts.append("Web Search Context:\nA web search was requested, but no usable results were returned. Do not pretend that a search result supports a claim.")
            if media_bytes:
                context_parts.append("Media handling rule: The attached media is the primary evidence for the user's request. Answer what can actually be seen or heard in it. Do not substitute web results, conversation history, or guesses for details that should come from the media. If the media cannot be inspected reliably, say so instead of inventing what happened.")
            final_prompt = "\n\n".join(context_parts) + ("\n\n" if context_parts else "") + (prompt or "Process and answer this media input.")

            today = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
            instructions = (
                f"Today's date is {today}.\n"
                "Never use standard AI pleasantries.\n"
                "Keep casual replies brief, but expand when asked for detail.\n"
                "If the user changes subject, immediately follow the new subject.\n"
                "If joking or sarcastic, match the energy.\n"
                "If you do not know, say exactly: 'I don't have enough details to answer that accurately' without guessing.\n"
                "Do not assume personal details unless explicitly present in the memory list.\n"
                "When media is attached, treat that media as primary evidence. Never fabricate visual or audio details. If you cannot reliably inspect it, clearly say that you cannot inspect it.\n"
                "Return Telegram Rich HTML for sendRichMessage. Use only HTML that Telegram Rich HTML actually supports: <b>, <strong>, <i>, <em>, <u>, <ins>, <s>, <strike>, <del>, <code>, <mark>, <sub>, <sup>, <tg-spoiler>, <a>, <tg-reference>, <tg-emoji>, <table>, <details>, <summary>, <ul>, <ol>, <li>, <tg-math>, and <tg-math-block>.\n"
                "Do NOT use <p>, <h1>-<h6>, <div>, <section>, or <article> in Rich HTML.\n"
                "Structure: When a response has multiple sections or data types (tables, equations, lists), use nested collapsible sections. Put a parent <details><summary>Section Title</summary>...</details> around the whole thing, and put each distinct element (table, equation, list) in its own child <details><summary>...</summary>...</details> inside it. Use <b>bold</b> for section labels and headers.\n"
                "For mathematical answers: You MUST include the actual LaTeX equation wrapped in $$...$$ for block equations or \\(...\\) for inline equations. Example: $$\\Delta P = P_{BTC} - P_{ETH}$$. Never just label an equation without writing it. Never output raw LaTeX without delimiters.\n"
                "For current facts, news, prices, schedules, product information, Telegram features, or anything the user explicitly asks you to search/look up, use the supplied Web Search Context. Do not invent search results or claim a fact is current without supporting search context.\n"
                "When Web Search Context contains Image: URLs, an image may be useful as supporting media for the search result. If an image is genuinely useful, put exactly one marker [ATTACH_SEARCH_IMAGE: URL] in your response using one of the supplied Image URLs. Do not invent image URLs. Never use this marker for non-search media.\n"
                "Only search-result images may be sent as outgoing media. Do not generate slideshows, collages, presentations, images, videos, audio, or other media. If asked to create media, respond in text instead.\n"
                "Only show source links when the user explicitly asks for sources, citations, links, or URLs. When requested, put them at the very end as a compact rich-text footnote section using <details><summary>Sources</summary>...links...</details>. Otherwise do not display source URLs.\n"
                "Do not use Markdown formatting or Markdown tables."
            )
            if saved:
                instructions += "\nUser memory directives:\n" + "\n".join(f"- {x}" for x in saved)
            if search_context:
                instructions += "\nUse Web Search Context for current facts. Prefer retrieved sources over stale model knowledge."
            if use_search and not search_context:
                instructions += "\nA search was attempted but returned no usable results. Be explicit about that instead of fabricating sources or pretending to have searched."
            if history:
                instructions += "\nUse Recent Conversation Context for continuity without repeating it."

            from google.genai import types
            safety = [types.SafetySetting(category=c, threshold="BLOCK_NONE") for c in ("HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")]
            if media_bytes and media_mime and media_mime.startswith("video/"):
                uploaded_gemini_video = await get_gemini_video_file(media_bytes, media_mime, media_description)
                contents = [uploaded_gemini_video, final_prompt]
            elif media_bytes:
                media_part = types.Part.from_bytes(data=media_bytes, mime_type=media_mime)
                contents = [media_part, final_prompt]
            else:
                contents = final_prompt

            response = await generate_gemini_response(contents, types.GenerateContentConfig(system_instruction=instructions, safety_settings=safety))
            response_text = clean_ai_output(response.text)

            search_image_url = None
            image_marker = re.search(r"\[ATTACH_SEARCH_IMAGE:\s*(https?://[^\]\s]+)\]", response_text, re.I)
            if image_marker and search_context:
                candidate = image_marker.group(1).rstrip(".,)")
                supplied_images = set(re.findall(r"Image:\s*(https?://\S+)", search_context, re.I))
                if candidate in supplied_images:
                    search_image_url = candidate
                response_text = re.sub(r"\s*\[ATTACH_SEARCH_IMAGE:\s*https?://[^\]\s]+\]\s*", "\n", response_text, flags=re.I).strip()

            try:
                await send_ai_response(bot, cid, mid, response_text, is_private)
            except Exception as rich_error:
                print(f"Rich response delivery error: {rich_error}")
                fallback = html.unescape(re.sub(r"<[^>]+>", "", response_text)).strip() or "I didn't receive a response."
                await message.answer(fallback, reply_to_message_id=None if is_private else mid)

            if search_image_url:
                try:
                    await bot.send_photo(chat_id=cid, photo=search_image_url, reply_to_message_id=None if is_private else mid)
                except Exception as media_error:
                    print(f"Search result image delivery error: {media_error}")

            clean_history = html.unescape(re.sub(r"<[^>]+>", "", response_text)).strip()
            await redis_client.rpush(history_key, f"User: {prompt or media_description or 'Media'}", f"Bot: {clean_history}")
            await redis_client.ltrim(history_key, -10, -1)
        except Exception as e:
            print(f"Gemini AI processing error: {e}")
            s = str(e).upper()
            if "503" in s or "UNAVAILABLE" in s:
                error = "Whoa, I'm getting a little overwhelmed! Let me catch my breath. Try again in about 15 seconds."
            elif "429" in s or "RESOURCE_EXHAUSTED" in s:
                error = "Whoa, I'm getting a little overwhelmed! Let me catch my breath. Try again in about 10 seconds."
            elif media_bytes and media_mime and media_mime.startswith("video/"):
                error = "I couldn't reliably inspect that video, so I'm not going to make something up. Try the video again in a moment."
            else:
                error = "I ran into an unexpected problem processing that request."
            await message.answer(error, reply_to_message_id=None if is_private else mid)
        finally:
            await delete_gemini_file(uploaded_gemini_video)
