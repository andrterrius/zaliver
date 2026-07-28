"""Headless settings storage (Qt / JSON / in-memory adapters)."""

from __future__ import annotations

from zaliver.config.platform_settings import (
    PLATFORM_INSTAGRAM,
    PLATFORM_YOUTUBE,
    PLATFORM_YT_INST,
    PlatformSettings,
    is_instagram_platform,
    is_yt_inst_platform,
    normalize_platform,
    platform_display_name,
    platform_includes_instagram,
    platform_includes_youtube,
)
from zaliver.config.store import (
    DictSettingsStore,
    JsonFileSettingsStore,
    QSettingsStore,
    SettingsStore,
)

__all__ = [
    "PLATFORM_INSTAGRAM",
    "PLATFORM_YOUTUBE",
    "PLATFORM_YT_INST",
    "DictSettingsStore",
    "JsonFileSettingsStore",
    "PlatformSettings",
    "QSettingsStore",
    "SettingsStore",
    "is_instagram_platform",
    "is_yt_inst_platform",
    "normalize_platform",
    "platform_display_name",
    "platform_includes_instagram",
    "platform_includes_youtube",
]
