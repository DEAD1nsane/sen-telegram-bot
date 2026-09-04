"""Final compatibility layer for Rich/Advanced Editor media and source output."""

import html
import re
from contextvars import ContextVar


def _get(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    try:
        return getattr(obj, key, default)
    except Exception:
        return default


def _file_id(media):
    return _get(media, "file_id")


def _media_tuple(media, mime, description):
    file_id = _file_id(media)
    if not file_id:
        return None
    return (file_id, _get(media, "mime_type") or mime, _get(media, "file_size"), description)


def _walk_rich_media(obj, seen=None):
    """Find the first file-backed media item in a RichMessage tree."""
    if obj is None:
        return None
    if seen is None:
        seen = set()
    oid = id(obj)
    if oid in seen:
        return None
    seen.add(oid)

    block_type = str(_get(obj, "type", "") or "").lower()
    if block_type == "video":
        found = _media_tuple(_get(obj, "video"), "video/mp4", "Advanced editor video")
        if found:
            return found
    elif block_type == "animation":
        found = _media_tuple(_get(obj, "animation"), "video/mp4", "Advanced editor animation")
        if found:
            return found
    elif block_type == "photo":
        photos = _get(obj, "photo")
        if isinstance(photos, (list, tuple)):
            for photo in reversed(photos):
                found = _media_tuple(photo, "image/jpeg", "Advanced editor photo")
                if found:
                    return found
        else:
            found = _media_tuple(photos, "image/jpeg", "Advanced editor photo")
            if found:
                return found
    elif block_type == "document":
        found = _media_tuple(_get(obj, "document"), "application/octet-stream", "Advanced editor document")
        if found:
            return found
    elif block_type == "audio":
        found = _media_tuple(_get(obj, "audio"), "audio/mpeg", "Advanced editor audio")
        if found:
            return found
    elif block_type == "voice_note":
        found = _media_tuple(_get(obj, "voice_note"), "audio/ogg", "Advanced editor voice note")
        if found:
            return found

    for key, mime, description in (
        ("video", "video/mp4", "Advanced editor video"),
        ("animation", "video/mp4", "Advanced editor animation"),
        ("document", "application/octet-stream", "Advanced editor document"),
        ("audio", "audio/mpeg", "Advanced editor audio"),
        ("voice_note", "audio/ogg", "Advanced editor voice note"),
    ):
        found = _media_tuple(_get(obj, key), mime, description)
        if found:
            return found

    photo = _get(obj, "photo")
    if isinstance(photo, (list, tuple)):
        for item in reversed(photo):
            found = _media_tuple(item, "image/jpeg", "Advanced editor photo")
            if found:
                return found

    for key in ("blocks", "items", "children", "media", "content", "attachment", "cells", "model_extra", "api_kwargs"):
        value = _get(obj, key)
        if value is not None:
            found = _walk_rich_media(value, seen)
            if found:
                return found

    try:
        dumped = obj.model_dump(mode="python", exclude_none=True)
    except Exception:
        dumped = None
    if dumped is not None and dumped is not obj:
        found = _walk_rich_media(dumped, seen)
        if found:
            return found

    if isinstance(obj, dict):
        for value in obj.values():
            found = _walk_rich_media(value, seen)
            if found:
                return found
    elif isinstance(obj, (list, tuple, set)):
        for value in obj:
            found = _walk_rich_media(value, seen)
            if found:
                return found
    return None


_TEMP_FORGET_MEMORIES = ContextVar("sen_temp_forget_memories", default=False)


def install(main_module):
    original_get_memories = getattr(main_module, "get_memories", None)
    if original_get_memories is not None:
        async def get_memories_with_temp_forget(user_id_str):
            if _TEMP_FORGET_MEMORIES.get():
                return []
            return await original_get_memories(user_id_str)
        main_module.get_memories = get_memories_with_temp_forget
        print("Installed temporary-memory-forget guard")

        router = getattr(main_module, "router", None)
        handlers = getattr(router, "message", None)
        handlers = getattr(handlers, "handlers", []) if handlers is not None else []
        for handler in handlers:
            callback = getattr(handler, "callback", None)
            if callback is None:
                continue
            callback_name = getattr(callback, "__name__", "")
            if callback_name not in {"handle_conversation", "keyword_audio_first_handler"}:
                continue

            async def memory_aware_handler(message, _callback=callback):
                text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
                temporary_forget = bool(re.search(
                    r"\btemporarily\s+(?:forget|ignore)\s+(?:all\s+)?(?:your\s+)?(?:saved\s+)?memories?\b",
                    text,
                    re.I,
                ))
                token = _TEMP_FORGET_MEMORIES.set(temporary_forget)
                try:
                    return await _callback(message)
                finally:
                    _TEMP_FORGET_MEMORIES.reset(token)

            handler.callback = memory_aware_handler
            print(f"Installed temporary-memory-forget handler wrapper around {callback_name}")
            break

    original_resolver = getattr(main_module, "get_replied_video_media", None)
    if original_resolver is not None:
        def resolve_replied_media(message):
            try:
                found = original_resolver(message)
                if found:
                    return found
            except Exception as exc:
                print(f"Final replied-media resolver original error: {exc}")

            replied = _get(message, "reply_to_message")
            if replied is None:
                return None

            photo = _get(replied, "photo")
            if photo:
                try:
                    found = _media_tuple(photo[-1], "image/jpeg", "Replied-to photo")
                except (IndexError, TypeError):
                    found = None
                if found:
                    return found

            for attr, mime, description in (
                ("video", "video/mp4", "Replied-to video"),
                ("animation", "video/mp4", "Replied-to animation"),
                ("video_note", "video/mp4", "Replied-to video note"),
                ("document", "application/octet-stream", "Replied-to document"),
            ):
                found = _media_tuple(_get(replied, attr), mime, description)
                if found:
                    return found

            rich_message = _get(replied, "rich_message")
            found = _walk_rich_media(rich_message)
            if found:
                print(f"Final Rich/Advanced Editor media found: message_id={_get(replied, 'message_id')} description={found[3]} mime={found[1]}")
                return found

            for attr in ("model_extra", "api_kwargs"):
                found = _walk_rich_media(_get(replied, attr))
                if found:
                    print(f"Final Rich/Advanced Editor media found in {attr}: message_id={_get(replied, 'message_id')} description={found[3]}")
                    return found

            try:
                found = _walk_rich_media(replied.model_dump(mode="python", exclude_none=True))
            except Exception:
                found = None
            if found:
                print(f"Final Rich/Advanced Editor media found in message dump: message_id={_get(replied, 'message_id')} description={found[3]}")
            return found

        main_module.get_replied_video_media = resolve_replied_media
        print("Installed final Rich/Advanced Editor media resolver")

    original_free_search = None
    original_send = getattr(main_module, "send_ai_response", None)
    if original_send is not None:
        original_free_search = getattr(main_module, "free_web_search", None)

        def _source_entries(context):
            entries = []
            seen = set()
            for chunk in re.split(r"\n\s*\n", (context or "").strip()):
                title_match = re.search(r"^Title:\s*(.+)$", chunk, re.I | re.M)
                url_match = re.search(r"^URL:\s*(https?://\S+)$", chunk, re.I | re.M)
                if not url_match:
                    continue
                url = url_match.group(1).rstrip(".,)")
                if url in seen:
                    continue
                seen.add(url)
                title = title_match.group(1).strip() if title_match else url
                entries.append((title, url))
                if len(entries) >= 8:
                    break
            return entries

        def _asked_for_sources(text):
            return bool(re.search(r"\b(?:source|sources|citation|citations|reference|references|footnote|footnotes|links?|urls?)\b", text or "", re.I))

        def _clean_response(text, source_requested=False):
            text = text or "I didn't receive a response."
            text = re.sub(r"\s*\[ATTACH_SEARCH_IMAGE:\s*https?://[^\]\s]+\]\s*", "\n", text, flags=re.I)
            if source_requested:
                def drop_details(match):
                    block = match.group(0)
                    if re.search(r"\b(?:source|sources|citation|citations|reference|references|footnote|footnotes|links?|urls?)\b", block, re.I):
                        return ""
                    return block
                text = re.sub(r"<details\b[^>]*>.*?</details>", drop_details, text, flags=re.I | re.S)
                text = re.sub(r"(?:\n|^)\s*(?:sources?|references?|citations?|footnotes?)\s*:?[ \t]*(?:\n|$).*\Z", "", text, flags=re.I | re.S)
            return text.strip()

        async def send_clean_response(chat_id, msg_id, response_text, is_private):
            search_query, search_context = getattr(main_module, "_SEN_LAST_SEARCH", ("", ""))
            wants_sources = _asked_for_sources(search_query)
            cleaned = _clean_response(response_text, wants_sources)
            if search_context and wants_sources:
                entries = _source_entries(search_context)
                if entries:
                    source_html = ["<details><summary>Sources</summary>"]
                    for i, (title, url) in enumerate(entries, 1):
                        source_html.append(f'<a name="sen-source-{i}"></a><a href="{html.escape(url, quote=True)}">[{i}] {html.escape(title, quote=False)}</a>')
                    source_html.append("</details>")
                    cleaned = cleaned.rstrip() + "\n\n" + "\n".join(source_html)
            rich = main_module.InputRichMessage(html=main_module.sanitize_rich_html(main_module.render_math_markup(cleaned)))
            kwargs = {"chat_id": chat_id, "rich_message": rich}
            if not is_private:
                kwargs["reply_parameters"] = main_module.ReplyParameters(message_id=msg_id)
            return await main_module.bot.send_rich_message(**kwargs)

        main_module.send_ai_response = send_clean_response
        print("Installed final raw-output/source sanitizer")

    if original_free_search is not None:
        async def tracked_search(query, news=False):
            result = await original_free_search(query, news=news)
            main_module._SEN_LAST_SEARCH = (query or "", result or "")
            return result
        main_module.free_web_search = tracked_search
        main_module._SEN_LAST_SEARCH = ("", "")
        print("Installed deterministic search-context tracking")

    original_generate = getattr(main_module, "generate_gemini_response", None)
    if original_generate is not None:
        async def grounded_generate(contents, config, max_attempts=4):
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
            return await original_generate(contents, config, max_attempts=max_attempts)
        main_module.generate_gemini_response = grounded_generate
        print("Installed final Gemini visual-grounding wrapper")
