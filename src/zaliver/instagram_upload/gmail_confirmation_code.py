"""Чтение кода подтверждения Instagram из Gmail."""

from __future__ import annotations

import re
import time

from zaliver.instagram_upload.gmail_availability import (
    GMAIL_INBOX_URL,
    dismiss_gmail_smart_features_if_present,
    gmail_inbox_ready,
)
from zaliver.instagram_upload.logutil import emit_instagram_log

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
    r"authenticate\s+your\s+account|"
    r"Use\s+\d{6,8}\s+to\s+log\s+into\s+Instagram|"
    r"log\s+back\s+into\s+Instagram|"
    r"to\s+log\s+into\s+Instagram",
    re.IGNORECASE,
)
_META_CODE_NEAR_RE = re.compile(
    r"Use\s+(\d{8})\s+to\s+log\s+into\s+Instagram|"
    r"(?:confirm\s+your\s+identity|подтвердите\s+(?:свою\s+)?личность|"
    r"use\s+the\s+following\s+code|следующ(?:ий|им)\s+код|"
    r"(?:Or\s+)?use\s+this\s+code\s+in\s+the\s+app)[^\d]{0,80}(\d{8})",
    re.IGNORECASE,
)
_META_SUBJECT_USE_CODE_RE = re.compile(
    r"Use\s+(\d{8})\s+to\s+log\s+into\s+Instagram",
    re.IGNORECASE,
)

_EMAIL_WAIT_MAX_S = 120.0
_EMAIL_POLL_S = 4.0
# Письмо видно в списке, но не открывается / код не читается — снова #inbox/.
_OPEN_EMAIL_MAX_S = 10.0

# Desktop: tr.zA; mobile Gmail (iPhone preset): #tl_ listitem / .ksQvef
_DESKTOP_ROW = "tr.zA"
# Только строки треда внутри #tl_ (без дублей из широкого OR-селектора).
_MOBILE_LISTITEM = '#tl_ div.ksQvef[role="listitem"]'
_MOBILE_ROW = _MOBILE_LISTITEM
_AUTHENTICATE_SUBJECT_RE = re.compile(
    r"Authenticate\s+your\s+profile|"
    r"подтвердите\s+(свой\s+)?профиль|"
    r"authenticate\s+your\s+account",
    re.IGNORECASE,
)


def _log(message: str) -> None:
    emit_instagram_log(message, tag="[gmail]")


def _bring_to_front(page) -> None:
    try:
        page.bring_to_front()
    except Exception:
        pass


def _goto_inbox(page) -> None:
    """Открыть https://mail.google.com/mail/u/0/#inbox/ (без page.reload)."""
    try:
        from zaliver.instagram_upload.gmail_availability import (
            force_desktop_emulation_for_page,
        )

        force_desktop_emulation_for_page(page)
    except Exception as e:
        _log(f"Gmail: desktop emulation перед inbox: {e!r}")
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
    # Mobile Gmail: «Primary» в шапке (#tltbt), не role=tab.
    try:
        mob = page.locator("#tltbt .SGqfCc, .WqEu7b .SGqfCc").filter(
            has_text=re.compile(r"^\s*Primary\b", re.I)
        ).first
        if mob.count() > 0 and mob.is_visible(timeout=400):
            mob.click(timeout=5000)
            page.wait_for_timeout(800)
    except Exception:
        pass


def _first_visible_row(candidates):
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


def _mobile_list_row_count(page) -> int:
    try:
        return int(page.locator(_MOBILE_LISTITEM).count())
    except Exception:
        return 0


def _iter_mobile_list_rows(page, *, limit: int = 40):
    """Строки inbox сверху вниз (новые выше)."""
    rows = page.locator(_MOBILE_LISTITEM)
    try:
        n = min(int(rows.count()), max(1, int(limit)))
    except Exception:
        return
    for i in range(n):
        yield rows.nth(i)


