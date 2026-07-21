"""Проверка доступности Instagram: главная + уже выполнен вход."""

from __future__ import annotations

import time

from zaliver.instagram_upload.register import (
    INSTAGRAM_URL,
    accept_instagram_cookie_consent_if_present,
    ensure_instagram_session_relogin,
    _extract_logged_in_username,
    _instagram_already_logged_in,
    _instagram_login_form_visible,
    _is_accounts_suspended,
    _is_classic_login_form_visible,
    _is_instagram_url,
    _is_saved_profile_chooser_screen,
    _navigate_page_to,
    _onetap_password_visible,
)
from zaliver.youtube_upload import studio as _studio

_IG_READY_MAX_S = 90.0
# Антидетект отдаёт CDP раньше, чем вкладка уходит с about:blank.
_BLANK_SETTLE_S = 20.0


class InstagramAccountSuspendedError(RuntimeError):
    """Редирект на https://www.instagram.com/accounts/suspended/."""


def _log(message: str) -> None:
    _studio._log(f"[instagram] {message}")


def _page_url(page) -> str:
    try:
        return (page.url or "").strip()
    except Exception:
        return ""


def _wait_leave_about_blank(page, *, max_seconds: float = _BLANK_SETTLE_S) -> str:
    """
    После launch CDP часто уже есть, а вкладка ещё about:blank.
    Ждём ухода с blank, не блокируя 90 с на goto.
    """
    deadline = time.monotonic() + max(0.0, float(max_seconds))
    while time.monotonic() < deadline:
        cur = _page_url(page)
        low = cur.lower()
        if cur and low not in ("about:blank", "about:srcdoc", ""):
            _log(f"Instagram: вкладка ушла с about:blank → {cur!r}")
            return cur
        try:
            page.wait_for_timeout(250)
        except Exception:
            time.sleep(0.25)
    return _page_url(page)


def _raise_if_accounts_suspended(page) -> None:
    """/accounts/suspended → стоп (закрытие профиля + тег ошибки проверки)."""
    if not _is_accounts_suspended(page):
        return
    url = _page_url(page)
    _log(f"Instagram: аккаунт на /accounts/suspended — стоп (URL={url!r}).")
    raise InstagramAccountSuspendedError(
        "Instagram: аккаунт на /accounts/suspended "
        f"(URL={url!r})."
    )


def session_login_from_custom_data(custom_data: dict[str, object] | None) -> str:
    """inst_login для классической формы входа (вне регистрации)."""
    if not isinstance(custom_data, dict):
        return ""
    from zaliver.ui.profile_account_data_dialog import INST_LOGIN_KEY

    return str(custom_data.get(INST_LOGIN_KEY) or "").strip()


def session_password_from_custom_data(custom_data: dict[str, object] | None) -> str:
    """inst_password, иначе gmail_password (для re-login вне регистрации)."""
    if not isinstance(custom_data, dict):
        return ""
    from zaliver.ui.profile_account_data_dialog import (
        GMAIL_PASSWORD_KEY,
        INST_PASSWORD_KEY,
    )

    inst = str(custom_data.get(INST_PASSWORD_KEY) or "").strip()
    if inst:
        return inst
    return str(custom_data.get(GMAIL_PASSWORD_KEY) or "").strip()


def session_twofa_from_custom_data(custom_data: dict[str, object] | None) -> str:
    """inst_2fa для экрана authenticator при re-login."""
    if not isinstance(custom_data, dict):
        return ""
    from zaliver.ui.profile_account_data_dialog import INST_2FA_KEY

    return str(custom_data.get(INST_2FA_KEY) or "").strip().replace(" ", "")


def _needs_session_relogin(page) -> bool:
    return (
        _is_saved_profile_chooser_screen(page)
        or _onetap_password_visible(page)
        or _is_classic_login_form_visible(page)
    )


def verify_instagram_home_available(
    page,
    *,
    max_seconds: float = _IG_READY_MAX_S,
    session_login: str = "",
    session_password: str = "",
    session_twofa: str = "",
) -> str:
    """
    Открыть главную Instagram и убедиться, что сессия уже залогинена.
    При экране сохранённого профиля / форме логина — re-login (не регистрация).
    Возвращает username (может быть пустым, если ник не удалось извлечь).
    """
    url0 = _page_url(page)
    low0 = url0.lower()

    if _is_instagram_url(url0):
        _log(f"Instagram: уже на сайте (URL={url0!r}) — без повторной навигации.")
    else:
        if low0 in ("about:blank", "about:srcdoc", ""):
            _log(
                "Instagram: вкладка ещё about:blank — ждём старт браузера "
                f"(до {_BLANK_SETTLE_S:.0f} с)…"
            )
            url0 = _wait_leave_about_blank(page)
            low0 = url0.lower()

        if _is_instagram_url(url0):
            _log(f"Instagram: уже на Instagram (URL={url0!r}).")
        else:
            # Обычный page.goto(domcontentloaded) на about:blank в CDP часто
            # зависает на десятки секунд — используем тот же обход, что и регистрация.
            _log(
                f"Instagram: открываем главную через надёжную навигацию "
                f"(текущий URL={url0!r})"
            )
            _navigate_page_to(page, INSTAGRAM_URL)

    _raise_if_accounts_suspended(page)
    accept_instagram_cookie_consent_if_present(page, appear_seconds=8.0)
    _raise_if_accounts_suspended(page)

    deadline = time.monotonic() + max(5.0, float(max_seconds))
    last_url = ""
    relogin_tried = False
    while time.monotonic() < deadline:
        last_url = _page_url(page)
        _raise_if_accounts_suspended(page)
        if not relogin_tried and _needs_session_relogin(page):
            relogin_tried = True
            uname = ensure_instagram_session_relogin(
                page,
                login=session_login,
                password=session_password,
                twofa_secret=session_twofa,
                max_seconds=min(90.0, max(20.0, deadline - time.monotonic())),
            )
            if uname:
                _raise_if_accounts_suspended(page)
                _log(
                    "Instagram: вход после re-login подтверждён"
                    + (f" (@{uname})" if uname not in ("", "saved_profile") else "")
                    + f", URL={_page_url(page)!r}."
                )
                return uname if uname != "saved_profile" else (
                    _extract_logged_in_username(page) or ""
                )
            continue

        if _instagram_login_form_visible(page) and not _needs_session_relogin(page):
            raise RuntimeError(
                "Instagram: не выполнен вход в аккаунт "
                f"(экран логина, URL={last_url!r})."
            )
        if _instagram_already_logged_in(page):
            username = _extract_logged_in_username(page)
            _log(
                "Instagram: вход в аккаунт подтверждён"
                + (f" (@{username})" if username else "")
                + f", URL={last_url!r}."
            )
            return username
        # Cookie / промежуточный редирект — ещё раз принять cookies.
        accept_instagram_cookie_consent_if_present(page, appear_seconds=1.5)
        page.wait_for_timeout(400)

    raise RuntimeError(
        "Instagram: не дождались залогиненной главной "
        f"(URL={last_url or _page_url(page)!r})."
    )
