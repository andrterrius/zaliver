"""Подключение 2FA (Authenticator app) через Meta Accounts Center."""

from __future__ import annotations

import re
import time
from collections.abc import Callable

from zaliver.instagram_upload.logutil import emit_instagram_log, instagram_entrypoint
from zaliver.instagram_upload.register import (
    _click_by_text,
    _is_accounts_suspended,
    _navigate_page_to,
    _page_url,
    accept_instagram_cookie_consent_if_present,
    dismiss_instagram_scraping_warning_if_present,
)
from zaliver.youtube_upload.totp import get_totp_token

ACCOUNTS_CENTER_URL = (
    "https://accountscenter.instagram.com/?entry_point=app_settings"
)
PASSWORD_SECURITY_URL = "https://accountscenter.instagram.com/password_and_security/"
# CDP часто готов раньше, чем антидетект уводит вкладку с about:blank.
_BLANK_SETTLE_S = 20.0

_PASSWORD_SECURITY_RE = re.compile(
    r"Password\s+and\s+security|Пароль\s+и\s+безопасность",
    re.I,
)
_TWO_FACTOR_RE = re.compile(
    r"Two[- ]factor\s+authentication|Двухфакторная\s+аутентификация|"
    r"Двухэтапная\s+аутентификация",
    re.I,
)
_CONTINUE_RE = re.compile(r"^(Continue|Next|Продолжить|Далее)$", re.I)
_SKIP_RE = re.compile(r"^(Skip|Пропустить)$", re.I)
_NEW_PASSWORD_HEADING_RE = re.compile(
    r"создайте\s+новый\s+пароль|"
    r"create\s+(a\s+)?new\s+password",
    re.I,
)
_ENTER_CODE_BTN_RE = re.compile(
    r"^(Enter\s+code|Ввести\s+код)$",
    re.I,
)
_NEXT_RE = re.compile(r"^(Next|Далее)$", re.I)
_AUTH_APP_RE = re.compile(
    r"Authentication\s+app|Приложение\s+для\s+аутентификации|"
    r"Приложение\s+аутентификации",
    re.I,
)
_COPY_KEY_RE = re.compile(
    r"Copy\s+key|Скопировать\s+ключ|Copy\s+code|Скопировать\s+код",
    re.I,
)
_CANT_SCAN_RE = re.compile(
    r"Can.?t\s+scan|Не\s+удаётся\s+сканировать|Can.?t\s+scan\s+the\s+QR|"
    r"Enter\s+key\s+manually|Ввести\s+ключ\s+вручную",
    re.I,
)
_DONE_ONLY_RE = re.compile(r"^(Done|Готово)$", re.I)
_SUCCESS_ON_RE = re.compile(
    r"Two[- ]factor\s+authentication\s+is\s+on|"
    r"Двухфакторная\s+аутентификация\s+включена|"
    r"Двухэтапная\s+аутентификация\s+включена",
    re.I,
)
_ALREADY_ENABLED_MGMT_RE = re.compile(
    r"How you get login codes|"
    r"Add a backup method|"
    r"Authorized logins|"
    r"Trusted devices|"
    r"Как вы получаете\s+коды|"
    r"Добавить резервн|"
    r"Доверенные устройства",
    re.I,
)
_CLOSE_BTN_RE = re.compile(r"^(Close|Закрыть)$", re.I)
_EXTRA_PROTECTION_RE = re.compile(
    r"Set\s+up\s+extra\s+protection|"
    r"Add\s+an\s+extra\s+login\s+step|"
    r"extra\s+protection\s+for\s+your\s+account|"
    r"Дополнительн\w*\s+защит|"
    r"дополнительн\w*\s+шаг\s+входа|"
    r"Настройте\s+дополнительн",
    re.I,
)
_GET_STARTED_RE = re.compile(r"^(Get\s+started|Начать)$", re.I)
_CHOOSE_ACCOUNT_RE = re.compile(
    r"Choose\s+an\s+account|Выберите\s+аккаунт",
    re.I,
)
_EMAIL_CHECK_RE = re.compile(
    r"Check\s+your\s+email|"
    r"Enter\s+the\s+code\s+we\s+sent|"
    r"Two\s+Step\s+Verification|"
    r"Проверьте\s+электронную\s+почту|"
    r"Проверьте\s+(свою\s+)?почту|"
    r"Введите\s+код,\s+который\s+мы\s+отправили|"
    r"Мы\s+отправили\s+код\s+сюда|"
    r"Двухэтапн\w*\s+проверк",
    re.I,
)
_EMAIL_CODE_LABEL_RE = re.compile(
    r"^(Code|Код|Введите\s+код|Enter\s+(the\s+)?code)$",
    re.I,
)
_CODE_SCREEN_RE = re.compile(
    r"Get\s+your\s+code\s+from\s+your\s+authentication\s+app|"
    r"Enter\s+the\s+6[- ]digit\s+code|"
    r"Введите\s+6[- ]значный\s+код|"
    r"код\s+из\s+приложения",
    re.I,
)
_WRONG_CODE_RE = re.compile(
    r"This\s+code\s+isn.?t\s+right|"
    r"code\s+isn.?t\s+right|"
    r"Please\s+try\s+again|"
    r"Неверный\s+код|"
    r"код\s+неверен|"
    r"Этот\s+код\s+неверен|"
    r"Попробуйте\s+(ещё|еще)\s+раз|"
    r"Попробуйте\s+снова",
    re.I,
)
# «SBIC D4C2 UQB7 EO5J TN6D QZX2 YF5B JIIO» — группы по 4 base32.
_SPACED_SECRET_RE = re.compile(
    r"\b((?:[A-Z2-7]{4}\s+){3,}[A-Z2-7]{4})\b",
    re.I,
)
_BASE32_SECRET_RE = re.compile(r"\b([A-Z2-7]{16,64})\b")
_OTPAUTH_RE = re.compile(r"otpauth://[^\s\"'<>]+", re.I)
# Рядом с ключом кнопка «Copy key» — COPY/KEY валидны в base32 и липнут к секрету.
_COPY_UI_PHRASE_RE = re.compile(
    r"\b(Copy\s*key|Copy\s*code|Скопировать\s+ключ|Скопировать\s+код|"
    r"Copy|Копировать)\b",
    re.I,
)
_SECRET_UI_SUFFIX_RE = re.compile(
    r"(COPYKEY|COPYCODE|COPY|KEY|КОПИРОВАТЬ|КОПИР)+$",
    re.I,
)
_CODE_INPUT_SEL = (
    'input[autocomplete="one-time-code"], '
    'input[inputmode="numeric"], '
    'input[maxlength="6"], '
    'input[maxlength="8"], '
    'input[aria-label*="code" i], '
    'input[aria-label*="код" i], '
    'input[placeholder*="code" i], '
    'input[placeholder*="код" i], '
    'input[name*="code" i], '
    'input[type="text"][maxlength="6"]'
)

