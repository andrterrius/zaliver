"""Лимит смены названия канала в custom_data профиля."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

YT_CHANNEL_NAME_CHANGE_AVAILABLE_AT_KEY = "yt_channel_name_change_available_at"
NAME_CHANGE_COOLDOWN_DAYS = 14


def _parse_iso_datetime(raw: object) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def channel_name_change_available_at(
    custom_data: dict[str, object] | None,
) -> datetime | None:
    if not isinstance(custom_data, dict):
        return None
    return _parse_iso_datetime(custom_data.get(YT_CHANNEL_NAME_CHANGE_AVAILABLE_AT_KEY))


def is_channel_name_change_blocked(
    custom_data: dict[str, object] | None,
    *,
    now: datetime | None = None,
) -> bool:
    available_at = channel_name_change_available_at(custom_data)
    if available_at is None:
        return False
    current = now or datetime.now(timezone.utc)
    return current < available_at


def channel_name_change_blocked_label(
    custom_data: dict[str, object] | None,
) -> str:
    available_at = channel_name_change_available_at(custom_data)
    if available_at is None:
        return ""
    local = available_at.astimezone()
    return local.strftime("%d.%m.%Y %H:%M")


def channel_name_change_cooldown_payload(
    *,
    now: datetime | None = None,
) -> dict[str, str]:
    current = now or datetime.now(timezone.utc)
    available_at = current + timedelta(days=NAME_CHANGE_COOLDOWN_DAYS)
    return {
        YT_CHANNEL_NAME_CHANGE_AVAILABLE_AT_KEY: available_at.astimezone(
            timezone.utc
        ).isoformat()
    }
