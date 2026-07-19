"""Проверка доступности Gmail (первый шаг пайплайна Instagram)."""

from __future__ import annotations

import re
import time

from zaliver.youtube_upload.google_login import (
    GoogleLoginCredentials,
    attempt_google_login_for_studio,
    google_auth_interaction_visible,
)
from zaliver.youtube_upload import studio as _studio

GMAIL_WORKSPACE_URL = "https://workspace.google.com/intl/ru/gmail/#inbox"
GMAIL_INBOX_URL = "https://mail.google.com/mail/u/0/#inbox"

# Временно для тестов: не останавливать профиль антидетекта после проверки Gmail.
KEEP_PROFILE_OPEN_AFTER_GMAIL_CHECK = True

_SIGN_IN_RE = re.compile(
    r"войти(\s+в\s+gmail)?|sign\s*in(\s+to\s+gmail)?",
    re.IGNORECASE,
)
_COMPOSE_RE = re.compile(r"compose|написать", re.IGNORECASE)
_INBOX_RE = re.compile(r"inbox|входящие", re.IGNORECASE)
_SMART_FEATURES_HEADING_RE = re.compile(
    r"smart\s+features|умн(ые|ых)\s+функц|"
    r"turn\s+on\s+smart|включите\s+умн",
    re.IGNORECASE,
)
_SMART_FEATURES_NEXT_RE = re.compile(
    r"^далее$|^next$|^continue$|^продолжить$|^done$|^готово$|"
    r"^сохранить$|^save$",
    re.IGNORECASE,
)

_GMAIL_READY_MAX_S = 120.0
_SMART_FEATURES_SCREENS = 3


def _log(message: str) -> None:
    _studio._log(f"[gmail] {message}")


def _page_url_lower(page) -> str:
    try:
        return (page.url or "").strip().lower()
    except Exception:
        return ""


def _is_mail_google_url(url: str) -> bool:
    u = (url or "").strip().lower()
    return "mail.google.com" in u


def _gmail_shell_ready_js(page) -> bool:
    """Быстрая проверка UI почты одним evaluate (без цепочки is_visible timeout)."""
    try:
        return bool(
            page.evaluate(
                """() => {
                    const visible = (el) => {
                        if (!el) return false;
                        const s = window.getComputedStyle(el);
                        if (s.display === 'none' || s.visibility === 'hidden') return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    };
                    const compose =
                        document.querySelector('div[gh="cm"]') ||
                        document.querySelector('[role="button"][gh="cm"]') ||
                        document.querySelector('div.T-I.T-I-KE.L3');
                    if (visible(compose)) return true;
                    const inboxLink = document.querySelector('a[href*="#inbox"]');
                    if (visible(inboxLink)) return true;
                    const nav = document.querySelector('div[role="navigation"]');
                    if (visible(nav)) return true;
                    return false;
                }"""
            )
        )
    except Exception:
        return False


def _gmail_compose_visible(page) -> bool:
    selectors = (
        'div[gh="cm"]',
        'div.T-I.T-I-KE.L3',
        '[role="button"][gh="cm"]',
    )
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=150):
                return True
        except Exception:
            pass
    try:
        loc = page.get_by_role("button", name=_COMPOSE_RE).first
        if loc.count() > 0 and loc.is_visible(timeout=150):
            return True
    except Exception:
        pass
    return False


def _gmail_inbox_nav_visible(page) -> bool:
    try:
        loc = page.locator('a[href*="#inbox"]').first
        if loc.count() > 0 and loc.is_visible(timeout=150):
            return True
    except Exception:
        pass
    try:
        loc = page.get_by_role("link", name=_INBOX_RE).first
        if loc.count() > 0 and loc.is_visible(timeout=150):
            return True
    except Exception:
        pass
    try:
        loc = page.locator('div[role="navigation"]').first
        if loc.count() > 0 and loc.is_visible(timeout=150):
            return True
    except Exception:
        pass
    return False