KEEP_PROFILE_OPEN_AFTER_IG_2FA = False


class InstagramAccountSuspendedError(RuntimeError):
    """Редирект на https://www.instagram.com/accounts/suspended/."""


class InstagramLoginRequiredError(RuntimeError):
    """Редирект на https://www.instagram.com/accounts/login/."""


def _log(message: str) -> None:
    emit_instagram_log(message, tag="[instagram-2fa]")


def _is_accounts_login(page) -> bool:
    """URL /accounts/login — нет сессии Instagram."""
    url = ""
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    return "/accounts/login" in url


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
            _log(f"Instagram 2FA: вкладка ушла с about:blank → {cur!r}")
            return cur
        try:
            page.wait_for_timeout(250)
        except Exception:
            time.sleep(0.25)
    return _page_url(page)


def _is_accounts_center_url(url: str) -> bool:
    u = (url or "").strip().lower()
    return "accountscenter.instagram.com" in u


def _ensure_accounts_center_open(page) -> None:
    """
    После launch (обычно YouTube Studio) дождаться ухода с about:blank
    и открыть Accounts Center навигацией — без start_url в launch.
    """
    url0 = _page_url(page)
    low0 = url0.lower()
    if low0 in ("about:blank", "about:srcdoc", ""):
        _log(
            "Instagram 2FA: вкладка ещё about:blank — ждём старт браузера "
            f"(до {_BLANK_SETTLE_S:.0f} с)…"
        )
        url0 = _wait_leave_about_blank(page)
        low0 = url0.lower()

    if _is_accounts_center_url(url0):
        _log(f"Instagram 2FA: уже в Accounts Center (URL={url0!r}).")
        return

    _log(
        f"Instagram 2FA: текущий URL={url0!r} — открываем Accounts Center…"
    )
    _navigate_page_to(page, ACCOUNTS_CENTER_URL, label="Instagram 2FA")


def _raise_if_instagram_session_abort(page) -> None:
    """
    suspended / login → стоп с ошибкой (тег + закрытие профиля).
    """
    url = _page_url(page)
    if _is_accounts_suspended(page):
        _log(f"Instagram 2FA: редирект на suspended — стоп (URL={url!r}).")
        raise InstagramAccountSuspendedError(
            "Instagram 2FA: аккаунт на /accounts/suspended "
            f"(URL={url!r})."
        )
    if _is_accounts_login(page):
        _log(f"Instagram 2FA: редирект на login — стоп (URL={url!r}).")
        raise InstagramLoginRequiredError(
            "Instagram 2FA: нет входа, /accounts/login "
            f"(URL={url!r})."
        )


# Старое имя — алиас для вызовов в потоке.
_raise_if_instagram_suspended = _raise_if_instagram_session_abort


def _wait_raise_if_instagram_suspended(page, *, settle_s: float = 4.0) -> None:
    """Подождать возможный редирект на suspended/login после навигации."""
    deadline = time.monotonic() + max(0.5, float(settle_s))
    while True:
        _raise_if_instagram_session_abort(page)
        if time.monotonic() >= deadline:
            return
        try:
            page.wait_for_timeout(300)
        except Exception:
            time.sleep(0.3)


def _visible(loc, *, timeout: float = 600) -> bool:
    try:
        return loc.count() > 0 and loc.first.is_visible(timeout=timeout)
    except Exception:
        return False


def _click_role_button(page, pattern: re.Pattern[str]) -> bool:
    try:
        btn = page.get_by_role("button", name=pattern).first
        if _visible(btn, timeout=1200):
            btn.click(timeout=10_000)
            return True
    except Exception:
        pass
    try:
        btn = page.locator('[role="button"]').filter(has_text=pattern).first
        if _visible(btn, timeout=800):
            btn.click(timeout=10_000)
            return True
    except Exception:
        pass
    return False


def _click_continue(page) -> bool:
    if _click_role_button(page, _CONTINUE_RE):
        _log("Instagram 2FA: нажали Continue/Next.")
        page.wait_for_timeout(900)
        return True
    if _click_by_text(page, _CONTINUE_RE, prefer_link=False):
        _log("Instagram 2FA: нажали Continue (по тексту).")
        page.wait_for_timeout(900)
        return True
    return False


def _click_enter_code(page) -> bool:
    if _click_role_button(page, _ENTER_CODE_BTN_RE):
        _log("Instagram 2FA: нажали Enter code.")
        page.wait_for_timeout(900)
        return True
    if _click_by_text(page, _ENTER_CODE_BTN_RE, prefer_link=False):
        _log("Instagram 2FA: нажали Enter code (по тексту).")
        page.wait_for_timeout(900)
        return True
    return False


def _click_next_enabled(page) -> bool:
    """Нажать Next только если кнопка не aria-disabled."""
    try:
        btn = page.get_by_role("button", name=_NEXT_RE).first
        if _visible(btn, timeout=1200):
            disabled = (btn.get_attribute("aria-disabled") or "").strip().lower()
            if disabled in ("true", "1"):
                return False
            btn.click(timeout=10_000)
            _log("Instagram 2FA: нажали Next.")
            page.wait_for_timeout(900)
            return True
    except Exception:
        pass
    try:
        btn = page.locator('[role="button"]').filter(has_text=_NEXT_RE).first
        if _visible(btn, timeout=800):
            disabled = (btn.get_attribute("aria-disabled") or "").strip().lower()
            if disabled in ("true", "1"):
                return False
            btn.click(timeout=10_000)
            _log("Instagram 2FA: нажали Next (locator).")
            page.wait_for_timeout(900)
            return True
    except Exception:
        pass
    return False


def _normalize_secret(raw: str) -> str:
    s = re.sub(r"\s+", "", (raw or "").strip().upper())
    s = s.replace("-", "").replace("=", "")
    # Срезать хвост от кнопки Copy key (COPY — валидные base32-символы).
    while True:
        cleaned = _SECRET_UI_SUFFIX_RE.sub("", s)
        if cleaned == s:
            break
        s = cleaned
    return s


