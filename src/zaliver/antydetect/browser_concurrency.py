"""Лимит одновременно открытых браузеров (все сценарии профилей и заливки)."""

from __future__ import annotations

import sys

MAX_CONCURRENT_BROWSERS_MIN = 1
MAX_CONCURRENT_BROWSERS_MAX = 10
DEFAULT_MAX_CONCURRENT_BROWSERS = 3 if sys.platform == "darwin" else 5
SETTINGS_KEY = "antydetect/max_concurrent_browsers"

# Instagram multi-tab (пауза 0): вкладок на один открытый профиль.
INSTAGRAM_TABS_PER_PROFILE_MIN = 1
INSTAGRAM_TABS_PER_PROFILE_MAX = 10
DEFAULT_INSTAGRAM_TABS_PER_PROFILE = 3
SETTINGS_KEY_INSTAGRAM_TABS_PER_PROFILE = "instagram/tabs_per_profile"


def clamp_max_concurrent_browsers(value: int | float | str | None) -> int:
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        n = DEFAULT_MAX_CONCURRENT_BROWSERS
    return max(
        MAX_CONCURRENT_BROWSERS_MIN,
        min(MAX_CONCURRENT_BROWSERS_MAX, n),
    )


def max_concurrent_browsers_from_settings(
    settings: object | None = None,
) -> int:
    from zaliver.config.store import ensure_settings_store

    s = ensure_settings_store(settings)
    if not s.contains(SETTINGS_KEY):
        return DEFAULT_MAX_CONCURRENT_BROWSERS
    return clamp_max_concurrent_browsers(
        s.value(SETTINGS_KEY, DEFAULT_MAX_CONCURRENT_BROWSERS)
    )


def clamp_instagram_tabs_per_profile(value: int | float | str | None) -> int:
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        n = DEFAULT_INSTAGRAM_TABS_PER_PROFILE
    return max(
        INSTAGRAM_TABS_PER_PROFILE_MIN,
        min(INSTAGRAM_TABS_PER_PROFILE_MAX, n),
    )


def instagram_tabs_per_profile_from_settings(
    settings: object | None = None,
) -> int:
    from zaliver.config.store import ensure_settings_store

    s = ensure_settings_store(settings)
    if not s.contains(SETTINGS_KEY_INSTAGRAM_TABS_PER_PROFILE):
        return DEFAULT_INSTAGRAM_TABS_PER_PROFILE
    return clamp_instagram_tabs_per_profile(
        s.value(
            SETTINGS_KEY_INSTAGRAM_TABS_PER_PROFILE,
            DEFAULT_INSTAGRAM_TABS_PER_PROFILE,
        )
    )


def compute_instagram_tabs_per_profile(
    profile_ids: list[str] | tuple[str, ...],
    tabs_per_profile: int | float | str | None,
    *,
    max_concurrent_browsers: int | float | str | None = None,
) -> dict[str, int]:
    """
    Сколько вкладок залива на каждый выбранный профиль (пауза 0).

    Каждый профиль получает одинаковое число вкладок из настройки.
    Если профилей больше лимита окон — multi-tab выключен (по 1 вкладке).
    """
    ids = [p.strip() for p in (profile_ids or []) if (p or "").strip()]
    if not ids:
        return {}
    n_tabs = clamp_instagram_tabs_per_profile(tabs_per_profile)
    if max_concurrent_browsers is not None:
        cap = clamp_max_concurrent_browsers(max_concurrent_browsers)
        if len(ids) > cap:
            return {pid: 1 for pid in ids}
    return {pid: n_tabs for pid in ids}
