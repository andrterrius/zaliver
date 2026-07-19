"""Чтение кода подтверждения Instagram из Gmail."""

from __future__ import annotations

import re
import time

from zaliver.instagram_upload.gmail_availability import (
    GMAIL_INBOX_URL,
    dismiss_gmail_smart_features_if_present,
    gmail_inbox_ready,
)
from zaliver.youtube_upload import studio as _studio

_CODE_RE = re.compile(r"\b(\d{6})\b")
_SUBJECT_CODE_RE = re.compile(
    r"(\d{6})\s+is\s+your\s+instagram\s+code|"
    r"код\s+подтверждения.*?(\d{6})|"
    r"confirmation\s+code.*?(\d{6})|"
    r"instagram\s+code.*?(\d{6})",
    re.IGNORECASE | re.DOTALL,
)
_IG_SENDER_RE = re.compile(r"instagram|no-reply@mail\.instagram\.com", re.IGNORECASE)
_IG_SUBJECT_HINT_RE = re.compile(
    r"instagram\s+code|код.*instagram|confirmation\s+code",
    re.IGNORECASE,
)

_EMAIL_WAIT_MAX_S = 120.0
_EMAIL_POLL_S = 4.0
# Письмо видно в списке, но не открывается / код не читается — reload.
_OPEN_EMAIL_MAX_S = 10.0


def _log(message: str) -> None:
    _studio._log(f"[gmail] {message}")


def _bring_to_front(page) -> None:
    try:
        page.bring_to_front()
    except Exception:
        pass


def _goto_inbox(page) -> None:
    try:
        page.goto(GMAIL_INBOX_URL, wait_until="domcontentloaded", timeout=90_000)
    except Exception as e:
        _log(f"Gmail: goto inbox: {e!r}")
        try:
            page.reload(wait_until="domcontentloaded", timeout=60_000)
        except Exception as e2:
            _log(f"Gmail: reload: {e2!r}")
    page.wait_for_timeout(1500)
    dismiss_gmail_smart_features_if_present(page)


def _reload_inbox(page) -> None:
    """Обновить Inbox (reload или goto)."""
    _log("Gmail: обновляем Inbox…")
    try:
        page.reload(wait_until="domcontentloaded", timeout=60_000)
    except Exception:
        _goto_inbox(page)
    page.wait_for_timeout(1200)
    dismiss_gmail_smart_features_if_present(page)
    _click_primary_tab(page)


def _click_primary_tab(page) -> None:
    """На вкладке Primary / Входящие / Основной, если есть."""
    patterns = (
        re.compile(r"^primary$", re.I),
        re.compile(r"^входящие$", re.I),
        re.compile(r"^основной$", re.I),
        re.compile(r"^основные$", re.I),
    )
    for pat in patterns:
        try:
            tab = page.get_by_role("tab", name=pat).first
            if tab.count() > 0 and tab.is_visible(timeout=400):
                tab.click(timeout=5000)
                page.wait_for_timeout(800)
                return
        except Exception:
            continue


def _find_instagram_row(page):
    """Строка письма от Instagram с кодом (в списке Inbox)."""
    # Prefer unread Instagram rows with code in subject.
    candidates = [
        page.locator('tr.zA').filter(has_text=_IG_SENDER_RE).filter(
            has_text=_CODE_RE
        ),
        page.locator('tr.zA').filter(has_text=_IG_SENDER_RE).filter(
            has_text=_IG_SUBJECT_HINT_RE
        ),
        page.locator('span.zF[email="no-reply@mail.instagram.com"]').locator(
            "xpath=ancestor::tr[contains(@class,'zA')][1]"
        ),
        page.locator('span.zF[name="Instagram"]').locator(
            "xpath=ancestor::tr[contains(@class,'zA')][1]"
        ),
        page.locator("tr.zA").filter(has_text=re.compile(r"instagram", re.I)).filter(
            has_text=_CODE_RE
        ),
    ]
    for loc in candidates:
        try:
            if loc.count() <= 0:
                continue
            row = loc.first
            if row.is_visible(timeout=800):
                return row
        except Exception:
            continue
    return None


def _extract_code_from_text(text: str) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    m = _SUBJECT_CODE_RE.search(raw)
    if m:
        for g in m.groups():
            if g and g.isdigit() and len(g) == 6:
                return g
    # Явные фразы рядом с кодом.
    near = re.search(
        r"(?:confirmation\s+code|код\s+подтверждения|instagram\s+code|"
        r"enter\s+this\s+confirmation\s+code)[^\d]{0,40}(\d{6})",
        raw,
        re.IGNORECASE,
    )
    if near:
        return near.group(1)
    # Крупный одиночный код в письме Instagram (часто на отдельной строке).
    alone = re.findall(r"(?:^|\n)\s*(\d{6})\s*(?:$|\n)", raw)
    if len(alone) == 1:
        return alone[0]
    codes = _CODE_RE.findall(raw)
    if codes:
        # Первый 6-значный после упоминания Instagram / code.
        lower = raw.lower()
        for c in codes:
            idx = lower.find(c)
            window = lower[max(0, idx - 80) : idx + 20]
            if "instagram" in window or "code" in window or "код" in window:
                return c
        return codes[0]
    return None