def _secret_from_otpauth(uri: str) -> str:
    m = re.search(r"[?&]secret=([A-Z2-7]+)", uri or "", re.I)
    if not m:
        return ""
    return _normalize_secret(m.group(1))


def _secret_from_text(text: str) -> str:
    """Достать секрет из текста; приоритет — формат групп по 4 символа."""
    raw = (text or "").strip()
    if not raw:
        return ""
    if "otpauth://" in raw.lower():
        secret = _secret_from_otpauth(raw)
        if len(secret) >= 16:
            return secret
    # Убрать «Copy key» до матча, иначе COPY станет 9-й группой ключа.
    scrubbed = _COPY_UI_PHRASE_RE.sub(" ", raw)
    m = _SPACED_SECRET_RE.search(scrubbed.upper())
    if m:
        secret = _normalize_secret(m.group(1))
        if 16 <= len(secret) <= 64:
            return secret
    compact = _normalize_secret(scrubbed)
    m2 = _BASE32_SECRET_RE.search(compact)
    if m2:
        secret = _normalize_secret(m2.group(1))
        if 16 <= len(secret) <= 64:
            return secret
    return ""


def _extract_secret_from_page(page) -> str:
    """Достать TOTP-секрет (spaced key / otpauth / Copy key)."""
    # 1) Видимый текст диалога — ключ вида «SBIC D4C2 UQB7 …»
    try:
        body = page.locator("body").inner_text(timeout=2000) or ""
    except Exception:
        body = ""
    secret = _secret_from_text(body)
    if secret:
        return secret

    # 2) otpauth:// / длинные текстовые узлы
    try:
        html = page.content() or ""
    except Exception:
        html = ""
    for m in _OTPAUTH_RE.finditer(html):
        secret = _secret_from_otpauth(m.group(0))
        if len(secret) >= 16:
            return secret
    spaced = _SPACED_SECRET_RE.search((html or "").upper())
    if spaced:
        secret = _normalize_secret(spaced.group(1))
        if 16 <= len(secret) <= 64:
            return secret

    try:
        found = page.evaluate(
            """() => {
  const out = [];
  const walk = (root) => {
    if (!root) return;
    if (root.nodeType === 3) {
      const t = (root.textContent || '').trim();
      if (t.length >= 16) out.push(t);
      return;
    }
    if (root.getAttribute) {
      for (const a of ['href', 'data-clipboard-text', 'value', 'aria-label']) {
        const v = root.getAttribute(a);
        if (v && v.length >= 16) out.push(v);
      }
    }
    for (const c of root.childNodes || []) walk(c);
  };
  walk(document.body);
  return out.slice(0, 120);
}"""
        )
    except Exception:
        found = []

    for text in found or []:
        secret = _secret_from_text(str(text or ""))
        if secret:
            return secret

    # 3) Copy key → clipboard
    if _click_role_button(page, _COPY_KEY_RE) or _click_by_text(
        page, _COPY_KEY_RE, prefer_link=False
    ):
        _log("Instagram 2FA: нажали Copy key.")
        page.wait_for_timeout(500)
        try:
            clip = page.evaluate(
                """async () => {
  try {
    if (navigator.clipboard && navigator.clipboard.readText) {
      return await navigator.clipboard.readText();
    }
  } catch (e) {}
  return '';
}"""
            )
            secret = _secret_from_text(str(clip or ""))
            if secret:
                return secret
        except Exception as e:
            _log(f"Instagram 2FA: clipboard недоступен: {e!r}")

    return ""


def _reveal_manual_key_if_needed(page) -> None:
    if _click_role_button(page, _CANT_SCAN_RE) or _click_by_text(
        page, _CANT_SCAN_RE, prefer_link=False
    ):
        _log("Instagram 2FA: открыли ручной ввод ключа (Can't scan).")
        page.wait_for_timeout(800)


def _wait_for_generated_secret(
    page,
    *,
    deadline: float,
    login_credentials=None,
) -> str:
    """Ждать генерации ключа на экране после Continue (Authentication app)."""
    _log("Instagram 2FA: ждём генерацию ключа…")
    while time.monotonic() < deadline:
        _guard_step(page, login_credentials=login_credentials, deadline=deadline)
        _reveal_manual_key_if_needed(page)
        secret = _extract_secret_from_page(page)
        if secret and len(secret) >= 16:
            return secret
        # Кнопка Enter code уже есть — ключ должен быть рядом
        try:
            if page.get_by_role("button", name=_ENTER_CODE_BTN_RE).first.is_visible(
                timeout=300
            ):
                secret = _extract_secret_from_page(page)
                if secret:
                    return secret
        except Exception:
            pass
        page.wait_for_timeout(700)
    return ""


def _wait_for_code_input(
    page,
    *,
    deadline: float,
    login_credentials=None,
):
    """Дождаться окна ввода кода (появляется с задержкой после Enter code)."""
    _log("Instagram 2FA: ждём окно ввода кода…")
    while time.monotonic() < deadline:
        _guard_step(page, login_credentials=login_credentials, deadline=deadline)
        try:
            if page.get_by_text(_CODE_SCREEN_RE).first.is_visible(timeout=400):
                inp = page.locator(_CODE_INPUT_SEL).first
                if _visible(inp, timeout=800):
                    return inp
        except Exception:
            pass
        inp = page.locator(_CODE_INPUT_SEL).first
        if _visible(inp, timeout=400):
            return inp
        # label «Enter code» у input
        try:
            lab = page.locator('label:text-is("Enter code"), label:text-is("Ввести код")')
            if lab.count() > 0:
                for_id = lab.first.get_attribute("for") or ""
                if for_id:
                    inp2 = page.locator(f"#{for_id}")
                    if _visible(inp2, timeout=500):
                        return inp2.first
        except Exception:
            pass
        page.wait_for_timeout(500)
    return None


def _email_check_screen_visible(page) -> bool:
    try:
        if page.get_by_text(_EMAIL_CHECK_RE).first.is_visible(timeout=350):
            return True
    except Exception:
        pass
    try:
        # Bloks: <h2 aria-label="Проверьте электронную почту">
        if page.get_by_role(
            "heading",
            name=re.compile(
                r"Check\s+your\s+email|Проверьте\s+электронную\s+почту|"
                r"Проверьте\s+(свою\s+)?почту",
                re.I,
            ),
        ).first.is_visible(timeout=250):
            return True
    except Exception:
        pass
    try:
        heading = page.locator(
            'h2[aria-label*="Проверьте электронную почту" i], '
            'h2[aria-label*="Check your email" i]'
        ).first
        code_inp = page.locator(
            'input[aria-label="Введите код" i], '
            'input[aria-label="Enter code" i], '
            'input[inputmode="numeric"]'
        ).first
        if heading.count() > 0 and code_inp.count() > 0:
            return True
    except Exception:
        pass
    return False


