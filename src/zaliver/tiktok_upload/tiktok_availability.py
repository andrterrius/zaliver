"""Проверка доступности TikTok: главная + уже выполнен вход."""

from __future__ import annotations

import time

from zaliver.tiktok_upload.logutil import emit_tiktok_log, tiktok_entrypoint
from zaliver.tiktok_upload.register import (
    TIKTOK_URL,
    accept_tiktok_cookie_consent_if_present,
    dismiss_tiktok_scraping_warning_if_present,
    ensure_tiktok_session_relogin,
    _extract_logged_in_username,
    _tiktok_already_logged_in,
    _tiktok_logged_in_nav_visible,
    _tiktok_login_form_visible,
    _tiktok_sidebar_login_visible,
    _is_accounts_suspended,
    _is_classic_login_form_visible,
    _is_tiktok_url,
    _is_mobile_logged_out_landing,
    _is_saved_profile_chooser_screen,
    _navigate_page_to,
    _onetap_password_visible,
    _is_chrome_net_error_url,
)

_IG_READY_MAX_S = 90.0
# Антидетект отдаёт CDP раньше, чем вкладка уходит с about:blank.
_BLANK_SETTLE_S = 20.0


class TikTokAccountSuspendedError(RuntimeError):
    """Редирект на https://www.tiktok.com/accounts/suspended/."""


def _log(message: str) -> None:
    emit_tiktok_log(message, tag="[tiktok]")


def _page_url(page) -> str:
    try:
        return (page.url or "").strip()
    except Exception:
        return ""


def _is_tiktok_home_feed_url(url: str) -> bool:
    """
    Главная лента TikTok (/), а не /reel/, /p/, профиль и т.п.
    Нужно при keep_browser_open: после залива часто остаёмся на странице Reel.
    """
    u = (url or "").strip()
    if not _is_tiktok_url(u):
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
            "https://www.tiktok.com",
            "https://www.tiktok.com",
            "https://www.tiktok.com",
            "https://www.tiktok.com",
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
            _log(f"TikTok: вкладка ушла с about:blank → {cur!r}")
            return cur
        try:
            page.wait_for_timeout(250)
        except Exception:
            time.sleep(0.25)
    return _page_url(page)


def _wait_tiktok_network_ready(page, *, max_seconds: float = 45.0) -> None:
    """chrome-error / ERR_PROXY: локальный прокси антидетекта поднимается после CDP."""
    url0 = _page_url(page)
    if _is_tiktok_url(url0) and not _is_chrome_net_error_url(url0):
        return
    if not _is_chrome_net_error_url(url0) and url0:
        return
    _log(
        "TikTok: нет сети на старте "
        f"(URL={url0!r}) — ждём прокси антидетекта…"
    )
    deadline = time.monotonic() + max(8.0, float(max_seconds))
    last_err = ""
    while time.monotonic() < deadline:
        try:
            page.wait_for_timeout(2500)
        except Exception:
            time.sleep(2.5)
        try:
            _navigate_page_to(page, TIKTOK_URL)
        except Exception as e:
            last_err = str(e)
            _log(f"TikTok: повтор главной после ошибки сети: {e!r}")
            continue
        cur = _page_url(page)
        if _is_tiktok_url(cur) and not _is_chrome_net_error_url(cur):
            _log(f"TikTok: сеть появилась, URL={cur!r}")
            return
        last_err = cur
    raise RuntimeError(
        "TikTok: нет интернета в профиле (прокси не поднялся, "
        f"chrome-error). URL={_page_url(page)!r}"
        + (f" {last_err}" if last_err else "")
    )


def _raise_if_accounts_suspended(page) -> None:
    """/accounts/suspended → стоп (закрытие профиля + тег ошибки проверки)."""
    if not _is_accounts_suspended(page):
        return
    url = _page_url(page)
    _log(f"TikTok: аккаунт на /accounts/suspended — стоп (URL={url!r}).")
    raise TikTokAccountSuspendedError(
        "TikTok: аккаунт на /accounts/suspended "
        f"(URL={url!r})."
    )


def session_login_from_custom_data(custom_data: dict[str, object] | None) -> str:
    """tt_login для классической формы входа (вне регистрации)."""
    if not isinstance(custom_data, dict):
        return ""
    from zaliver.core.profiles.account_data import TT_LOGIN_KEY

    return str(custom_data.get(TT_LOGIN_KEY) or "").strip()


def session_password_from_custom_data(custom_data: dict[str, object] | None) -> str:
    """tt_password, иначе gmail_password (для re-login вне регистрации)."""
    if not isinstance(custom_data, dict):
        return ""
    from zaliver.core.profiles.account_data import (
        GMAIL_PASSWORD_KEY,
        TT_PASSWORD_KEY,
    )

    inst = str(custom_data.get(TT_PASSWORD_KEY) or "").strip()
    if inst:
        return inst
    return str(custom_data.get(GMAIL_PASSWORD_KEY) or "").strip()


def session_twofa_from_custom_data(custom_data: dict[str, object] | None) -> str:
    """tt_2fa для экрана authenticator при re-login."""
    if not isinstance(custom_data, dict):
        return ""
    from zaliver.core.profiles.account_data import TT_2FA_KEY

    return str(custom_data.get(TT_2FA_KEY) or "").strip().replace(" ", "")


def _needs_session_relogin(page) -> bool:
    return (
        _is_saved_profile_chooser_screen(page)
        or _onetap_password_visible(page)
        or _is_classic_login_form_visible(page)
        or _is_mobile_logged_out_landing(page)
        or _tiktok_sidebar_login_visible(page)
    )


