import asyncio
import os
import re
from datetime import datetime, timezone

import httpx
import redis.asyncio as redis
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import (
    FSInputFile,
    Message,
    InputRichMessage,
    InputRichBlockHeading,
    InputRichBlockParagraph,
    InputRichBlockCollapsibleDetails,
    InputRichBlockMathematicalExpression,
    InputRichBlockList,
    InputRichBlockListItem,
    InputRichBlockTable,
    InputRichTableRow,
    InputRichTableCell,
)
from aiohttp import web
from google import genai
from google.genai import types
from redis.exceptions import RedisError

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
SEARXNG_URL = os.getenv("SEARXNG_URL", "https://railway.app")

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
app = web.Application()


# ==========================================
# Native Block Parser (API 10.2)
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
    except Exception as e:  # noqa: BLE001
        print(f"SearXNG error: {e}")
    return ""


async def collapse_message(chat_id: int, message_id: int, delay: int):
    """Waits for the delay (in seconds), then dynamically edits the message into an accordion state."""
    await asyncio.sleep(delay)
    blocks = [
        InputRichBlockCollapsibleDetails(
            summary="Active Memories Collapsed",
            blocks=[
                InputRichBlockParagraph(
                    text="Your memory list is hidden to conserve space."
                )
            ],
        )
    ]
    try:
        await bot.edit_message_rich_message(
            chat_id=chat_id,
            message_id=message_id,
            rich_message=InputRichMessage(blocks=blocks),
        )
    except Exception as e:
        pass


async def get_formatted_memories_blocks(user_id_str: str) -> list:
    """Returns the memory list structured securely as an API 10.2 List Block with native Headings."""
    try:
        raw_items = await redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
        if not raw_items:
            return [
                InputRichBlockHeading(text="Active Memory Directives", level=2),
                InputRichBlockParagraph(text="Your memory list is currently empty."),
            ]

        memories = [
            item.decode("utf-8") if isinstance(item, bytes) else item
            for item in raw_items
        ]

        list_items = []
        for i, mem in enumerate(memories):
            list_items.append(
                InputRichBlockListItem(
                    blocks=[
                        InputRichBlockHeading(text=f"Directive {i+1}", level=3),
                        InputRichBlockParagraph(text=mem),
                    ]
                )
            )

        return [
            InputRichBlockHeading(text="Active Memory Directives", level=2),
            InputRichBlockList(items=list_items),
        ]
    except Exception as e:
        print(f"Error fetching memory list format: {e}")
        return [InputRichBlockParagraph(text="Could not retrieve memory list.")]


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
                return await bot.send_audio(
                    chat_id=chat_id,
                    audio=FSInputFile(audio_payload),
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
            except Exception as e:  # noqa: BLE001
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
                    raise
        elif os.path.exists(file_path):
            msg = await attempt_send(file_path)
            if msg and msg.audio and msg.audio.file_id:
                await redis_client.set(f"audio_cache:{key}", msg.audio.file_id)
    except Exception as send_err:  # noqa: BLE001
        print(f"Error sending audio ({key}): {send_err}")


# ==========================================
# Handlers
# ==========================================


@router.message(Command("delete", "del"))
async def handle_delete(message: Message):
    if message.from_user.id == OWNER_ID:
        chat_id = message.chat.id
        reply_msg = message.reply_to_message
        if reply_msg and BOT_INFO and reply_msg.from_user.id == BOT_INFO.id:
            try:
                await bot.delete_message(chat_id, reply_msg.message_id)
            except TelegramAPIError:
                pass
        try:
            await bot.delete_message(chat_id, message.message_id)
        except TelegramAPIError:
            pass


@router.message(Command("help", "commands"))
async def handle_help(message: Message):
    blocks = [
        InputRichBlockHeading(text="Sen Bot Command Hub", level=1),
        InputRichBlockCollapsibleDetails(
            summary="View Available Directives",
            blocks=[
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
                                    text="what do you remember - Displays rules in a formatted matrix"
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
                                InputRichBlockParagraph(
                                    text="forget all - Clears all memory"
                                )
                            ]
                        ),
                    ]
                )
            ],
        ),
    ]
    if message.chat.type == "private":
        await message.answer_rich_message(rich_message=InputRichMessage(blocks=blocks))
    else:
        await message.answer_rich_message(
            rich_message=InputRichMessage(blocks=blocks),
            reply_to_message_id=message.message_id,
        )