def _new_password_screen_visible(page) -> bool:
    """Экран «Создайте новый пароль» после кода из почты (Skip / Пропустить)."""
    try:
        h = page.locator(
            'h3[aria-label*="новый пароль" i], '
            'h3[aria-label*="new password" i], '
            'h2[aria-label*="новый пароль" i], '
            'h2[aria-label*="new password" i]'
        ).first
        if h.count() > 0 and h.is_visible(timeout=300):
            return True
    except Exception:
        pass
    try:
        if page.get_by_role(
            "heading", name=_NEW_PASSWORD_HEADING_RE
        ).first.is_visible(timeout=300):
            return True
    except Exception:
        pass
    try:
        pwd = page.locator(
            'input[type="password"][aria-label*="Новый пароль" i], '
            'input[type="password"][aria-label*="New password" i]'
        ).first
        skip = page.locator(
            '[role="button"][aria-label="Пропустить" i], '
            '[role="button"][aria-label="Skip" i]'
        ).first
        if (
            pwd.count() > 0
            and pwd.is_visible(timeout=250)
            and skip.count() > 0
            and skip.is_visible(timeout=250)
        ):
            return True
    except Exception:
        pass
    return False


def _click_skip_new_password(page) -> bool:
    for sel in (
        '[role="button"][aria-label="Пропустить" i]',
        '[role="button"][aria-label="Skip" i]',
    ):
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible(timeout=400):
                btn.click(timeout=8000)
                _log("Instagram: «Создайте новый пароль» — нажали «Пропустить».")
                return True
        except Exception:
            continue
    try:
        btn = page.get_by_role("button", name=_SKIP_RE).first
        if btn.count() > 0 and btn.is_visible(timeout=400):
            btn.click(timeout=8000)
            _log("Instagram: «Создайте новый пароль» — нажали «Пропустить».")
            return True
    except Exception:
        pass
    # Bloks: клик по тексту «Пропустить».
    if _click_by_text(page, _SKIP_RE, timeout_ms=2500):
        _log("Instagram: «Создайте новый пароль» — нажали «Пропустить» (текст).")
        return True
    return False


def dismiss_new_password_screen_if_present(page) -> bool:
    """Если экран нового пароля — Skip. True если нажали."""
    if not _new_password_screen_visible(page):
        return False
    return _click_skip_new_password(page)


def _fill_meta_email_code(page, code: str) -> bool:
    code = (code or "").strip()
    if not code:
        return False
    targets = []
    try:
        targets.append(
            page.locator(
                'input[aria-label="Введите код" i], '
                'input[aria-label="Enter code" i], '
                'input[aria-label*="Введите код" i]'
            ).first
        )
    except Exception:
        pass
    try:
        targets.append(page.get_by_label(_EMAIL_CODE_LABEL_RE).first)
    except Exception:
        pass
    targets.append(page.locator('input[inputmode="numeric"]').first)
    targets.append(page.locator('input[type="text"]').first)
    targets.append(page.locator(_CODE_INPUT_SEL).first)
    for target in targets:
        try:
            if target is None:
                continue
            if hasattr(target, "count") and target.count() <= 0:
                continue
            if not _visible(target, timeout=1500):
                continue
            target.click(timeout=5000)
            target.fill("")
            target.fill(code, timeout=10_000)
            _log("Instagram 2FA: ввели код из письма Meta.")
            page.wait_for_timeout(400)
            return True
        except Exception as e:
            _log(f"Instagram 2FA: fill email code failed: {e!r}")
            continue
    return False


def _click_continue_enabled(page, *, deadline: float) -> bool:
    while time.monotonic() < deadline:
        try:
            btn = page.locator(
                '[role="button"][aria-label="Продолжить" i], '
                '[role="button"][aria-label="Continue" i]'
            ).first
            if btn.count() > 0:
                disabled = (btn.get_attribute("aria-disabled") or "").strip().lower()
                if disabled not in ("true", "1"):
                    btn.click(timeout=10_000, force=True)
                    _log("Instagram: нажали «Продолжить» (aria-label).")
                    page.wait_for_timeout(900)
                    return True
        except Exception:
            pass
        try:
            btn = page.get_by_role("button", name=_CONTINUE_RE).first
            if _visible(btn, timeout=600):
                disabled = (btn.get_attribute("aria-disabled") or "").strip().lower()
                if disabled not in ("true", "1"):
                    btn.click(timeout=10_000)
                    _log("Instagram 2FA: нажали Continue.")
                    page.wait_for_timeout(900)
                    return True
        except Exception:
            pass
        if _click_continue(page):
            return True
        page.wait_for_timeout(350)
    return False


def _open_gmail_tab(accounts_page, login_credentials=None):
    from zaliver.instagram_upload.gmail_availability import (
        force_desktop_emulation_for_page,
        verify_gmail_inbox_available,
    )

    context = accounts_page.context
    gmail = None
    try:
        gmail = context.new_page()
        _log("Instagram: открыли вкладку Gmail (desktop emulation)…")
        # Контекст может быть iPhone 12 Pro — без этого Gmail mobile ломает селекторы.
        force_desktop_emulation_for_page(gmail)
        verify_gmail_inbox_available(gmail, login_credentials=login_credentials)
        return gmail
    except Exception:
        if gmail is not None:
            try:
                if not gmail.is_closed():
                    gmail.close()
            except Exception:
                pass
        raise