def _find_instagram_row(page, *, exclude_codes=None):
    """Строка письма от Instagram с кодом (desktop tr.zA или mobile listitem)."""
    exclude = {
        str(c).strip()
        for c in (exclude_codes or ())
        if c is not None and str(c).strip()
    }

    # Mobile: только тема .HhG5wd, сверху вниз — свежее письмо первым.
    if _is_mobile_gmail_list(page) and _mobile_list_row_count(page) > 0:
        for row in _iter_mobile_list_rows(page):
            try:
                subject = _mobile_row_subject_text(row)
            except Exception:
                continue
            if not subject:
                continue
            if not (
                _IG_SENDER_RE.search(subject)
                or _IG_SUBJECT_HINT_RE.search(subject)
                or re.search(r"instagram", subject, re.I)
            ):
                # Отправитель в соседнем блоке — смотрим всю строку только на Meta/IG маркер.
                try:
                    sender = row.locator("[id^='ti_f_']").first.inner_text(timeout=400)
                except Exception:
                    sender = ""
                if not re.search(r"instagram", sender or "", re.I):
                    if not (_CODE_RE.search(subject) and _IG_SUBJECT_HINT_RE.search(subject)):
                        continue
            code = _extract_code_from_text(subject)
            if not code or code in exclude:
                continue
            try:
                if row.is_visible(timeout=300):
                    _log(f"Gmail (mobile): Instagram «{subject[:70]}»")
                    return row
            except Exception:
                continue
        return None

    candidates = [
        page.locator(_DESKTOP_ROW).filter(has_text=_IG_SENDER_RE).filter(
            has_text=_CODE_RE
        ),
        page.locator(_DESKTOP_ROW).filter(has_text=_IG_SENDER_RE).filter(
            has_text=_IG_SUBJECT_HINT_RE
        ),
        page.locator('span.zF[email="no-reply@mail.instagram.com"]').locator(
            "xpath=ancestor::tr[contains(@class,'zA')][1]"
        ),
        page.locator('span.zF[name="Instagram"]').locator(
            "xpath=ancestor::tr[contains(@class,'zA')][1]"
        ),
        page.locator(_DESKTOP_ROW).filter(has_text=re.compile(r"instagram", re.I)).filter(
            has_text=_CODE_RE
        ),
    ]
    return _first_visible_row(candidates)


def _find_meta_auth_row(page, *, exclude_codes=None):
    """
    Строка письма Meta: «Use NNNNNNNN to log into Instagram» / Authenticate.
    Mobile: код только из темы, берём самое верхнее (новое) подходящее письмо.
    """
    exclude = {
        str(c).strip()
        for c in (exclude_codes or ())
        if c is not None and str(c).strip()
    }

    if _is_mobile_gmail_list(page) and _mobile_list_row_count(page) > 0:
        # 1) «Use 65753582 to log into Instagram» — только .HhG5wd, сверху вниз.
        for row in _iter_mobile_list_rows(page):
            try:
                subject = _mobile_row_subject_text(row)
            except Exception:
                continue
            m = _META_SUBJECT_USE_CODE_RE.search(subject or "")
            if not m:
                continue
            code = m.group(1)
            if code in exclude:
                continue
            try:
                if row.is_visible(timeout=300):
                    _log(f"Gmail (mobile): Meta login «{subject[:70]}» → {code}")
                    return row
            except Exception:
                continue
        # 2) Authenticate your profile (тема, не превью body).
        for row in _iter_mobile_list_rows(page):
            try:
                subject = _mobile_row_subject_text(row)
            except Exception:
                continue
            if not _AUTHENTICATE_SUBJECT_RE.search(subject or ""):
                continue
            code = _extract_meta_code_from_text(subject)
            if code and code in exclude:
                continue
            try:
                if row.is_visible(timeout=300):
                    _log(f"Gmail (mobile): Meta auth «{subject[:70]}»")
                    return row
            except Exception:
                continue
        return None

    candidates = [
        page.locator(_DESKTOP_ROW)
        .filter(has_text=re.compile(r"noreply@account\.meta\.com", re.I))
        .filter(has_text=_META_SUBJECT_HINT_RE),
        page.locator('span[email="noreply@account.meta.com"]').locator(
            "xpath=ancestor::tr[contains(@class,'zA')][1]"
        ),
        page.locator(_DESKTOP_ROW)
        .filter(has_text=_META_SENDER_RE)
        .filter(has_text=_META_SUBJECT_HINT_RE),
        page.locator(_DESKTOP_ROW)
        .filter(has_text=_META_SENDER_RE)
        .filter(has_text=_META_CODE_RE),
        page.locator(_DESKTOP_ROW).filter(has_text=_META_SUBJECT_USE_CODE_RE),
    ]
    return _first_visible_row(candidates)


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
    """8-значный код из письма Meta (Authenticate / log into Instagram)."""
    raw = (text or "").strip()
    if not raw:
        return None
    m_subj = _META_SUBJECT_USE_CODE_RE.search(raw)
    if m_subj:
        return m_subj.group(1)
    near = _META_CODE_NEAR_RE.search(raw)
    if near:
        for g in near.groups():
            if g and g.isdigit() and len(g) == 8:
                return g
    alone = re.findall(r"(?:^|\n)\s*(\d{8})\s*(?:$|\n)", raw)
    if len(alone) == 1:
        return alone[0]
    codes = _META_CODE_RE.findall(raw)
    if not codes:
        return None
    lower = raw.lower()
    for c in codes:
        idx = lower.find(c)
        window = lower[max(0, idx - 100) : idx + 40]
        if any(
            k in window
            for k in (
                "confirm",
                "identity",
                "authenticate",
                "instagram",
                "log into",
                "log back",
                "meta",
                "код",
                "личн",
                "code in the app",
            )
        ):
            return c
    return codes[0]


def _open_message_body_selectors() -> tuple[str, ...]:
    return (
        # Desktop
        "div.a3s.aiL",
        "div.ii.gt",
        "div[data-message-id]",
        "div.adn.ads",
        # Mobile Gmail conversation
        "div.qgRHze",
        "div.IoGNdb",
        "div[id^='cvcmsgbod_']",
        "div.LnPMz",
    )