def _auth_ui_on_mail_page(page) -> bool:
    """На mail.google.com не гоняем полный google_auth_interaction_visible (дорого)."""
    url = _page_url_lower(page)
    if "accounts.google.com" in url:
        return True
    # Лёгкие маркеры встроенного логина, если вдруг оказались не на accounts.
    try:
        if page.locator('input[name="Passwd"], #identifierId').first.count() > 0:
            loc = page.locator('input[name="Passwd"], #identifierId').first
            if loc.is_visible(timeout=100):
                return True
    except Exception:
        pass
    return False


def gmail_inbox_ready(page) -> bool:
    """Почтовый ящик открыт: URL mail.google.com + Compose / навигация Inbox."""
    if not _is_mail_google_url(_page_url_lower(page)):
        return False
    if _auth_ui_on_mail_page(page):
        return False
    if _smart_features_dialog_visible(page):
        return False
    if _gmail_shell_ready_js(page):
        return True
    return _gmail_compose_visible(page) or _gmail_inbox_nav_visible(page)


def _smart_features_dialog_visible(page) -> bool:
    """Окно «Turn on smart features…» после первого входа в Gmail."""
    try:
        root = page.locator('div.e0Pzhd[jscontroller="G90DNc"], div.e0Pzhd').first
        if root.count() > 0 and root.is_visible(timeout=150):
            return True
    except Exception:
        pass
    try:
        # Только видимый экран: в DOM всегда 3 панели WNielf, скрытые с display:none.
        panels = page.locator("div.WNielf")
        n = min(panels.count(), 6)
        for i in range(n):
            if panels.nth(i).is_visible(timeout=100):
                return True
    except Exception:
        pass
    try:
        h = page.locator('[role="heading"]').filter(
            has_text=_SMART_FEATURES_HEADING_RE
        ).first
        if h.count() > 0 and h.is_visible(timeout=150):
            return True
    except Exception:
        pass
    return False


def _click_smart_features_second_option(page) -> bool:
    """Всегда второй radio на ТЕКУЩЕМ видимом экране (Turn off / выключить)."""
    # jsname второго варианта на экранах 1/2/3 мастера.
    named = (
        page.locator('li[role="option"][jsname="e65Ih"]').first,
        page.locator('li[role="option"][jsname="vXCQJd"]').first,
        page.locator('li[role="option"][jsname="PZtNne"]').first,
    )
    for target in named:
        try:
            if target.count() <= 0:
                continue
            if not target.is_visible(timeout=400):
                continue
            try:
                target.scroll_into_view_if_needed(timeout=3_000)
            except Exception:
                pass
            try:
                target.click(timeout=10_000)
            except Exception:
                target.click(timeout=10_000, force=True)
            return True
        except Exception:
            continue

    # Видимый listbox → второй option (не путать с option'ами скрытых экранов).
    try:
        listboxes = page.locator(
            'ul[role="listbox"][data-list-type="SINGLE_SELECT_RADIO"], '
            'ul[role="listbox"]'
        )
        n = min(listboxes.count(), 6)
        for i in range(n):
            lb = listboxes.nth(i)
            try:
                if not lb.is_visible(timeout=200):
                    continue
            except Exception:
                continue
            opts = lb.locator('li[role="option"]')
            if opts.count() < 2:
                continue
            target = opts.nth(1)
            if not target.is_visible(timeout=400):
                continue
            try:
                target.scroll_into_view_if_needed(timeout=3_000)
            except Exception:
                pass
            try:
                target.click(timeout=10_000)
            except Exception:
                target.click(timeout=10_000, force=True)
            return True
    except Exception:
        pass

    # JS: только среди видимых option'ов.
    try:
        return bool(
            page.evaluate(
                """() => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const st = window.getComputedStyle(el);
                        if (st.display === 'none' || st.visibility === 'hidden') return false;
                        if (Number(st.opacity) === 0) return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    };
                    const root =
                        document.querySelector('div.e0Pzhd') ||
                        document.body;
                    const opts = Array.from(
                        root.querySelectorAll('li[role="option"]')
                    ).filter(isVisible);
                    if (opts.length < 2) return false;
                    opts[1].click();
                    return true;
                }"""
            )
        )
    except Exception:
        return False


