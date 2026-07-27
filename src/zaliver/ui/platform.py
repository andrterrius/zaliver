"""Платформа приложения (YouTube / Instagram) и подмена брендинга в UI."""

from __future__ import annotations

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

from zaliver.config.platform_settings import (
    PLATFORM_CHOICES,
    PLATFORM_INSTAGRAM,
    PLATFORM_YOUTUBE,
    PlatformSettings,
    normalize_platform,
    platform_display_name,
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


__all__ = [
    "PLATFORM_CHOICES",
    "PLATFORM_INSTAGRAM",
    "PLATFORM_YOUTUBE",
    "PlatformSettings",
    "apply_platform_branding",
    "brand_text",
    "normalize_platform",
    "platform_display_name",
]
