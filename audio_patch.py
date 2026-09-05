"""Compatibility fixes for code-formatted mentions and keyword audio delivery."""

import os
import re


def _get(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _entity_ranges(text, entities):
    """Return Python string ranges for Telegram code/pre entities."""
    ranges = []
    if not text or not entities:
        return ranges

    cumulative = [0]
    for char in text:
        cumulative.append(cumulative[-1] + len(char.encode("utf-16-le")) // 2)

    for entity in entities:
        entity_type = _get(entity, "type")
        if hasattr(entity_type, "value"):
            entity_type = entity_type.value
        if entity_type not in ("code", "pre"):
            continue

        offset = _get(entity, "offset", 0)
        length = _get(entity, "length", 0)
        end_offset = offset + length
        start = next((i for i, units in enumerate(cumulative) if units >= offset), None)
        end = next((i for i, units in enumerate(cumulative) if units >= end_offset), None)
        if start is not None and end is not None:
            ranges.append((start, end))

    return ranges


def _strip_code_spans(text, entities=None):
    """Remove literal and Telegram-formatted inline/fenced code."""
    text = text or ""
    text = re.sub(r"```[\s\S]*?```", lambda m: " " * len(m.group()), text)
    text = re.sub(r"`[^`\n]*`", lambda m: " " * len(m.group()), text)

    ranges = _entity_ranges(text, entities)
    if ranges:
        chars = list(text)
        for start, end in ranges:
            for i in range(start, end):
                chars[i] = " "
        text = "".join(chars)
    return text


def _wrap_conversation_handler(main_module):
    router = getattr(main_module, "router", None)
    handlers = getattr(getattr(router, "message", None), "handlers", [])
    for handler in handlers:
        callback = getattr(handler, "callback", None)
        callback_name = getattr(callback, "__name__", "") if callback else ""
        # final_patch wraps the conversation handler before this module loads,
        # so its callback is named memory_aware_handler rather than
        # handle_conversation. The previous guard therefore never installed.
        if callback_name not in {"handle_conversation", "keyword_audio_first_handler", "memory_aware_handler"}:
            continue

        async def guarded_handler(message, _callback=callback):
            text = _get(message, "text") or _get(message, "caption") or ""
            entities = (
                _get(message, "entities")
                if _get(message, "text") is not None
                else _get(message, "caption_entities")
            )
            bot_info = getattr(main_module, "BOT_INFO", None)
            username = _get(bot_info, "username")
            if username:
                mention_re = re.compile(r"(?<![A-Za-z0-9_])@" + re.escape(username) + r"\b", re.I)
                code_stripped = _strip_code_spans(text, entities)
                # Only suppress the handler when every bot mention is inside
                # code. A normal mention elsewhere must still trigger it.
                if mention_re.search(text) and not mention_re.search(code_stripped):
                    return
            return await _callback(message)

        handler.callback = guarded_handler
        print(f"Installed code-span mention guard around {callback_name}")
        return


def _install_direct_keyword_audio(main_module):
    async def send_keyword_audio(message, filename):
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(main_module.__file__)), filename)
            if not os.path.isfile(path):
                print(f"Keyword audio file missing: {path}")
                return False

            # Always upload the original file directly from the repository root.
            # Do not cache/reuse a Telegram file_id and do not re-encode the file.
            await message.answer_audio(
                audio=main_module.FSInputFile(path),
                reply_parameters=(
                    None
                    if message.chat.type == "private"
                    else main_module.ReplyParameters(message_id=message.message_id)
                ),
            )
            return True
        except Exception as e:
            print(f"Keyword audio delivery error ({filename}): {e}")
            return False

    main_module.send_keyword_audio = send_keyword_audio
    print("Installed direct repository-root keyword audio delivery")


def install(main_module):
    _wrap_conversation_handler(main_module)
    _install_direct_keyword_audio(main_module)