def _click_smart_features_next(page) -> bool:
    """Next / Далее / Save — только видимая и enabled кнопка."""
    named = (
        page.locator('button[jsname="O5kDGc"]').first,  # Next экран 1
        page.locator('button[jsname="Efeutd"]').first,  # Next экран 2
        page.locator('button[jsname="plIjzf"]').first,  # Save экран 3
    )
    for target in named:
        try:
            if target.count() <= 0:
                continue
            if not target.is_visible(timeout=400):
                continue
            try:
                target.wait_for(state="visible", timeout=2_000)
            except Exception:
                pass
            # После выбора radio кнопка снимает disabled.
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                try:
                    if target.is_enabled():
                        break
                except Exception:
                    break
                page.wait_for_timeout(150)
            try:
                target.click(timeout=10_000)
            except Exception:
                target.click(timeout=10_000, force=True)
            return True
        except Exception:
            continue

    candidates = [
        page.get_by_role("button", name=_SMART_FEATURES_NEXT_RE),
        page.locator("div.e0Pzhd button").filter(has_text=_SMART_FEATURES_NEXT_RE),
        page.locator("div.WNielf button").filter(has_text=_SMART_FEATURES_NEXT_RE),
        page.locator("button").filter(has_text=_SMART_FEATURES_NEXT_RE),
    ]
    for loc in candidates:
        try:
            n = min(loc.count(), 8)
            for i in range(n):
                target = loc.nth(i)
                if not target.is_visible(timeout=300):
                    continue
                try:
                    if not target.is_enabled():
                        deadline = time.monotonic() + 3.0
                        while time.monotonic() < deadline:
                            if target.is_enabled():
                                break
                            page.wait_for_timeout(150)
                except Exception:
                    pass
                try:
                    target.click(timeout=10_000)
                except Exception:
                    target.click(timeout=10_000, force=True)
                return True
        except Exception:
            continue
    try:
        return bool(
            page.evaluate(
                """() => {
                    const labels = [
                        'Next', 'Далее', 'Continue', 'Продолжить',
                        'Done', 'Готово', 'Save', 'Сохранить',
                    ];
                    const isVisible = (el) => {
                        if (!el) return false;
                        const st = window.getComputedStyle(el);
                        if (st.display === 'none' || st.visibility === 'hidden') return false;
                        if (el.disabled) return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    };
                    const root =
                        document.querySelector('div.e0Pzhd') || document;
                    const buttons = Array.from(root.querySelectorAll('button'));
                    for (const btn of buttons) {
                        const t = (btn.innerText || btn.textContent || '').trim();
                        if (!labels.some((x) => t === x || t.includes(x))) continue;
                        if (!isVisible(btn)) continue;
                        btn.click();
                        return true;
                    }
                    return false;
                }"""
            )
        )
    except Exception:
        return False


def _run_smart_features_wizard_screens(
    page, *, max_screens: int = _SMART_FEATURES_SCREENS
) -> int:
    """Пройти экраны мастера: 2-й radio → Next/Save. Возвращает число пройденных."""
    handled = 0
    for screen in range(1, max_screens + 1):
        if not _smart_features_dialog_visible(page):
            break
        if not _click_smart_features_second_option(page):
            _log(f"Gmail: smart features экран {screen}: 2-й radio не найден.")
            break
        page.wait_for_timeout(500)
        if not _click_smart_features_next(page):
            _log(
                f"Gmail: smart features экран {screen}: "
                "кнопка «Далее»/«Сохранить» не найдена."
            )
            break
        handled += 1
        _log(f"Gmail: smart features — экран {screen}/{max_screens} пройден.")
        page.wait_for_timeout(900)
    return handled


