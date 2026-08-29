import os
import re
import asyncio
from datetime import datetime

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Update, Message, FSInputFile
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    InputRichMessage,
    InputRichBlockHeading,
    InputRichBlockParagraph,
    InputRichBlockMathematicalExpression,
    InputRichBlockList,
    InputRichBlockListItem,
    InputRichBlockTable,
    InputRichTableRow,
    InputRichTableCell,
)

import redis.asyncio as redis
import httpx
from google import genai
from google.genai import types

# ==========================================
# Environment & Config
# ==========================================
redis_url = os.environ.get("REDIS_URL")
if not redis_url:
    host = os.environ.get("REDISHOST", "localhost")
    port = os.environ.get("REDISPORT", "6379")
    password = os.environ.get("REDISPASSWORD", "")
    redis_url = (
        f"redis://default:{password}@{host}:{port}"
        if password
        else f"redis://{host}:{port}"
    )

if "upstash" in redis_url.lower() and redis_url.startswith("redis://"):
    redis_url = redis_url.replace("redis://", "rediss://", 1)

if redis_url.startswith("rediss://"):
    redis_client = redis.from_url(redis_url, ssl_cert_reqs=None)
else:
    redis_client = redis.from_url(redis_url)

API_TOKEN = os.getenv("BOT_TOKEN")
SEARXNG_URL = os.getenv(
    "SEARXNG_URL", "https://searxng-railway-production-3252.up.railway.app/search"
)

bot = Bot(token=API_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

BOT_INFO = None

gemini_api_key = os.getenv("GEMINI_API_KEY")
if not gemini_api_key:
    raise ValueError("CRITICAL CONFIGURATION ERROR: 'GEMINI_API_KEY' missing.")

gemini_client = genai.Client(api_key=gemini_api_key)
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# ==========================================
# Stateful Block Parser (API 10.2)
# ==========================================


def parse_text_to_blocks(text: str) -> list:
    """Statefully tokenizes raw text into native Telegram Bot API 10.2 Rich Blocks."""
    blocks = []
    lines = text.split("\n")

    paragraph_buffer = []
    in_code_block = False
    code_buffer = []

    def flush_buffers():
        if paragraph_buffer:
            text_content = "\n".join(paragraph_buffer).strip()
            if text_content:
                blocks.append(InputRichBlockParagraph(text=text_content))
            paragraph_buffer.clear()

    for line in lines:
        if line.strip().startswith("```"):
            if in_code_block:
                in_code_block = False
                flush_buffers()
                clean_code = "\n".join(code_buffer)
                blocks.append(
                    InputRichBlockParagraph(text=f"Code Snippet:\n{clean_code}")
                )
                code_buffer.clear()
            else:
                flush_buffers()
                in_code_block = True
            continue

        if in_code_block:
            code_buffer.append(line)
            continue

        if line.strip().startswith("$$") and line.strip().endswith("$$"):
            flush_buffers()
            blocks.append(
                InputRichBlockMathematicalExpression(expression=line.strip().strip("$"))
            )
            continue

        heading_match = re.match(r"^(#{1,3})\s+(.*)", line)
        if heading_match:
            flush_buffers()
            level = len(heading_match.group(1))
            blocks.append(
                InputRichBlockHeading(text=heading_match.group(2).strip(), level=level)
            )
            continue

        if line.strip().startswith("|") and line.strip().endswith("|"):
            flush_buffers()
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.match(r"^\-+$", c) for c in cells):
                continue

            table_cells = [InputRichTableCell(text=c) for c in cells]
            new_row = InputRichTableRow(cells=table_cells)

            if blocks and isinstance(blocks[-1], InputRichBlockTable):
                blocks[-1].rows.append(new_row)
            else:
                blocks.append(
                    InputRichBlockTable(
                        is_bordered=True, is_striped=True, rows=[new_row]
                    )
                )
            continue

        list_match = re.match(r"^(\d+\.|\-|\*)\s+(.*)", line)
        if list_match:
            flush_buffers()
            item_content = list_match.group(2).strip()
            new_item = InputRichBlockListItem(
                blocks=[InputRichBlockParagraph(text=item_content)]
            )

            if blocks and isinstance(blocks[-1], InputRichBlockList):
                blocks[-1].items.append(new_item)
            else:
                blocks.append(InputRichBlockList(items=[new_item]))
            continue

        if not line.strip():
            flush_buffers()
            continue

        paragraph_buffer.append(line)

    flush_buffers()

    if not blocks:
        blocks.append(InputRichBlockParagraph(text=" "))

    return blocks


