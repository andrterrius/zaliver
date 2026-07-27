"""Settings store protocol and adapters (no UI dependency)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SettingsStore(Protocol):
    """Key/value settings used by core and UI adapters."""

    def contains(self, key: str) -> bool: ...

    def value(self, key: str, default: Any = None, type: Any = None) -> Any: ...

    def setValue(self, key: str, value: Any) -> None: ...

    def remove(self, key: str) -> None: ...

    def sync(self) -> None: ...


class DictSettingsStore:
    """In-memory store for tests and headless/web backends."""

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self._data: dict[str, Any] = dict(initial or {})

    def contains(self, key: str) -> bool:
        return key in self._data

    def value(self, key: str, default: Any = None, type: Any = None) -> Any:
        if key not in self._data:
            return default
        raw = self._data[key]
        if type is None or raw is None:
            return raw
        try:
            return type(raw)
        except (TypeError, ValueError):
            return default

    def setValue(self, key: str, value: Any) -> None:
        self._data[key] = value

    def remove(self, key: str) -> None:
        self._data.pop(key, None)

    def sync(self) -> None:
        return None

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)


class JsonFileSettingsStore:
    """Persistent JSON settings for headless / FastAPI (no Qt)."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).expanduser()
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return
        if isinstance(raw, dict):
            self._data = {str(k): v for k, v in raw.items()}

    def contains(self, key: str) -> bool:
        return key in self._data

    def value(self, key: str, default: Any = None, type: Any = None) -> Any:
        if key not in self._data:
            return default
        raw = self._data[key]
        if type is None or raw is None:
            return raw
        try:
            return type(raw)
        except (TypeError, ValueError):
            return default

    def setValue(self, key: str, value: Any) -> None:
        self._data[key] = value

    def remove(self, key: str) -> None:
        self._data.pop(key, None)

    def sync(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except OSError:
            pass

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)


class QSettingsStore:
    """Adapter over PyQt6 QSettings (desktop UI)."""

    def __init__(
        self,
        settings: Any | None = None,
        *,
        organization: str = "Zaliver",
        application: str = "Zaliver",
    ) -> None:
        if settings is None:
            from PyQt6.QtCore import QSettings

            settings = QSettings(organization, application)
        self._inner = settings

    @property
    def inner(self) -> Any:
        return self._inner

    def contains(self, key: str) -> bool:
        return bool(self._inner.contains(key))

    def value(self, key: str, default: Any = None, type: Any = None) -> Any:
        if type is not None:
            return self._inner.value(key, default, type=type)
        return self._inner.value(key, default)

    def setValue(self, key: str, value: Any) -> None:
        self._inner.setValue(key, value)

    def remove(self, key: str) -> None:
        self._inner.remove(key)

    def sync(self) -> None:
        self._inner.sync()


def ensure_settings_store(settings: Any | None = None) -> SettingsStore:
    """Accept SettingsStore, QSettings, PlatformSettings, or None → SettingsStore."""
    if settings is None:
        return QSettingsStore()
    # PlatformSettings / nested wrappers expose .store
    store_attr = getattr(settings, "store", None)
    if store_attr is not None and store_attr is not settings:
        if isinstance(store_attr, SettingsStore) or all(
            callable(getattr(store_attr, name, None))
            for name in ("contains", "value", "setValue", "sync")
        ):
            # Prefer the scoped PlatformSettings itself when callers pass it:
            # shared keys still resolve correctly via PlatformSettings.value.
            if type(settings).__name__ == "PlatformSettings":
                return settings  # type: ignore[return-value]
    if isinstance(settings, SettingsStore):
        return settings
    # Duck-typed QSettings
    if all(
        callable(getattr(settings, name, None))
        for name in ("contains", "value", "setValue", "sync")
    ):
        if type(settings).__name__ == "QSettings" or hasattr(
            settings, "organizationName"
        ):
            return QSettingsStore(settings)
        return settings  # type: ignore[return-value]
    return QSettingsStore(settings)