def _handle_email_verification_if_needed(
    page,
    *,
    login_credentials=None,
    deadline: float,
) -> bool:
    """
    Экран «Check your email» / Two Step Verification:
    Gmail (2-я вкладка) → письмо Meta → 8-значный код → Continue.
    Returns True если экран был и обработан.
    """
    if not _email_check_screen_visible(page):
        return False

    from zaliver.instagram_upload.gmail_confirmation_code import (
        fetch_instagram_confirmation_code_from_gmail,
        fetch_meta_authenticate_code_from_gmail,
    )

    _log(
        "Instagram: экран «Проверьте электронную почту» — "
        "ждём 3 с, затем открываем Gmail…"
    )
    page.wait_for_timeout(3000)

    gmail = _open_gmail_tab(page, login_credentials)
    used: set[str] = set()
    try:
        for attempt in range(1, 4):
            wait_s = min(120.0, max(30.0, deadline - time.monotonic()))
            meta_wait = min(40.0, wait_s)
            code = ""
            try:
                code = fetch_meta_authenticate_code_from_gmail(
                    gmail,
                    max_seconds=meta_wait,
                    exclude_codes=used,
                )
            except Exception as e_meta:
                _log(
                    f"Instagram: письмо Meta не найдено ({e_meta!r}) — "
                    "пробуем код Instagram (6 цифр)…"
                )
                code = fetch_instagram_confirmation_code_from_gmail(
                    gmail,
                    max_seconds=wait_s,
                    exclude_codes=used,
                )
            used.add(code)
            try:
                page.bring_to_front()
            except Exception:
                pass
            page.wait_for_timeout(500)
            if not _email_check_screen_visible(page):
                _log("Instagram: экран почты уже сменился.")
                return True
            if not _fill_meta_email_code(page, code):
                raise RuntimeError(
                    "Instagram: не удалось ввести код из письма "
                    f"(URL={_page_url(page)!r})."
                )
            if not _click_continue_enabled(
                page, deadline=min(deadline, time.monotonic() + 15.0)
            ):
                raise RuntimeError(
                    "Instagram: не удалось нажать «Продолжить» после кода почты "
                    f"(URL={_page_url(page)!r})."
                )
            settle = min(deadline, time.monotonic() + 20.0)
            while time.monotonic() < settle:
                if not _email_check_screen_visible(page):
                    _log("Instagram: код почты принят.")
                    dismiss_new_password_screen_if_present(page)
                    return True
                if _wrong_code_visible(page):
                    _log(
                        f"Instagram: код почты {code} отклонён "
                        f"(попытка {attempt}) — ждём новое письмо…"
                    )
                    break
                page.wait_for_timeout(400)
            else:
                if not _email_check_screen_visible(page):
                    dismiss_new_password_screen_if_present(page)
                    return True
            if not _email_check_screen_visible(page):
                dismiss_new_password_screen_if_present(page)
                return True
        raise RuntimeError(
            "Instagram: код из письма не принят "
            f"(URL={_page_url(page)!r})."
        )
    finally:
        try:
            if gmail is not None and not gmail.is_closed():
                gmail.close()
                _log("Instagram: вкладка Gmail закрыта.")
        except Exception:
            pass
        try:
            page.bring_to_front()
        except Exception:
            pass
        # После закрытия Gmail экран нового пароля мог уже открыться.
        try:
            dismiss_new_password_screen_if_present(page)
        except Exception:
            pass


def _guard_step(
    page,
    *,
    login_credentials=None,
    deadline: float,
) -> bool:
    """
    Общие прерывания на любом шаге:
    suspended / login → ошибка; scraping_warning → Закрыть;
    окно почты Meta → ввод кода; «Создайте новый пароль» → Пропустить.
    Returns True если обработали почту / skip / warning.
    """
    _raise_if_instagram_suspended(page)
    if dismiss_instagram_scraping_warning_if_present(page):
        return True
    if dismiss_new_password_screen_if_present(page):
        return True
    handled = _handle_email_verification_if_needed(
        page,
        login_credentials=login_credentials,
        deadline=deadline,
    )
    if handled:
        _raise_if_instagram_suspended(page)
        dismiss_instagram_scraping_warning_if_present(page)
        dismiss_new_password_screen_if_present(page)
    return handled


def _extra_protection_intro_visible(page) -> bool:
    """Интро «Set up extra protection…» / Two factor + Get started."""
    try:
        if page.get_by_text(_EXTRA_PROTECTION_RE).first.is_visible(timeout=700):
            return True
    except Exception:
        pass
    try:
        # Заголовок диалога «Two factor» + кнопка Get started
        heading = page.get_by_role(
            "heading", name=re.compile(r"^Two\s*factor$|^Двухфактор", re.I)
        ).first
        if _visible(heading, timeout=500):
            btn = page.get_by_role("button", name=_GET_STARTED_RE).first
            if _visible(btn, timeout=500):
                return True
    except Exception:
        pass
    return False


def _click_extra_protection_get_started(page) -> bool:
    """Окно «Set up extra protection…» → Get started."""
    if not _extra_protection_intro_visible(page):
        return False
    if _click_role_button(page, _GET_STARTED_RE):
        _log("Instagram 2FA: интро 2FA — нажали Get started.")
        page.wait_for_timeout(1000)
        return True
    if _click_by_text(page, _GET_STARTED_RE, prefer_link=False):
        _log("Instagram 2FA: интро 2FA — нажали Get started (по тексту).")
        page.wait_for_timeout(1000)
        return True
    try:
        btn = page.locator('[role="button"]').filter(has_text=_GET_STARTED_RE).first
        if _visible(btn, timeout=800):
            btn.click(timeout=10_000)
            _log("Instagram 2FA: интро 2FA — нажали Get started (locator).")
            page.wait_for_timeout(1000)
            return True
    except Exception:
        pass
    return False


def _select_instagram_account_if_needed(page) -> None:
    """Если есть выбор аккаунта — кликаем первый с меткой Instagram."""
    try:
        if not page.get_by_text(_CHOOSE_ACCOUNT_RE).first.is_visible(timeout=1500):
            # Иногда заголовок другой, но список аккаунтов есть.
            pass
    except Exception:
        pass

    # Предпочитаем кнопку, в которой есть «Instagram».
    candidates = [
        page.locator('[role="button"]').filter(
            has_text=re.compile(r"Instagram", re.I)
        ),
        page.get_by_role("button").filter(has_text=re.compile(r"Instagram", re.I)),
    ]
    for loc in candidates:
        try:
            first = loc.first
            if _visible(first, timeout=1200):
                first.click(timeout=10_000)
                _log("Instagram 2FA: выбрали аккаунт Instagram.")
                page.wait_for_timeout(1000)
                return
        except Exception:
            continue

    # Fallback: первая кнопка в диалоге Two factor (кроме Close).
    try:
        dialog = page.locator('[aria-hidden="false"]').filter(
            has_text=re.compile(r"Two[- ]factor|Двухфактор", re.I)
        ).first
        if _visible(dialog, timeout=800):
            btns = dialog.locator('[role="button"]')
            for i in range(min(btns.count(), 8)):
                btn = btns.nth(i)
                label = (btn.get_attribute("aria-label") or "").strip().lower()
                text = (btn.inner_text(timeout=500) or "").strip().lower()
                if label in ("close", "закрыть", "back", "назад"):
                    continue
                if not text and not label:
                    continue
                if _GET_STARTED_RE.search(text):
                    continue
                if "instagram" in text or (text and "close" not in label):
                    btn.click(timeout=10_000)
                    _log("Instagram 2FA: выбрали первый аккаунт в диалоге.")
                    page.wait_for_timeout(1000)
                    return
    except Exception:
        pass


