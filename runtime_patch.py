"""Runtime compatibility patches loaded after main.py.

Keeps the bot's keyword-audio behavior compatible with the older implementation
without changing the main conversation pipeline.
"""

import os


def install(main_module):
    """Patch runtime behavior that is safe to replace after importing main."""

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

            kwargs = {
                "audio": audio,
                "reply_parameters": reply_parameters,
            }
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
