"""Лимит одновременно открытых браузеров (все сценарии профилей и заливки)."""

from __future__ import annotations

import sys

from PyQt6.QtCore import QSettings

MAX_CONCURRENT_BROWSERS_MIN = 1
MAX_CONCURRENT_BROWSERS_MAX = 10
DEFAULT_MAX_CONCURRENT_BROWSERS = 3 if sys.platform == "darwin" else 5
SETTINGS_KEY = "antydetect/max_concurrent_browsers"


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
    settings: QSettings | None = None,
) -> int:
    s = settings or QSettings("Zaliver", "Zaliver")
    if not s.contains(SETTINGS_KEY):
        return DEFAULT_MAX_CONCURRENT_BROWSERS
    return clamp_max_concurrent_browsers(s.value(SETTINGS_KEY, DEFAULT_MAX_CONCURRENT_BROWSERS))
