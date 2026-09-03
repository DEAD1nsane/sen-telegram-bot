from pathlib import Path

p = Path('main.py')
t = p.read_text()

if 'async def generate_gemini_response(' not in t:
    marker = '@router.message(F.community_chat_added)\n'
    helper = '''async def generate_gemini_response(contents, config, max_attempts=4):
    retry_delays = (2, 4, 8)
    for attempt in range(max_attempts):
        try:
            return await gemini_client.aio.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=contents,
                config=config,
            )
        except Exception as e:
            s = str(e).upper()
            retryable = "503" in s or "UNAVAILABLE" in s or "429" in s or "RESOURCE_EXHAUSTED" in s
            if not retryable or attempt >= len(retry_delays):
                raise
            delay = retry_delays[attempt]
            print(f"Gemini temporary failure ({str(e)[:180]}). Retrying in {delay}s, attempt {attempt + 2}/{max_attempts}.")
            await asyncio.sleep(delay)

'''
    if marker not in t:
        raise SystemExit('conversation marker missing')
    t = t.replace(marker, helper + marker, 1)

t = t.replace('@router.message(F.text | F.caption | F.voice | F.photo)', '@router.message(F.text | F.caption | F.voice | F.photo | F.video)', 1)

old = '    reply_to_bot = bool(message.reply_to_message and BOT_INFO and message.reply_to_message.from_user and message.reply_to_message.from_user.id == BOT_INFO.id)\n    has_media_input = bool(message.photo or message.voice)'
new = '    reply_to_bot = bool(message.reply_to_message and BOT_INFO and message.reply_to_message.from_user and message.reply_to_message.from_user.id == BOT_INFO.id)\n    replied_video = bool(message.reply_to_message and message.reply_to_message.video)\n    has_media_input = bool(message.photo or message.voice or replied_video)'
if old not in t: raise SystemExit('media state missing')
t = t.replace(old, new, 1)

old = '    if message.voice is not None:\n        if not (tagged or reply_to_bot): return\n    elif not (tagged or reply_to_bot or is_private): return'
new = '    if message.voice is not None:\n        if not (tagged or reply_to_bot): return\n    elif message.video is not None:\n        if not (replied_video and (tagged or reply_to_bot)): return\n    elif not (tagged or reply_to_bot or is_private): return'
if old not in t: raise SystemExit('gating missing')
t = t.replace(old, new, 1)

old = '        if message.reply_to_message.sticker:\n            replied_context += f"\\n[Replied-to message contains a sticker: {message.reply_to_message.sticker.emoji or \'sticker\'}]"'
new = old + '\n        if message.reply_to_message.video:\n            replied_context += "\\n[Replied-to message contains a video]"'
if old not in t: raise SystemExit('reply context missing')
t = t.replace(old, new, 1)

marker = '    if message.reply_to_message and message.reply_to_message.sticker and not media_bytes:\n        media_bytes, media_mime, media_description = await get_sticker_input(message.reply_to_message)\n'
video = '''
    if replied_video and not media_bytes:
        video = message.reply_to_message.video
        if video.file_size and video.file_size > 20 * 1024 * 1024:
            await message.answer("That video is over Telegram's 20 MB bot download limit, so I can't inspect it.", reply_to_message_id=None if is_private else mid)
            return
        media_bytes = await download_telegram_media(video.file_id)
        media_mime = getattr(video, "mime_type", None) or "video/mp4"
        media_description = "Replied-to video"
        if not media_bytes:
            await message.answer("I couldn't download that video to inspect it. Try sending the video again and reply to it.", reply_to_message_id=None if is_private else mid)
            return
'''
if marker not in t: raise SystemExit('sticker marker missing')
if 'if replied_video and not media_bytes:' not in t:
    t = t.replace(marker, marker + video, 1)

old = '''        response = await gemini_client.aio.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=contents,
            config=types.GenerateContentConfig(system_instruction=instructions, safety_settings=safety),
        )'''
new = '''        response = await generate_gemini_response(
            contents,
            types.GenerateContentConfig(system_instruction=instructions, safety_settings=safety),
        )'''
if old not in t: raise SystemExit('Gemini call missing')
t = t.replace(old, new, 1)

old = '''    except Exception as e:
        print(f"Gemini AI processing error: {e}")
        error = "Whoa, I'm getting a little overwhelmed! Let me catch my breath." if "429" in str(e) else "I am currently broken right now, the owner needs to fix me."
        await message.answer(error, reply_to_message_id=None if is_private else mid)'''
new = '''    except Exception as e:
        print(f"Gemini AI processing error: {e}")
        s = str(e).upper()
        if "503" in s or "UNAVAILABLE" in s:
            error = "Whoa, I'm getting a little overwhelmed! Let me catch my breath. Try again in about 15 seconds."
        elif "429" in s or "RESOURCE_EXHAUSTED" in s:
            error = "Whoa, I'm getting a little overwhelmed! Let me catch my breath. Try again in about 10 seconds."
        else:
            error = "I ran into an unexpected problem processing that request."
        await message.answer(error, reply_to_message_id=None if is_private else mid)'''
if old not in t: raise SystemExit('error block missing')
t = t.replace(old, new, 1)

p.write_text(t)
print('main.py patched successfully')