def dismiss_gmail_smart_features_if_present(
    page, *, max_screens: int = _SMART_FEATURES_SCREENS
) -> bool:
    """
    Мастер «Turn on smart features»: до 3 экранов с radio —
    всегда второй вариант → «Далее»/«Сохранить», затем reload.
    """
    if not _smart_features_dialog_visible(page):
        return False

    _log("Gmail: окно smart features — выбираем 2-й вариант и «Далее»…")
    handled = _run_smart_features_wizard_screens(page, max_screens=max_screens)

    _log("Gmail: перезагружаем страницу после smart features…")
    try:
        page.reload(wait_until="domcontentloaded", timeout=90_000)
    except Exception as e:
        _log(f"Gmail: reload после smart features: {e!r}")
        try:
            page.goto(GMAIL_INBOX_URL, wait_until="domcontentloaded", timeout=90_000)
        except Exception as e2:
            _log(f"Gmail: goto inbox после smart features: {e2!r}")
    page.wait_for_timeout(1500)

    # Иногда мастер снова мелькает после reload — добиваем ещё раз.
    if _smart_features_dialog_visible(page):
        _log("Gmail: smart features снова виден после reload — добиваем…")
        handled += _run_smart_features_wizard_screens(page, max_screens=max_screens)
        try:
            page.reload(wait_until="domcontentloaded", timeout=90_000)
        except Exception:
            pass
        page.wait_for_timeout(1200)

    return handled > 0 or not _smart_features_dialog_visible(page)


def _first_open_page(context, preferred=None):
    """Первая открытая вкладка контекста (после закрытия окна логина)."""
    if preferred is not None:
        try:
            if not preferred.is_closed():
                return preferred
        except Exception:
            pass
    try:
        for pg in context.pages:
            try:
                if not pg.is_closed():
                    return pg
            except Exception:
                continue
    except Exception:
        pass
    return preferred


def _find_google_auth_page(context, *, base_page, before_pages):
    for pg in context.pages:
        if pg in before_pages:
            continue
        try:
            if pg.is_closed():
                continue
        except Exception:
            continue
        url = _page_url_lower(pg)
        if "accounts.google.com" in url or google_auth_interaction_visible(pg):
            return pg
    for pg in context.pages:
        if pg is base_page:
            continue
        try:
            if pg.is_closed():
                continue
        except Exception:
            continue
        url = _page_url_lower(pg)
        if "accounts.google.com" in url or google_auth_interaction_visible(pg):
            return pg
    if google_auth_interaction_visible(base_page) or "accounts.google.com" in _page_url_lower(
        base_page
    ):
        return base_page
    return None


def _click_gmail_sign_in(page) -> bool:
    """Кнопка «Войти» на landing workspace.google.com/gmail."""
    candidates = [
        page.locator('a[aria-label="Войти в Gmail"]'),
        page.locator('a[aria-label*="Войти в Gmail" i]'),
        page.locator('a[aria-label*="Sign in to Gmail" i]'),
        page.locator('a[href*="accounts.google.com"][href*="mail.google.com"]'),
        page.locator("a.button").filter(has_text=_SIGN_IN_RE),
        page.get_by_role("link", name=_SIGN_IN_RE),
        page.get_by_role("button", name=_SIGN_IN_RE),
        page.locator("span.button__content").filter(has_text=_SIGN_IN_RE),
    ]
    for loc in candidates:
        try:
            target = loc.first
            if target.count() <= 0:
                continue
            if not target.is_visible(timeout=800):
                continue
            # Клик по тексту внутри кнопки — поднимаемся к <a>, если нужно.
            try:
                href_el = target.locator("xpath=ancestor-or-self::a[1]").first
                if href_el.count() > 0 and href_el.is_visible(timeout=200):
                    target = href_el
            except Exception:
                pass
            _log("Gmail: клик «Войти»…")
            target.click(timeout=5000)
            return True
        except Exception:
            continue
    return False