# ==========================================
# Helpers
# ==========================================


async def free_web_search(query: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        params = {"q": query, "format": "json"}
        async with httpx.AsyncClient() as client:
            res = await client.get(
                SEARXNG_URL, params=params, headers=headers, timeout=8.0
            )
            if res.status_code == 200:
                results = res.json().get("results", [])[:15]
                snippets = []
                for item in results:
                    title = item.get("title", "")
                    content = item.get("content", "")
                    url = item.get("url", "")
                    if title or content:
                        snippets.append(
                            f"Title: {title}\nContent: {content}\nURL: {url}"
                        )
                if snippets:
                    return "\n\n".join(snippets)
    except Exception as e:
        print(f"SearXNG error: {e}")

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                "[https://html.duckduckgo.com/html/](https://html.duckduckgo.com/html/)",
                data={"q": query},
                headers=headers,
                timeout=8.0,
            )
            raw = re.findall(
                r'<a class="result__snippet[^">]*>(.*?)</a>', res.text, re.DOTALL
            )
            urls = re.findall(r'href="(https?://[^"]+)"', res.text)
            clean = []
            for i, snippet in enumerate(raw[:15]):
                text_clean = re.sub(r"<[^>]+>", "", snippet).strip()
                link = urls[i] if i < len(urls) else ""
                if text_clean:
                    clean.append(f"Content: {text_clean}\nURL: {link}")
            if clean:
                return "\n\n".join(clean)
    except Exception as e:
        print(f"DuckDuckGo error: {e}")
    return ""


async def auto_delete_message(
    chat_id: int, bot_msg_id: int, user_msg_id: int, delay: int
):
    """Waits for the delay (in seconds), then silently deletes both the bot's response and user's command."""
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=bot_msg_id)
    except Exception as e:
        print(f"Error auto-deleting bot memory list: {e}")
    try:
        await bot.delete_message(chat_id=chat_id, message_id=user_msg_id)
    except Exception as e:
        print(f"Error auto-deleting user memory command: {e}")


async def send_audio_track(
    chat_id: int,
    msg_id: int,
    key: str,
    file_path: str,
    title: str,
    performer: str,
    is_private: bool,
):
    try:
        cached_id = await redis_client.get(f"audio_cache:{key}")
        reply_to = None if is_private else msg_id

        async def attempt_send(audio_payload):
            if isinstance(audio_payload, str):
                return await bot.send_audio(
                    chat_id=chat_id,
                    audio=audio_payload,
                    title=title,
                    performer=performer,
                    reply_to_message_id=reply_to,
                )
            else:
                audio_file = FSInputFile(audio_payload)
                return await bot.send_audio(
                    chat_id=chat_id,
                    audio=audio_file,
                    title=title,
                    performer=performer,
                    reply_to_message_id=reply_to,
                )

        if cached_id:
            try:
                await attempt_send(
                    cached_id.decode("utf-8")
                    if isinstance(cached_id, bytes)
                    else cached_id
                )
            except Exception as e:
                if "message to be replied not found" in str(e).lower():
                    await bot.send_audio(
                        chat_id=chat_id,
                        audio=(
                            cached_id.decode("utf-8")
                            if isinstance(cached_id, bytes)
                            else cached_id
                        ),
                        title=title,
                        performer=performer,
                    )
                else:
                    raise e
        elif os.path.exists(file_path):
            try:
                msg = await attempt_send(file_path)
            except Exception as e:
                if "message to be replied not found" in str(e).lower():
                    audio_file = FSInputFile(file_path)
                    msg = await bot.send_audio(
                        chat_id=chat_id,
                        audio=audio_file,
                        title=title,
                        performer=performer,
                    )
                else:
                    raise e
            if msg and msg.audio and msg.audio.file_id:
                await redis_client.set(f"audio_cache:{key}", msg.audio.file_id)
    except Exception as send_err:
        print(f"Error sending audio ({key}): {send_err}")


