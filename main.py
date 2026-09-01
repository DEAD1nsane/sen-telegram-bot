import os
import re
import json
import asyncio
from datetime import datetime, timezone
from aiohttp import web

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message,
    FSInputFile,
    LinkPreviewOptions,
    Update,
)
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties

import redis.asyncio as redis
import httpx
from google import genai
from google.genai import types

# ==========================================
# Environment & Config
# ==========================================
redis_url = os.environ.get("REDIS_URL", "")
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

API_TOKEN = os.getenv("BOT_TOKEN", "")
SEARXNG_URL = os.getenv(
    "SEARXNG_URL", "https://searxng-railway-production-3252.up.railway.app/search"
)

bot = Bot(token=API_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

BOT_INFO = None

gemini_api_key = os.getenv("GEMINI_API_KEY", "")
if not gemini_api_key:
    raise ValueError("CRITICAL CONFIGURATION ERROR: 'GEMINI_API_KEY' missing.")

gemini_client = genai.Client(api_key=gemini_api_key)
OWNER_ID = int(os.getenv("OWNER_ID", "0"))


# ==========================================
# Telegram Bot API: Rich Messages & Streaming
# ==========================================


async def send_rich_message(chat_id: int, markdown: str, reply_to: int = None):
    """Send a rich formatted message via sendRichMessage using Markdown."""
    url = f"https://api.telegram.org/bot{API_TOKEN}/sendRichMessage"
    payload = {"chat_id": chat_id, "rich_message": {"markdown": markdown}}
    if reply_to:
        payload["reply_parameters"] = {"message_id": reply_to}
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(url, json=payload, timeout=10.0)
            result = res.json()
            if not result.get("ok"):
                print(f"sendRichMessage API error: {result}")
            return result
    except Exception as e:
        print(f"sendRichMessage exception: {e}")
        return None


async def send_message_draft(
    chat_id: int, text: str, reply_to: int = None, can_stop: bool = True
):
    """Send or edit a streaming draft (Thinking... or partial response)."""
    url = f"https://api.telegram.org/bot{API_TOKEN}/sendMessageDraft"
    payload = {
        "chat_id": chat_id,
        "content": text,
        "can_stop": can_stop,
    }
    if reply_to:
        payload["reply_parameters"] = {"message_id": reply_to}
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(url, json=payload, timeout=10.0)
            return res.json()
    except Exception as e:
        print(f"sendMessageDraft error: {e}")
        return None


async def send_rich_message_draft(
    chat_id: int, blocks: list, reply_to: int = None, can_stop: bool = True
):
    """Send or edit a streaming rich draft."""
    blocks = normalize_table_blocks(blocks)
    url = f"https://api.telegram.org/bot{API_TOKEN}/sendRichMessageDraft"
    payload = {
        "chat_id": chat_id,
        "rich_message": {"blocks": blocks},
        "can_stop": can_stop,
    }
    if reply_to:
        payload["reply_parameters"] = {"message_id": reply_to}
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(url, json=payload, timeout=10.0)
            return res.json()
    except Exception as e:
        print(f"sendRichMessageDraft error: {e}")
        return None


def extract_json(text: str) -> dict | None:
    """Extract the first JSON object found in text."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


async def send_formatted_response(message: Message, content: str, auto_delete: int = 0):
    """Send content: RichMessage markdown if present, otherwise plain text."""
    rich_md = extract_rich_markdown(content)
    if rich_md:
        result = await send_rich_message(
            message.chat.id,
            rich_md,
            reply_to=None if message.chat.type == "private" else message.message_id,
        )
        if result and result.get("ok"):
            if auto_delete > 0:
                asyncio.create_task(
                    collapse_message(
                        message.chat.id,
                        result.get("result", {}).get("message_id", 0),
                        auto_delete,
                    )
                )
            return
        print(f"sendRichMessage failed: {result}")

    if message.chat.type == "private":
        sent_msg = await message.answer(text=content, parse_mode=None)
    else:
        sent_msg = await message.answer(
            text=content, parse_mode=None, reply_to_message_id=message.message_id
        )
    if sent_msg and auto_delete > 0:
        asyncio.create_task(
            collapse_message(message.chat.id, sent_msg.message_id, auto_delete)
        )


def normalize_table_blocks(blocks: list) -> list:
    """Convert AI-generated table format (headers/rows) to Telegram API format (cells with is_header)."""
    normalized = []
    for block in blocks:
        if block.get("type") == "table" and "headers" in block and "rows" in block:
            headers = block.get("headers", [])
            rows = block.get("rows", [])
            num_headers = len(headers) or 1
            cells = []
            header_row = []
            for header in headers:
                text = (
                    header.get("text", "") if isinstance(header, dict) else str(header)
                )
                header_row.append({"type": "text", "text": text, "is_header": True})
            cells.append(header_row)
            flat_cells = []
            for row in rows:
                for cell in row:
                    text = cell.get("text", "") if isinstance(cell, dict) else str(cell)
                    flat_cells.append({"type": "text", "text": text})
            for i in range(0, len(flat_cells), num_headers):
                cells.append(flat_cells[i : i + num_headers])
            normalized.append({"type": "table", "cells": cells})
        else:
            normalized.append(block)
    return normalized


def extract_text_from_blocks(blocks: list) -> str:
    """Extract plain text from rich message blocks for fallback."""
    parts = []
    for block in blocks:
        btype = block.get("type")
        if btype == "paragraph":
            for item in block.get("text", []):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
        elif btype == "list":
            for item in block.get("items", []):
                if item.get("type") == "text":
                    parts.append(f"- {item.get('text', '')}")
        elif btype == "table":
            if "headers" in block and "rows" in block:
                headers = [
                    h.get("text", "") if isinstance(h, dict) else str(h)
                    for h in block.get("headers", [])
                ]
                parts.append(" | ".join(headers))
                for row in block.get("rows", []):
                    cells = [
                        c.get("text", "") if isinstance(c, dict) else str(c)
                        for c in row
                    ]
                    parts.append(" | ".join(cells))
            else:
                for row in block.get("cells", []):
                    cells = [c.get("text", "") for c in row if c.get("type") == "text"]
                    parts.append(" | ".join(cells))
    return "\n".join(parts) if parts else ""


def extract_rich_markdown(text: str) -> str | None:
    """Extract markdown content from code block for rich message."""
    match = re.search(r"```(?:md|markdown)?\s*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


async def send_ai_response(message: Message, response_text: str, is_private: bool):
    """Send AI response: rich Markdown if present, otherwise plain text."""
    rich_md = extract_rich_markdown(response_text)
    if rich_md:
        result = await send_rich_message(
            message.chat.id,
            rich_md,
            reply_to=None if is_private else message.message_id,
        )
        if result and result.get("ok"):
            return
        print(f"sendRichMessage failed: {result}")

    preview_opts = LinkPreviewOptions(is_disabled=False, prefer_small_media=True)
    if is_private:
        await message.answer(
            text=response_text,
            parse_mode=None,
            link_preview_options=preview_opts,
        )
    else:
        await message.answer(
            text=response_text,
            parse_mode=None,
            reply_to_message_id=message.message_id,
            link_preview_options=preview_opts,
        )


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
                "https://html.duckduckgo.com/html/",
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


async def get_formatted_memories(user_id_str: str) -> str:
    try:
        raw_items = await redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
        if not raw_items:
            return "```\nYour memory list is currently empty.\n```"

        memories = [
            item.decode("utf-8") if isinstance(item, bytes) else item
            for item in raw_items
        ]

        lines = ["```", "**Active Memory Directives**", "---"]
        for mem in memories:
            lines.append(f"- {mem}")
        lines.append("```")
        return "\n".join(lines)
    except Exception as e:
        print(f"Error fetching memory list format: {e}")
        return "```\nCould not retrieve memory list.\n```"


async def collapse_message(chat_id: int, message_id: int, delay: int):
    await asyncio.sleep(delay)
    content = "Active Memories Collapsed"
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=content, parse_mode=None
        )
    except Exception as e:
        print(f"Error collapsing memory list: {e}")


async def auto_delete_message(
    chat_id: int, bot_msg_id: int, user_msg_id: int, delay: int
):
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
    content = "```\n**Sen Bot Command Hub**\n---\n- remember [item],, [item2] - Adds items to memory\n- what do you remember - Displays rules in a formatted list\n- edit [number] [new fact] - Edits a specific rule\n- forget [number],, [number2] - Removes memories\n- forget all - Clears all memory\n```"
    await send_formatted_response(message, content, auto_delete=60)


def text_in(options: set):
    return lambda message: message.text and message.text.lower() in options


def text_startswith(prefix: str):
    return lambda message: message.text and message.text.lower().startswith(prefix)


@router.message(text_in({"what do you remember", "how do you remember"}))
async def handle_what_remember(message: Message):
    user_id_str = str(message.from_user.id)
    content = await get_formatted_memories(user_id_str)
    await send_formatted_response(message, content, auto_delete=60)


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
    content = await get_formatted_memories(user_id_str)
    await send_formatted_response(message, content, auto_delete=60)


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
            content = await get_formatted_memories(user_id_str)
            await send_formatted_response(message, content, auto_delete=60)
            return

    error_content = "Usage: edit [number] [new text]"
    sent_msg = await message.answer(text=error_content)
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

    content = "Cleared all your saved memories."
    if message.chat.type == "private":
        sent_msg = await message.answer(text=content)
    else:
        sent_msg = await message.answer(
            text=content, reply_to_message_id=message.message_id
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

    content = await get_formatted_memories(user_id_str)

    if message.chat.type == "private":
        sent_msg = await message.answer(text=content)
    else:
        sent_msg = await message.answer(
            text=content, reply_to_message_id=message.message_id
        )

    if sent_msg:
        asyncio.create_task(collapse_message(message.chat.id, sent_msg.message_id, 60))


# ==========================================
# Primary Chat, Mentions & Audio Engine
# ==========================================


@router.message(F.text | F.caption | F.voice | F.audio)
async def handle_conversation(message: Message):
    text = message.text or message.caption or ""
    text_no_html = re.sub(r"<[^>]+>", "", text)

    if re.search(r"\bsen\b", text_no_html, re.IGNORECASE):
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
    if re.search(r"\bmagic(?:al|ally)?\b", text_no_html, re.IGNORECASE):
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
        bot_username and bot_username.lower() in text_no_html.lower()
    ) or "@gemini" in text_no_html.lower()
    is_reply_to_bot = bool(
        message.reply_to_message
        and BOT_INFO
        and message.reply_to_message.from_user.id == BOT_INFO.id
    )
    is_private = message.chat.type == "private"

    if is_tagged or is_reply_to_bot or is_private:
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

            cooldown_key = f"cooldown:{user_id_str}"
            if await redis_client.exists(cooldown_key):
                warn_content = "Slow down, request limit reached."
                if is_private:
                    await message.answer(text=warn_content)
                else:
                    await message.answer(text=warn_content, reply_to_message_id=msg_id)
                return

            await redis_client.set(cooldown_key, "1", ex=4)

            try:
                await send_message_draft(
                    chat_id,
                    "Thinking...",
                    reply_to=None if is_private else msg_id,
                    can_stop=True,
                )

                raw_mem = await redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
                saved_facts = [
                    i.decode("utf-8") if isinstance(i, bytes) else i for i in raw_mem
                ]

                history_key = f"chat_history:{chat_id}:{user_id_str}"
                raw_hist = await redis_client.lrange(history_key, 0, -1)
                chat_history = [
                    h.decode("utf-8") if isinstance(h, bytes) else h for h in raw_hist
                ]

                search_keywords = {
                    "search",
                    "google",
                    "look up",
                    "lookup",
                    "find",
                    "show",
                    "show me",
                    "table",
                    "list",
                }
                explicit_search = any(
                    word in clean_prompt.lower() for word in search_keywords
                )

                search_query = clean_prompt
                if explicit_search and len(clean_prompt.split()) <= 4:
                    if (
                        replied_context
                        and "I don't have enough details" not in replied_context
                        and "I am currently broken" not in replied_context
                    ):
                        search_query = replied_context
                    elif chat_history:
                        for past_msg in reversed(chat_history):
                            if (
                                past_msg.startswith("User: ")
                                and len(past_msg.split()) > 2
                            ):
                                search_query = past_msg.replace("User: ", "").strip()
                                break

                search_context = (
                    await free_web_search(search_query) if explicit_search else ""
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
                    f"Today's date is {today_str}. Keep responses structural using double line-breaks to separate ideas. "
                    "Never use standard AI pleasantries. Do not start responses with 'As an AI' or end with generic offers for help. "
                    "Keep casual replies brief, but dynamically expand your response length when explicitly asked for details or when playing interactive games. "
                    "If the user changes the subject abruptly, drop the previous topic immediately and adapt to the new flow. "
                    "If the user is clearly joking or sarcastic, match their energy rather than taking the prompt literally. "
                    "If you do not know the answer or the provided context is insufficient, state 'I don't have enough details to answer that accurately' directly without guessing. "
                    "Do not assume personal details about the user unless they are explicitly provided in your memory list. "
                    "By default, respond as plain text with no formatting. "
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
                    chat = gemini_client.aio.chats.create(
                        model="gemini-3.5-flash-lite",
                        config=types.GenerateContentConfig(
                            system_instruction=bot_instructions,
                            safety_settings=safety_overrides,
                        ),
                    )
                    response = await chat.send_message(contents)
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

                response_text = response.text or ""

                response_text = response_text.replace("\u2022", "").replace("```", "")

                await send_ai_response(message, response_text, is_private)

                clean_history_text = re.sub(r"<[^>]+>", "", response_text)
                await redis_client.rpush(
                    history_key,
                    f"User: {clean_prompt or 'Voice Note'}",
                    f"Bot: {clean_history_text}",
                )
                await redis_client.ltrim(history_key, -10, -1)

            except Exception as ai_err:
                print(f"Gemini API error: {ai_err}")
                error_content = (
                    "I am currently broken right now, the owner needs to fix me."
                )
                if "429" in str(ai_err):
                    error_content = "Whoa, I'm getting a little overwhelmed! Let me catch my breath for a minute."

                if is_private:
                    await message.answer(text=error_content)
                else:
                    await message.answer(text=error_content, reply_to_message_id=msg_id)


# ==========================================
# Polling Execution Loop & Healthcheck Server
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
    port = int(os.environ.get("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Healthcheck server listening on port {port}")

    try:
        await dp.start_polling(bot)
    finally:
        print("SIGTERM received! Cleaning up database connections...")
        await bot.session.close()
        await redis_client.aclose()
        await runner.cleanup()
        print("Cleanup complete. Process exiting.")


if __name__ == "__main__":
    asyncio.run(main())