def _click_gmail_sign_in_maybe_popup(page, *, wait_popup_s: float = 8.0):
    """
    Клик «Войти». Если открылось новое окно/вкладка — вернуть её,
    иначе None (логин на той же вкладке).
    """
    context = page.context
    before_pages = set(context.pages)
    if not _click_gmail_sign_in(page):
        return None, False

    deadline = time.monotonic() + wait_popup_s
    while time.monotonic() < deadline:
        # Новая вкладка/окно с Google auth.
        for pg in list(context.pages):
            if pg in before_pages or pg is page:
                continue
            try:
                if pg.is_closed():
                    continue
            except Exception:
                continue
            try:
                _log(f"Gmail: открылось окно входа Google: {pg.url!r}")
            except Exception:
                _log("Gmail: открылось окно входа Google.")
            return pg, True

        auth = _find_google_auth_page(
            context, base_page=page, before_pages=before_pages
        )
        if auth is not None and auth is not page:
            try:
                _log(f"Gmail: найдено окно входа Google: {auth.url!r}")
            except Exception:
                _log("Gmail: найдено окно входа Google.")
            return auth, True

        # Логин в той же вкладке.
        if google_auth_interaction_visible(page) or "accounts.google.com" in _page_url_lower(
            page
        ):
            return None, True

        page.wait_for_timeout(300)

    return None, True


def _already_on_inbox_url(page) -> bool:
    url = _page_url_lower(page)
    return _is_mail_google_url(url) and "#inbox" in url


def _goto_gmail_inbox(page) -> None:
    if _already_on_inbox_url(page) and (
        _gmail_shell_ready_js(page) or gmail_inbox_ready(page)
    ):
        return
    _log(f"Gmail: открываем почту в первой вкладке ({GMAIL_INBOX_URL})…")
    try:
        page.goto(GMAIL_INBOX_URL, wait_until="domcontentloaded", timeout=90_000)
    except Exception as e:
        _log(f"Gmail: goto inbox: {e!r}")
    page.wait_for_timeout(500)


def _inbox_or_incoming_visible(page) -> bool:
    """На первой вкладке есть Inbox / Входящие (или Compose)."""
    if gmail_inbox_ready(page):
        return True
    try:
        loc = page.get_by_text(_INBOX_RE).first
        if loc.count() > 0 and loc.is_visible(timeout=150):
            return _is_mail_google_url(_page_url_lower(page))
    except Exception:
        pass
    return False


def _wait_gmail_inbox_ready(page, *, max_seconds: float = _GMAIL_READY_MAX_S) -> None:
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        if _smart_features_dialog_visible(page):
            dismiss_gmail_smart_features_if_present(page)
            continue
        if _inbox_or_incoming_visible(page):
            _log(f"Gmail: почтовый ящик открыт (URL={page.url!r}).")
            return
        page.wait_for_timeout(250)
    raise RuntimeError(
        f"Gmail: почтовый ящик не открылся за {max_seconds:.0f} с "
        f"(URL={page.url!r})."
    )


def _page_looks_signed_in(page) -> bool:
    """Уже в аккаунте: Inbox/Входящие, smart features или mail.google.com без UI входа."""
    try:
        if page is None or page.is_closed():
            return False
    except Exception:
        return False
    if _inbox_or_incoming_visible(page):
        return True
    if _smart_features_dialog_visible(page):
        return True
    url = _page_url_lower(page)
    if _is_mail_google_url(url) and not _auth_ui_on_mail_page(page):
        return True
    return False


def _wait_until_auth_or_signed_in(
    page,
    *,
    max_seconds: float = 12.0,
) -> str:
    """
    После «Войти» / редиректа: либо UI входа Google, либо уже вошли.
    Возвращает ``\"auth\"`` | ``\"signed_in\"`` | ``\"unknown\"``.
    """
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        if _page_looks_signed_in(page):
            return "signed_in"
        if google_auth_interaction_visible(page) or "accounts.google.com" in _page_url_lower(
            page
        ):
            # Даём сессии шанс авто-редиректнуть в почту, если cookie уже есть.
            page.wait_for_timeout(800)
            if _page_looks_signed_in(page):
                return "signed_in"
            if google_auth_interaction_visible(page):
                return "auth"
            if "accounts.google.com" in _page_url_lower(page):
                # Ещё на accounts, но без явного UI — подождём чуть-чуть.
                page.wait_for_timeout(500)
                continue
        page.wait_for_timeout(300)
    if _page_looks_signed_in(page):
        return "signed_in"
    if google_auth_interaction_visible(page) or "accounts.google.com" in _page_url_lower(
        page
    ):
        return "auth"
    return "unknown"


