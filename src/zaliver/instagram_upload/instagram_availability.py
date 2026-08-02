"""Проверка доступности Instagram: главная + уже выполнен вход."""

from __future__ import annotations

import time

from zaliver.instagram_upload.logutil import emit_instagram_log, instagram_entrypoint
from zaliver.instagram_upload.register import (
    INSTAGRAM_URL,
    accept_instagram_cookie_consent_if_present,
    dismiss_instagram_scraping_warning_if_present,
    ensure_instagram_session_relogin,
    _extract_logged_in_username,
    _instagram_already_logged_in,
    _instagram_logged_in_nav_visible,
    _instagram_login_form_visible,
    _is_accounts_suspended,
    _is_classic_login_form_visible,
    _is_instagram_url,
    _is_mobile_logged_out_landing,
    _is_saved_profile_chooser_screen,
    _navigate_page_to,
    _onetap_password_visible,
)

_IG_READY_MAX_S = 90.0
# Антидетект отдаёт CDP раньше, чем вкладка уходит с about:blank.
_BLANK_SETTLE_S = 20.0


class InstagramAccountSuspendedError(RuntimeError):
    """Редирект на https://www.instagram.com/accounts/suspended/."""


def _log(message: str) -> None:
    emit_instagram_log(message, tag="[instagram]")


def _page_url(page) -> str:
    try:
        return (page.url or "").strip()
    except Exception:
        return ""


def _is_instagram_home_feed_url(url: str) -> bool:
    """
    Главная лента Instagram (/), а не /reel/, /p/, профиль и т.п.
    Нужно при keep_browser_open: после залива часто остаёмся на странице Reel.
    """
    u = (url or "").strip()
    if not _is_instagram_url(u):
        return False
    try:
        from urllib.parse import urlparse

        path = (urlparse(u).path or "/").strip() or "/"
        # Нормализуем: "" / "/" / лишние слэши → корень.
        while "//" in path:
            path = path.replace("//", "/")
        path = path.rstrip("/") or "/"
        return path == "/"
    except Exception:
        low = u.lower().split("#", 1)[0].split("?", 1)[0].rstrip("/")
        return low in (
            "https://www.instagram.com",
            "http://www.instagram.com",
            "https://instagram.com",
            "http://instagram.com",
        )


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
    from zaliver.core.profiles.account_data import INST_LOGIN_KEY

    return str(custom_data.get(INST_LOGIN_KEY) or "").strip()