def _ensure_auth_app_selected(page) -> None:
    # Radio TOTP уже часто выбран; кликаем Authentication app на всякий случай.
    try:
        radio = page.locator('input[type="radio"][value="TOTP"]').first
        if radio.count() > 0:
            checked = radio.get_attribute("aria-checked") == "true" or radio.is_checked()
            if not checked:
                page.get_by_role("radio", name=_AUTH_APP_RE).first.click(timeout=5000)
                _log("Instagram 2FA: выбрали Authentication app.")
                page.wait_for_timeout(400)
            else:
                _log("Instagram 2FA: Authentication app уже выбран.")
            return
    except Exception:
        pass
    try:
        label = page.locator("label").filter(has_text=_AUTH_APP_RE).first
        if _visible(label, timeout=800):
            label.click(timeout=5000)
            _log("Instagram 2FA: клик по label Authentication app.")
            page.wait_for_timeout(400)
    except Exception:
        pass


def _fill_totp_code(page, code: str, inp=None) -> bool:
    code = (code or "").strip()
    if not code:
        return False
    targets = []
    if inp is not None:
        targets.append(inp)
    targets.append(page.locator(_CODE_INPUT_SEL).first)
    try:
        targets.append(page.get_by_label(re.compile(r"Enter\s+code|Ввести\s+код", re.I)).first)
    except Exception:
        pass
    for target in targets:
        try:
            if target is None:
                continue
            if hasattr(target, "count") and target.count() <= 0:
                continue
            if not _visible(target, timeout=1500):
                continue
            target.click(timeout=5000)
            target.fill("")
            target.fill(code, timeout=10_000)
            _log("Instagram 2FA: ввели OTP-код.")
            page.wait_for_timeout(400)
            return True
        except Exception as e:
            _log(f"Instagram 2FA: fill OTP attempt failed: {e!r}")
            continue
    return False


def _wrong_code_visible(page) -> bool:
    try:
        if page.get_by_text(_WRONG_CODE_RE).first.is_visible(timeout=500):
            return True
    except Exception:
        pass
    try:
        inp = page.locator(_CODE_INPUT_SEL).first
        if inp.count() > 0 and (inp.get_attribute("aria-invalid") or "").lower() == "true":
            return True
    except Exception:
        pass
    return False


def _success_on_visible(page) -> bool:
    try:
        return page.get_by_text(_SUCCESS_ON_RE).first.is_visible(timeout=500)
    except Exception:
        return False


def _totp_seconds_left() -> int:
    return 30 - (int(time.time()) % 30)


def _wait_next_totp_code(secret: str, previous: str = "") -> str:
    """Ждать смены 30-секундного TOTP (новый код)."""
    prev = (previous or "").strip() or get_totp_token(secret)
    _log(
        f"Instagram 2FA: ждём новый OTP "
        f"(осталось ~{_totp_seconds_left()} с в текущем окне)…"
    )
    deadline = time.monotonic() + 35.0
    while time.monotonic() < deadline:
        cur = get_totp_token(secret)
        if cur != prev:
            time.sleep(0.35)
            _log("Instagram 2FA: новый OTP готов.")
            return cur
        time.sleep(0.35)
    return get_totp_token(secret)


def _fresh_totp_code(secret: str, *, previous: str = "") -> str:
    """Взять актуальный OTP; если до смены <3 с или код тот же — дождаться нового."""
    if previous and get_totp_token(secret) == previous:
        return _wait_next_totp_code(secret, previous)
    if _totp_seconds_left() <= 3:
        return _wait_next_totp_code(secret, get_totp_token(secret))
    return get_totp_token(secret)


def _click_next_after_code(page, *, deadline: float) -> bool:
    while time.monotonic() < deadline:
        if _click_next_enabled(page):
            return True
        if _click_continue(page):
            return True
        page.wait_for_timeout(350)
    return False


def _submit_totp_with_retries(
    page,
    secret: str,
    *,
    deadline: float,
    max_attempts: int = 4,
    login_credentials=None,
) -> None:
    """
    Ввести OTP → Next. При «This code isn't right» —
    дождаться нового кода и повторить.
    """
    last_otp = ""
    for attempt in range(1, max(1, int(max_attempts)) + 1):
        _guard_step(page, login_credentials=login_credentials, deadline=deadline)
        otp = _fresh_totp_code(secret, previous=last_otp)
        last_otp = otp
        _log(f"Instagram 2FA: попытка OTP {attempt}/{max_attempts}…")
        if not _fill_totp_code(page, otp):
            raise RuntimeError(
                "Instagram 2FA: не удалось ввести OTP-код "
                f"(URL={_page_url(page)!r})."
            )
        if not _click_next_after_code(
            page, deadline=min(deadline, time.monotonic() + 15.0)
        ):
            raise RuntimeError(
                "Instagram 2FA: не удалось нажать Next после ввода кода "
                f"(URL={_page_url(page)!r})."
            )

        outcome_deadline = min(deadline, time.monotonic() + 20.0)
        while time.monotonic() < outcome_deadline:
            _guard_step(page, login_credentials=login_credentials, deadline=deadline)
            if _success_on_visible(page):
                _log("Instagram 2FA: OTP принят.")
                return
            if _wrong_code_visible(page):
                _log(
                    "Instagram 2FA: код неверный "
                    "(«This code isn't right») — ждём следующий OTP…"
                )
                last_otp = otp
                break
            page.wait_for_timeout(400)
        else:
            # Ни успех, ни явная ошибка — ещё раз проверить
            if _success_on_visible(page):
                return
            if _wrong_code_visible(page):
                _log("Instagram 2FA: код неверный — повтор с новым OTP…")
                last_otp = otp
                continue
            raise RuntimeError(
                "Instagram 2FA: после Next нет ни успеха, ни ошибки кода "
                f"(URL={_page_url(page)!r})."
            )
        # wrong code → next attempt (after waiting for new totp in _fresh_totp_code)
        continue

    raise RuntimeError(
        f"Instagram 2FA: OTP не принят после {max_attempts} попыток "
        f"(URL={_page_url(page)!r})."
    )


