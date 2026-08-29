import asyncio
from datetime import datetime, timezone
import os
import re

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message
from aiohttp import web
from google import genai
from google.genai import types
import httpx
import redis.asyncio as redis

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

# ==========================================
# Core Messaging Network Client
# ==========================================


async def transmit_rich_payload(
    chat_id: int, blocks: list, reply_to_id: int = None
) -> dict:
    url = f"https://api.telegram.org/bot{API_TOKEN}/sendRichMessage"
    payload = {"chat_id": chat_id, "blocks": blocks}
    if reply_to_id:
        payload["reply_parameters"] = {"message_id": reply_to_id}

    async with httpx.AsyncClient() as client:
        try:
            res = await client.post(url, json=payload, timeout=10.0)
            return res.json()
        except Exception as e:
            print(f"Error transmitting rich payload: {e}")
            return {}


# ==========================================
# Rich Text Structural Helpers
# ==========================================


def wrap_text_payload(content: str) -> dict:
    return {"text": content}


# ==========================================
# Stateful Block Parser (Raw Dictionary Output)
# ==========================================


def parse_text_to_blocks(text: str) -> list:
    blocks = []
    lines = text.split("\n")
    paragraph_buffer = []
    in_code_block = False
    code_buffer = []

    def flush_buffers():
        if paragraph_buffer:
            text_content = "\n".join(paragraph_buffer).strip()
            if text_content:
                blocks.append(
                    {"type": "paragraph", "text": wrap_text_payload(text_content)}
                )
            paragraph_buffer.clear()

    for line in lines:
        if line.strip().startswith("```"):
            if in_code_block:
                in_code_block = False
                flush_buffers()
                clean_code = "\n".join(code_buffer)
                blocks.append(
                    {
                        "type": "paragraph",
                        "text": wrap_text_payload(f"Code Snippet:\n{clean_code}"),
                    }
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
                {
                    "type": "mathematical_expression",
                    "expression": line.strip().strip("$"),
                }
            )
            continue

        heading_match = re.match(r"^(#{1,3})\s+(.*)", line)
        if heading_match:
            flush_buffers()
            level = len(heading_match.group(1))
            blocks.append(
                {
                    "type": "heading",
                    "text": wrap_text_payload(heading_match.group(2).strip()),
                    "level": level,
                }
            )
            continue

        if line.strip().startswith("|") and line.strip().endswith("|"):
            flush_buffers()
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.match(r"^\-+$", c) for c in cells):
                continue
            table_cells = [{"text": wrap_text_payload(c)} for c in cells]
            new_row = {"cells": table_cells}
            if blocks and blocks[-1].get("type") == "table":
                blocks[-1]["rows"].append(new_row)
            else:
                blocks.append(
                    {
                        "type": "table",
                        "is_bordered": True,
                        "is_striped": True,
                        "rows": [new_row],
                    }
                )
            continue

        list_match = re.match(r"^(\d+\.|\-|\*)\s+(.*)", line)
        if list_match:
            flush_buffers()
            item_content = list_match.group(2).strip()
            new_item = {
                "blocks": [
                    {
                        "type": "paragraph",
                        "text": wrap_text_payload(item_content),
                    }
                ]
            }
            if blocks and blocks[-1].get("type") == "list":
                blocks[-1]["items"].append(new_item)
            else:
                blocks.append({"type": "list", "items": [new_item]})
            continue

        if not line.strip():
            flush_buffers()
            continue

        paragraph_buffer.append(line)

    flush_buffers()
    if not blocks:
        blocks.append({"type": "paragraph", "text": wrap_text_payload(" ")})
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
                "[https://duckduckgo.com](https://duckduckgo.com)",
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
    if not bot_msg_id:
        return
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=bot_msg_id)
    except Exception:
        pass
    try:
        await bot.delete_message(chat_id=chat_id, message_id=user_msg_id)
    except Exception:
        pass


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
    try:
        raw_items = await redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
        blocks = [
            {
                "type": "heading",
                "text": wrap_text_payload("Active Memory Directives"),
                "level": 2,
            }
        ]
        if not raw_items:
            blocks.append(
                {
                    "type": "paragraph",
                    "text": wrap_text_payload("Your memory list is currently empty."),
                }
            )
        else:
            rows = [
                {
                    "cells": [
                        {"text": wrap_text_payload("Index")},
                        {"text": wrap_text_payload("Memory Directive")},
                    ]
                }
            ]
            for idx, item in enumerate(raw_items):
                mem_text = item.decode("utf-8") if isinstance(item, bytes) else item
                rows.append(
                    {
                        "cells": [
                            {"text": wrap_text_payload(str(idx + 1))},
                            {"text": wrap_text_payload(mem_text)},
                        ]
                    }
                )
            blocks.append({"type": "table", "is_bordered": True, "rows": rows})
        reply_to = message.message_id if message.chat.type != "private" else None
        res = await transmit_rich_payload(message.chat.id, blocks, reply_to)
        sent_msg_id = res.get("result", {}).get("message_id")
        if sent_msg_id:
            asyncio.create_task(
                auto_delete_message(
                    message.chat.id, sent_msg_id, message.message_id, 60
                )
            )
    except Exception as e:
        print(f"Error rendering structured memory blocks: {e}")
        await bot.send_message(
            chat_id=message.chat.id, text="⚠️ Error loading active memory layout."
        )


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
        {
            "type": "heading",
            "text": wrap_text_payload("Sen Bot Command Hub"),
            "level": 1,
        },
        {
            "type": "list",
            "items": [
                {
                    "blocks": [
                        {
                            "type": "paragraph",
                            "text": wrap_text_payload(
                                "remember [item],, [item2] - Adds items to memory"
                            ),
                        }
                    ]
                },
                {
                    "blocks": [
                        {
                            "type": "paragraph",
                            "text": wrap_text_payload(
                                "what do you remember - Displays rules in a formatted"
                                " list"
                            ),
                        }
                    ]
                },
                {
                    "blocks": [
                        {
                            "type": "paragraph",
                            "text": wrap_text_payload(
                                "edit [number] [new fact] - Edits a specific rule"
                            ),
                        }
                    ]
                },
                {
                    "blocks": [
                        {
                            "type": "paragraph",
                            "text": wrap_text_payload(
                                "forget [number],, [number2] - Removes memories"
                            ),
                        }
                    ]
                },
                {
                    "blocks": [
                        {
                            "type": "paragraph",
                            "text": wrap_text_payload("forget all - Clears all memory"),
                        }
                    ]
                },
            ],
        },
    ]
    reply_to = message.message_id if message.chat.type != "private" else None
    res = await transmit_rich_payload(message.chat.id, blocks, reply_to)
    sent_msg_id = res.get("result", {}).get("message_id")
    if sent_msg_id:
        asyncio.create_task(
            auto_delete_message(message.chat.id, sent_msg_id, message.message_id, 60)
        )


