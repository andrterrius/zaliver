"""Ключи custom_data учёток и payload для merge — без Qt (безопасно для API)."""

from __future__ import annotations

SECTION_YOUTUBE = "youtube"
SECTION_INSTAGRAM = "instagram"
SECTION_GMAIL = "gmail"

YT_LOGIN_KEY = "yt_login"
YT_PASSWORD_KEY = "yt_password"
YT_2FA_KEY = "yt_2fa"
YT_OLDEST_NAME_KEY = "yt_oldest_name"

INST_LOGIN_KEY = "inst_login"
INST_PASSWORD_KEY = "inst_password"
INST_2FA_KEY = "inst_2fa"

GMAIL_LOGIN_KEY = "gmail_login"
GMAIL_PASSWORD_KEY = "gmail_password"
GMAIL_2FA_KEY = "gmail_2fa"


def build_account_credentials_payload(
    *,
    login: str,
    password: str,
    twofa: str,
    clear_oldest_channel: bool = True,
) -> dict[str, str]:
    """Payload для merge custom_data YouTube; при импорте сбрасывает yt_oldest_name."""
    payload = {
        YT_LOGIN_KEY: (login or "").strip(),
        YT_PASSWORD_KEY: password or "",
        YT_2FA_KEY: (twofa or "").strip(),
    }
    if clear_oldest_channel:
        payload[YT_OLDEST_NAME_KEY] = ""
    return payload


def build_instagram_credentials_payload(
    *,
    login: str,
    password: str,
    twofa: str = "",
) -> dict[str, str]:
    """Только Instagram-поля — merge не затирает yt_* / gmail_*."""
    return {
        INST_LOGIN_KEY: (login or "").strip(),
        INST_PASSWORD_KEY: password or "",
        INST_2FA_KEY: (twofa or "").strip(),
    }


def build_gmail_credentials_payload(
    *,
    login: str,
    password: str,
    twofa: str = "",
) -> dict[str, str]:
    """Все Gmail-поля целиком — merge не затирает yt_* / inst_*."""
    return {
        GMAIL_LOGIN_KEY: (login or "").strip(),
        GMAIL_PASSWORD_KEY: password or "",
        GMAIL_2FA_KEY: (twofa or "").strip(),
    }