def _extract_code_from_open_message(page) -> str | None:
    # Тема письма.
    try:
        subject = page.locator("h2.hP").first
        if subject.count() > 0 and subject.is_visible(timeout=500):
            code = _extract_code_from_text(subject.inner_text(timeout=2000) or "")
            if code:
                return code
    except Exception:
        pass
    # Тело письма.
    for sel in (
        "div.a3s.aiL",
        "div.ii.gt",
        "div[data-message-id]",
        "div.adn.ads",
    ):
        try:
            body = page.locator(sel).first
            if body.count() <= 0:
                continue
            text = body.inner_text(timeout=3000) or ""
            code = _extract_code_from_text(text)
            if code:
                return code
        except Exception:
            continue
    # Весь текст страницы как запасной вариант.
    try:
        return _extract_code_from_text(page.inner_text("body", timeout=3000) or "")
    except Exception:
        return None


def _message_pane_open(page) -> bool:
    """Открыто ли тело/тема письма (не только список)."""
    for sel in ("h2.hP", "div.a3s.aiL", "div.ii.gt"):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=300):
                return True
        except Exception:
            continue
    return False


def _click_instagram_row(page, row) -> bool:
    """Клик по строке письма. True если клик ушёл."""
    for click_sel in (
        "td.a4W",
        "span.bqe",
        "div.y6",
        "span.bog",
        "td.yX",
    ):
        try:
            cell = row.locator(click_sel).first
            if cell.count() > 0 and cell.is_visible(timeout=300):
                cell.click(timeout=5000)
                return True
        except Exception:
            continue
    try:
        row.click(timeout=5000)
        return True
    except Exception:
        return False


def _open_instagram_row_and_read_code(page, row) -> str | None:
    """
    Открыть письмо Instagram и вытащить код.
    Если за ``_OPEN_EMAIL_MAX_S`` не открылось / код не читается — None
    (вызывающий делает reload и повтор).
    """
    open_deadline = time.monotonic() + _OPEN_EMAIL_MAX_S

    # Иногда код уже виден в превью строки.
    try:
        preview = row.inner_text(timeout=1500) or ""
        preview_code = _extract_code_from_text(preview)
    except Exception:
        preview_code = None

    _log("Gmail: открываем письмо Instagram…")
    if not _click_instagram_row(page, row):
        _log(
            f"Gmail: клик по письму не удался за {_OPEN_EMAIL_MAX_S:.0f} с — "
            "обновим страницу."
        )
        return None

    while time.monotonic() < open_deadline:
        if _message_pane_open(page):
            code = _extract_code_from_open_message(page) or preview_code
            if code:
                return code
            # Панель есть, но код ещё не подгрузился — коротко подождём.
            page.wait_for_timeout(400)
            continue
        # Ещё не открылось — повторный клик, если строка всё ещё есть.
        try:
            again = _find_instagram_row(page)
            if again is not None:
                _click_instagram_row(page, again)
        except Exception:
            pass
        page.wait_for_timeout(400)

    _log(
        f"Gmail: письмо есть, но не открылось / код не прочитан "
        f"за {_OPEN_EMAIL_MAX_S:.0f} с — обновляем страницу."
    )
    return None


def fetch_instagram_confirmation_code_from_gmail(
    gmail_page,
    *,
    max_seconds: float = _EMAIL_WAIT_MAX_S,
) -> str:
    """
    Переключиться на Gmail, обновить Inbox, открыть письмо Instagram,
    вернуть 6-значный код.

    Если письмо уже в списке, но не открывается дольше 10 с —
    reload Inbox и повторная попытка.
    """
    _bring_to_front(gmail_page)
    deadline = time.monotonic() + max(30.0, float(max_seconds))
    last_err: str | None = None

    while time.monotonic() < deadline:
        try:
            dismiss_gmail_smart_features_if_present(gmail_page)
            if not gmail_inbox_ready(gmail_page):
                _goto_inbox(gmail_page)
                gmail_page.wait_for_timeout(1200)
                dismiss_gmail_smart_features_if_present(gmail_page)
                _click_primary_tab(gmail_page)
            else:
                _reload_inbox(gmail_page)

            row = _find_instagram_row(gmail_page)
            if row is None:
                # Иногда письмо в Social.
                for social_pat in (
                    re.compile(r"^social$", re.I),
                    re.compile(r"^социальные", re.I),
                ):
                    try:
                        tab = gmail_page.get_by_role("tab", name=social_pat).first
                        if tab.count() > 0 and tab.is_visible(timeout=300):
                            tab.click(timeout=4000)
                            gmail_page.wait_for_timeout(800)
                            row = _find_instagram_row(gmail_page)
                            if row is not None:
                                break
                    except Exception:
                        continue
            if row is None:
                last_err = "письмо Instagram ещё не пришло"
                _log(f"Gmail: {last_err}, ждём…")
                gmail_page.wait_for_timeout(int(_EMAIL_POLL_S * 1000))
                continue

            code = _open_instagram_row_and_read_code(gmail_page, row)
            if code:
                _log(f"Gmail: код подтверждения Instagram = {code}")
                return code

            last_err = (
                f"письмо не открылось за {_OPEN_EMAIL_MAX_S:.0f} с "
                "(или код не найден) — reload"
            )
            _log(f"Gmail: {last_err}")
            # Сразу reload и следующая попытка (без лишней паузы poll).
            _reload_inbox(gmail_page)
            continue
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            _log(f"Gmail: ошибка чтения кода: {last_err}")
        gmail_page.wait_for_timeout(int(_EMAIL_POLL_S * 1000))

    raise RuntimeError(
        f"Gmail: не удалось получить код Instagram за {max_seconds:.0f} с"
        + (f" ({last_err})" if last_err else "")
    )