# ==========================================
# Direct Native Block Builders
# ==========================================


async def send_formatted_memories_as_blocks(message: Message, user_id_str: str):
    """Directly builds native rich blocks via code iteration, completely bypassing text parsing."""
    try:
        raw_items = await redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
        blocks = [InputRichBlockHeading(text="Active Memory Directives", level=2)]

        if not raw_items:
            blocks.append(
                InputRichBlockParagraph(text="Your memory list is currently empty.")
            )
        else:
            list_items = []
            for item in raw_items:
                mem_text = item.decode("utf-8") if isinstance(item, bytes) else item
                list_items.append(
                    InputRichBlockListItem(
                        blocks=[InputRichBlockParagraph(text=mem_text)]
                    )
                )
            blocks.append(InputRichBlockList(items=list_items))

        if message.chat.type == "private":
            sent_msg = await message.answer_rich_message(
                rich_message=InputRichMessage(blocks=blocks)
            )
        else:
            sent_msg = await message.answer_rich_message(
                rich_message=InputRichMessage(blocks=blocks),
                reply_to_message_id=message.message_id,
            )

        if sent_msg:
            asyncio.create_task(
                auto_delete_message(
                    message.chat.id, sent_msg.message_id, message.message_id, 60
                )
            )

    except Exception as e:
        print(f"Error rendering structured memory blocks: {e}")
        await message.answer("⚠️ Error loading active memory layout.")


# ==========================================
# Aiogram Handlers
# ==========================================


@router.message(Command("delete", "del"))
async def handle_delete(message: Message):
    if message.from_user.id == OWNER_ID:
        chat_id = message.chat.id
        reply_msg = message.reply_to_message
        if reply_msg and BOT_INFO and reply_msg.from_user.id == BOT_INFO.id:
            try:
                await bot.delete_message(chat_id, reply_msg.message_id)
            except Exception:
                pass
        try:
            await bot.delete_message(chat_id, message.message_id)
        except Exception:
            pass


@router.message(Command("help", "commands"))
async def handle_help(message: Message):
    blocks = [
        InputRichBlockHeading(text="Sen Bot Command Hub", level=1),
        InputRichBlockList(
            items=[
                InputRichBlockListItem(
                    blocks=[
                        InputRichBlockParagraph(
                            text="remember [item],, [item2] - Adds items to memory"
                        )
                    ]
                ),
                InputRichBlockListItem(
                    blocks=[
                        InputRichBlockParagraph(
                            text="what do you remember - Displays rules in a formatted list"
                        )
                    ]
                ),
                InputRichBlockListItem(
                    blocks=[
                        InputRichBlockParagraph(
                            text="edit [number] [new fact] - Edits a specific rule"
                        )
                    ]
                ),
                InputRichBlockListItem(
                    blocks=[
                        InputRichBlockParagraph(
                            text="forget [number],, [number2] - Removes memories"
                        )
                    ]
                ),
                InputRichBlockListItem(
                    blocks=[
                        InputRichBlockParagraph(text="forget all - Clears all memory")
                    ]
                ),
            ]
        ),
    ]
    if message.chat.type == "private":
        sent_msg = await message.answer_rich_message(
            rich_message=InputRichMessage(blocks=blocks)
        )
    else:
        sent_msg = await message.answer_rich_message(
            rich_message=InputRichMessage(blocks=blocks),
            reply_to_message_id=message.message_id,
        )

    if sent_msg:
        asyncio.create_task(
            auto_delete_message(
                message.chat.id, sent_msg.message_id, message.message_id, 60
            )
        )


def text_in(options: set):
    return lambda message: message.text and message.text.lower() in options


def text_startswith(prefix: str):
    return lambda message: message.text and message.text.lower().startswith(prefix)


@router.message(text_in({"what do you remember", "how do you remember"}))
async def handle_what_remember(message: Message):
    user_id_str = str(message.from_user.id)
    await send_formatted_memories_as_blocks(message, user_id_str)


