"""Compatibility fixes for code-formatted mentions and keyword audio delivery."""

import os
import re


def _get(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _strip_code_spans(text):
    """Remove fenced and inline code so mentions inside code are not triggers."""
    text = text or ""
    # Fenced code blocks must be removed before inline code.
    text = re.sub(r"```[\\s\\S]*?```", "", text)
    text = re.sub(r"`[^`\\n]*`", "", text)
    return text


def _wrap_conversation_handler(main_module):
    router = getattr(main_module, "router", None)
    handlers = getattr(getattr(router, "message", None), "handlers", [])
    for handler in handlers:
        callback = getattr(handler, "callback", None)
        if callback is None or getattr(callback, "__name__", "") != "handle_conversation":
            continue

        async def guarded_handler(message, _callback=callback):
            text = _get(message, "text") or _get(message, "caption") or ""
            bot_info = getattr(main_module, "BOT_INFO", None)
            username = _get(bot_info, "username")
            if username:
                code_stripped = _strip_code_spans(text)
                mention_re = re.compile(r"(?<![A-Za-z0-9_])@" + re.escape(username) + r"\\b", re.I)
                if mention_re.search(text) and not mention_re.search(code_stripped):
                    # The only bot mention is inside code formatting. Do not
                    # let the normal conversation handler respond to it.
                    return
            return await _callback(message)

        handler.callback = guarded_handler
        print("Installed code-span mention guard")
        return


def _install_direct_keyword_audio(main_module):
    async def send_keyword_audio(message, filename):
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(main_module.__file__)), filename)
            if not os.path.isfile(path):
                print(f"Keyword audio file missing: {path}")
                return False

            # Always upload the original file directly from the repository root.
            # Do not cache/reuse a Telegram file_id and do not re-encode the file,
            # so Telegram receives the original embedded audio metadata unchanged.
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
