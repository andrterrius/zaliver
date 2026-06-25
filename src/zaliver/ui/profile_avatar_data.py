"""Хранение аватарок канала в custom_data профиля."""

from __future__ import annotations

import base64

YT_AVATAR_PNG_B64_KEY = "yt_avatar_png_b64"


def avatar_png_to_custom_data_payload(png_bytes: bytes) -> dict[str, str]:
    if not png_bytes:
        return {YT_AVATAR_PNG_B64_KEY: ""}
    encoded = base64.b64encode(png_bytes).decode("ascii")
    return {YT_AVATAR_PNG_B64_KEY: encoded}


def avatar_png_from_custom_data(custom_data: dict[str, object] | None) -> bytes | None:
    if not isinstance(custom_data, dict):
        return None
    raw = custom_data.get(YT_AVATAR_PNG_B64_KEY)
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        return None


def profile_has_avatar(custom_data: dict[str, object] | None) -> bool:
    data = avatar_png_from_custom_data(custom_data)
    return bool(data)
