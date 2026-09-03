"""Runtime compatibility patches loaded after main.py.

Restores the older keyword-audio behavior and keeps its metadata intact,
without changing the main conversation implementation.
"""

import os
import re


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

    # The old implementation stopped after a keyword audio trigger. The
    # current handler sends the audio but then continues into Gemini, which
    # can create an unwanted second response. Replace only that registered
    # handler callback, preserving all of its existing filters.
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
