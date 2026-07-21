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
_META_CODE_RE = re.compile(r"\b(\d{8})\b")
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
_META_SENDER_RE = re.compile(
    r"noreply@account\.meta\.com|\bMeta\b",
    re.IGNORECASE,
)
_META_SUBJECT_HINT_RE = re.compile(
    r"Authenticate\s+your\s+profile|"
    r"подтвердите\s+(свой\s+)?профиль|"
    r"authenticate\s+your\s+account",
    re.IGNORECASE,
)
_META_CODE_NEAR_RE = re.compile(
    r"(?:confirm\s+your\s+identity|подтвердите\s+(?:свою\s+)?личность|"
    r"use\s+the\s+following\s+code|следующ(?:ий|им)\s+код)[^\d]{0,60}(\d{8})",
    re.IGNORECASE,
)

_EMAIL_WAIT_MAX_S = 120.0
_EMAIL_POLL_S = 4.0
# Письмо видно в списке, но не открывается / код не читается — снова #inbox/.
_OPEN_EMAIL_MAX_S = 10.0


def _log(message: str) -> None:
    _studio._log(f"[gmail] {message}")


def _bring_to_front(page) -> None:
    try:
        page.bring_to_front()
    except Exception:
        pass


def _goto_inbox(page) -> None:
    """Открыть https://mail.google.com/mail/u/0/#inbox/ (без page.reload)."""
    _log(f"Gmail: переходим на {GMAIL_INBOX_URL}")
    try:
        page.goto(GMAIL_INBOX_URL, wait_until="domcontentloaded", timeout=90_000)
    except Exception as e:
        _log(f"Gmail: goto inbox: {e!r}")
        try:
            page.evaluate("(u) => { location.assign(u); }", GMAIL_INBOX_URL)
            page.wait_for_load_state("domcontentloaded", timeout=60_000)
        except Exception as e2:
            _log(f"Gmail: location.assign inbox: {e2!r}")
    page.wait_for_timeout(1500)
    dismiss_gmail_smart_features_if_present(page)


def _reload_inbox(page) -> None:
    """Снова открыть Inbox через goto #inbox/ (не page.reload)."""
    _goto_inbox(page)
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