def session_password_from_custom_data(custom_data: dict[str, object] | None) -> str:
    """inst_password, иначе gmail_password (для re-login вне регистрации)."""
    if not isinstance(custom_data, dict):
        return ""
    from zaliver.core.profiles.account_data import (
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
    from zaliver.core.profiles.account_data import INST_2FA_KEY

    return str(custom_data.get(INST_2FA_KEY) or "").strip().replace(" ", "")


def _needs_session_relogin(page) -> bool:
    return (
        _is_saved_profile_chooser_screen(page)
        or _onetap_password_visible(page)
        or _is_classic_login_form_visible(page)
        or _is_mobile_logged_out_landing(page)
    )


@instagram_entrypoint
def verify_instagram_home_available(
    page,
    *,
    max_seconds: float = _IG_READY_MAX_S,
    session_login: str = "",
    session_password: str = "",
    session_twofa: str = "",
    profile_id: str | None = None,
    login_credentials=None,
) -> str:
    """
    Открыть главную Instagram и убедиться, что сессия уже залогинена.
    При экране сохранённого профиля / форме логина — re-login (не регистрация).
    Возвращает username (может быть пустым, если ник не удалось извлечь).
    """
    url0 = _page_url(page)
    low0 = url0.lower()

    if _is_instagram_home_feed_url(url0):
        _log(f"Instagram: уже на главной (URL={url0!r}) — без повторной навигации.")
    elif _is_instagram_url(url0):
        # После залива с keep_browser_open часто /reel/... — сайдбар «Новая публикация»
        # надёжнее с главной ленты.
        _log(
            f"Instagram: на сайте, но не главная (URL={url0!r}) — "
            "переходим на главную…"
        )
        _navigate_page_to(page, INSTAGRAM_URL)
    else:
        if low0 in ("about:blank", "about:srcdoc", ""):
            _log(
                "Instagram: вкладка ещё about:blank — ждём старт браузера "
                f"(до {_BLANK_SETTLE_S:.0f} с)…"
            )
            url0 = _wait_leave_about_blank(page)
            low0 = url0.lower()

        if _is_instagram_home_feed_url(url0):
            _log(f"Instagram: уже на главной (URL={url0!r}).")
        elif _is_instagram_url(url0):
            _log(
                f"Instagram: на сайте, но не главная (URL={url0!r}) — "
                "переходим на главную…"
            )
            _navigate_page_to(page, INSTAGRAM_URL)
        else:
            # Обычный page.goto(domcontentloaded) на about:blank в CDP часто
            # зависает на десятки секунд — используем тот же обход, что и регистрация.
            _log(
                f"Instagram: открываем главную через надёжную навигацию "
                f"(текущий URL={url0!r})"
            )
            _navigate_page_to(page, INSTAGRAM_URL)

    # Через 1.5 с переоткрываем домен Instagram (mobile splash / cold start).
    _log("Instagram: ждём 1.5 с и переоткрываем главную…")
    try:
        page.wait_for_timeout(1500)
    except Exception:
        time.sleep(1.5)
    _navigate_page_to(page, INSTAGRAM_URL)

    _raise_if_accounts_suspended(page)
    accept_instagram_cookie_consent_if_present(page, appear_seconds=2.0)
    dismiss_instagram_scraping_warning_if_present(page)
    _raise_if_accounts_suspended(page)

    deadline = time.monotonic() + max(5.0, float(max_seconds))
    last_url = ""
    relogin_tried = False
    nav_wait_logged = False
    while time.monotonic() < deadline:
        last_url = _page_url(page)
        _raise_if_accounts_suspended(page)
        if dismiss_instagram_scraping_warning_if_present(page):
            page.wait_for_timeout(400)
            continue
        if not relogin_tried and _needs_session_relogin(page):
            relogin_tried = True
            uname = ensure_instagram_session_relogin(
                page,
                login=session_login,
                password=session_password,
                twofa_secret=session_twofa,
                max_seconds=min(90.0, max(20.0, deadline - time.monotonic())),
                login_credentials=login_credentials,
            )
            if uname:
                _raise_if_accounts_suspended(page)
                dismiss_instagram_scraping_warning_if_present(page)
                # После re-login UI может ещё не успеть отрисоваться.
                nav_deadline = min(deadline, time.monotonic() + 20.0)
                while time.monotonic() < nav_deadline:
                    if dismiss_instagram_scraping_warning_if_present(page):
                        page.wait_for_timeout(400)
                        continue
                    if _instagram_logged_in_nav_visible(page):
                        break
                    page.wait_for_timeout(400)
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
            # sessionid часто есть раньше, чем отрисуется сайдбар
            # («Новая публикация» / Home / Profile).
            if not _instagram_logged_in_nav_visible(page):
                if not nav_wait_logged:
                    nav_wait_logged = True
                    _log(
                        "Instagram: сессия есть, ждём UI сайдбара… "
                        f"URL={last_url!r}"
                    )
                page.wait_for_timeout(500)
                continue
            username = _extract_logged_in_username(page)
            _log(
                "Instagram: вход в аккаунт подтверждён"
                + (f" (@{username})" if username else "")
                + f", URL={last_url!r}."
            )
            return username
        # Cookie / промежуточный редирект — ещё раз принять cookies.
        accept_instagram_cookie_consent_if_present(page, appear_seconds=1.5)
        dismiss_instagram_scraping_warning_if_present(page)
        page.wait_for_timeout(400)

    raise RuntimeError(
        "Instagram: не дождались залогиненной главной "
        f"(URL={last_url or _page_url(page)!r})."
    )