@router.message(text_startswith("remember "))
async def handle_remember(message: Message):
    user_id_str = str(message.from_user.id)
    clean_prompt = message.text.strip()
    parts = [p.strip()[:200] for p in clean_prompt[9:].split(",,") if p.strip()]
    for part in parts[:10]:
        try:
            pos = await redis_client.lpos(f"memory_list:{user_id_str}", part)
            if pos is None:
                await redis_client.rpush(f"memory_list:{user_id_str}", part)
        except Exception:
            pass
    await redis_client.ltrim(f"memory_list:{user_id_str}", -25, -1)
    await send_formatted_memories_as_blocks(message, user_id_str)


@router.message(text_startswith("edit "))
async def handle_edit(message: Message):
    user_id_str = str(message.from_user.id)
    clean_prompt = message.text.strip()
    parts = clean_prompt[5:].strip().split(" ", 1)

    if len(parts) == 2 and parts[0].isdigit():
        idx, new_val = int(parts[0]) - 1, parts[1].strip()
        raw_items = await redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
        if 0 <= idx < len(raw_items):
            await redis_client.lset(f"memory_list:{user_id_str}", idx, new_val)
            await send_formatted_memories_as_blocks(message, user_id_str)
            return

    error_blocks = [InputRichBlockParagraph(text="Usage: edit [number] [new text]")]
    sent_msg = await message.answer_rich_message(
        rich_message=InputRichMessage(blocks=error_blocks)
    )
    if sent_msg:
        asyncio.create_task(
            auto_delete_message(
                message.chat.id, sent_msg.message_id, message.message_id, 10
            )
        )


@router.message(F.text.lower() == "forget all")
async def handle_forget_all(message: Message):
    user_id_str = str(message.from_user.id)
    chat_id = message.chat.id
    await redis_client.delete(
        f"memory_list:{user_id_str}", f"chat_history:{chat_id}:{user_id_str}"
    )

    blocks = [InputRichBlockParagraph(text="Cleared all your saved memories.")]
    if message.chat.type == "private":
        sent_msg = await message.answer_rich_message(
            rich_message=InputRichMessage(blocks=blocks)
        )
    else:
        sent_msg = await message.answer_rich_message(
            rich_message=InputRichMessage(blocks=blocks),
            reply_to_message_id=message.message_id,
        )

    if sent_msg:
        asyncio.create_task(
            auto_delete_message(
                message.chat.id, sent_msg.message_id, message.message_id, 10
            )
        )


@router.message(text_startswith("forget "))
async def handle_forget(message: Message):
    user_id_str = str(message.from_user.id)
    clean_prompt = message.text.strip()
    try:
        indices = [
            int(n.strip()) - 1
            for n in clean_prompt[7:].split(",,")
            if n.strip().isdigit()
        ]
        raw_items = await redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
        if raw_items and indices:
            memories = [
                i.decode("utf-8") if isinstance(i, bytes) else i for i in raw_items
            ]
            for i in sorted(set(indices), reverse=True):
                if 0 <= i < len(memories):
                    memories.pop(i)
            await redis_client.delete(f"memory_list:{user_id_str}")
            if memories:
                await redis_client.rpush(f"memory_list:{user_id_str}", *memories)
    except Exception:
        pass

    await send_formatted_memories_as_blocks(message, user_id_str)


# ==========================================
# Primary Chat, Mentions & Audio Engine
# ==========================================