def text_in(options: set):
    return (
        lambda message: message.text
        and message.text.strip().lstrip("/").lower() in options
    )


def text_startswith(prefix: str):
    return lambda message: message.text and message.text.strip().lstrip(
        "/"
    ).lower().startswith(prefix)


@router.message(text_in({"what do you remember", "how do you remember"}))
async def handle_what_remember(message: Message):
    await send_formatted_memories_as_blocks(message, str(message.from_user.id))


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
    error_blocks = [
        {
            "type": "paragraph",
            "text": wrap_text_payload("Usage: edit [number] [new text]"),
        }
    ]
    reply_to = message.message_id if message.chat.type != "private" else None
    res = await transmit_rich_payload(message.chat.id, error_blocks, reply_to)
    sent_msg_id = res.get("result", {}).get("message_id")
    if sent_msg_id:
        asyncio.create_task(
            auto_delete_message(message.chat.id, sent_msg_id, message.message_id, 10)
        )


@router.message(F.text.lower() == "forget all")
async def handle_forget_all(message: Message):
    user_id_str = str(message.from_user.id)
    await redis_client.delete(
        f"memory_list:{user_id_str}", f"chat_history:{message.chat.id}:{user_id_str}"
    )
    blocks = [
        {
            "type": "paragraph",
            "text": wrap_text_payload("Cleared all your saved memories."),
        }
    ]
    reply_to = message.message_id if message.chat.type != "private" else None
    res = await transmit_rich_payload(message.chat.id, blocks, reply_to)
    sent_msg_id = res.get("result", {}).get("message_id")
    if sent_msg_id:
        asyncio.create_task(
            auto_delete_message(message.chat.id, sent_msg_id, message.message_id, 10)
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
        reply_to_id = None if is_private else msg_id
        if await redis_client.exists(cooldown_key):
            warn_blocks = [
                {
                    "type": "paragraph",
                    "text": wrap_text_payload("Slow down, request limit reached."),
                }
            ]
            await transmit_rich_payload(chat_id, warn_blocks, reply_to_id)
            return
            await redis_client.set(cooldown_key, "1", ex=4)
        replied_context = (
            message.reply_to_message.text or message.reply_to_message.caption or ""
            if message.reply_to_message
            else ""
        )
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
                today_str = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
                bot_instructions = (
                    f"Today's date is {today_str}. Keep responses structural using"
                    " double line-breaks to separate ideas. Never use standard AI"
                    " pleasantries. Do not start responses with 'As an AI' or end with"
                    " generic offers for help. Keep casual replies brief, but"
                    " dynamically expand your response length when explicitly asked for"
                    " details or when playing interactive games. If the user changes"
                    " the subject abruptly, drop the previous topic immediately and"
                    " adapt to the new flow. If the user is clearly joking or"
                    " sarcastic, match their energy rather than taking the prompt"
                    " literally. If you do not know the answer or the provided"
                    " context is insufficient, state 'I don't have enough details to"
                    " answer that accurately' directly without guessing. Do not assume"
                    " personal details about the user unless they are explicitly"
                    " provided in your memory list.\n\nFORMATTING REQUIREMENTS:\n-"
                    " Headings: Always start lines with #, ##, or ### to build"
                    " structured sections.\n- Lists: Always start lines with - or *"
                    " to compile options natively.\n- Tables: Always structure tabular"
                    " analytics cleanly using standard markdown | cell | structures.\n-"
                    " Equations: Wrap inline mathematics or formulas within structural"
                    " $$ counters."
                )
                if search_context:
                    bot_instructions += (
                        "When referencing 'Web Search Context', state the information"
                        " directly without saying 'According to my search' or 'I found"
                        " this online'. Never include or state URLs/links from the Web"
                        " Search Context unless the user explicitly asks for links,"
                        " sources, or URLs in their prompt. "
                    )
                if chat_history:
                    bot_instructions += (
                        "Use the 'Recent Conversation Context' to track pronouns and"
                        " subjects, but never summarize or repeat the history back to"
                        " the user. "
                    )
                if saved_facts:
                    bot_instructions += (
                        "\n\nYou must strictly follow these User Instructions. They"
                        " override any baseline behavior and are your absolute highest"
                        " priority:\n" + "\n".join(f"- {f}" for f in saved_facts)
                    )
                safety_overrides = [
                    types.SafetySetting(category=cat, threshold="BLOCK_NONE")
                    for cat in [
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
                            f"Primary model failed ({primary_err}), falling back to"
                            " gemini-2.5-flash..."
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
                await transmit_rich_payload(chat_id, rich_blocks, reply_to_id)
                await redis_client.rpush(
                    history_key,
                    f"User: {clean_prompt or 'Voice Note'}",
                    f"Bot: {raw_text}",
                )
                await redis_client.ltrim(history_key, -10, -1)
            except Exception as ai_err:
                print(f"Gemini API error: {ai_err}")
                error_blocks = [
                    {
                        "type": "paragraph",
                        "text": wrap_text_payload(
                            "I am currently broken right now, the owner needs to fix me."
                        ),
                    }
                ]
                if "429" in str(ai_err):
                    error_blocks = [
                        {
                            "type": "paragraph",
                            "text": wrap_text_payload(
                                "Whoa, I'm getting a little overwhelmed! Let me catch my"
                                " breath for a minute."
                            ),
                        }
                    ]
                await transmit_rich_payload(chat_id, error_blocks, reply_to_id)
            finally:
                await redis_client.delete(cooldown_key)


# ==========================================
# Web Hooks & App Initialization
# ==========================================


async def health_check(request):
    return web.Response(text="200 OK - Bot is running.", status=200)


async def main():
    global BOT_INFO
    try:
        print("Clearing conflicting webhooks from Telegram servers...")
        await bot.delete_webhook(drop_pending_updates=True)
        BOT_INFO = await bot.get_me()
        print(f"Bot authenticated as {BOT_INFO.username}")
    except Exception as e:
        print(f"Failed to fetch bot info: {e}")
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Healthcheck server listening on port {port}")
    try:
        await dp.start_polling(bot, skip_updates=True)
    finally:
        await bot.session.close()
        await redis_client.aclose()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