def _open_inbox_on_first_then_close_auth(
    auth_page,
    base_page,
    *,
    max_seconds: float = _GMAIL_READY_MAX_S,
):
    """
    После входа во 2-й вкладке: открыть почту в 1-й,
    дождаться Inbox/Входящие, только потом закрыть 2-ю.
    """
    context = base_page.context
    first = _first_open_page(context, preferred=base_page)
    if first is None:
        first = base_page
    try:
        first.bring_to_front()
    except Exception:
        pass
    _log(f"Gmail: перешли на первую вкладку (URL={getattr(first, 'url', '')!r}).")

    if not _inbox_or_incoming_visible(first) and not _smart_features_dialog_visible(first):
        _goto_gmail_inbox(first)

    if _smart_features_dialog_visible(first):
        dismiss_gmail_smart_features_if_present(first)

    _wait_gmail_inbox_ready(first, max_seconds=max_seconds)

    if auth_page is not None and auth_page is not first:
        try:
            if not auth_page.is_closed():
                auth_page.close()
                _log("Gmail: вторая вкладка (вход Google) закрыта — в первой есть Inbox/Входящие.")
        except Exception as e:
            _log(f"Gmail: не удалось закрыть окно входа: {e!r}")

    try:
        first.bring_to_front()
    except Exception:
        pass
    return first