def text_in(options: set):
    return lambda message: message.text and message.text.lower() in options


def text_startswith(prefix: str):
    return lambda message: message.text and message.text.lower().startswith(prefix)


@router.message(text_in({"what do you remember", "how do you remember"}))
async def handle_what_remember(message: Message):
    user_id_str = str(message.from_user.id)
    blocks = await get_formatted_memories_blocks(user_id_str)

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
        asyncio.create_task(collapse_message(message.chat.id, sent_msg.message_id, 60))


@router.message(text_startswith("remember "))
async def handle_remember(message: Message):
    user_id_str = str(message.from_user.id)
    parts = [p.strip()[:200] for p in message.text.strip()[9:].split(",,") if p.strip()]
    for part in parts[:10]:
        try:
            if await redis_client.lpos(f"memory_list:{user_id_str}", part) is None:
                await redis_client.rpush(f"memory_list:{user_id_str}", part)
        except RedisError:
            pass
    await redis_client.ltrim(f"memory_list:{user_id_str}", -25, -1)

    blocks = [
        InputRichBlockHeading(text="Memory Saved", level=1)
    ] + await get_formatted_memories_blocks(user_id_str)

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
        asyncio.create_task(collapse_message(message.chat.id, sent_msg.message_id, 60))


@router.message(text_startswith("edit "))
async def handle_edit(message: Message):
    user_id_str = str(message.from_user.id)
    parts = message.text.strip()[5:].strip().split(" ", 1)
    if len(parts) == 2 and parts[0].isdigit():
        idx, new_val = int(parts[0]) - 1, parts[1].strip()
        raw_items = await redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
        if 0 <= idx < len(raw_items):
            await redis_client.lset(f"memory_list:{user_id_str}", idx, new_val)
            blocks = [
                InputRichBlockHeading(text="Memory Updated", level=1)
            ] + await get_formatted_memories_blocks(user_id_str)
        else:
            blocks = [
                InputRichBlockHeading(text="Error", level=1),
                InputRichBlockParagraph(text="Invalid memory number."),
            ] + await get_formatted_memories_blocks(user_id_str)
    else:
        blocks = [
            InputRichBlockHeading(text="Command Syntax Error", level=2),
            InputRichBlockParagraph(text="Usage: edit [number] [new text]"),
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

    if sent_msg and len(blocks) > 2:
        asyncio.create_task(collapse_message(message.chat.id, sent_msg.message_id, 60))


@router.message(F.text.lower() == "forget all")
async def handle_forget_all(message: Message):
    user_id_str = str(message.from_user.id)
    await redis_client.delete(
        f"memory_list:{user_id_str}", f"chat_history:{message.chat.id}:{user_id_str}"
    )
    blocks = [
        InputRichBlockHeading(text="Memory Purged", level=1),
        InputRichBlockParagraph(text="Cleared all your saved memories."),
    ]
    if message.chat.type == "private":
        await message.answer_rich_message(rich_message=InputRichMessage(blocks=blocks))
    else:
        await message.answer_rich_message(
            rich_message=InputRichMessage(blocks=blocks),
            reply_to_message_id=message.message_id,
        )


@router.message(text_startswith("forget "))
async def handle_forget(message: Message):
    user_id_str = str(message.from_user.id)
    try:
        indices = [
            int(n.strip()) - 1
            for n in message.text.strip()[7:].split(",,")
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
            blocks = [
                InputRichBlockHeading(text="Memory Removed", level=1)
            ] + await get_formatted_memories_blocks(user_id_str)
        else:
            blocks = [
                InputRichBlockHeading(text="Error", level=1),
                InputRichBlockParagraph(text="No valid memory numbers specified."),
            ] + await get_formatted_memories_blocks(user_id_str)
    except RedisError:
        blocks = [
            InputRichBlockHeading(text="System Error", level=2),
            InputRichBlockParagraph(text="Error removing memory."),
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

    if sent_msg and len(blocks) > 2:
        asyncio.create_task(collapse_message(message.chat.id, sent_msg.message_id, 60))


# ==========================================
# Core Engine
# ==========================================


@router.message(F.text | F.caption | F.voice | F.audio)
async def handle_conversation(message: Message):
    text = message.text or message.caption or ""
    text_no_code = re.sub(r"(?s).*?", "", text)
    text_no_code = re.sub(r"(?s).*?", "", text_no_code)

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
        reply_id = None if is_private else msg_id

        if await redis_client.exists(cooldown_key):
            warn_blocks = [
                InputRichBlockHeading(text="Rate Limit Reached", level=2),
                InputRichBlockParagraph(text="Slow down, request limit reached."),
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

        if message.reply_to_message:
            replied_context = (
                message.reply_to_message.text or message.reply_to_message.caption or ""
            )
        else:
            replied_context = ""

        audio_bytes = None
        audio_mime = "audio/ogg"

        if message.content_type in ["voice", "audio"]:
            audio_obj = message.voice or message.audio
            if audio_obj:
                file_info = await bot.get_file(audio_obj.file_id)
                audio_stream = await bot.download_file(file_info.file_path)
                if audio_stream:
                    audio_bytes = audio_stream.read()
                if getattr(audio_obj, "mime_type", None):
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

                search_context = (
                    await free_web_search(clean_prompt)
                    if any(
                        w in clean_prompt.lower()
                        for w in ("search", "google", "look up", "find")
                    )
                    else ""
                )

                context_parts = []
                if replied_context:
                    context_parts.append(
                        f"Message User is Replying To:\n{replied_context}"
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

                today_str = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
                bot_instructions = (
                    f"Today's date is {today_str}. "
                    "Never use standard AI pleasantries. Do not start responses with 'As an AI' or end with generic offers for help. "
                    "Keep all responses brief and strictly to the point, avoiding any unnecessary fluff. "
                    "You may generate structural Markdown such as tables, headers (##), and lists (1., -). Do not use any HTML tags. "
                )

                if saved_facts:
                    bot_instructions += (
                        "\n\nYou must strictly follow these overrides:\n"
                        + "\n".join(f"- {f}" for f in saved_facts)
                    )

                safety_overrides = [
                    types.SafetySetting(category=c, threshold="BLOCK_NONE")
                    for c in [
                        "HARM_CATEGORY_HATE_SPEECH",
                        "HARM_CATEGORY_HARASSMENT",
                        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                        "HARM_CATEGORY_DANGEROUS_CONTENT",
                    ]
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
                    chat = gemini_client.aio.chats.create(
                        model="gemini-3.5-flash-lite",
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

            except RedisError:
                error_blocks = [
                    InputRichBlockHeading(text="System Error", level=2),
                    InputRichBlockParagraph(
                        text="Whoa, I'm getting a little overwhelmed! Let me catch my breath."
                    ),
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
# Web Hooks & App Initialization
# ==========================================


async def health_check(request):
    return web.Response(text="200 OK - Bot is running.", status=200)


app.router.add_get("/", health_check)
app.router.add_get("/health", health_check)


async def on_startup(app_instance):
    global BOT_INFO
    try:
        print("Clearing conflicting webhooks from Telegram servers...")
        await bot.delete_webhook(drop_pending_updates=True)

        BOT_INFO = await bot.get_me()
        print(f"Bot authenticated as {BOT_INFO.username}")
    except Exception as e:  # noqa: BLE001
        print(f"Failed to fetch bot info: {e}")

    asyncio.create_task(dp.start_polling(bot))


async def on_shutdown(app_instance):
    await bot.session.close()
    await redis_client.aclose()


app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
