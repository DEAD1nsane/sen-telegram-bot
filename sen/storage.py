"""Redis-backed storage helpers for interactions, menus, and caching."""

from __future__ import annotations

from .config import (
    redis_client,
    INTERACTION_TTL,
    MENU_TTL,
    interaction_key,
    menu_identity_key,
)


async def set_interaction(chat_id: int, user_id: int, action: str) -> None:
    await redis_client.set(interaction_key(chat_id, user_id), action, ex=INTERACTION_TTL)


async def get_interaction(chat_id: int, user_id: int) -> str | None:
    value = await redis_client.get(interaction_key(chat_id, user_id))
    return value.decode() if isinstance(value, bytes) else value


async def clear_interaction(chat_id: int, user_id: int) -> None:
    await redis_client.delete(interaction_key(chat_id, user_id))


async def register_menu_identity(chat_id: int, user_id: int, message_id: int) -> None:
    await redis_client.set(
        menu_identity_key(chat_id, user_id), str(message_id), ex=MENU_TTL + 5
    )


async def get_menu_identity(chat_id: int, user_id: int) -> int | None:
    value = await redis_client.get(menu_identity_key(chat_id, user_id))
    if value is None:
        return None
    try:
        return int(value.decode() if isinstance(value, bytes) else value)
    except (TypeError, ValueError):
        return None


async def clear_menu_identity(chat_id: int, user_id: int) -> None:
    await redis_client.delete(menu_identity_key(chat_id, user_id))


async def get_memories(user_id_str: str, temp_forget: bool = False) -> list[str]:
    """Fetch saved memories for a user. Returns empty list if temp_forget is set."""
    if temp_forget:
        return []
    try:
        raw = await redis_client.lrange(f"memory_list:{user_id_str}", 0, -1)
        return [x.decode() if isinstance(x, bytes) else str(x) for x in raw]
    except Exception as e:
        print(f"Memory read error: {e}")
        return []