@tiktok_entrypoint
def verify_tiktok_home_available(
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
    Открыть главную TikTok и убедиться, что сессия уже залогинена:
    слева сайдбар без кнопки Log in / Войти.
    При экране сохранённого профиля / форме логина — re-login (не регистрация).
    Возвращает username (может быть пустым, если ник не удалось извлечь).
    """
    url0 = _page_url(page)
    _wait_tiktok_network_ready(page)
    url0 = _page_url(page)
    low0 = url0.lower()

    if _is_tiktok_home_feed_url(url0):
        _log(f"TikTok: уже на главной (URL={url0!r}) — без повторной навигации.")
    elif _is_tiktok_url(url0):
        # После залива с keep_browser_open часто /video/... — сайдбар Log in
        # надёжнее проверять с главной ленты.
        _log(
            f"TikTok: на сайте, но не главная (URL={url0!r}) — "
            "переходим на главную…"
        )
        _navigate_page_to(page, TIKTOK_URL)
    else:
        if low0 in ("about:blank", "about:srcdoc", ""):
            _log(
                "TikTok: вкладка ещё about:blank — ждём старт браузера "
                f"(до {_BLANK_SETTLE_S:.0f} с)…"
            )
            url0 = _wait_leave_about_blank(page)
            low0 = url0.lower()

        if _is_tiktok_home_feed_url(url0):
            _log(f"TikTok: уже на главной (URL={url0!r}).")
        elif _is_tiktok_url(url0):
            _log(
                f"TikTok: на сайте, но не главная (URL={url0!r}) — "
                "переходим на главную…"
            )
            _navigate_page_to(page, TIKTOK_URL)
        else:
            # Обычный page.goto(domcontentloaded) на about:blank в CDP часто
            # зависает на десятки секунд — используем тот же обход, что и регистрация.
            _log(
                f"TikTok: открываем главную через надёжную навигацию "
                f"(текущий URL={url0!r})"
            )
            _navigate_page_to(page, TIKTOK_URL)

    # Через 1.5 с переоткрываем домен TikTok (mobile splash / cold start).
    _log("TikTok: ждём 1.5 с и переоткрываем главную…")
    try:
        page.wait_for_timeout(1500)
    except Exception:
        time.sleep(1.5)
    _navigate_page_to(page, TIKTOK_URL)

    _raise_if_accounts_suspended(page)
    accept_tiktok_cookie_consent_if_present(page, appear_seconds=2.0)
    dismiss_tiktok_scraping_warning_if_present(page)
    _raise_if_accounts_suspended(page)

    deadline = time.monotonic() + max(5.0, float(max_seconds))
    last_url = ""
    relogin_tried = False
    nav_wait_logged = False
    while time.monotonic() < deadline:
        last_url = _page_url(page)
        _raise_if_accounts_suspended(page)
        if dismiss_tiktok_scraping_warning_if_present(page):
            page.wait_for_timeout(400)
            continue
        if not relogin_tried and _needs_session_relogin(page):
            relogin_tried = True
            uname = ensure_tiktok_session_relogin(
                page,
                login=session_login,
                password=session_password,
                twofa_secret=session_twofa,
                max_seconds=min(90.0, max(20.0, deadline - time.monotonic())),
                login_credentials=login_credentials,
            )
            if uname:
                _raise_if_accounts_suspended(page)
                dismiss_tiktok_scraping_warning_if_present(page)
                # После re-login UI может ещё не успеть отрисоваться.
                nav_deadline = min(deadline, time.monotonic() + 20.0)
                while time.monotonic() < nav_deadline:
                    if dismiss_tiktok_scraping_warning_if_present(page):
                        page.wait_for_timeout(400)
                        continue
                    if _tiktok_logged_in_nav_visible(page):
                        break
                    page.wait_for_timeout(400)
                _log(
                    "TikTok: вход после re-login подтверждён"
                    + (f" (@{uname})" if uname not in ("", "saved_profile") else "")
                    + f", URL={_page_url(page)!r}."
                )
                return uname if uname != "saved_profile" else (
                    _extract_logged_in_username(page) or ""
                )
            continue

        if _tiktok_login_form_visible(page) and not _needs_session_relogin(page):
            raise RuntimeError(
                "TikTok: не выполнен вход в аккаунт "
                f"(экран логина, URL={last_url!r})."
            )
        if _tiktok_already_logged_in(page):
            # sessionid часто есть раньше, чем отрисуется левый сайдбар
            # (исчезнет Log in / Войти).
            if not _tiktok_logged_in_nav_visible(page):
                if not nav_wait_logged:
                    nav_wait_logged = True
                    _log(
                        "TikTok: сессия есть, ждём сайдбар без Log in… "
                        f"URL={last_url!r}"
                    )
                page.wait_for_timeout(500)
                continue
            username = _extract_logged_in_username(page)
            _log(
                "TikTok: вход в аккаунт подтверждён"
                + (f" (@{username})" if username else "")
                + f", URL={last_url!r}."
            )
            return username
        # Cookie / промежуточный редирект — ещё раз принять cookies.
        accept_tiktok_cookie_consent_if_present(page, appear_seconds=1.5)
        dismiss_tiktok_scraping_warning_if_present(page)
        page.wait_for_timeout(400)

    raise RuntimeError(
        "TikTok: не дождались залогиненной главной "
        f"(URL={last_url or _page_url(page)!r})."
    )
