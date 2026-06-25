"""Сопоставление вырезанных аватарок с отмеченными профилями."""

from __future__ import annotations

import random

from zaliver.ui.antic_profile_row import _profile_id, _profile_name


def build_selected_profile_avatar_rows(
    profiles: list[dict[str, object]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for profile in profiles:
        rows.append(
            {
                "profile_id": _profile_id(profile),
                "profile_name": _profile_name(profile),
                "avatar_png": b"",
                "avatar_index": 0,
                "status": "Выберите файлы с аватарками",
                "can_save": False,
            }
        )
    return rows


def assign_avatars_to_selected_profiles(
    profiles: list[dict[str, object]],
    avatar_pngs: list[bytes],
    *,
    shuffle: bool = False,
) -> list[dict[str, object]]:
    """Сопоставляет аватарки с профилями по порядку (сверху вниз, слева направо)."""
    pngs = list(avatar_pngs)
    if shuffle and len(pngs) > 1:
        random.shuffle(pngs)
    rows: list[dict[str, object]] = []
    for i, profile in enumerate(profiles):
        png = pngs[i] if i < len(pngs) else b""
        has_data = bool(png)
        rows.append(
            {
                "profile_id": _profile_id(profile),
                "profile_name": _profile_name(profile),
                "avatar_png": png,
                "avatar_index": i + 1 if has_data else 0,
                "status": "Готово к сохранению" if has_data else "Нет аватарки в файле",
                "can_save": has_data,
            }
        )
    for j, png in enumerate(pngs[len(profiles) :], start=len(profiles) + 1):
        rows.append(
            {
                "profile_id": "",
                "profile_name": "",
                "avatar_png": png,
                "avatar_index": j,
                "status": "Лишняя аватарка (нет профиля)",
                "can_save": False,
            }
        )
    return rows
