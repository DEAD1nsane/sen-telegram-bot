"""Compatibility hook for Telegram RichMessage media.

Telegram Rich Messages can contain RichBlockVideo objects inside
Message.rich_message.blocks instead of populating Message.video.
Sen's existing media pipeline expects Message.video, so expose a
read-only compatibility view for rich video blocks without changing
Telegram's parsed update data.
"""

from aiogram.types import Message


_original_message_getattribute = Message.__getattribute__


def _find_rich_video(obj, seen=None):
    if obj is None:
        return None
    if seen is None:
        seen = set()
    obj_id = id(obj)
    if obj_id in seen:
        return None
    seen.add(obj_id)

    if isinstance(obj, (list, tuple)):
        for item in obj:
            found = _find_rich_video(item, seen)
            if found is not None:
                return found
        return None

    if isinstance(obj, dict):
        block_type = str(obj.get("type", "")).lower()
        if block_type == "video":
            video = obj.get("video")
            if video and (video.get("file_id") if isinstance(video, dict) else getattr(video, "file_id", None)):
                return video
        for key in ("blocks", "items", "cells"):
            value = obj.get(key)
            found = _find_rich_video(value, seen)
            if found is not None:
                return found
        return None

    block_type = str(getattr(obj, "type", "")).lower()
    if block_type == "video":
        video = getattr(obj, "video", None)
        if video is not None and getattr(video, "file_id", None):
            return video

    for attr in ("blocks", "items", "cells"):
        try:
            value = getattr(obj, attr, None)
        except Exception:
            value = None
        if value:
            found = _find_rich_video(value, seen)
            if found is not None:
                return found
    return None


def _message_getattribute(self, name):
    value = _original_message_getattribute(self, name)
    if name != "video" or value is not None:
        return value

    try:
        rich_message = _original_message_getattribute(self, "rich_message")
        rich_video = _find_rich_video(rich_message)
        if rich_video is not None:
            return rich_video
    except Exception:
        pass
    return value


Message.__getattribute__ = _message_getattribute