def _extract_code_from_open_message(page) -> str | None:
    # Тема письма (desktop h2.hP / mobile .jzNoVc).
    for sel in ("h2.hP", "span.jzNoVc", ".xWfkye span.jzNoVc"):
        try:
            subject = page.locator(sel).first
            if subject.count() > 0 and subject.is_visible(timeout=500):
                code = _extract_code_from_text(subject.inner_text(timeout=2000) or "")
                if code:
                    return code
        except Exception:
            pass
    for sel in _open_message_body_selectors():
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
    try:
        return _extract_code_from_text(page.inner_text("body", timeout=3000) or "")
    except Exception:
        return None


def _extract_meta_code_from_open_message(page) -> str | None:
    for sel in ("h2.hP", "span.jzNoVc", ".xWfkye span.jzNoVc"):
        try:
            subject = page.locator(sel).first
            if subject.count() > 0 and subject.is_visible(timeout=500):
                code = _extract_meta_code_from_text(
                    subject.inner_text(timeout=2000) or ""
                )
                if code:
                    return code
        except Exception:
            pass
    for sel in _open_message_body_selectors():
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


def _is_mobile_gmail_list(page) -> bool:
    """Mobile Gmail inbox (#tl_ / ksQvef), не desktop tr.zA."""
    try:
        if page.locator("#tl_").count() > 0 and page.locator("#tl_").first.is_visible(
            timeout=200
        ):
            return True
    except Exception:
        pass
    try:
        if page.locator(_MOBILE_ROW).count() > 0:
            return True
    except Exception:
        pass
    return False


def _mobile_row_subject_text(row) -> str:
    """Тема письма в mobile-списке (.HhG5wd / #ti_s_*)."""
    for sel in ("div.HhG5wd", "[id^='ti_s_']", ".SGqfCc.HhG5wd"):
        try:
            loc = row.locator(sel).first
            if loc.count() > 0:
                t = (loc.inner_text(timeout=800) or "").strip()
                if t:
                    return t
        except Exception:
            continue
    try:
        return (row.inner_text(timeout=800) or "").strip()
    except Exception:
        return ""


def _message_pane_open(page) -> bool:
    """Открыто ли тело/тема письма (не только список)."""
    for sel in (
        "h2.hP",
        "div.a3s.aiL",
        "div.ii.gt",
        "span.jzNoVc",
        "div.qgRHze",
        "div[id^='cvcmsgbod_']",
        "div.Atp2Qd",
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=300):
                return True
        except Exception:
            continue
    return False


def _click_instagram_row(page, row) -> bool:
    """Клик по строке письма (desktop / mobile). True если клик ушёл."""
    for click_sel in (
        "td.a4W",
        "span.bqe",
        "div.y6",
        "span.bog",
        "td.yX",
        # Mobile list row
        "div.Akvd4",
        "div.HhG5wd",
        "div.MpFCYc",
        "div.immPke",
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
    Вытащить 6-значный код из письма Instagram.
    Mobile: код из темы в списке, письмо не открываем.
    Desktop: открываем письмо; если за ``_OPEN_EMAIL_MAX_S`` не вышло — None.
    """
    # Mobile: «790186 is your Instagram code» уже в названии строки.
    if _is_mobile_gmail_list(page):
        subject = _mobile_row_subject_text(row)
        code = _extract_code_from_text(subject)
        if code:
            _log(f"Gmail (mobile): код из названия письма = {code}")
            return code
        _log("Gmail (mobile): в названии нет кода Instagram — откроем письмо…")

    open_deadline = time.monotonic() + _OPEN_EMAIL_MAX_S

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
            page.wait_for_timeout(400)
            continue
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
    """
    8-значный код Meta.
    Mobile: строго из темы «Use NNNNNNNN to log into Instagram», без открытия.
    """
    if _is_mobile_gmail_list(page):
        subject = _mobile_row_subject_text(row)
        m = _META_SUBJECT_USE_CODE_RE.search(subject or "")
        if m:
            code = m.group(1)
            _log(f"Gmail (mobile): код Meta из названия = {code}")
            return code
        code = _extract_meta_code_from_text(subject)
        if code:
            _log(f"Gmail (mobile): код Meta из названия = {code}")
            return code
        _log("Gmail (mobile): в названии нет кода Meta — откроем письмо…")

    open_deadline = time.monotonic() + _OPEN_EMAIL_MAX_S
    try:
        preview = row.inner_text(timeout=1500) or ""
        preview_code = _extract_meta_code_from_text(preview)
    except Exception:
        preview_code = None

    _log("Gmail: открываем письмо Meta (код Instagram / Authenticate)…")
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

            row = _find_instagram_row(gmail_page, exclude_codes=exclude)
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

            row = _find_meta_auth_row(gmail_page, exclude_codes=exclude)
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
