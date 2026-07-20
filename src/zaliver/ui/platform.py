"""Платформа приложения (YouTube / Instagram) и подмена брендинга в UI."""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QGroupBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QWidget,
)

PLATFORM_YOUTUBE = "youtube"
PLATFORM_INSTAGRAM = "instagram"

PLATFORM_CHOICES: tuple[tuple[str, str, str], ...] = (
    (PLATFORM_YOUTUBE, "YouTube", "Залив видео на YouTube"),
    (PLATFORM_INSTAGRAM, "Instagram", "Залив видео на Instagram"),
)

# Общие для всех платформ (антидетект, ключ LLM).
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

# Длинные фразы первыми, чтобы не резать их короткими заменами.
_BRAND_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("YouTube Studio", "Instagram"),
    ("YouTube Data API v3", "Instagram API"),
    ("YouTube Data API", "Instagram API"),
    ("YoutubeDataApiError", "InstagramApiError"),
    ("YOUTUBE_API_KEY", "INSTAGRAM_API_KEY"),
    ("YouTube Shorts", "Instagram Reels"),
    ("studio.youtube.com", "instagram.com"),
    ("youtube.com", "instagram.com"),
    ("YouTube", "Instagram"),
    ("Youtube", "Instagram"),
    ("youtube", "instagram"),
)

# Фразы, где «YouTube» оставляем даже в режиме Instagram
# (логин/пароль из yt_* , кнопки подстановки и т.п.).
_BRAND_PRESERVE_PHRASES: tuple[str, ...] = (
    "подставить логин и пароль от YouTube",
    "подставить логин и пароль от Gmail",
    "данные YouTube не меняются",
    "из YouTube-данных",
    "YouTube-данных",
    "Gmail/YouTube-данных",
    "Нет YouTube-данных",
    "Подставлено из YouTube",
    "Подставлено из Gmail",
    "нет yt_login / yt_password",
    "yt_login / yt_password",
    "yt_login и yt_password",
    "gmail_login / gmail_password",
    "gmail_login / gmail_password / gmail_2fa",
    "yt_login / yt_password / yt_2fa",
)


def normalize_platform(value: str | None) -> str:
    v = (value or "").strip().lower()
    if v == PLATFORM_INSTAGRAM:
        return PLATFORM_INSTAGRAM
    return PLATFORM_YOUTUBE


def platform_display_name(platform: str) -> str:
    return "Instagram" if normalize_platform(platform) == PLATFORM_INSTAGRAM else "YouTube"


def brand_text(text: str, platform: str) -> str:
    """Подменить YouTube → Instagram в пользовательском тексте."""
    if normalize_platform(platform) != PLATFORM_INSTAGRAM or not text:
        return text
    out = text
    preserved: list[tuple[str, str]] = []
    for i, phrase in enumerate(_BRAND_PRESERVE_PHRASES):
        if phrase not in out:
            continue
        token = f"\x00ZALIVER_KEEP_{i}\x00"
        preserved.append((token, phrase))
        out = out.replace(phrase, token)
    for old, new in _BRAND_REPLACEMENTS:
        out = out.replace(old, new)
    for token, phrase in preserved:
        out = out.replace(token, phrase)
    return out


def apply_platform_branding(root: QWidget, platform: str) -> None:
    """Пройти по виджетам и заменить видимые строки YouTube → Instagram."""
    if normalize_platform(platform) != PLATFORM_INSTAGRAM:
        return

    def _set_text(getter, setter) -> None:
        try:
            raw = getter()
        except Exception:
            return
        if not isinstance(raw, str) or not raw:
            return
        branded = brand_text(raw, platform)
        if branded != raw:
            setter(branded)

    widgets = [root, *root.findChildren(QWidget)]
    for w in widgets:
        if isinstance(w, QLabel):
            _set_text(w.text, w.setText)
            _set_text(w.toolTip, w.setToolTip)
        elif isinstance(w, QAbstractButton):
            _set_text(w.text, w.setText)
            _set_text(w.toolTip, w.setToolTip)
        elif isinstance(w, QLineEdit):
            _set_text(w.text, w.setText)
            _set_text(w.placeholderText, w.setPlaceholderText)
            _set_text(w.toolTip, w.setToolTip)
        elif isinstance(w, QPlainTextEdit):
            _set_text(w.placeholderText, w.setPlaceholderText)
            _set_text(w.toolTip, w.setToolTip)
        elif isinstance(w, QGroupBox):
            _set_text(w.title, w.setTitle)
            _set_text(w.toolTip, w.setToolTip)
        elif isinstance(w, QComboBox):
            _set_text(w.toolTip, w.setToolTip)
            for i in range(w.count()):
                item = w.itemText(i)
                branded = brand_text(item, platform)
                if branded != item:
                    w.setItemText(i, branded)
        elif isinstance(w, QListWidget):
            _set_text(w.toolTip, w.setToolTip)
            for i in range(w.count()):
                item = w.item(i)
                if item is None:
                    continue
                branded = brand_text(item.text(), platform)
                if branded != item.text():
                    item.setText(branded)
                tip = item.toolTip()
                branded_tip = brand_text(tip, platform)
                if branded_tip != tip:
                    item.setToolTip(branded_tip)
        else:
            _set_text(w.toolTip, w.setToolTip)

    title = root.windowTitle()
    branded_title = brand_text(title, platform)
    if branded_title != title:
        root.setWindowTitle(branded_title)


class PlatformSettings:
    """QSettings с префиксом platforms/{id}/; антидетект и ключ ИИ — общие."""

    def __init__(self, settings: QSettings, platform: str) -> None:
        self._inner = settings
        self._platform = normalize_platform(platform)

    @property
    def platform(self) -> str:
        return self._platform

    @property
    def inner(self) -> QSettings:
        return self._inner

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
        if self._inner.contains(full):
            return True
        if self._is_shared(key):
            return False
        return (
            self._platform == PLATFORM_YOUTUBE and self._inner.contains(key)
        )

    def value(self, key: str, default: Any = None, type: Any = None) -> Any:
        full = self._full_key(key)
        if not self._inner.contains(full) and not self._is_shared(key):
            if self._platform == PLATFORM_YOUTUBE and self._inner.contains(key):
                legacy = (
                    self._inner.value(key, default, type=type)
                    if type is not None
                    else self._inner.value(key, default)
                )
                self._inner.setValue(full, legacy)
                return legacy
        if type is not None:
            return self._inner.value(full, default, type=type)
        return self._inner.value(full, default)

    def setValue(self, key: str, value: Any) -> None:
        self._inner.setValue(self._full_key(key), value)

    def remove(self, key: str) -> None:
        self._inner.remove(self._full_key(key))

    def sync(self) -> None:
        self._inner.sync()
