"""General replied-to media compatibility for Telegram Rich/Advanced Editor messages."""


def _get(obj, key):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    try:
        return getattr(obj, key, None)
    except Exception:
        return None


def _media_tuple(media, mime, description):
    if media is None:
        return None
    file_id = _get(media, "file_id")
    if file_id:
        return (
            file_id,
            _get(media, "mime_type") or mime,
            _get(media, "file_size"),
            description,
        )
    return None


def _walk_rich_media(obj, seen=None):
    """Find file-backed media inside Telegram RichMessage/Advanced Editor data."""
    if obj is None:
        return None
    if seen is None:
        seen = set()
    oid = id(obj)
    if oid in seen:
        return None
    seen.add(oid)

    # Direct media fields used by different Telegram/aiogram representations.
    candidates = (
        ("photo", "image/jpeg", "Replied-to photo"),
        ("image", "image/jpeg", "Replied-to image"),
        ("video", "video/mp4", "Replied-to video"),
        ("animation", "video/mp4", "Replied-to animation"),
        ("document", "application/octet-stream", "Replied-to document"),
    )
    for key, mime, description in candidates:
        value = _get(obj, key)
        found = _media_tuple(value, mime, description)
        if found:
            return found

    # Rich blocks may expose their media under a generic media/content field.
    for key in ("media", "content", "attachment"):
        value = _get(obj, key)
        if value is not None:
            found = _walk_rich_media(value, seen)
            if found:
                return found

    # Traverse common RichMessage containers.
    for key in ("blocks", "items", "children", "model_extra", "api_kwargs"):
        value = _get(obj, key)
        if value is not None:
            found = _walk_rich_media(value, seen)
            if found:
                return found

    try:
        dumped = obj.model_dump(mode="python", exclude_none=True)
        if dumped is not obj:
            found = _walk_rich_media(dumped, seen)
            if found:
                return found
    except Exception:
        pass

    if isinstance(obj, dict):
        for value in obj.values():
            found = _walk_rich_media(value, seen)
            if found:
                return found
    elif isinstance(obj, (list, tuple, set)):
        for value in obj:
            found = _walk_rich_media(value, seen)
            if found:
                return found

    return None


def _resolve(message, original):
    replied = getattr(message, "reply_to_message", None)
    if not replied:
        return None

    # Preserve any existing resolver behavior first, including the existing
    # Advanced Editor video compatibility patch.
    try:
        found = original(message)
        if found:
            return found
    except Exception as exc:
        print(f"Replied media original resolver error: {exc}")

    # Standard Telegram message media. Photos use the largest available size.
    photo = getattr(replied, "photo", None)
    if photo:
        try:
            largest = photo[-1]
        except (IndexError, TypeError):
            largest = None
        found = _media_tuple(largest, "image/jpeg", "Replied-to photo")
        if found:
            return found

    for attr, mime, description in (
        ("video", "video/mp4", "Replied-to video"),
        ("animation", "video/mp4", "Replied-to animation"),
        ("document", "application/octet-stream", "Replied-to document"),
        ("video_note", "video/mp4", "Replied-to video note"),
    ):
        found = _media_tuple(getattr(replied, attr, None), mime, description)
        if found:
            return found

    # Advanced Editor / Rich Message content is not always represented by the
    # normal Message.photo/video/document fields. Walk the typed object and its
    # raw model representation for a file-backed media object.
    for attr in ("rich_message", "model_extra", "api_kwargs"):
        raw = getattr(replied, attr, None)
        found = _walk_rich_media(raw)
        if found:
            print(
                f"Replied Advanced Editor media found: "
                f"message_id={getattr(replied, 'message_id', None)} "
                f"description={found[3]} mime={found[1]}"
            )
            return found

    try:
        dumped = replied.model_dump(mode="python", exclude_none=True)
        found = _walk_rich_media(dumped)
        if found:
            print(
                f"Replied media found in message dump: "
                f"message_id={getattr(replied, 'message_id', None)} "
                f"description={found[3]} mime={found[1]}"
            )
            return found
    except Exception:
        pass

    return None


def install(main_module):
    original = getattr(main_module, "get_replied_video_media", None)
    if original is None:
        print("Replied media patch skipped: resolver not found")
        return

    # runtime_patch may already have wrapped this resolver. Keep that wrapper
    # as the first resolver so existing video handling remains intact.
    main_module.get_replied_video_media = lambda message: _resolve(message, original)
    print("Installed general replied-to photo/video/document/animation media resolver")