def _wait_success_and_click_done(
    page,
    *,
    deadline: float,
    login_credentials=None,
) -> None:
    """
    Экран «Two-factor authentication is on» → Done → пауза 1 с.
    Успех считаем только после этого.
    """
    _log("Instagram 2FA: ждём экран успешного включения…")
    seen = False
    while time.monotonic() < deadline:
        _guard_step(page, login_credentials=login_credentials, deadline=deadline)
        try:
            if page.get_by_text(_SUCCESS_ON_RE).first.is_visible(timeout=600):
                seen = True
                break
        except Exception:
            pass
        page.wait_for_timeout(400)
    if not seen:
        raise RuntimeError(
            "Instagram 2FA: не дождались экрана "
            "«Two-factor authentication is on» "
            f"(URL={_page_url(page)!r})."
        )
    _log("Instagram 2FA: 2FA успешно включена — жмём Done.")

    clicked = False
    click_deadline = min(deadline, time.monotonic() + 15.0)
    while time.monotonic() < click_deadline:
        _guard_step(page, login_credentials=login_credentials, deadline=deadline)
        if _click_role_button(page, _DONE_ONLY_RE):
            clicked = True
            break
        if _click_by_text(page, _DONE_ONLY_RE, prefer_link=False):
            clicked = True
            break
        page.wait_for_timeout(400)
    if not clicked:
        raise RuntimeError(
            "Instagram 2FA: не найдена кнопка Done на экране успеха "
            f"(URL={_page_url(page)!r})."
        )

    _log("Instagram 2FA: Done нажали, ждём 1 с перед закрытием профиля…")
    page.wait_for_timeout(1000)


def _looks_like_already_enabled(page) -> bool:
    """Экран управления уже включённой 2FA (не мастер настройки)."""
    try:
        if not page.get_by_text(_SUCCESS_ON_RE).first.is_visible(timeout=900):
            return False
    except Exception:
        return False
    try:
        if page.get_by_text(_ALREADY_ENABLED_MGMT_RE).first.is_visible(timeout=800):
            return True
    except Exception:
        pass
    # «is on» без радио выбора метода и без кнопки Done (мастер уже пройден)
    try:
        has_setup_radio = (
            page.locator('input[type="radio"][name="twoFactorMethod"]').count() > 0
        )
    except Exception:
        has_setup_radio = False
    try:
        has_done = page.get_by_role("button", name=_DONE_ONLY_RE).count() > 0
    except Exception:
        has_done = False
    if not has_setup_radio and not has_done:
        return True
    return False


def _finish_already_enabled(page) -> str:
    """2FA уже включена — успех, закрыть диалог и выйти."""
    _log("Instagram 2FA: уже включена — ставим успешный статус.")
    try:
        close = page.get_by_role("button", name=_CLOSE_BTN_RE).first
        if _visible(close, timeout=800):
            close.click(timeout=5000)
            _log("Instagram 2FA: закрыли диалог (Close).")
    except Exception:
        pass
    page.wait_for_timeout(1000)
    _log(f"Instagram 2FA: уже включена, URL={_page_url(page)!r}.")
    return ""