def verify_gmail_inbox_available(
    page,
    *,
    login_credentials: GoogleLoginCredentials | None = None,
    max_seconds: float = _GMAIL_READY_MAX_S,
) -> None:
    """
    Пайплайн проверки доступности для Instagram (шаг 1):
    сразу mail.google.com inbox в 1-й вкладке → (если нужно) Google login →
    дождаться Inbox/Входящие → закрыть лишнюю вкладку входа.
    Если сессия уже есть — пайплайн входа пропускаем.
    """
    _log(f"Gmail: открываем {GMAIL_INBOX_URL}")
    page.goto(GMAIL_INBOX_URL, wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(800)

    # Уже открыт inbox — сразу выходим, без повторного goto и долгого wait.
    if gmail_inbox_ready(page):
        _log(f"Gmail: уже в аккаунте — Inbox готов (URL={page.url!r}).")
        return

    if _page_looks_signed_in(page):
        _log("Gmail: уже в аккаунте — дожидаемся UI Inbox.")
        if _smart_features_dialog_visible(page):
            dismiss_gmail_smart_features_if_present(page)
        if not _inbox_or_incoming_visible(page) and not _already_on_inbox_url(page):
            _goto_gmail_inbox(page)
        _wait_gmail_inbox_ready(page, max_seconds=max_seconds)
        return

    auth_page = None
    url = _page_url_lower(page)
    if "accounts.google.com" in url or google_auth_interaction_visible(page):
        _log("Gmail: уже на странице входа Google.")
        auth_page = page
    elif not _is_mail_google_url(url):
        # Редирект не на mail и не на accounts — запасной путь через workspace «Войти».
        _log(f"Gmail: не inbox/accounts ({page.url!r}) — пробуем workspace «Войти».")
        try:
            page.goto(GMAIL_WORKSPACE_URL, wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(1500)
        except Exception as e:
            _log(f"Gmail: goto workspace: {e!r}")
        auth_page, clicked = _click_gmail_sign_in_maybe_popup(page)
        if not clicked:
            page.wait_for_timeout(2000)
            if gmail_inbox_ready(page) or _page_looks_signed_in(page):
                _log("Gmail: уже в аккаунте после ожидания — проверяем Inbox.")
                if not _inbox_or_incoming_visible(page):
                    _goto_gmail_inbox(page)
                _wait_gmail_inbox_ready(page, max_seconds=max_seconds)
                return
            auth_page, clicked = _click_gmail_sign_in_maybe_popup(page)
            if not clicked:
                raise RuntimeError(
                    "Gmail: не найдена кнопка «Войти» на "
                    f"{GMAIL_WORKSPACE_URL} (URL={page.url!r})."
                )
        page.wait_for_timeout(800)

    # После «Войти» сессия могла уже быть — не гоняем пайплайн входа зря.
    check_targets = []
    if auth_page is not None:
        check_targets.append(auth_page)
    if page not in check_targets:
        check_targets.append(page)
    early_auth = False
    for target in check_targets:
        state = _wait_until_auth_or_signed_in(target, max_seconds=8.0)
        if state == "signed_in":
            _log("Gmail: сессия уже активна — пайплайн входа не нужен, открываем Inbox.")
            if auth_page is not None and auth_page is not page:
                page = _open_inbox_on_first_then_close_auth(
                    auth_page, page, max_seconds=max_seconds
                )
            else:
                if not _inbox_or_incoming_visible(page):
                    _goto_gmail_inbox(page)
                if _smart_features_dialog_visible(page):
                    dismiss_gmail_smart_features_if_present(page)
                _wait_gmail_inbox_ready(page, max_seconds=max_seconds)
            return
        if state == "auth":
            early_auth = True
            break

    if gmail_inbox_ready(page) or _page_looks_signed_in(page):
        _log("Gmail: уже в аккаунте — проверяем Inbox.")
        if not _inbox_or_incoming_visible(page):
            _goto_gmail_inbox(page)
        _wait_gmail_inbox_ready(page, max_seconds=max_seconds)
        return

    login_target = auth_page if auth_page is not None else page
    needs_login = early_auth or google_auth_interaction_visible(login_target)

    if not needs_login and auth_page is None:
        page.wait_for_timeout(1500)
        auth_page = _find_google_auth_page(
            page.context, base_page=page, before_pages={page}
        )
        if auth_page is not None:
            login_target = auth_page
            state = _wait_until_auth_or_signed_in(auth_page, max_seconds=8.0)
            if state == "signed_in":
                _log("Gmail: во 2-й вкладке уже вошли — открываем Inbox в 1-й.")
                page = _open_inbox_on_first_then_close_auth(
                    auth_page, page, max_seconds=max_seconds
                )
                return
            needs_login = google_auth_interaction_visible(login_target)

    # accounts.google.com без видимого UI входа часто = уже залогинены / редирект.
    if not needs_login and "accounts.google.com" in _page_url_lower(login_target):
        state = _wait_until_auth_or_signed_in(login_target, max_seconds=8.0)
        if state == "signed_in":
            _log("Gmail: редирект после accounts — уже в аккаунте.")
            if auth_page is not None and auth_page is not page:
                page = _open_inbox_on_first_then_close_auth(
                    auth_page, page, max_seconds=max_seconds
                )
            else:
                if not _inbox_or_incoming_visible(page):
                    _goto_gmail_inbox(page)
                _wait_gmail_inbox_ready(page, max_seconds=max_seconds)
            return
        needs_login = google_auth_interaction_visible(login_target)

    if needs_login:
        if login_credentials is None:
            raise RuntimeError(
                "Gmail: требуется вход в Google, но логин/пароль профиля не заданы."
            )
        _log("Gmail: запускаем стандартный пайплайн входа Google…")
        attempt_google_login_for_studio(
            login_target,
            login_credentials,
            handle_channel_switcher=False,
        )
        if auth_page is not None and auth_page is not page:
            page = _open_inbox_on_first_then_close_auth(
                auth_page, page, max_seconds=max_seconds
            )
            return

    # Логин в той же вкладке / без popup — дожимаем inbox здесь.
    if not gmail_inbox_ready(page) and not _smart_features_dialog_visible(page):
        url = _page_url_lower(page)
        if not _is_mail_google_url(url):
            _goto_gmail_inbox(page)

    if _smart_features_dialog_visible(page):
        dismiss_gmail_smart_features_if_present(page)

    _wait_gmail_inbox_ready(page, max_seconds=max_seconds)
