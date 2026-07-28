"""Platform id helpers and scoped settings (UI-agnostic)."""

from __future__ import annotations

from typing import Any

from zaliver.config.store import SettingsStore, ensure_settings_store

PLATFORM_YOUTUBE = "youtube"
PLATFORM_INSTAGRAM = "instagram"
PLATFORM_YT_INST = "yt_inst"

PLATFORM_CHOICES: tuple[tuple[str, str, str], ...] = (
    (PLATFORM_YOUTUBE, "YouTube", "Залив видео на YouTube"),
    (PLATFORM_INSTAGRAM, "Instagram", "Залив видео на Instagram"),
    (
        PLATFORM_YT_INST,
        "Yt+Inst",
        "Одно видео на YouTube и Instagram (2 вкладки)",
    ),
)

# Shared across platforms (antidetect, LLM key).
_SHARED_KEY_PREFIXES: tuple[str, ...] = (
    "antydetect/",
)
_SHARED_KEYS: frozenset[str] = frozenset(
    {
        "ai/base_url",
        "ai/api_key",
        "ai/model",
    }
)


def normalize_platform(value: str | None) -> str:
    v = (value or "").strip().lower().replace("+", "_").replace("-", "_")
    if v in (PLATFORM_INSTAGRAM, "ig", "inst"):
        return PLATFORM_INSTAGRAM
    if v in (
        PLATFORM_YT_INST,
        "youtube_instagram",
        "youtube_inst",
        "ytinstagram",
        "yt_ig",
    ):
        return PLATFORM_YT_INST
    return PLATFORM_YOUTUBE


def platform_display_name(platform: str) -> str:
    p = normalize_platform(platform)
    if p == PLATFORM_INSTAGRAM:
        return "Instagram"
    if p == PLATFORM_YT_INST:
        return "Yt+Inst"
    return "YouTube"


def is_instagram_platform(platform: str | None) -> bool:
    return normalize_platform(platform) == PLATFORM_INSTAGRAM


def is_yt_inst_platform(platform: str | None) -> bool:
    return normalize_platform(platform) == PLATFORM_YT_INST


def platform_includes_youtube(platform: str | None) -> bool:
    p = normalize_platform(platform)
    return p in (PLATFORM_YOUTUBE, PLATFORM_YT_INST)


def platform_includes_instagram(platform: str | None) -> bool:
    p = normalize_platform(platform)
    return p in (PLATFORM_INSTAGRAM, PLATFORM_YT_INST)


def platform_settings_storage_id(platform: str | None) -> str:
    """
    Namespace for PlatformSettings keys.

    Yt+Inst shares the YouTube namespace for prep/shared UI keys (folders, uniquify).
    Platform-specific upload params (IG pause/tabs, YT API) should be read via
    PlatformSettings(store, youtube|instagram) explicitly when in yt_inst mode.
    """
    p = normalize_platform(platform)
    if p == PLATFORM_YT_INST:
        return PLATFORM_YOUTUBE
    return p


class PlatformSettings:
    """Settings with platforms/{id}/ prefix; antidetect and AI keys are shared."""

    def __init__(self, settings: Any, platform: str) -> None:
        if type(settings).__name__ == "PlatformSettings":
            self._store = settings.store  # type: ignore[attr-defined]
        elif isinstance(settings, SettingsStore):
            self._store = settings
        else:
            self._store = ensure_settings_store(settings)
        self._platform = normalize_platform(platform)
        self._storage_platform = platform_settings_storage_id(self._platform)

    @property
    def platform(self) -> str:
        return self._platform

    @property
    def store(self) -> SettingsStore:
        return self._store

    @property
    def inner(self) -> SettingsStore:
        """Alias for store (legacy name from QSettings era)."""
        return self._store

    def _is_shared(self, key: str) -> bool:
        if key in _SHARED_KEYS:
            return True
        return any(key.startswith(p) for p in _SHARED_KEY_PREFIXES)

    def _full_key(self, key: str) -> str:
        if self._is_shared(key):
            return key
        return f"platforms/{self._storage_platform}/{key}"

    def contains(self, key: str) -> bool:
        full = self._full_key(key)
        if self._store.contains(full):
            return True
        if self._is_shared(key):
            return False
        return self._storage_platform == PLATFORM_YOUTUBE and self._store.contains(key)

    def value(self, key: str, default: Any = None, type: Any = None) -> Any:
        full = self._full_key(key)
        if not self._store.contains(full) and not self._is_shared(key):
            if self._storage_platform == PLATFORM_YOUTUBE and self._store.contains(key):
                legacy = (
                    self._store.value(key, default, type=type)
                    if type is not None
                    else self._store.value(key, default)
                )
                self._store.setValue(full, legacy)
                return legacy
        if type is not None:
            return self._store.value(full, default, type=type)
        return self._store.value(full, default)

    def setValue(self, key: str, value: Any) -> None:
        self._store.setValue(self._full_key(key), value)

    def remove(self, key: str) -> None:
        self._store.remove(self._full_key(key))

    def sync(self) -> None:
        self._store.sync()