@router.message(F.text | F.caption | F.voice | F.audio)
async def handle_conversation(message: Message):
    text = message.text or message.caption or ""
    text_no_code = re.sub(r"(?s)```.*?```", "", text)
    text_no_code = re.sub(r"(?s)`.*?`", "", text_no_code)

    if re.search(r"\bsen\b", text_no_code, re.IGNORECASE):
        asyncio.create_task(
            send_audio_track(
                message.chat.id,
                message.message_id,
                "sen",
                "Devin_The_Dude_Anythang.mp3",
                "Anythang",
                "Devin The Dude",
                message.chat.type == "private",
            )
        )
    if re.search(r"\bmagic(?:al|ally)?\b", text_no_code, re.IGNORECASE):
        asyncio.create_task(
            send_audio_track(
                message.chat.id,
                message.message_id,
                "magic",
                "Do You Believe In Magic.mp3",
                "Do You Believe In Magic",
                "The Lovin' Spoonful",
                message.chat.type == "private",
            )
        )

    bot_username = f"@{BOT_INFO.username}" if BOT_INFO else ""
    is_tagged = (
        bot_username and bot_username.lower() in text_no_code.lower()
    ) or "@gemini" in text_no_code.lower()
    is_reply_to_bot = bool(
        message.reply_to_message
        and BOT_INFO
        and message.reply_to_message.from_user.id == BOT_INFO.id
    )
    is_private = message.chat.type == "private"

    if (
        is_tagged
        or is_reply_to_bot
        or is_private
        or message.content_type in ["voice", "audio"]
    ):
        user_id_str = str(message.from_user.id)
        chat_id = message.chat.id
        msg_id = message.message_id

        clean_prompt = (
            text.replace(bot_username, "")
            .replace(bot_username.lower(), "")
            .replace("@gemini", "")
            .replace("@Gemini", "")
            .strip()
        )

        cooldown_key = f"cooldown:{user_id_str}"
        if await redis_client.exists(cooldown_key):
            warn_blocks = [
                InputRichBlockParagraph(text="Slow down, request limit reached.")
            ]
            if is_private:
                await message.answer_rich_message(
                    rich_message=InputRichMessage(blocks=warn_blocks)
                )
            else:
                await message.answer_rich_message(
                    rich_message=InputRichMessage(blocks=warn_blocks),
                    reply_to_message_id=msg_id,
                )
            return

        await redis_client.setex(cooldown_key, 4, "1")

        replied = message.reply_to_message
        replied_context = replied.text or replied.caption or "" if replied else ""

        audio_bytes = None
        audio_mime = "audio/ogg"
        if message.content_type in ["voice", "audio"]:
            audio_obj = message.voice or message.audio
            if audio_obj:
                file_info = await bot.get_file(audio_obj.file_id)
                audio_stream = await bot.download_file(file_info.file_path)
                if audio_stream:
                    audio_bytes = audio_stream.read()
                if hasattr(audio_obj, "mime_type") and audio_obj.mime_type:
                    audio_mime = audio_obj.mime_type

        if not clean_prompt and replied_context:
            clean_prompt = "What are your thoughts on this?"

        if clean_prompt or replied_context or audio_bytes:
            try:
                raw_mem = await redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
                saved_facts = [
                    i.decode("utf-8") if isinstance(i, bytes) else i for i in raw_mem
                ]

                history_key = f"chat_history:{chat_id}:{user_id_str}"
                raw_hist = await redis_client.lrange(history_key, 0, -1)
                chat_history = [
                    h.decode("utf-8") if isinstance(h, bytes) else h for h in raw_hist
                ]

                search_keywords = {"search", "google", "look up", "lookup", "find"}
                explicit_search = any(
                    word in clean_prompt.lower() for word in search_keywords
                )
                search_context = (
                    await free_web_search(clean_prompt) if explicit_search else ""
                )

                context_parts = []
                if replied_context:
                    context_parts.append(
                        f'Message User is Replying To:\n"{replied_context}"'
                    )
                if chat_history:
                    context_parts.append(
                        "Recent Conversation Context:\n" + "\n".join(chat_history)
                    )
                if search_context:
                    context_parts.append(f"Web Search Context:\n{search_context}")

                final_prompt = (
                    clean_prompt
                    if clean_prompt
                    else "Process and answer this voice note."
                )
                if context_parts:
                    final_prompt = (
                        "\n\n".join(context_parts)
                        + f"\n\nUser Question: {final_prompt}"
                    )

                today_str = datetime.now().strftime("%A, %B %d, %Y")
                bot_instructions = (
                    f"Today's date is {today_str}. Keep responses structural using double line-breaks to separate ideas. "
                    "Never use standard AI pleasantries. Do not start responses with 'As an AI' or end with generic offers for help. "
                    "Keep casual replies brief, but dynamically expand your response length when explicitly asked for details or when playing interactive games. "
                    "If the user changes the subject abruptly, drop the previous topic immediately and adapt to the new flow. "
                    "If the user is clearly joking or sarcastic, match their energy rather than taking the prompt literally. "
                    "If you do not know the answer or the provided context is insufficient, state 'I don't have enough details to answer that accurately' directly without guessing. "
                    "Do not assume personal details about the user unless they are explicitly provided in your memory list. "
                )

                if search_context:
                    bot_instructions += (
                        "When referencing 'Web Search Context', state the information directly without saying 'According to my search' or 'I found this online'. "
                        "Never include or state URLs/links from the Web Search Context unless the user explicitly asks for links, sources, or URLs in their prompt. "
                    )

                if chat_history:
                    bot_instructions += "Use the 'Recent Conversation Context' to track pronouns and subjects, but never summarize or repeat the history back to the user. "

                if saved_facts:
                    bot_instructions += (
                        "\n\nYou must strictly follow these User Instructions. They override any baseline behavior and are your absolute highest priority:\n"
                        + "\n".join(f"- {f}" for f in saved_facts)
                    )

                safety_overrides = [
                    types.SafetySetting(
                        category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"
                    ),
                    types.SafetySetting(
                        category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"
                    ),
                    types.SafetySetting(
                        category="HARM_CATEGORY_SEXUALLY_EXPLICIT",
                        threshold="BLOCK_NONE",
                    ),
                    types.SafetySetting(
                        category="HARM_CATEGORY_DANGEROUS_CONTENT",
                        threshold="BLOCK_NONE",
                    ),
                ]

                if audio_bytes:
                    contents = [
                        types.Part.from_bytes(data=audio_bytes, mime_type=audio_mime),
                        final_prompt,
                    ]
                    response = await gemini_client.aio.models.generate_content(
                        model="gemini-3.5-flash-lite",
                        contents=contents,
                        config=types.GenerateContentConfig(
                            system_instruction=bot_instructions,
                            safety_settings=safety_overrides,
                        ),
                    )
                else:
                    try:
                        chat = gemini_client.aio.chats.create(
                            model="gemini-3.5-flash-lite",
                            config=types.GenerateContentConfig(
                                system_instruction=bot_instructions,
                                safety_settings=safety_overrides,
                            ),
                        )
                        response = await chat.send_message(final_prompt)
                    except Exception as primary_err:
                        print(
                            f"Primary model failed ({primary_err}), falling back to gemini-2.5-flash..."
                        )
                        chat = gemini_client.aio.chats.create(
                            model="gemini-2.5-flash",
                            config=types.GenerateContentConfig(
                                system_instruction=bot_instructions,
                                safety_settings=safety_overrides,
                            ),
                        )
                        response = await chat.send_message(final_prompt)

                raw_text = response.text or ""
                rich_blocks = parse_text_to_blocks(raw_text)

                if is_private:
                    await message.answer_rich_message(
                        rich_message=InputRichMessage(blocks=rich_blocks)
                    )
                else:
                    await message.answer_rich_message(
                        rich_message=InputRichMessage(blocks=rich_blocks),
                        reply_to_message_id=msg_id,
                    )

                await redis_client.rpush(
                    history_key,
                    f"User: {clean_prompt or 'Voice Note'}",
                    f"Bot: {raw_text}",
                )
                await redis_client.ltrim(history_key, -10, -1)

            except Exception as ai_err:
                print(f"Gemini API error: {ai_err}")
                error_blocks = [
                    InputRichBlockParagraph(
                        text="I am currently broken right now, the owner needs to fix me."
                    )
                ]
                if "429" in str(ai_err):
                    error_blocks = [
                        InputRichBlockParagraph(
                            text="Whoa, I'm getting a little overwhelmed! Let me catch my breath for a minute."
                        )
                    ]

                if is_private:
                    await message.answer_rich_message(
                        rich_message=InputRichMessage(blocks=error_blocks)
                    )
                else:
                    await message.answer_rich_message(
                        rich_message=InputRichMessage(blocks=error_blocks),
                        reply_to_message_id=msg_id,
                    )


# ==========================================
# Polling Execution Loop
# ==========================================


async def main():
    global BOT_INFO
    try:
        BOT_INFO = await bot.get_me()
        print(f"Bot authenticated as {BOT_INFO.username}")
    except Exception as e:
        print(f"Failed to fetch bot info: {e}")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
#
