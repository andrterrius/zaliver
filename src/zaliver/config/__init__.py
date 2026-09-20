"""Headless settings storage (Qt / JSON / in-memory adapters)."""

from __future__ import annotations

from zaliver.config.platform_settings import (
    PLATFORM_INSTAGRAM,
    PLATFORM_TIKTOK,
    PLATFORM_YOUTUBE,
    PLATFORM_YT_INST,
    PLATFORM_YT_INST_TT,
    PlatformSettings,
    is_combined_upload_platform,
    is_instagram_platform,
    is_tiktok_platform,
    is_yt_inst_platform,
    is_yt_inst_tt_platform,
    normalize_platform,
    platform_display_name,
    platform_includes_instagram,
    platform_includes_tiktok,
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
    "PLATFORM_TIKTOK",
    "PLATFORM_YOUTUBE",
    "PLATFORM_YT_INST",
    "PLATFORM_YT_INST_TT",
    "DictSettingsStore",
    "JsonFileSettingsStore",
    "PlatformSettings",
    "QSettingsStore",
    "SettingsStore",
    "is_combined_upload_platform",
    "is_instagram_platform",
    "is_tiktok_platform",
    "is_yt_inst_platform",
    "is_yt_inst_tt_platform",
    "normalize_platform",
    "platform_display_name",
    "platform_includes_instagram",
    "platform_includes_tiktok",
    "platform_includes_youtube",
]
