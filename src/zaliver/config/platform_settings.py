"""Platform id helpers and scoped settings (UI-agnostic)."""

from __future__ import annotations

from typing import Any

from zaliver.config.store import SettingsStore, ensure_settings_store

PLATFORM_YOUTUBE = "youtube"
PLATFORM_INSTAGRAM = "instagram"

PLATFORM_CHOICES: tuple[tuple[str, str, str], ...] = (
    (PLATFORM_YOUTUBE, "YouTube", "Залив видео на YouTube"),
    (PLATFORM_INSTAGRAM, "Instagram", "Залив видео на Instagram"),
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
    v = (value or "").strip().lower()
    if v == PLATFORM_INSTAGRAM:
        return PLATFORM_INSTAGRAM
    return PLATFORM_YOUTUBE


def platform_display_name(platform: str) -> str:
    return "Instagram" if normalize_platform(platform) == PLATFORM_INSTAGRAM else "YouTube"


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
        return f"platforms/{self._platform}/{key}"

    def contains(self, key: str) -> bool:
        full = self._full_key(key)
        if self._store.contains(full):
            return True
        if self._is_shared(key):
            return False
        return self._platform == PLATFORM_YOUTUBE and self._store.contains(key)

    def value(self, key: str, default: Any = None, type: Any = None) -> Any:
        full = self._full_key(key)
        if not self._store.contains(full) and not self._is_shared(key):
            if self._platform == PLATFORM_YOUTUBE and self._store.contains(key):
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
