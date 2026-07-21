"""Проверка доступности Instagram: главная + уже выполнен вход."""

from __future__ import annotations

import time

from zaliver.instagram_upload.register import (
    INSTAGRAM_URL,
    accept_instagram_cookie_consent_if_present,
    _extract_logged_in_username,
    _instagram_already_logged_in,
    _instagram_login_form_visible,
    _is_instagram_url,
    _navigate_page_to,
)
from zaliver.youtube_upload import studio as _studio

_IG_READY_MAX_S = 90.0
# Антидетект отдаёт CDP раньше, чем start_url уходит с about:blank.
_BLANK_SETTLE_S = 20.0


def _log(message: str) -> None:
    _studio._log(f"[instagram] {message}")


def _page_url(page) -> str:
    try:
        return (page.url or "").strip()
    except Exception:
        return ""


def _wait_leave_about_blank(page, *, max_seconds: float = _BLANK_SETTLE_S) -> str:
    """
    После launch CDP часто уже есть, а вкладка ещё about:blank, пока антидетект
    грузит start_url. Ждём ухода с blank, не блокируя 90 с на goto.
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


def verify_instagram_home_available(
    page,
    *,
    max_seconds: float = _IG_READY_MAX_S,
) -> str:
    """
    Открыть главную Instagram и убедиться, что сессия уже залогинена.
    Возвращает username (может быть пустым, если ник не удалось извлечь).
    """
    url0 = _page_url(page)
    low0 = url0.lower()

    if _is_instagram_url(url0):
        _log(f"Instagram: уже на сайте (URL={url0!r}) — без повторной навигации.")
    else:
        if low0 in ("about:blank", "about:srcdoc", ""):
            _log(
                "Instagram: вкладка ещё about:blank — ждём start_url антидетекта "
                f"(до {_BLANK_SETTLE_S:.0f} с)…"
            )
            url0 = _wait_leave_about_blank(page)
            low0 = url0.lower()

        if _is_instagram_url(url0):
            _log(f"Instagram: start_url уже открыл Instagram (URL={url0!r}).")
        else:
            # Обычный page.goto(domcontentloaded) на about:blank в CDP часто
            # зависает на десятки секунд — используем тот же обход, что и регистрация.
            _log(
                f"Instagram: открываем главную через надёжную навигацию "
                f"(текущий URL={url0!r})"
            )
            _navigate_page_to(page, INSTAGRAM_URL)

    accept_instagram_cookie_consent_if_present(page, appear_seconds=8.0)

    deadline = time.monotonic() + max(5.0, float(max_seconds))
    last_url = ""
    while time.monotonic() < deadline:
        last_url = _page_url(page)
        if _instagram_login_form_visible(page):
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
