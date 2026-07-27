"""Headless settings storage (Qt / JSON / in-memory adapters)."""

from __future__ import annotations

from zaliver.config.platform_settings import (
    PLATFORM_INSTAGRAM,
    PLATFORM_YOUTUBE,
    PlatformSettings,
    normalize_platform,
    platform_display_name,
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
    "DictSettingsStore",
    "JsonFileSettingsStore",
    "PlatformSettings",
    "QSettingsStore",
    "SettingsStore",
    "normalize_platform",
    "platform_display_name",
]
