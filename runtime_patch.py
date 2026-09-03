"""Runtime compatibility patches loaded after main.py."""

import html
import os
import re
from contextvars import ContextVar


_SEARCH_STATE = ContextVar("sen_search_state", default=("", ""))


def _find_rich_video(obj, seen=None):
    """Find video/animation media anywhere inside a Telegram RichMessage."""
    if obj is None:
        return None
    if seen is None:
        seen = set()
    oid = id(obj)
    if oid in seen:
        return None
    seen.add(oid)

    if isinstance(obj, dict):
        block_type = str(obj.get("type", "")).lower()
        if block_type == "video":
            media = obj.get("video")
            if isinstance(media, dict) and media.get("file_id"):
                return media["file_id"], media.get("mime_type") or "video/mp4", media.get("file_size"), "Rich editor video"
        if block_type == "animation":
            media = obj.get("animation")
            if isinstance(media, dict) and media.get("file_id"):
                return media["file_id"], media.get("mime_type") or "video/mp4", media.get("file_size"), "Rich editor video animation"
        for key in ("video", "animation"):
            media = obj.get(key)
            if isinstance(media, dict) and media.get("file_id"):
                return media["file_id"], media.get("mime_type") or "video/mp4", media.get("file_size"), "Rich editor video"
        for value in obj.values():
            found = _find_rich_video(value, seen)
            if found:
                return found
        return None

    if isinstance(obj, (list, tuple, set)):
        for value in obj:
            found = _find_rich_video(value, seen)
            if found:
                return found
        return None

    block_type = str(getattr(obj, "type", "")).lower()
    if block_type == "video":
        media = getattr(obj, "video", None)
        if media is not None and getattr(media, "file_id", None):
            return media.file_id, getattr(media, "mime_type", None) or "video/mp4", getattr(media, "file_size", None), "Rich editor video"
    if block_type == "animation":
        media = getattr(obj, "animation", None)
        if media is not None and getattr(media, "file_id", None):
            return media.file_id, getattr(media, "mime_type", None) or "video/mp4", getattr(media, "file_size", None), "Rich editor video animation"

    try:
        dump = obj.model_dump(mode="python", exclude_none=True)
        if dump is not obj:
            found = _find_rich_video(dump, seen)
            if found:
                return found
    except Exception:
        pass

    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if callable(value) or isinstance(value, (str, bytes, int, float, bool)):
            continue
        found = _find_rich_video(value, seen)
        if found:
            return found
    return None


def _get_replied_video_media(message, original_getter):
    replied = getattr(message, "reply_to_message", None)
    if not replied:
        return None
    try:
        result = original_getter(message)
        if result:
            return result
    except Exception as exc:
        print(f"Normal replied-media resolver error: {exc}")

    candidates = [getattr(replied, "rich_message", None), replied]
    for candidate in candidates:
        try:
            found = _find_rich_video(candidate)
            if found:
                print(
                    f"Rich reply video found: message_id={getattr(replied, 'message_id', None)} "
                    f"file_id={found[0]} size={found[2]} mime={found[1]}"
                )
                return found
        except Exception as exc:
            print(f"Rich reply media candidate error: {exc}")
    return None


def _source_requested(query):
    return bool(re.search(r"\b(?:source|sources|citation|citations|links?|urls?|footnotes?)\b", query or "", re.I))


def _source_links(search_context, footnotes=False):
    entries = []
    seen = set()
    for chunk in re.split(r"\n\s*\n", (search_context or "").strip()):
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
    if not entries:
        return ""

    if footnotes:
        return "\n\n<b>Sources</b>\n" + "\n".join(
            f"[{i}] <a href=\"{html.escape(url, quote=True)}\">{html.escape(title)}</a>"
            for i, (title, url) in enumerate(entries, 1)
        )
    return "\n\n<b>Sources</b>\n" + "\n".join(
        f"<a href=\"{html.escape(url, quote=True)}\">{html.escape(title)}</a>"
        for title, url in entries
    )