def _find_meta_auth_row(page):
    """Строка письма Meta «Authenticate your profile» (8-значный код)."""
    candidates = [
        page.locator("tr.zA")
        .filter(has_text=re.compile(r"noreply@account\.meta\.com", re.I))
        .filter(has_text=_META_SUBJECT_HINT_RE),
        page.locator('span[email="noreply@account.meta.com"]').locator(
            "xpath=ancestor::tr[contains(@class,'zA')][1]"
        ),
        page.locator("tr.zA")
        .filter(has_text=_META_SENDER_RE)
        .filter(has_text=_META_SUBJECT_HINT_RE),
        page.locator("tr.zA")
        .filter(has_text=_META_SENDER_RE)
        .filter(has_text=_META_CODE_RE),
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


def _extract_meta_code_from_text(text: str) -> str | None:
    """8-значный код из письма Meta (Authenticate your profile)."""
    raw = (text or "").strip()
    if not raw:
        return None
    near = _META_CODE_NEAR_RE.search(raw)
    if near:
        return near.group(1)
    alone = re.findall(r"(?:^|\n)\s*(\d{8})\s*(?:$|\n)", raw)
    if len(alone) == 1:
        return alone[0]
    codes = _META_CODE_RE.findall(raw)
    if not codes:
        return None
    lower = raw.lower()
    for c in codes:
        idx = lower.find(c)
        window = lower[max(0, idx - 100) : idx + 30]
        if any(
            k in window
            for k in (
                "confirm",
                "identity",
                "authenticate",
                "meta",
                "код",
                "личн",
            )
        ):
            return c
    return codes[0]


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


def _extract_meta_code_from_open_message(page) -> str | None:
    try:
        subject = page.locator("h2.hP").first
        if subject.count() > 0 and subject.is_visible(timeout=500):
            code = _extract_meta_code_from_text(subject.inner_text(timeout=2000) or "")
            if code:
                return code
    except Exception:
        pass
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
            code = _extract_meta_code_from_text(text)
            if code:
                return code
        except Exception:
            continue
    try:
        return _extract_meta_code_from_text(page.inner_text("body", timeout=3000) or "")
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
    (вызывающий снова открывает #inbox/ и повторяет).
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
            "откроем #inbox/ снова."
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
        f"за {_OPEN_EMAIL_MAX_S:.0f} с — откроем #inbox/ снова."
    )
    return None


def _open_meta_row_and_read_code(page, row) -> str | None:
    open_deadline = time.monotonic() + _OPEN_EMAIL_MAX_S
    try:
        preview = row.inner_text(timeout=1500) or ""
        preview_code = _extract_meta_code_from_text(preview)
    except Exception:
        preview_code = None

    _log("Gmail: открываем письмо Meta (Authenticate your profile)…")
    if not _click_instagram_row(page, row):
        _log(
            f"Gmail: клик по письму Meta не удался за {_OPEN_EMAIL_MAX_S:.0f} с — "
            "откроем #inbox/ снова."
        )
        return None

    while time.monotonic() < open_deadline:
        if _message_pane_open(page):
            code = _extract_meta_code_from_open_message(page) or preview_code
            if code:
                return code
            page.wait_for_timeout(400)
            continue
        try:
            again = _find_meta_auth_row(page)
            if again is not None:
                _click_instagram_row(page, again)
        except Exception:
            pass
        page.wait_for_timeout(400)

    _log(
        f"Gmail: письмо Meta есть, но не открылось / код не прочитан "
        f"за {_OPEN_EMAIL_MAX_S:.0f} с — откроем #inbox/ снова."
    )
    return None


def fetch_instagram_confirmation_code_from_gmail(
    gmail_page,
    *,
    max_seconds: float = _EMAIL_WAIT_MAX_S,
    exclude_codes: set[str] | frozenset[str] | None = None,
) -> str:
    """
    Переключиться на Gmail, открыть #inbox/, письмо Instagram,
    вернуть 6-значный код.

    Если письмо уже в списке, но не открывается дольше 10 с —
    снова goto #inbox/ и повторная попытка.

    ``exclude_codes`` — коды, которые уже пробовали на Instagram:
    такие письма пропускаем и ждём новое.
    """
    _bring_to_front(gmail_page)
    deadline = time.monotonic() + max(30.0, float(max_seconds))
    last_err: str | None = None
    exclude = {
        str(c).strip()
        for c in (exclude_codes or ())
        if c is not None and str(c).strip()
    }

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
                last_err = "письмо Instagram ещё не пришло"
                _log(f"Gmail: {last_err}, ждём…")
                gmail_page.wait_for_timeout(int(_EMAIL_POLL_S * 1000))
                continue

            code = _open_instagram_row_and_read_code(gmail_page, row)
            if code:
                if code in exclude:
                    last_err = (
                        f"код {code} уже пробовали — ждём новое письмо"
                    )
                    _log(f"Gmail: {last_err}")
                    _reload_inbox(gmail_page)
                    gmail_page.wait_for_timeout(int(_EMAIL_POLL_S * 1000))
                    continue
                _log(f"Gmail: код подтверждения Instagram = {code}")
                return code

            last_err = (
                f"письмо не открылось за {_OPEN_EMAIL_MAX_S:.0f} с "
                "(или код не найден) — снова #inbox/"
            )
            _log(f"Gmail: {last_err}")
            # Сразу goto #inbox/ и следующая попытка (без лишней паузы poll).
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


def fetch_meta_authenticate_code_from_gmail(
    gmail_page,
    *,
    max_seconds: float = _EMAIL_WAIT_MAX_S,
    exclude_codes: set[str] | frozenset[str] | None = None,
) -> str:
    """
    Inbox → письмо Meta «Authenticate your profile» → 8-значный код.
    """
    _bring_to_front(gmail_page)
    deadline = time.monotonic() + max(30.0, float(max_seconds))
    last_err: str | None = None
    exclude = {
        str(c).strip()
        for c in (exclude_codes or ())
        if c is not None and str(c).strip()
    }

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

            row = _find_meta_auth_row(gmail_page)
            if row is None:
                last_err = "письмо Meta ещё не пришло"
                _log(f"Gmail: {last_err}, ждём…")
                gmail_page.wait_for_timeout(int(_EMAIL_POLL_S * 1000))
                continue

            code = _open_meta_row_and_read_code(gmail_page, row)
            if code:
                if code in exclude:
                    last_err = f"код {code} уже пробовали — ждём новое письмо"
                    _log(f"Gmail: {last_err}")
                    _reload_inbox(gmail_page)
                    gmail_page.wait_for_timeout(int(_EMAIL_POLL_S * 1000))
                    continue
                _log(f"Gmail: код Meta Authenticate = {code}")
                return code

            last_err = (
                f"письмо Meta не открылось за {_OPEN_EMAIL_MAX_S:.0f} с "
                "(или код не найден) — снова #inbox/"
            )
            _log(f"Gmail: {last_err}")
            _reload_inbox(gmail_page)
            continue
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            _log(f"Gmail: ошибка чтения кода Meta: {last_err}")
        gmail_page.wait_for_timeout(int(_EMAIL_POLL_S * 1000))

    raise RuntimeError(
        f"Gmail: не удалось получить код Meta за {max_seconds:.0f} с"
        + (f" ({last_err})" if last_err else "")
    )