@instagram_entrypoint
def setup_instagram_totp_2fa(
    page,
    *,
    on_secret: Callable[[str], None] | None = None,
    login_credentials=None,
    session_login: str = "",
    session_password: str = "",
    session_twofa: str = "",
    max_seconds: float = 180.0,
    profile_id: str | None = None,
) -> str:
    """
    Accounts Center → Password and security → 2FA →
    (почта Meta может появиться на любом шаге) → Auth app →
    сохранить секрет → ввести OTP → подтвердить.

    Returns:
        Нормализованный TOTP-секрет.
    """
    from zaliver.instagram_upload.register import (
        INSTAGRAM_URL,
        accept_instagram_cookie_consent_if_present,
        ensure_instagram_session_relogin,
        _instagram_already_logged_in,
        _is_classic_login_form_visible,
        _is_instagram_url,
        _is_saved_profile_chooser_screen,
        _navigate_page_to,
        _onetap_password_visible,
    )

    deadline = time.monotonic() + max(30.0, float(max_seconds))

    def _g() -> bool:
        return _guard_step(
            page, login_credentials=login_credentials, deadline=deadline
        )

    def _needs_relogin() -> bool:
        return (
            _is_saved_profile_chooser_screen(page)
            or _onetap_password_visible(page)
            or _is_classic_login_form_visible(page)
        )

    # Разлогин на главной (не регистрация): форма / Continue → пароль → 2FA → Save.
    user_login = (session_login or "").strip()
    if not user_login and login_credentials is not None:
        user_login = str(getattr(login_credentials, "email", "") or "").strip()
    pwd = (session_password or "").strip()
    if not pwd and login_credentials is not None:
        pwd = str(getattr(login_credentials, "password", "") or "").strip()
    twofa = (session_twofa or "").strip()
    try:
        url0 = (_page_url(page) or "").lower()
        if not _is_instagram_url(url0) and "accountscenter" not in url0:
            # Могли открыть about:blank — сначала главная Instagram.
            if url0 in ("about:blank", "about:srcdoc", ""):
                _navigate_page_to(page, INSTAGRAM_URL, label="Instagram 2FA")
                accept_instagram_cookie_consent_if_present(page, appear_seconds=6.0)
                # Дать UI отрисовать chooser / форму / ленту.
                settle = time.monotonic() + 8.0
                while time.monotonic() < settle:
                    if _needs_relogin() or _instagram_already_logged_in(page):
                        break
                    page.wait_for_timeout(350)
        if _needs_relogin():
            ensure_instagram_session_relogin(
                page,
                login=user_login,
                password=pwd,
                twofa_secret=twofa,
                max_seconds=90.0,
            )
    except Exception as e:
        # Если это уже не экран разлогина — продолжим; иначе пробросим.
        if _needs_relogin():
            raise
        _log(f"Instagram 2FA: pre-check session: {e!r}")

    _log(f"Instagram 2FA: открываем Accounts Center ({ACCOUNTS_CENTER_URL})")
    _ensure_accounts_center_open(page)
    _wait_raise_if_instagram_suspended(page, settle_s=5.0)
    accept_instagram_cookie_consent_if_present(page, appear_seconds=5.0)
    _g()

    # Password and security
    opened_ps = False
    if _click_by_text(page, _PASSWORD_SECURITY_RE, prefer_link=True):
        opened_ps = True
        _log("Instagram 2FA: клик Password and security.")
    else:
        try:
            link = page.locator('a[href*="password_and_security"]').first
            if _visible(link, timeout=1500):
                link.click(timeout=10_000)
                opened_ps = True
                _log("Instagram 2FA: клик href password_and_security.")
        except Exception:
            pass
    if not opened_ps:
        _log("Instagram 2FA: прямая навигация на password_and_security…")
        _navigate_page_to(page, PASSWORD_SECURITY_URL, label="Instagram 2FA")
    page.wait_for_timeout(1000)
    _wait_raise_if_instagram_suspended(page, settle_s=4.0)
    _g()

    # Иногда сразу после Password and security — интро «Set up extra protection…»
    intro_deadline = min(deadline, time.monotonic() + 8.0)
    while time.monotonic() < intro_deadline:
        _g()
        if _click_extra_protection_get_started(page):
            break
        if not _extra_protection_intro_visible(page):
            break
        page.wait_for_timeout(400)

    _g()
    # Two-factor authentication (если интро уже увело в мастер — кнопки может не быть)
    opened_2fa = (
        _click_role_button(page, _TWO_FACTOR_RE)
        or _click_by_text(page, _TWO_FACTOR_RE, prefer_link=False)
    )
    if opened_2fa:
        _log("Instagram 2FA: открыли Two-factor authentication.")
        page.wait_for_timeout(1200)
        _wait_raise_if_instagram_suspended(page, settle_s=3.0)
        _g()
        _click_extra_protection_get_started(page)
    else:
        _g()
        _click_extra_protection_get_started(page)
        try:
            in_flow = (
                _looks_like_already_enabled(page)
                or _email_check_screen_visible(page)
                or page.locator(
                    'input[type="radio"][name="twoFactorMethod"]'
                ).count()
                > 0
                or page.get_by_text(_CHOOSE_ACCOUNT_RE).first.is_visible(timeout=500)
                or page.get_by_text(
                    re.compile(r"Help protect your account|Choose how", re.I)
                ).first.is_visible(timeout=400)
            )
        except Exception:
            in_flow = False
        _g()
        if not in_flow:
            raise RuntimeError(
                "Instagram 2FA: не найдена кнопка Two-factor authentication "
                f"(URL={_page_url(page)!r})."
            )
        _log(
            "Instagram 2FA: пункт Two-factor не нужен — уже в мастере "
            f"(URL={_page_url(page)!r})."
        )

    # Выбор аккаунта / почта Meta / уже включена / метод Auth app
    method_deadline = min(deadline, time.monotonic() + 90.0)
    while time.monotonic() < method_deadline:
        if _g():
            page.wait_for_timeout(400)
            continue
        if _click_extra_protection_get_started(page):
            page.wait_for_timeout(400)
            continue
        if _looks_like_already_enabled(page):
            return _finish_already_enabled(page)
        try:
            if page.locator('input[type="radio"][name="twoFactorMethod"]').count() > 0:
                break
            # «Authentication app» есть и на экране уже включённой 2FA —
            # не путать с мастером настройки (радио TOTP).
            if (
                page.locator('input[type="radio"][value="TOTP"]').count() > 0
                or page.get_by_text(
                    re.compile(r"Help protect your account|Choose how", re.I)
                ).first.is_visible(timeout=400)
            ):
                break
        except Exception:
            pass
        _select_instagram_account_if_needed(page)
        page.wait_for_timeout(400)

    _g()
    if _looks_like_already_enabled(page):
        return _finish_already_enabled(page)

    _g()
    if _looks_like_already_enabled(page):
        return _finish_already_enabled(page)

    _ensure_auth_app_selected(page)
    _g()
    if not _click_continue(page):
        # После Continue иногда сразу почта — обработать и повторить Continue
        if _g() and _click_continue(page):
            pass
        else:
            raise RuntimeError(
                "Instagram 2FA: не удалось нажать Continue после выбора Authentication app "
                f"(URL={_page_url(page)!r})."
            )
    _g()

    # Ждём генерацию ключа («SBIC D4C2 UQB7 …»), убираем пробелы → inst_2fa
    secret = _wait_for_generated_secret(
        page,
        deadline=min(deadline, time.monotonic() + 90.0),
        login_credentials=login_credentials,
    )
    if not secret or len(secret) < 16:
        raise RuntimeError(
            "Instagram 2FA: не дождались сгенерированного ключа "
            f"(URL={_page_url(page)!r})."
        )

    _log(f"Instagram 2FA: секрет получен (len={len(secret)}), сохраняем в inst_2fa…")
    if on_secret is not None:
        try:
            on_secret(secret)
        except Exception as e:
            _log(f"Instagram 2FA: on_secret callback failed: {e!r}")
            raise

    # Enter code → ждём окно ввода (появляется с задержкой)
    _g()
    if not _click_enter_code(page):
        # Иногда кнопка появляется чуть позже после генерации ключа
        enter_deadline = min(deadline, time.monotonic() + 20.0)
        clicked = False
        while time.monotonic() < enter_deadline:
            _g()
            if _click_enter_code(page):
                clicked = True
                break
            page.wait_for_timeout(500)
        if not clicked:
            raise RuntimeError(
                "Instagram 2FA: не найдена кнопка Enter code "
                f"(URL={_page_url(page)!r})."
            )

    code_inp = _wait_for_code_input(
        page,
        deadline=min(deadline, time.monotonic() + 45.0),
        login_credentials=login_credentials,
    )
    if code_inp is None:
        raise RuntimeError(
            "Instagram 2FA: не дождались окна ввода кода "
            f"(URL={_page_url(page)!r})."
        )

    _submit_totp_with_retries(
        page,
        secret,
        deadline=deadline,
        max_attempts=4,
        login_credentials=login_credentials,
    )

    _wait_success_and_click_done(
        page,
        deadline=min(deadline, time.monotonic() + 45.0),
        login_credentials=login_credentials,
    )

    _log(f"Instagram 2FA: подключение завершено, URL={_page_url(page)!r}.")
    return secret
