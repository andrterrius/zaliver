"""Сопоставление вырезанных аватарок и названий каналов с отмеченными профилями."""

from __future__ import annotations

import random
import re
from pathlib import Path

from zaliver.ui.antic_profile_row import _profile_id, _profile_name
from zaliver.ui.profile_list_helpers import _profile_custom_data
from zaliver.ui.profile_avatar_data import (
    channel_name_change_blocked_label,
    is_channel_name_change_blocked,
)

_NAME_SPLIT_RE = re.compile(r"[,;\n]+")


def parse_channel_names_text(data_text: str) -> list[str]:
    """Названия каналов, разделённые запятой, точкой с запятой или новой строкой."""
    names: list[str] = []
    seen: set[str] = set()
    for part in _NAME_SPLIT_RE.split(data_text or ""):
        name = part.strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def parse_channel_names_file(path: str | Path) -> list[str]:
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8-sig")
    return parse_channel_names_text(text)


def _profile_custom_data_dict(profile: dict[str, object]) -> dict[str, object]:
    cd = profile.get("custom_data")
    if isinstance(cd, dict):
        return cd
    return _profile_custom_data(profile)


def _empty_row(profile: dict[str, object], *, status: str) -> dict[str, object]:
    cd = _profile_custom_data_dict(profile)
    blocked = is_channel_name_change_blocked(cd)
    blocked_label = channel_name_change_blocked_label(cd) if blocked else ""
    return {
        "profile_id": _profile_id(profile),
        "profile_name": _profile_name(profile),
        "avatar_png": b"",
        "avatar_index": 0,
        "channel_name": "",
        "name_index": 0,
        "video_default_title": "",
        "video_title_index": 0,
        "skip_name_change": blocked,
        "name_change_reason": (
            f"Лимит 14 дн. (до {blocked_label})" if blocked else ""
        ),
        "status": status,
        "can_save": False,
    }


def build_selected_profile_avatar_rows(
    profiles: list[dict[str, object]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for profile in profiles:
        rows.append(
            _empty_row(profile, status="Выберите файлы с аватарками и/или названия")
        )
    return rows


def _cycle_pick(items: list, index: int):
    if not items:
        return None
    return items[index % len(items)]


def assign_avatars_to_selected_profiles(
    profiles: list[dict[str, object]],
    avatar_pngs: list[bytes],
    *,
    shuffle: bool = False,
    channel_names: list[str] | None = None,
    shuffle_names: bool = False,
    video_default_titles: list[str] | None = None,
    shuffle_video_titles: bool = False,
) -> list[dict[str, object]]:
    """Сопоставляет аватарки, названия каналов и названия для видео с профилями по порядку.

    Если элементов меньше, чем профилей, список зацикливается (10 аватарок на 30 профилей
    — каждая аватарка назначается трём профилям подряд).
    """
    pngs = list(avatar_pngs)
    if shuffle and len(pngs) > 1:
        random.shuffle(pngs)

    names = list(channel_names or [])
    if shuffle_names and len(names) > 1:
        random.shuffle(names)

    video_titles = list(video_default_titles or [])
    if shuffle_video_titles and len(video_titles) > 1:
        random.shuffle(video_titles)

    profile_count = len(profiles)
    rows: list[dict[str, object]] = []
    for i, profile in enumerate(profiles):
        png_pick = _cycle_pick(pngs, i)
        png = png_pick if isinstance(png_pick, (bytes, bytearray)) else b""
        has_avatar = bool(png)
        channel_name = str(_cycle_pick(names, i) or "")
        has_name = bool(channel_name)
        video_title = str(_cycle_pick(video_titles, i) or "")
        has_video_title = bool(video_title)

        cd = _profile_custom_data_dict(profile)
        skip_name = is_channel_name_change_blocked(cd)
        blocked_label = channel_name_change_blocked_label(cd) if skip_name else ""
        name_reason = (
            f"Лимит 14 дн. (до {blocked_label})" if skip_name and has_name else ""
        )

        can_save = has_avatar or (has_name and not skip_name) or has_video_title
        status_parts: list[str] = []
        if has_avatar:
            status_parts.append("Аватарка")
        if has_name:
            if skip_name:
                status_parts.append("Название не будет изменено")
            else:
                status_parts.append("Название")
        if has_video_title:
            status_parts.append("Название для видео")
        if not status_parts:
            status_parts.append("Нечего сохранять")

        rows.append(
            {
                "profile_id": _profile_id(profile),
                "profile_name": _profile_name(profile),
                "avatar_png": png,
                "avatar_index": (i % len(pngs)) + 1 if pngs else 0,
                "channel_name": channel_name,
                "name_index": (i % len(names)) + 1 if names else 0,
                "video_default_title": video_title,
                "video_title_index": (i % len(video_titles)) + 1 if video_titles else 0,
                "skip_name_change": skip_name,
                "name_change_reason": name_reason,
                "status": " · ".join(status_parts),
                "can_save": can_save,
            }
        )

    if profile_count > 0 and len(pngs) > profile_count:
        for j, png in enumerate(pngs[profile_count:], start=profile_count + 1):
            rows.append(
                {
                    "profile_id": "",
                    "profile_name": "",
                    "avatar_png": png,
                    "avatar_index": j,
                    "channel_name": "",
                    "name_index": 0,
                    "video_default_title": "",
                    "video_title_index": 0,
                    "skip_name_change": False,
                    "name_change_reason": "",
                    "status": "Лишняя аватарка (нет профиля)",
                    "can_save": False,
                }
            )
    if profile_count > 0 and len(names) > profile_count:
        for j, name in enumerate(names[profile_count:], start=profile_count + 1):
            rows.append(
                {
                    "profile_id": "",
                    "profile_name": "",
                    "avatar_png": b"",
                    "avatar_index": 0,
                    "channel_name": name,
                    "name_index": j,
                    "video_default_title": "",
                    "video_title_index": 0,
                    "skip_name_change": False,
                    "name_change_reason": "",
                    "status": "Лишнее название (нет профиля)",
                    "can_save": False,
                }
            )
    if profile_count > 0 and len(video_titles) > profile_count:
        for j, title in enumerate(video_titles[profile_count:], start=profile_count + 1):
            rows.append(
                {
                    "profile_id": "",
                    "profile_name": "",
                    "avatar_png": b"",
                    "avatar_index": 0,
                    "channel_name": "",
                    "name_index": 0,
                    "video_default_title": title,
                    "video_title_index": j,
                    "skip_name_change": False,
                    "name_change_reason": "",
                    "status": "Лишнее название для видео (нет профиля)",
                    "can_save": False,
                }
            )
    return rows