def _strip_model_source_blocks(text):
    pattern = re.compile(r"<details\b[^>]*>.*?</details>", re.I | re.S)

    def replace(match):
        block = match.group(0)
        if re.search(r"\b(?:source|sources|citation|citations|footnote|footnotes|links?|urls?)\b", block, re.I):
            return ""
        return block

    return pattern.sub(replace, text or "")


def install(main_module):
    original_getter = getattr(main_module, "get_replied_video_media", None)
    if original_getter is not None:
        main_module.get_replied_video_media = lambda message: _get_replied_video_media(message, original_getter)
        print("Installed rich-editor reply video compatibility patch")

    original_search = getattr(main_module, "free_web_search", None)
    if original_search is not None:
        async def tracked_search(query, news=False):
            result = await original_search(query, news=news)
            _SEARCH_STATE.set((query or "", result or ""))
            return result
        main_module.free_web_search = tracked_search

    original_send = getattr(main_module, "send_ai_response", None)
    if original_send is not None:
        async def send_with_real_sources(chat_id, msg_id, response_text, is_private):
            query, context = _SEARCH_STATE.get()
            if context and _source_requested(query):
                response_text = _strip_model_source_blocks(response_text).rstrip()
                footnotes = bool(re.search(r"\bfootnotes?\b", query, re.I))
                response_text += _source_links(context, footnotes=footnotes)
            return await original_send(chat_id, msg_id, response_text, is_private)
        main_module.send_ai_response = send_with_real_sources

    async def send_keyword_audio(message, filename):
        metadata = {
            "Devin_The_Dude_Anythang.mp3": ("Anythang", "Devin The Dude"),
            "Do You Believe In Magic.mp3": ("Do You Believe In Magic", "The Lovin' Spoonful"),
        }
        title, performer = metadata.get(filename, (None, None))
        try:
            cache_key = main_module.audio_cache_key(filename)
            cached = await main_module.redis_client.get(cache_key)
            audio = cached.decode() if isinstance(cached, bytes) else cached
            kwargs = {"audio": audio, "reply_parameters": None if message.chat.type == "private" else main_module.ReplyParameters(message_id=message.message_id)}
            if title:
                kwargs["title"] = title
            if performer:
                kwargs["performer"] = performer
            if audio:
                await message.answer_audio(**kwargs)
                return True
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
            if not os.path.isfile(path):
                print(f"Keyword audio file missing: {path}")
                return False
            kwargs["audio"] = main_module.FSInputFile(path)
            sent = await message.answer_audio(**kwargs)
            audio = getattr(getattr(sent, "audio", None), "file_id", None)
            if audio:
                await main_module.redis_client.set(cache_key, audio, ex=main_module.AUDIO_CACHE_TTL)
            return True
        except Exception as exc:
            print(f"Keyword audio delivery error ({filename}): {exc}")
            return False

    main_module.send_keyword_audio = send_keyword_audio

    original_handler = getattr(main_module, "handle_conversation", None)
    if original_handler is not None:
        async def keyword_audio_first_handler(message):
            text = message.text or message.caption or ""
            if text and not text.startswith("/"):
                if re.search(r"\bsen\b", text, re.I):
                    await send_keyword_audio(message, main_module.TRIGGER_AUDIO_FILES["sen"])
                    return
                if re.search(r"\bmagical\b", text, re.I):
                    await send_keyword_audio(message, main_module.TRIGGER_AUDIO_FILES["magical"])
                    return
                if re.search(r"\bmagic\b", text, re.I):
                    await send_keyword_audio(message, main_module.TRIGGER_AUDIO_FILES["magic"])
                    return
            await original_handler(message)

        for handler in main_module.router.message.handlers:
            if getattr(handler, "callback", None) is original_handler:
                handler.callback = keyword_audio_first_handler
                print("Installed keyword-audio-first handler compatibility patch")
                break
