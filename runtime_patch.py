"""Runtime compatibility patches loaded after main.py."""

import os
import re


def _model_to_data(obj):
    """Turn aiogram/Pydantic rich-message objects into plain nested data."""
    if obj is None:
        return None
    try:
        dumper = getattr(obj, "model_dump", None)
        if callable(dumper):
            return dumper(mode="json", exclude_none=True)
    except Exception:
        pass
    if isinstance(obj, dict):
        return {k: _model_to_data(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_model_to_data(v) for v in obj]
    return obj


def _find_rich_video(data):
    """Find an actual Telegram file_id in any nested rich media block."""
    if data is None:
        return None

    if isinstance(data, list):
        for item in data:
            found = _find_rich_video(item)
            if found:
                return found
        return None

    if not isinstance(data, dict):
        return None

    block_type = str(data.get("type", "")).lower()
    if block_type == "video":
        media = data.get("video")
        if isinstance(media, dict) and media.get("file_id"):
            return (
                media["file_id"],
                (media.get("mime_type") or "video/mp4"),
                media.get("file_size"),
                "Rich editor video",
            )
    elif block_type == "animation":
        media = data.get("animation")
        if isinstance(media, dict) and media.get("file_id"):
            return (
                media["file_id"],
                (media.get("mime_type") or "video/mp4"),
                media.get("file_size"),
                "Rich editor video animation",
            )

    # Rich messages can nest blocks in lists, quotations, collages,
    # slideshows, details, table cells, and list items.
    for key in ("blocks", "items", "cells"):
        value = data.get(key)
        found = _find_rich_video(value)
        if found:
            return found
    return None


def _get_replied_video_media(message, original_getter):
    replied = getattr(message, "reply_to_message", None)
    if not replied:
        return None

    # First use normal Telegram message fields.
    try:
        result = original_getter(message)
        if result:
            return result
    except Exception:
        pass

    # Rich editor media lives under Message.rich_message.blocks and is not
    # necessarily mirrored into Message.video by Telegram/aiogram.
    try:
        rich = getattr(replied, "rich_message", None)
        data = _model_to_data(rich)
        result = _find_rich_video(data)
        if result:
            print(
                f"Rich reply video found: message_id={getattr(replied, 'message_id', None)} "
                f"file_id={result[0]} size={result[2]} mime={result[1]}"
            )
            return result
    except Exception as e:
        print(f"Rich reply media extraction error: {e}")

    return None


def install(main_module):
    """Patch runtime behavior after main.py has registered its handlers."""

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

            reply_parameters = (
                None
                if message.chat.type == "private"
                else main_module.ReplyParameters(message_id=message.message_id)
            )

            kwargs = {"audio": audio, "reply_parameters": reply_parameters}
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
                try:
                    await main_module.redis_client.set(
                        cache_key, audio, ex=main_module.AUDIO_CACHE_TTL
                    )
                except Exception as e:
                    print(f"Keyword audio cache write failure: {e}")
            return True
        except Exception as e:
            print(f"Keyword audio delivery error ({filename}): {e}")
            return False

    main_module.send_keyword_audio = send_keyword_audio

    # Replace the media resolver used by handle_conversation. It first keeps
    # all existing handling, then falls back to rich_message inspection.
    original_getter = getattr(main_module, "get_replied_video_media", None)
    if original_getter is not None:
        main_module.get_replied_video_media = lambda message: _get_replied_video_media(
            message, original_getter
        )
        print("Installed rich-editor reply video compatibility patch")

    # The old implementation stopped after a keyword audio trigger. The
    # current handler sends the audio but then continues into Gemini.
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
