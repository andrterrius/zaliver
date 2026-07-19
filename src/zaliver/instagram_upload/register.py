"""Регистрация аккаунта Instagram (после входа в Gmail)."""

from __future__ import annotations

import base64
import calendar
import random
import re
import time

from zaliver.youtube_upload.google_login import (
    GoogleLoginCredentials,
    random_birthday,
)
from zaliver.youtube_upload import studio as _studio
from zaliver.antydetect.profile_tags import IG_REGISTER_SMS_ERROR_TAG

INSTAGRAM_URL = "https://www.instagram.com/"

# При ошибке регистрации оставлять профиль открытым (ручная капча / отладка).
# При успехе профиль всегда закрывается автоматически.
KEEP_PROFILE_OPEN_AFTER_IG_REGISTER = True


class InstagramSmsCaptchaError(RuntimeError):
    """Циферная image-капча (/accounts/suspended) — авторег не продолжаем."""

    def __init__(self, detail: str = "") -> None:
        base = IG_REGISTER_SMS_ERROR_TAG
        msg = f"{base}: {detail}" if (detail or "").strip() else base
        super().__init__(msg)

    @classmethod
    def matches(cls, err: str) -> bool:
        text = err or ""
        return IG_REGISTER_SMS_ERROR_TAG in text


class InstagramAlreadyLoggedInError(RuntimeError):
    """В профиле уже выполнен вход в Instagram — регистрация не нужна."""

    def __init__(self, username: str = "", detail: str = "") -> None:
        self.username = (username or "").strip().lstrip("@")
        bits = ["Instagram: уже выполнен вход в аккаунт"]
        if self.username:
            bits.append(f"(@{self.username})")
        if (detail or "").strip():
            bits.append(f"— {detail.strip()}")
        super().__init__(" ".join(bits))


def abort_if_instagram_sms_image_captcha(page) -> None:
    """Если циферная капча / suspended — сразу ошибка с тегом SMS."""
    if _is_accounts_suspended(page) or _is_image_captcha_screen(page):
        url = ""
        try:
            url = page.url or ""
        except Exception:
            url = ""
        _log(
            f"Instagram: image/SMS captcha — авторег остановлен "
            f"({IG_REGISTER_SMS_ERROR_TAG}), URL={url!r}"
        )
        raise InstagramSmsCaptchaError(f"URL={url!r}")

_CREATE_ACCOUNT_RE = re.compile(
    r"создать\s+новый\s+аккаунт|create\s+(new\s+)?account|sign\s*up",
    re.IGNORECASE,
)
_SUBMIT_RE = re.compile(r"^отправить$|^submit$|^sign\s*up$", re.IGNORECASE)
_NEXT_RE = re.compile(r"^далее$|^next$", re.IGNORECASE)
_CONTINUE_RE = re.compile(r"^продолжить$|^continue$", re.IGNORECASE)
_LOG_IN_RE = re.compile(r"^войти$|^log\s*in$", re.IGNORECASE)
_SAVE_INFO_RE = re.compile(
    r"^save\s*info$|^сохранить(\s+данные)?$|^сохранить\s+информацию$",
    re.IGNORECASE,
)
_SAVE_LOGIN_INFO_HEADING_RE = re.compile(
    r"save\s+your\s+login\s+info|"
    r"сохранить\s+(данные\s+для\s+)?входа|"
    r"сохранить\s+информацию\s+для\s+входа",
    re.IGNORECASE,
)
_I_AGREE_RE = re.compile(
    r"^i\s+agree$|^я\s+согласен[а]?$|^принимаю$|^согласен[а]?$",
    re.IGNORECASE,
)
_TERMS_HEADING_RE = re.compile(
    r"to\s+sign\s+up,?\s+read\s+and\s+agree\s+to\s+our\s+terms|"
    r"прочитайте\s+и\s+примите\s+(наши\s+)?условия|"
    r"ознакомьтесь\s+и\s+примите\s+(наши\s+)?условия",
    re.IGNORECASE,
)
_ALLOW_ALL_COOKIES_RE = re.compile(
    r"^allow\s+all\s+cookies$|"
    r"^разрешить\s+все\s+(файлы\s+)?cookie[s]?$|"
    r"^принять\s+все\s+(файлы\s+)?cookie[s]?$|"
    r"^разрешить\s+все$",
    re.IGNORECASE,
)
_COOKIE_CONSENT_HEADING_RE = re.compile(
    r"allow\s+the\s+use\s+of\s+cookies|"
    r"разрешить\s+использование\s+(файлов\s+)?cookie|"
    r"разрешить\s+использование\s+файлов\s+cookie\s+от\s+instagram|"
    r"использовать\s+файлы\s+cookie|"
    r"cookie[s]?\s+(by|from)\s+instagram|"
    r"cookies?\s+from\s+instagram\s+on\s+this\s+browser",
    re.IGNORECASE,
)
_CONFIRM_CODE_HEADING_RE = re.compile(
    r"введите\s+код\s+подтверждения|enter\s+(the\s+)?confirmation\s+code|"
    r"enter\s+the\s+code",
    re.IGNORECASE,
)
_HUMAN_CONFIRM_RE = re.compile(
    r"confirm\s+you.?re\s+human|"
    r"подтвердите[,\s]+что\s+вы\s+человек|"
    r"подтвердите[,\s]+что\s+вы\s+не\s+робот",
    re.IGNORECASE,
)
_IMAGE_CAPTCHA_PLACEHOLDER_RE = re.compile(
    r"enter\s+the\s+code\s+from\s+the\s+image|"
    r"введите\s+код\s+с\s+картинки|"
    r"введите\s+текст\s+с\s+изображения",
    re.IGNORECASE,
)
_USERNAME_TAKEN_RE = re.compile(
    r"занято|is\s+taken|not\s+available|уже\s+используется|username.*unavailable",
    re.IGNORECASE,
)
_MONTH_NAMES_RU = (
    "",
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)

_SIGNUP_READY_MAX_S = 90.0
_CAPTCHA_OR_CODE_WAIT_MAX_S = 120.0
_CAPTCHA_FULLY_LOADED_MAX_S = 60.0
# Пауза после «iframe капчи появился» перед createTask в RuCaptcha.
_CAPTCHA_IFRAME_SETTLE_S = 10.0
_CONFIRM_SCREEN_MAX_S = 90.0
_HOME_AFTER_CODE_MAX_S = 60.0
_MANUAL_CAPTCHA_LOG_EVERY_S = 30.0


def _log(message: str) -> None:
    _studio._log(f"[instagram] {message}")


def email_local_part(email: str) -> str:
    raw = (email or "").strip()
    if "@" in raw:
        return raw.split("@", 1)[0].strip()
    return raw


def _click_by_text(page, pattern: re.Pattern[str], *, prefer_link: bool = True) -> bool:
    candidates = []
    if prefer_link:
        candidates.append(page.get_by_role("link", name=pattern))
        candidates.append(
            page.locator('a[aria-label*="Создать" i], a[aria-label*="Create" i]')
        )
    candidates.extend(
        [
            page.get_by_role("button", name=pattern),
            page.locator("a").filter(has_text=pattern),
            page.locator('[role="button"]').filter(has_text=pattern),
            page.locator("span").filter(has_text=pattern),
        ]
    )
    for loc in candidates:
        try:
            target = loc.first
            if target.count() <= 0:
                continue
            if not target.is_visible(timeout=800):
                continue
            try:
                clickable = target.locator(
                    "xpath=ancestor-or-self::a[1] | ancestor-or-self::*[@role='button'][1]"
                ).first
                if clickable.count() > 0 and clickable.is_visible(timeout=200):
                    target = clickable
            except Exception:
                pass
            target.click(timeout=8000)
            return True
        except Exception:
            continue
    return False


def _fill_labeled_input(page, label_patterns: list[str], value: str) -> None:
    """Заполнить input рядом с label/placeholder по тексту (RU/EN)."""
    value = value or ""
    for label in label_patterns:
        pat = re.compile(re.escape(label), re.IGNORECASE)
        # label[for] → input#id
        try:
            lab = page.locator("label").filter(has_text=pat).first
            if lab.count() > 0 and lab.is_visible(timeout=500):
                for_id = lab.get_attribute("for") or ""
                if for_id:
                    inp = page.locator(f"#{for_id}")
                    if inp.count() > 0:
                        inp.first.fill(value, timeout=10_000)
                        return
                # input внутри label
                inp = lab.locator("input").first
                if inp.count() > 0:
                    inp.fill(value, timeout=10_000)
                    return
        except Exception:
            pass
        # placeholder / aria-label
        try:
            inp = page.get_by_placeholder(pat).first
            if inp.count() > 0 and inp.is_visible(timeout=400):
                inp.fill(value, timeout=10_000)
                return
        except Exception:
            pass
        try:
            inp = page.locator(
                f'input[aria-label*="{label}" i], input[placeholder*="{label}" i]'
            ).first
            if inp.count() > 0 and inp.is_visible(timeout=400):
                inp.fill(value, timeout=10_000)
                return
        except Exception:
            pass
    raise RuntimeError(f"Instagram: не найдено поле ввода ({label_patterns!r})")


def _select_combobox_option(page, aria_patterns: list[str], option_texts: list[str]) -> None:
    combo = None
    for aria in aria_patterns:
        pat = re.compile(aria, re.IGNORECASE)
        try:
            loc = page.get_by_role("combobox", name=pat).first
            if loc.count() > 0 and loc.is_visible(timeout=600):
                combo = loc
                break
        except Exception:
            continue
    if combo is None:
        raise RuntimeError(f"Instagram: не найден combobox ({aria_patterns!r})")

    combo.click(timeout=10_000)
    page.wait_for_timeout(400)

    for text in option_texts:
        if not text:
            continue
        opt_pat = re.compile(rf"^{re.escape(text)}$", re.IGNORECASE)
        try:
            opt = page.get_by_role("option", name=opt_pat).first
            if opt.count() > 0 and opt.is_visible(timeout=1500):
                opt.click(timeout=10_000)
                page.wait_for_timeout(300)
                return
        except Exception:
            pass
        try:
            opt = page.locator('[role="option"]').filter(has_text=opt_pat).first
            if opt.count() > 0 and opt.is_visible(timeout=800):
                opt.click(timeout=10_000)
                page.wait_for_timeout(300)
                return
        except Exception:
            pass
    raise RuntimeError(
        f"Instagram: не выбрана опция {option_texts!r} в combobox {aria_patterns!r}"
    )


def _fill_birthday(page) -> tuple[int, int, int]:
    day, month, year = random_birthday()
    _log(f"Instagram: дата рождения {day:02d}.{month:02d}.{year}")
    month_ru = _MONTH_NAMES_RU[month]
    month_en = calendar.month_name[month]
    _select_combobox_option(
        page,
        [r"день|day|выберите\s+день"],
        [str(day)],
    )
    _select_combobox_option(
        page,
        [r"месяц|month|выберите\s+месяц"],
        [month_ru, month_en, str(month)],
    )
    _select_combobox_option(
        page,
        [r"год|year|выберите\s+год"],
        [str(year)],
    )
    return day, month, year


def _username_taken_visible(page) -> bool:
    try:
        loc = page.get_by_text(_USERNAME_TAKEN_RE).first
        return loc.count() > 0 and loc.is_visible(timeout=500)
    except Exception:
        return False


def _is_accounts_suspended(page) -> bool:
    """URL /accounts/suspended/ — экран циферной image-капчи Instagram."""
    url = ""
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    return "/accounts/suspended" in url


def _signup_form_visible(page) -> bool:
    """Форма регистрации видна / URL emailsignup без suspended."""
    try:
        if page.get_by_text(
            re.compile(r"зарегистрируйтесь|sign\s*up\s+for\s+instagram", re.I)
        ).first.is_visible(timeout=400):
            return True
    except Exception:
        pass
    url = ""
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    if "/accounts/emailsignup" in url and "/accounts/suspended" not in url:
        return True
    return False


_EXTRACT_LOGGED_IN_USERNAME_JS = """
() => {
  const reserved = new Set([
    'explore', 'reels', 'direct', 'accounts', 'stories', 'about', 'legal',
    'privacy', 'terms', 'p', 'tv', 'reel', 'share', 'nametag', 'directory',
    'web', 'developer', 'graphql', 'api', 'static', 'lite', 'push', 'emailsignup',
    'popular', 'tagged', 'followers', 'following', 'saved', 'liked', 'guide',
    'live', 'shop', 'shopping', 'challenge', 'locations', 'devtools', 'inbox',
    'notifications', 'activity', 'create', 'home', 'search', 'settings',
  ]);
  const pick = (href) => {
    if (!href) return '';
    try {
      const u = new URL(href, location.origin);
      const m = (u.pathname || '').match(/^\\/([A-Za-z0-9._]{2,30})\\/?$/);
      if (!m) return '';
      const name = m[1];
      if (reserved.has(name.toLowerCase())) return '';
      return name;
    } catch (e) {
      return '';
    }
  };

  // Только ссылка профиля в навигации (aria-label Profile / Профиль),
  // не случайные /popular/ и чужие карточки в ленте.
  const profileSels = [
    'a[aria-label="Profile" i]',
    'a[aria-label="Профиль" i]',
    'a[role="link"][aria-label*="Profile" i]',
    'a[role="link"][aria-label*="Профиль" i]',
    'span[aria-label="Profile" i] a',
    'span[aria-label="Профиль" i] a',
  ];
  for (const sel of profileSels) {
    for (const a of Array.from(document.querySelectorAll(sel))) {
      const name = pick(a.getAttribute('href') || a.href || '');
      if (name) return name;
      const nested = a.querySelector('a[href^="/"]');
      if (nested) {
        const n2 = pick(nested.getAttribute('href') || nested.href || '');
        if (n2) return n2;
      }
    }
  }

  // svg Profile → ближайший a[href]
  for (const svg of Array.from(
    document.querySelectorAll('svg[aria-label="Profile"], svg[aria-label="Профиль"]')
  )) {
    let el = svg;
    for (let i = 0; i < 6 && el; i++) {
      if (el.tagName === 'A') {
        const name = pick(el.getAttribute('href') || el.href || '');
        if (name) return name;
        break;
      }
      el = el.parentElement;
    }
  }
  return '';
}
"""


def _extract_logged_in_username(page) -> str:
    """Попробовать вытащить ник из пункта Profile в навигации (не из ленты)."""
    try:
        name = page.evaluate(_EXTRACT_LOGGED_IN_USERNAME_JS)
        if isinstance(name, str) and name.strip():
            return name.strip().lstrip("@")
    except Exception:
        pass
    return ""


def _instagram_login_form_visible(page) -> bool:
    """Экран входа (ещё не залогинены)."""
    try:
        if page.get_by_text(
            re.compile(
                r"^войти$|^log\s*in$|phone\s+number,\s+username,\s+or\s+email|"
                r"номер\s+телефона,\s+имя\s+пользователя\s+или\s+эл",
                re.I,
            )
        ).first.is_visible(timeout=300):
            return True
    except Exception:
        pass
    url = ""
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    return "/accounts/login" in url


def _instagram_session_cookie_present(page) -> bool:
    """Настоящая сессия: cookie sessionid / ds_user_id."""
    try:
        try:
            cookies = page.context.cookies(["https://www.instagram.com"])
        except TypeError:
            cookies = page.context.cookies()
        except Exception:
            cookies = page.context.cookies()
    except Exception:
        return False
    names = {}
    for c in cookies or []:
        if not isinstance(c, dict):
            continue
        n = (c.get("name") or "").strip().lower()
        v = (c.get("value") or "").strip()
        if n and v:
            names[n] = v
    sid = names.get("sessionid") or ""
    if sid and sid not in ("0", '""', "null"):
        return True
    ds = names.get("ds_user_id") or ""
    return bool(ds and ds.isdigit())


def _instagram_logged_in_nav_visible(page) -> bool:
    """UI залогиненного приложения (Direct / Profile / Home+Create)."""
    strong = (
        'a[href="/direct/inbox/"], a[href*="/direct/inbox"]',
        'svg[aria-label="Profile"], svg[aria-label="Профиль"]',
        'a[aria-label="Profile" i], a[aria-label="Профиль" i]',
    )
    for sel in strong:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=250):
                return True
        except Exception:
            continue
    # Home + Create вместе — тоже сильный сигнал.
    home_ok = False
    create_ok = False
    try:
        h = page.locator(
            'svg[aria-label="Home"], svg[aria-label="Главная"]'
        ).first
        home_ok = h.count() > 0 and h.is_visible(timeout=200)
    except Exception:
        pass
    try:
        c = page.locator(
            'svg[aria-label="New post"], svg[aria-label="Create"], '
            'svg[aria-label="Создать"]'
        ).first
        create_ok = c.count() > 0 and c.is_visible(timeout=200)
    except Exception:
        pass
    return bool(home_ok and create_ok)


def _instagram_already_logged_in(page) -> bool:
    """
    True, если в профиле уже открыта лента / UI залогиненного Instagram
    (signup редиректит на главную).

    Не опираемся на случайные /username/ из ленты (типа /popular/).
    """
    if _signup_form_visible(page):
        return False
    if _instagram_login_form_visible(page):
        return False
    url = ""
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    if "instagram.com" not in url:
        return False
    if any(
        x in url
        for x in (
            "/accounts/emailsignup",
            "/accounts/signup",
            "/accounts/login",
            "/accounts/suspended",
            "chrome-error://",
        )
    ):
        return False

    if _instagram_session_cookie_present(page):
        return True
    return _instagram_logged_in_nav_visible(page)


def _raise_if_already_logged_in(page) -> None:
    if not _instagram_already_logged_in(page):
        return
    username = _extract_logged_in_username(page)
    url = ""
    try:
        url = page.url or ""
    except Exception:
        url = ""
    _log(
        "Instagram: обнаружен уже выполненный вход"
        + (f" (@{username})" if username else " (ник не определён)")
        + f", URL={url!r}"
    )
    raise InstagramAlreadyLoggedInError(username=username, detail=f"URL={url!r}")


def _is_saved_profile_chooser_screen(page) -> bool:
    """
    Экран «Continue <username>» / сохранённый профиль в браузере
    (рядом часто Create new account / Use another profile).
    """
    try:
        btn = page.locator(
            '[role="button"][aria-label^="Continue " i], '
            '[role="button"][aria-label^="Продолжить " i]'
        ).first
        if btn.count() > 0 and btn.is_visible(timeout=400):
            return True
    except Exception:
        pass
    try:
        cont = page.locator('[role="button"]').filter(has_text=_CONTINUE_RE).first
        create = page.locator(
            'a[aria-label*="Create new account" i], '
            'a[aria-label*="Create new account"], '
            'a[href*="emailsignup"]'
        ).first
        if (
            cont.count() > 0
            and cont.is_visible(timeout=300)
            and create.count() > 0
            and create.is_visible(timeout=300)
        ):
            return True
    except Exception:
        pass
    return False


def _extract_saved_profile_username(page) -> str:
    """Ник с экрана сохранённого профиля (Continue <user> / текст под аватаром)."""
    try:
        btn = page.locator(
            '[role="button"][aria-label^="Continue " i], '
            '[role="button"][aria-label^="Продолжить " i]'
        ).first
        if btn.count() > 0:
            label = (btn.get_attribute("aria-label") or "").strip()
            for prefix in ("Continue ", "Продолжить "):
                if label.lower().startswith(prefix.lower()):
                    name = label[len(prefix) :].strip().lstrip("@")
                    if name and re.fullmatch(r"[A-Za-z0-9._]{2,30}", name):
                        return name
    except Exception:
        pass
    return ""


def _click_saved_profile_continue(page) -> bool:
    """Нажать Continue на экране сохранённого профиля."""
    try:
        btn = page.locator(
            '[role="button"][aria-label^="Continue " i], '
            '[role="button"][aria-label^="Продолжить " i]'
        ).first
        if btn.count() > 0 and btn.is_visible(timeout=800):
            btn.click(timeout=8000)
            return True
    except Exception:
        pass
    # Точный Continue рядом с аватаром профиля (не intro «Confirm you're human»).
    try:
        btn = page.locator(
            '[role="button"][aria-label*="Continue" i], '
            '[role="button"][aria-label*="Продолжить" i]'
        ).filter(has_text=_CONTINUE_RE).first
        if btn.count() > 0 and btn.is_visible(timeout=500):
            btn.click(timeout=8000)
            return True
    except Exception:
        pass
    return _click_by_text(page, _CONTINUE_RE, prefer_link=False)


def _onetap_password_visible(page) -> bool:
    try:
        inp = page.locator('input[type="password"][name="pass"]').first
        if inp.count() > 0 and inp.is_visible(timeout=400):
            return True
    except Exception:
        pass
    try:
        inp = page.locator('input[type="password"]').first
        if inp.count() > 0 and inp.is_visible(timeout=300):
            return True
    except Exception:
        pass
    return False


def _fill_onetap_password(page, password: str) -> None:
    password = password or ""
    if not password:
        raise RuntimeError("Instagram: пустой yt_password для входа в сохранённый профиль.")
    candidates = (
        page.locator('input[type="password"][name="pass"]').first,
        page.get_by_label(re.compile(r"^password$|^пароль$", re.I)).first,
        page.locator('input[type="password"]').first,
    )
    last_err: Exception | None = None
    for inp in candidates:
        try:
            if inp.count() <= 0 or not inp.is_visible(timeout=500):
                continue
            inp.fill(password, timeout=10_000)
            return
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(
        "Instagram: не найдено поле Password на экране сохранённого профиля"
        + (f": {last_err!r}" if last_err else "")
    )


def _click_onetap_log_in(page) -> bool:
    try:
        btn = page.locator('[role="button"]').filter(has_text=_LOG_IN_RE).first
        if btn.count() > 0 and btn.is_visible(timeout=800):
            disabled = (btn.get_attribute("aria-disabled") or "").lower() == "true"
            if not disabled:
                btn.click(timeout=8000)
                return True
    except Exception:
        pass
    return _click_by_text(page, _LOG_IN_RE, prefer_link=False)


def _is_save_login_info_screen(page) -> bool:
    try:
        heading = page.get_by_text(_SAVE_LOGIN_INFO_HEADING_RE).first
        if heading.count() > 0 and heading.is_visible(timeout=400):
            return True
    except Exception:
        pass
    try:
        btn = page.get_by_role("button", name=_SAVE_INFO_RE).first
        if btn.count() > 0 and btn.is_visible(timeout=300):
            return True
    except Exception:
        pass
    return False


def _click_save_login_info(page) -> bool:
    """На экране «Save your login info?» нажать Save info."""
    try:
        btn = page.get_by_role("button", name=_SAVE_INFO_RE).first
        if btn.count() > 0 and btn.is_visible(timeout=800):
            btn.click(timeout=8000)
            return True
    except Exception:
        pass
    try:
        btn = page.locator("button").filter(has_text=_SAVE_INFO_RE).first
        if btn.count() > 0 and btn.is_visible(timeout=500):
            btn.click(timeout=8000)
            return True
    except Exception:
        pass
    return _click_by_text(page, _SAVE_INFO_RE, prefer_link=False)


def try_instagram_saved_profile_login(page, password: str) -> str | None:
    """
    Экран сохранённого профиля: Continue → пароль (yt_password) → Log in → Save info.

    Returns:
        username при успехе, иначе None (экрана нет / не удалось).
    """
    if not _is_saved_profile_chooser_screen(page):
        return None

    username = _extract_saved_profile_username(page)
    _log(
        "Instagram: экран сохранённого профиля"
        + (f" (@{username})" if username else "")
        + " — жмём Continue…"
    )
    if not _click_saved_profile_continue(page):
        _log("Instagram: не удалось нажать Continue на сохранённом профиле.")
        return None

    page.wait_for_timeout(800)
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if _onetap_password_visible(page):
            break
        if _instagram_already_logged_in(page):
            _log("Instagram: после Continue сразу вошли без пароля.")
            return username or _extract_logged_in_username(page) or "saved_profile"
        page.wait_for_timeout(400)
    else:
        _log("Instagram: поле пароля после Continue не появилось.")
        return None

    _log("Instagram: вводим yt_password на экране сохранённого профиля…")
    try:
        _fill_onetap_password(page, password)
    except Exception as e:
        _log(f"Instagram: не удалось ввести пароль: {e!r}")
        return None
    page.wait_for_timeout(400)

    if not _click_onetap_log_in(page):
        _log("Instagram: не удалось нажать Log in.")
        return None
    _log("Instagram: нажали Log in.")
    page.wait_for_timeout(1000)

    # Save your login info? → Save info
    save_deadline = time.monotonic() + 25.0
    while time.monotonic() < save_deadline:
        if _is_save_login_info_screen(page):
            if _click_save_login_info(page):
                _log("Instagram: Save your login info — нажали Save info.")
                page.wait_for_timeout(1000)
            break
        if _instagram_already_logged_in(page):
            break
        page.wait_for_timeout(400)

    # Дождаться ленты / сессии.
    home_deadline = time.monotonic() + 45.0
    while time.monotonic() < home_deadline:
        if _is_save_login_info_screen(page):
            _click_save_login_info(page)
            page.wait_for_timeout(800)
            continue
        if _instagram_already_logged_in(page):
            uname = (
                username
                or _extract_logged_in_username(page)
                or "saved_profile"
            )
            _log(f"Instagram: вход через сохранённый профиль успешен (@{uname}).")
            return uname
        page.wait_for_timeout(500)

    _log("Instagram: после Log in лента не открылась.")
    return None


def _wait_signup_form(
    page,
    *,
    max_seconds: float = _SIGNUP_READY_MAX_S,
    rucaptcha_api_key: str = "",
    on_manual_captcha=None,
) -> None:
    del rucaptcha_api_key, on_manual_captcha  # больше не решаем image captcha
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        if _signup_form_visible(page):
            return
        _raise_if_already_logged_in(page)
        abort_if_instagram_sms_image_captcha(page)
        page.wait_for_timeout(400)
    # Перед таймаутом — ещё раз: часто уже лента, а не «форма не открылась».
    _raise_if_already_logged_in(page)
    raise RuntimeError(
        f"Instagram: форма регистрации не открылась за {max_seconds:.0f} с "
        f"(URL={page.url!r})"
    )


def _is_confirmation_code_screen(page) -> bool:
    """Уже экран ввода кода из почты (капчи может не быть)."""
    try:
        heading = page.get_by_text(_CONFIRM_CODE_HEADING_RE).first
        if heading.count() > 0 and heading.is_visible(timeout=300):
            return True
    except Exception:
        pass
    try:
        field = page.locator('input[maxlength="6"]').first
        if field.count() > 0 and field.is_visible(timeout=300):
            return True
    except Exception:
        pass
    return False


def _is_terms_agree_screen(page) -> bool:
    """Экран «To sign up, read and agree to our terms» с кнопкой I agree."""
    try:
        heading = page.get_by_text(_TERMS_HEADING_RE).first
        if heading.count() > 0 and heading.is_visible(timeout=300):
            return True
    except Exception:
        pass
    # Кнопка именно «I agree» (не футер «you agree to…» на форме signup).
    try:
        btn = page.locator('[role="button"]').filter(has_text=_I_AGREE_RE).first
        if btn.count() > 0 and btn.is_visible(timeout=300):
            return True
    except Exception:
        pass
    return False


def click_instagram_terms_agree(page, *, max_seconds: float = 20.0) -> bool:
    """
    Нажать «I agree» на экране условий регистрации.
    True если кликнули (или экрана уже нет).
    """
    if not _is_terms_agree_screen(page):
        return False

    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        if not _is_terms_agree_screen(page):
            return True
        if _click_by_text(page, _I_AGREE_RE, prefer_link=False):
            _log("Instagram: экран условий — нажали «I agree».")
            page.wait_for_timeout(800)
            return True
        try:
            btn = page.locator('[role="button"]').filter(has_text=_I_AGREE_RE).first
            if btn.count() > 0 and btn.is_visible(timeout=400):
                btn.click(timeout=8000)
                _log("Instagram: экран условий — нажали «I agree» (role=button).")
                page.wait_for_timeout(800)
                return True
        except Exception:
            pass
        page.wait_for_timeout(400)

    _log("Instagram: экран условий виден, но «I agree» не нажалась.")
    return False


def accept_instagram_terms_if_present(page, *, max_seconds: float = 20.0) -> bool:
    """Если есть экран согласия с условиями — нажать I agree. True если обработали."""
    if not _is_terms_agree_screen(page):
        return False
    _log("Instagram: экран «agree to our terms».")
    return click_instagram_terms_agree(page, max_seconds=max_seconds)


def _is_cookie_consent_url(url: str) -> bool:
    u = (url or "").strip().lower()
    return "instagram.com" in u and "/consent" in u


def _is_cookie_consent_screen(page) -> bool:
    """Диалог cookies: EN Allow all / RU «Разрешить все cookie» (_abdc / /consent)."""
    try:
        if _is_cookie_consent_url(_page_url(page)):
            return True
    except Exception:
        pass
    try:
        heading = page.get_by_text(_COOKIE_CONSENT_HEADING_RE).first
        if heading.count() > 0 and heading.is_visible(timeout=300):
            return True
    except Exception:
        pass
    for sel in (
        'button:has-text("Разрешить все cookie")',
        'button:has-text("Allow all cookies")',
        "button._a9--._asz1",
        '[role="button"]',
    ):
        try:
            btn = page.locator(sel).filter(has_text=_ALLOW_ALL_COOKIES_RE).first
            if btn.count() > 0 and btn.is_visible(timeout=300):
                return True
        except Exception:
            continue
    try:
        # Старый баннер Instagram (класс _abdc) с кнопками _a9--.
        banner = page.locator("div._abdc").first
        if banner.count() > 0 and banner.is_visible(timeout=300):
            btn = banner.locator("button").filter(has_text=_ALLOW_ALL_COOKIES_RE).first
            if btn.count() > 0 and btn.is_visible(timeout=200):
                return True
            if banner.get_by_text(_COOKIE_CONSENT_HEADING_RE).count() > 0:
                return True
    except Exception:
        pass
    try:
        dialog = page.locator('[role="dialog"][aria-modal="true"]').filter(
            has_text=_COOKIE_CONSENT_HEADING_RE
        ).first
        if dialog.count() > 0 and dialog.is_visible(timeout=300):
            return True
    except Exception:
        pass
    return False


def _click_allow_all_cookies_button(page) -> bool:
    """Клик по Accept all: div[role=button] или <button class=_a9-->."""
    candidates = (
        page.get_by_role("button", name=_ALLOW_ALL_COOKIES_RE),
        page.locator("div._abdc button").filter(has_text=_ALLOW_ALL_COOKIES_RE),
        page.locator("button._a9--").filter(has_text=_ALLOW_ALL_COOKIES_RE),
        page.locator("button").filter(has_text=_ALLOW_ALL_COOKIES_RE),
        page.locator('[role="button"]').filter(has_text=_ALLOW_ALL_COOKIES_RE),
    )
    for loc in candidates:
        try:
            btn = loc.first
            if btn.count() <= 0:
                continue
            if not btn.is_visible(timeout=400):
                continue
            btn.click(timeout=8000)
            return True
        except Exception:
            continue
    return _click_by_text(page, _ALLOW_ALL_COOKIES_RE, prefer_link=False)


def click_instagram_allow_all_cookies(page, *, max_seconds: float = 20.0) -> bool:
    """
    Нажать «Allow all cookies» / «Разрешить все cookie».
    True если кликнули или диалог уже исчез.
    """
    if not _is_cookie_consent_screen(page):
        return False

    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        if not _is_cookie_consent_screen(page):
            return True
        if _click_allow_all_cookies_button(page):
            _log("Instagram: cookie consent — нажали «Allow all cookies».")
            page.wait_for_timeout(800)
            # После accept обычно редирект с /consent на главную.
            settle = time.monotonic() + 12.0
            while time.monotonic() < settle:
                if not _is_cookie_consent_screen(page):
                    _log(f"Instagram: после cookies URL={_page_url(page)!r}")
                    return True
                try:
                    page.wait_for_timeout(400)
                except Exception:
                    break
            return True
        page.wait_for_timeout(400)

    _log("Instagram: cookie consent виден, но «Allow all cookies» не нажалась.")
    return False


def accept_instagram_cookie_consent_if_present(
    page, *, max_seconds: float = 20.0
) -> bool:
    """Если есть /consent или диалог cookies — Accept all. True если обработали."""
    if not _is_cookie_consent_screen(page):
        return False
    _log("Instagram: экран согласия на cookies.")
    return click_instagram_allow_all_cookies(page, max_seconds=max_seconds)


def _captcha_iframe_visible(page) -> bool:
    try:
        frame = page.locator(
            'iframe#captcha-recaptcha, iframe[src*="captcha"], '
            'iframe[src*="recaptcha"], iframe[src*="referer_frame"]'
        ).first
        return frame.count() > 0 and frame.is_visible(timeout=500)
    except Exception:
        return False


# Готовность виджета внутри фрейма (не только наличие iframe в DOM).
_CAPTCHA_READY_JS = """
() => {
  const report = {
    hasSizedIframe: false,
    hasSitekey: false,
    hasAnchorSrc: false,
    hasCheckbox: false,
    hasResponseTa: false,
    hasChallengeUi: false,
    clients: 0,
    grecaptchaApi: false,
  };

  const iframeSel =
    'iframe#captcha-recaptcha, iframe[src*="captcha"], iframe[src*="recaptcha"], ' +
    'iframe[src*="referer_frame"], iframe[src*="google.com/recaptcha"], ' +
    'iframe[src*="recaptcha.net"], iframe[title*="reCAPTCHA" i], iframe[title*="recaptcha" i]';

  document.querySelectorAll(iframeSel).forEach((el) => {
    try {
      const r = el.getBoundingClientRect();
      const style = window.getComputedStyle(el);
      const visible =
        style.visibility !== 'hidden' &&
        style.display !== 'none' &&
        Number(style.opacity || '1') > 0 &&
        r.width >= 40 &&
        r.height >= 40;
      if (visible) report.hasSizedIframe = true;
    } catch (e) {}
    const src = el.getAttribute('src') || el.src || '';
    if (/\\/anchor|\\/enterprise\\/anchor|\\/bframe|enterprise\\/bframe/i.test(src)) {
      report.hasAnchorSrc = true;
    }
    try {
      const u = new URL(src, location.href);
      const k = (u.searchParams.get('k') || '').trim();
      if (/^6L[\\w-]{10,}$/.test(k)) report.hasSitekey = true;
    } catch (e) {}
  });

  document.querySelectorAll('[data-sitekey]').forEach((el) => {
    const k = (el.getAttribute('data-sitekey') || '').trim();
    if (/^6L[\\w-]{10,}$/.test(k)) report.hasSitekey = true;
  });

  if (
    document.querySelector(
      '#recaptcha-anchor, .rc-anchor, #rc-anchor-container, ' +
        '.rc-anchor-checkbox, .rc-anchor-content, #rc-anchor-alert'
    )
  ) {
    report.hasCheckbox = true;
  }
  if (
    document.querySelector(
      '.rc-imageselect, .rc-imageselect-payload, #rc-imageselect, ' +
        '.rc-doscaptcha, .rc-defaultchallenge'
    )
  ) {
    report.hasChallengeUi = true;
  }
  if (
    document.querySelector(
      '#g-recaptcha-response, textarea[name="g-recaptcha-response"], ' +
        'textarea[id^="g-recaptcha-response"]'
    )
  ) {
    report.hasResponseTa = true;
  }

  try {
    if (typeof ___grecaptcha_cfg !== 'undefined' && ___grecaptcha_cfg.clients) {
      report.clients = Object.keys(___grecaptcha_cfg.clients).length;
    }
  } catch (e) {}

  try {
    if (typeof grecaptcha !== 'undefined') {
      if (
        typeof grecaptcha.render === 'function' ||
        typeof grecaptcha.execute === 'function' ||
        (grecaptcha.enterprise &&
          (typeof grecaptcha.enterprise.render === 'function' ||
            typeof grecaptcha.enterprise.execute === 'function'))
      ) {
        report.grecaptchaApi = true;
      }
    }
  } catch (e) {}

  return report;
}
"""


def _captcha_load_status(page) -> dict:
    """Собрать признаки прогрузки капчи по всем фреймам страницы."""
    merged = {
        "ready": False,
        "hasSizedIframe": False,
        "hasSitekey": False,
        "hasAnchorSrc": False,
        "hasCheckbox": False,
        "hasResponseTa": False,
        "hasChallengeUi": False,
        "clients": 0,
        "grecaptchaApi": False,
    }
    try:
        frames = list(page.frames)
    except Exception:
        frames = []
    for fr in frames:
        try:
            st = fr.evaluate(_CAPTCHA_READY_JS)
        except Exception:
            continue
        if not isinstance(st, dict):
            continue
        for key in (
            "hasSizedIframe",
            "hasSitekey",
            "hasAnchorSrc",
            "hasCheckbox",
            "hasResponseTa",
            "hasChallengeUi",
            "grecaptchaApi",
        ):
            if st.get(key):
                merged[key] = True
        try:
            merged["clients"] = max(int(merged["clients"]), int(st.get("clients") or 0))
        except Exception:
            pass

    widget_ready = bool(
        merged["hasCheckbox"]
        or merged["hasChallengeUi"]
        or merged["hasResponseTa"]
        or merged["clients"] > 0
        or (merged["hasAnchorSrc"] and merged["grecaptchaApi"])
    )
    merged["ready"] = bool(
        merged["hasSizedIframe"] and merged["hasSitekey"] and widget_ready
    )
    return merged


def _captcha_fully_loaded(page) -> bool:
    """True, когда виджет капчи реально отрисован во фрейме."""
    try:
        return bool(_captcha_load_status(page).get("ready"))
    except Exception:
        return False


def _wait_captcha_fully_loaded(
    page,
    *,
    max_seconds: float = _CAPTCHA_FULLY_LOADED_MAX_S,
) -> bool:
    """
    Ждать полной прогрузки капчи во фрейме перед запросом в RuCaptcha.

    Требуем два подряд успешных опроса (~1.2 с стабильности), чтобы не
    отправлять createTask по полупустому iframe.
    """
    deadline = time.monotonic() + max(5.0, float(max_seconds))
    stable_hits = 0
    last_log = 0.0
    _log("Instagram: жду полной прогрузки капчи во фрейме…")
    while time.monotonic() < deadline:
        if _is_confirmation_code_screen(page):
            _log("Instagram: пока ждали капчу — уже экран кода.")
            return True
        status = _captcha_load_status(page)
        if status.get("ready"):
            stable_hits += 1
            if stable_hits >= 2:
                _log(
                    "Instagram: капча полностью прогружена "
                    f"(clients={status.get('clients')}, "
                    f"checkbox={status.get('hasCheckbox')}, "
                    f"anchor={status.get('hasAnchorSrc')}, "
                    f"responseTa={status.get('hasResponseTa')})."
                )
                return True
        else:
            stable_hits = 0
            now = time.monotonic()
            if now - last_log >= 5.0:
                _log(
                    "Instagram: капча ещё грузится "
                    f"(sized={status.get('hasSizedIframe')}, "
                    f"sitekey={status.get('hasSitekey')}, "
                    f"clients={status.get('clients')}, "
                    f"checkbox={status.get('hasCheckbox')}, "
                    f"anchor={status.get('hasAnchorSrc')})…"
                )
                last_log = now
        try:
            page.wait_for_timeout(600)
        except Exception:
            return False
    _log(
        f"Instagram: капча не прогрузилась полностью за {max_seconds:.0f} с."
    )
    return False


_EXTRACT_SITEKEY_JS = """
() => {
  const keys = [];
  const add = (k) => {
    if (typeof k === 'string') {
      const t = k.trim();
      if (t && !keys.includes(t)) keys.push(t);
    }
  };
  document.querySelectorAll('[data-sitekey]').forEach((el) => {
    add(el.getAttribute('data-sitekey'));
  });
  document.querySelectorAll('iframe[src*="recaptcha"], iframe[src*="google.com/recaptcha"], iframe[src*="recaptcha.net"]').forEach((el) => {
    try {
      const u = new URL(el.src, location.href);
      add(u.searchParams.get('k'));
    } catch (e) {}
  });
  try {
    if (typeof ___grecaptcha_cfg !== 'undefined' && ___grecaptcha_cfg.clients) {
      const walk = (obj, depth) => {
        if (!obj || depth > 6) return;
        if (typeof obj === 'string' && /^6L[\\w-]{10,}$/.test(obj)) add(obj);
        if (typeof obj !== 'object') return;
        if (Array.isArray(obj)) {
          obj.forEach((x) => walk(x, depth + 1));
          return;
        }
        for (const v of Object.values(obj)) walk(v, depth + 1);
      };
      walk(___grecaptcha_cfg.clients, 0);
    }
  } catch (e) {}
  return keys;
}
"""

_DETECT_CAPTCHA_META_JS = """
() => {
  const meta = {
    sitekeys: [],
    invisible: false,
    enterprise: false,
    apiDomain: '',
    dataS: '',
  };
  const addKey = (k) => {
    if (typeof k === 'string') {
      const t = k.trim();
      if (t && /^6L[\\w-]{10,}$/.test(t) && !meta.sitekeys.includes(t)) {
        meta.sitekeys.push(t);
      }
    }
  };
  const html = (document.documentElement && document.documentElement.innerHTML) || '';
  if (/grecaptcha\\.enterprise|recaptcha\\/enterprise|recaptchaenterprise/i.test(html)) {
    meta.enterprise = true;
  }
  if (typeof grecaptcha !== 'undefined' && grecaptcha.enterprise) {
    meta.enterprise = true;
  }
  document.querySelectorAll('[data-sitekey]').forEach((el) => {
    addKey(el.getAttribute('data-sitekey'));
    const size = (el.getAttribute('data-size') || '').toLowerCase();
    if (size === 'invisible') meta.invisible = true;
    const s = el.getAttribute('data-s');
    if (s && !meta.dataS) meta.dataS = s;
  });
  document.querySelectorAll('iframe[src*="recaptcha"], iframe[src*="google.com/recaptcha"], iframe[src*="recaptcha.net"]').forEach((el) => {
    try {
      const u = new URL(el.src, location.href);
      addKey(u.searchParams.get('k'));
      const size = (u.searchParams.get('size') || '').toLowerCase();
      if (size === 'invisible') meta.invisible = true;
      if (/enterprise/i.test(u.pathname + u.href)) meta.enterprise = true;
      if (u.hostname.includes('recaptcha.net')) meta.apiDomain = 'recaptcha.net';
      else if (u.hostname.includes('google.com')) meta.apiDomain = 'google.com';
    } catch (e) {}
  });
  document.querySelectorAll('script[src*="recaptcha"]').forEach((el) => {
    const src = el.getAttribute('src') || '';
    if (/enterprise/i.test(src)) meta.enterprise = true;
    if (/recaptcha\\.net/i.test(src)) meta.apiDomain = meta.apiDomain || 'recaptcha.net';
  });
  try {
    if (typeof ___grecaptcha_cfg !== 'undefined' && ___grecaptcha_cfg.clients) {
      const walk = (obj, depth) => {
        if (!obj || depth > 7) return;
        if (typeof obj === 'string') {
          addKey(obj);
          if (obj === 'invisible') meta.invisible = true;
          return;
        }
        if (typeof obj !== 'object') return;
        if (Array.isArray(obj)) {
          obj.forEach((x) => walk(x, depth + 1));
          return;
        }
        for (const [k, v] of Object.entries(obj)) {
          if (String(k).toLowerCase() === 'size' && String(v).toLowerCase() === 'invisible') {
            meta.invisible = true;
          }
          walk(v, depth + 1);
        }
      };
      walk(___grecaptcha_cfg.clients, 0);
    }
  } catch (e) {}
  return meta;
}
"""

_INJECT_TOKEN_JS = """
(token) => {
  const report = {
    setCount: 0,
    callbacks: 0,
    overridden: false,
    dataCallbacks: 0,
    posts: 0,
    sitekeyFns: 0,
    clients: 0,
    hasGrecaptcha: false,
    callbackPaths: [],
    checkboxMarked: false,
  };

  const isBadCallbackKey = (k) => {
    const key = String(k).toLowerCase();
    return (
      key.includes('error') ||
      key.includes('expired') ||
      key.includes('timeout') ||
      key.includes('close') ||
      key === 'reset'
    );
  };

  // Только success-callback. НЕ трогаем error-callback / expired-callback —
  // их вызов сбрасывает виджет (капча «обновляется»).
  const isSuccessCallbackKey = (k) => {
    const key = String(k).toLowerCase();
    if (isBadCallbackKey(key)) return false;
    return (
      key === 'callback' ||
      key === 'promise-callback' ||
      key === 'success-callback' ||
      key === 'promisecallback'
    );
  };

  const safeCall = (fn) => {
    if (typeof fn === 'string') {
      try {
        const resolved = window[fn];
        if (typeof resolved === 'function') return safeCall(resolved);
      } catch (e) {}
      return false;
    }
    if (typeof fn !== 'function') return false;
    try {
      fn(token);
      return true;
    } catch (e) {
      try {
        fn();
        return true;
      } catch (e2) {
        return false;
      }
    }
  };

  const ownEntries = (obj) => {
    const out = [];
    if (!obj || typeof obj !== 'object') return out;
    let keys;
    try {
      keys = Reflect.ownKeys(obj);
    } catch (e) {
      try {
        keys = Object.keys(obj);
      } catch (e2) {
        return out;
      }
    }
    for (const k of keys) {
      let v;
      try {
        v = obj[k];
      } catch (e) {
        continue;
      }
      out.push([k, v]);
    }
    return out;
  };

  const setNativeValue = (el, value) => {
    try {
      const proto =
        el.tagName === 'TEXTAREA'
          ? window.HTMLTextAreaElement.prototype
          : window.HTMLInputElement.prototype;
      const desc = Object.getOwnPropertyDescriptor(proto, 'value');
      if (desc && desc.set) desc.set.call(el, value);
      else el.value = value;
    } catch (e) {
      el.value = value;
    }
    try { el.innerHTML = value; } catch (e) {}
    try {
      el.setAttribute('value', value);
    } catch (e) {}
    try { el.style.display = ''; } catch (e) {}
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };

  document
    .querySelectorAll(
      '#g-recaptcha-response, textarea[name="g-recaptcha-response"], ' +
        'textarea[id^="g-recaptcha-response"], input[name="g-recaptcha-response"], ' +
        '[name="g-recaptcha-response"]'
    )
    .forEach((el) => {
      setNativeValue(el, token);
      report.setCount += 1;
    });

  if (report.setCount === 0) {
    try {
      const ta = document.createElement('textarea');
      ta.id = 'g-recaptcha-response';
      ta.name = 'g-recaptcha-response';
      ta.style.display = 'none';
      document.body.appendChild(ta);
      setNativeValue(ta, token);
      report.setCount += 1;
    } catch (e) {}
  }

  const patchG = (g) => {
    if (!g || typeof g !== 'object') return;
    report.hasGrecaptcha = true;
    try {
      g.getResponse = function () { return token; };
      report.overridden = true;
    } catch (e) {}
    try {
      if (g.enterprise) {
        g.enterprise.getResponse = function () { return token; };
        report.overridden = true;
      }
    } catch (e) {}
  };
  try {
    if (typeof grecaptcha !== 'undefined') patchG(grecaptcha);
    if (typeof window !== 'undefined' && window.grecaptcha) patchG(window.grecaptcha);
  } catch (e) {}

  // Визуально «галочка» в anchor-фрейме (после patch getResponse).
  try {
    const anchor = document.querySelector(
      '#recaptcha-anchor, .recaptcha-checkbox, #rc-anchor-container'
    );
    if (anchor) {
      anchor.setAttribute('aria-checked', 'true');
      anchor.classList.add('recaptcha-checkbox-checked');
      const border = document.querySelector('.recaptcha-checkbox-border');
      if (border) border.style.display = 'none';
      const check = document.querySelector('.recaptcha-checkbox-checkmark');
      if (check) check.style.display = 'block';
      report.checkboxMarked = true;
    }
  } catch (e) {}

  // Паттерн 2captcha findRecaptchaClients: clients[id][top][sub] где sitekey+size.
  // Также: любой function-sibling рядом с sitekey (Meta иногда не кладёт size).
  const callFindRecaptchaClients = () => {
    if (typeof ___grecaptcha_cfg === 'undefined' || !___grecaptcha_cfg.clients) return;
    const clients = ___grecaptcha_cfg.clients;
    try {
      report.clients = Object.keys(clients).length;
    } catch (e) {}
    for (const [cid, client] of ownEntries(clients)) {
      if (!client || typeof client !== 'object') continue;
      for (const [toplevelKey, toplevel] of ownEntries(client)) {
        if (!toplevel || typeof toplevel !== 'object') continue;
        for (const [sublevelKey, sublevel] of ownEntries(toplevel)) {
          if (!sublevel || typeof sublevel !== 'object') continue;
          let hasSitekey = false;
          let hasSize = false;
          try {
            hasSitekey = 'sitekey' in sublevel || !!(
              typeof sublevel.sitekey === 'string' &&
              /^6L[\\w-]{10,}$/.test(sublevel.sitekey)
            );
            hasSize = 'size' in sublevel;
          } catch (e) {
            continue;
          }
          if (!hasSitekey) continue;
          const tryKeys = hasSize
            ? ['callback', 'promise-callback', 'success-callback']
            : ['callback', 'promise-callback', 'success-callback'];
          for (const cbKey of tryKeys) {
            let cb;
            try {
              cb = sublevel[cbKey];
            } catch (e) {
              continue;
            }
            if (typeof cb !== 'function' && typeof cb !== 'string') continue;
            const path =
              '___grecaptcha_cfg.clients[' +
              JSON.stringify(cid) +
              '][' +
              JSON.stringify(toplevelKey) +
              '][' +
              JSON.stringify(sublevelKey) +
              '][' +
              JSON.stringify(cbKey) +
              ']';
            if (safeCall(cb)) {
              report.callbacks += 1;
              if (report.callbackPaths.length < 5) report.callbackPaths.push(path);
            }
          }
          // Любая function рядом с sitekey (кроме onload/error/expired).
          if (report.callbacks === 0 || !hasSize) {
            for (const [k, v] of ownEntries(sublevel)) {
              if (typeof v !== 'function') continue;
              const lk = String(k).toLowerCase();
              if (isBadCallbackKey(lk)) continue;
              if (lk === 'onload' || lk === 'render') continue;
              const path =
                '___grecaptcha_cfg.clients[' +
                JSON.stringify(cid) +
                '][' +
                JSON.stringify(toplevelKey) +
                '][' +
                JSON.stringify(sublevelKey) +
                '][' +
                JSON.stringify(k) +
                ']';
              if (safeCall(v)) {
                report.callbacks += 1;
                if (report.callbackPaths.length < 5) report.callbackPaths.push(path);
              }
            }
          }
        }
      }
    }
  };

  // Глубокий поиск ТОЛЬКО success-callback по имени ключа.
  const callNamedSuccessCallbacks = (obj, depth, seen, path) => {
    if (!obj || depth > 12) return;
    if (typeof obj !== 'object') return;
    if (seen.has(obj)) return;
    seen.add(obj);
    for (const [k, v] of ownEntries(obj)) {
      const nextPath = path + '[' + JSON.stringify(k) + ']';
      if (isSuccessCallbackKey(k) && (typeof v === 'function' || typeof v === 'string')) {
        if (safeCall(v)) {
          report.callbacks += 1;
          if (report.callbackPaths.length < 5) report.callbackPaths.push(nextPath);
        }
      } else if (v && typeof v === 'object') {
        callNamedSuccessCallbacks(v, depth + 1, seen, nextPath);
      }
    }
  };

  try {
    callFindRecaptchaClients();
    if (typeof ___grecaptcha_cfg !== 'undefined' && ___grecaptcha_cfg.clients) {
      if (report.callbacks === 0) {
        callNamedSuccessCallbacks(
          ___grecaptcha_cfg.clients,
          0,
          new WeakSet(),
          '___grecaptcha_cfg.clients'
        );
      }
    }
  } catch (e) {}

  // data-callback="funcName" — не вызывать data-error-callback / data-expired-callback.
  try {
    document.querySelectorAll('[data-callback]').forEach((el) => {
      const name = el.getAttribute('data-callback');
      if (!name || isBadCallbackKey(name)) return;
      try {
        const fn = window[name];
        if (typeof fn === 'function' && safeCall(fn)) {
          report.dataCallbacks += 1;
        }
      } catch (e) {}
    });
  } catch (e) {}

  const globalNames = [
    'onCaptchaSuccess',
    'captchaCallback',
    'recaptchaCallback',
    'onRecaptchaSuccess',
    'captchaResponseCallback',
  ];
  for (const name of globalNames) {
    try {
      const fn = window[name];
      if (typeof fn === 'function' && safeCall(fn)) report.dataCallbacks += 1;
    } catch (e) {}
  }

  // postMessage — Meta referer_frame / captcha bridge.
  const payloads = [
    token,
    { token },
    { response: token },
    { 'g-recaptcha-response': token },
    { type: 'recaptcha_response', token },
    { type: 'recaptcha-token', token },
    { type: 'captcha_solved', token },
    { type: 'CAPTCHA_SOLVED', response: token },
    { event: 'captcha_solved', payload: token },
    { source: 'recaptcha', response: token },
    { name: 'captchaResponse', payload: { response: token } },
    { method: 'captchaResponse', params: { response: token } },
  ];
  for (const p of payloads) {
    try {
      window.postMessage(p, '*');
      report.posts += 1;
    } catch (e) {}
    try {
      if (window.parent && window.parent !== window) {
        window.parent.postMessage(p, '*');
        report.posts += 1;
      }
    } catch (e) {}
    try {
      if (window.top && window.top !== window) {
        window.top.postMessage(p, '*');
        report.posts += 1;
      }
    } catch (e) {}
  }

  return report;
}
"""

def _collect_sitekeys(page) -> list[str]:
    keys: list[str] = []
    for frame in page.frames:
        try:
            found = frame.evaluate(_EXTRACT_SITEKEY_JS)
        except Exception:
            continue
        if isinstance(found, list):
            for k in found:
                if isinstance(k, str) and k.strip() and k.strip() not in keys:
                    keys.append(k.strip())
    return keys

def _detect_captcha_meta(page) -> dict:
    """Собрать sitekey + флаги invisible/enterprise/apiDomain со всех фреймов."""
    merged = {
        "sitekeys": [],
        "invisible": False,
        "enterprise": False,
        "apiDomain": "",
        "dataS": "",
    }
    # Сначала фреймы капчи — их sitekey приоритетнее.
    frames = list(page.frames)
    captcha_frames = []
    other_frames = []
    for fr in frames:
        try:
            url = (fr.url or "").lower()
        except Exception:
            url = ""
        if any(
            x in url
            for x in (
                "recaptcha",
                "captcha",
                "referer_frame",
                "google.com/recaptcha",
                "recaptcha.net",
            )
        ):
            captcha_frames.append(fr)
        else:
            other_frames.append(fr)

    for fr in captcha_frames + other_frames:
        try:
            meta = fr.evaluate(_DETECT_CAPTCHA_META_JS)
        except Exception:
            continue
        if not isinstance(meta, dict):
            continue
        for k in meta.get("sitekeys") or []:
            if isinstance(k, str) and k.strip() and k.strip() not in merged["sitekeys"]:
                merged["sitekeys"].append(k.strip())
        if meta.get("invisible"):
            merged["invisible"] = True
        if meta.get("enterprise"):
            merged["enterprise"] = True
        if meta.get("apiDomain") and not merged["apiDomain"]:
            merged["apiDomain"] = str(meta.get("apiDomain") or "").strip()
        if meta.get("dataS") and not merged["dataS"]:
            merged["dataS"] = str(meta.get("dataS") or "").strip()
    return merged

def _instagram_captcha_page_url(page) -> str:
    """URL для RuCaptcha: канонический Instagram, без about:blank."""
    try:
        cur = (page.url or "").strip()
    except Exception:
        cur = ""
    if "instagram.com" in cur.lower() and not cur.lower().startswith("about:"):
        try:
            from urllib.parse import urlsplit, urlunsplit

            parts = urlsplit(cur)
            path = parts.path or "/"
            return urlunsplit((parts.scheme or "https", parts.netloc, path, "", ""))
        except Exception:
            return cur
    return INSTAGRAM_URL


def _inject_frame_score(result: dict) -> int:
    """Насколько inject «успешен» в этом фрейме (callback важнее textarea)."""
    return (
        int(result.get("callbacks") or 0) * 100
        + int(result.get("dataCallbacks") or 0) * 50
        + int(result.get("sitekeyFns") or 0) * 20
        + (30 if result.get("overridden") else 0)
        + (10 if result.get("checkboxMarked") else 0)
        + int(result.get("clients") or 0) * 5
        + int(result.get("setCount") or 0)
    )


def _recaptcha_image_challenge_visible(page) -> bool:
    """True, если открыт popup reCAPTCHA с картинками (bframe / imageselect)."""
    try:
        status = _captcha_load_status(page)
        if status.get("hasChallengeUi"):
            return True
    except Exception:
        pass
    for frame in list(page.frames):
        try:
            url = (frame.url or "").lower()
        except Exception:
            url = ""
        if "/bframe" in url or "enterprise/bframe" in url:
            try:
                if frame.locator(
                    ".rc-imageselect, .rc-imageselect-payload, #rc-imageselect, "
                    ".rc-defaultchallenge, .rc-doscaptcha"
                ).first.count() > 0:
                    return True
            except Exception:
                return True
    return False


def _dismiss_recaptcha_image_challenge(page) -> bool:
    """
    Закрыть challenge с картинками, если он открылся.

    После inject токена кликать чекбокс нельзя: Google открывает imageselect,
    а Instagram так и не получает success-callback → «Далее» мёртва.
    """
    closed = False
    for frame in list(page.frames):
        try:
            url = (frame.url or "").lower()
        except Exception:
            url = ""
        if not any(x in url for x in ("/bframe", "recaptcha", "enterprise")):
            continue
        try:
            close_btn = frame.locator(
                "button[title='Close'], button[aria-label='Close'], "
                "button[title='Закрыть'], button[aria-label='Закрыть']"
            ).first
            if close_btn.count() > 0 and close_btn.is_visible(timeout=400):
                close_btn.click(timeout=2000, force=True)
                closed = True
                _log("Instagram: закрыл popup reCAPTCHA (картинки).")
                break
        except Exception:
            continue
    if not closed:
        try:
            page.keyboard.press("Escape")
            closed = True
            _log("Instagram: Escape — попытка закрыть challenge с картинками.")
        except Exception:
            pass
    if closed:
        try:
            page.wait_for_timeout(400)
        except Exception:
            pass
    return closed


def _inject_recaptcha_token(page, token: str) -> dict:
    """
    Вставить токен + вызвать success-callback, чтобы React включил Next.

    Важно: НЕ кликать чекбокс после inject — клик открывает challenge с
    картинками и ломает callback-путь.
    """
    injected = False
    best: dict | None = None
    frames: list = []

    # Сначала фреймы капчи / с grecaptcha, потом остальные (main в конце повторно).
    captcha_frames: list = []
    other_frames: list = []
    try:
        main = page.main_frame
    except Exception:
        main = None
    for fr in page.frames:
        try:
            url = (fr.url or "").lower()
        except Exception:
            url = ""
        if any(
            x in url
            for x in (
                "recaptcha",
                "captcha",
                "referer_frame",
                "google.com/recaptcha",
                "recaptcha.net",
            )
        ):
            captcha_frames.append(fr)
        elif main is not None and fr == main:
            continue
        else:
            other_frames.append(fr)
    frames = captcha_frames + other_frames
    if main is not None:
        frames.append(main)

    for frame in frames:
        try:
            result = frame.evaluate(_INJECT_TOKEN_JS, token)
            injected = True
            if isinstance(result, dict):
                if best is None or _inject_frame_score(result) > _inject_frame_score(best):
                    best = result
                if (
                    result.get("callbacks")
                    or result.get("dataCallbacks")
                    or result.get("overridden")
                    or result.get("checkboxMarked")
                ):
                    try:
                        furl = (frame.url or "")[:80]
                    except Exception:
                        furl = "?"
                    paths = result.get("callbackPaths") or []
                    path_s = ""
                    if isinstance(paths, list) and paths:
                        path_s = f", path={paths[0]!r}"
                    _log(
                        "Instagram: inject OK — "
                        f"set={result.get('setCount')}, "
                        f"callbacks={result.get('callbacks')}, "
                        f"dataCb={result.get('dataCallbacks')}, "
                        f"override={result.get('overridden')}, "
                        f"clients={result.get('clients')}, "
                        f"checkbox={result.get('checkboxMarked')}, "
                        f"frame={furl!r}{path_s}"
                    )
        except Exception:
            continue

    if not injected:
        raise RuntimeError("Instagram: не удалось вставить токен капчи в страницу/фреймы.")
    if best:
        paths = best.get("callbackPaths") or []
        path_s = ""
        if isinstance(paths, list) and paths:
            path_s = f", path={paths[0]!r}"
        _log(
            "Instagram: итог inject — "
            f"set={best.get('setCount')}, callbacks={best.get('callbacks')}, "
            f"override={best.get('overridden')}, clients={best.get('clients')}, "
            f"checkbox={best.get('checkboxMarked')}{path_s}"
        )
        if int(best.get("callbacks") or 0) == 0 and int(best.get("dataCallbacks") or 0) == 0:
            _log(
                "Instagram: success-callback не найден — "
                "только textarea/override. Next может остаться disabled."
            )
    page.wait_for_timeout(400)
    # Ещё раз в main — React-обработчик часто живёт на родительской странице.
    if main is not None:
        try:
            page.main_frame.evaluate(_INJECT_TOKEN_JS, token)
        except Exception:
            pass
    # Если уже открыт challenge с картинками — закрыть (не решать кликом).
    try:
        if _recaptcha_image_challenge_visible(page):
            _log(
                "Instagram: после inject открыт challenge с картинками — "
                "закрываю (клик чекбокса не делаем)."
            )
            _dismiss_recaptcha_image_challenge(page)
    except Exception as e:
        _log(f"Instagram: dismiss challenge: {e!r}")
    page.wait_for_timeout(400)
    return best or {}


def _click_instagram_next(page) -> bool:
    """Нажать активную «Далее»/Next. True если клик удался."""
    if _click_by_text(page, _NEXT_RE, prefer_link=False):
        _log("Instagram: нажали «Далее».")
        page.wait_for_timeout(800)
        return True
    try:
        page.get_by_role("button", name=_NEXT_RE).first.click(timeout=5000)
        _log("Instagram: нажали «Далее» (role=button).")
        page.wait_for_timeout(800)
        return True
    except Exception:
        return False


_RELOAD_CAPTCHA_FRAME_JS = """
() => {
  const report = { cleared: 0, reset: 0, reloaded: 0 };
  const clearSel =
    '#g-recaptcha-response, textarea[name="g-recaptcha-response"], ' +
    'textarea[id^="g-recaptcha-response"], input[name="g-recaptcha-response"], ' +
    '[name="g-recaptcha-response"]';
  document.querySelectorAll(clearSel).forEach((el) => {
    try {
      el.value = '';
      el.innerHTML = '';
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      report.cleared += 1;
    } catch (e) {}
  });

  try {
    if (typeof grecaptcha !== 'undefined') {
      if (typeof grecaptcha.reset === 'function') {
        try { grecaptcha.reset(); report.reset += 1; } catch (e) {}
      }
      if (grecaptcha.enterprise && typeof grecaptcha.enterprise.reset === 'function') {
        try { grecaptcha.enterprise.reset(); report.reset += 1; } catch (e) {}
      }
    }
  } catch (e) {}

  const iframeSel =
    'iframe#captcha-recaptcha, iframe[src*="captcha"], ' +
    'iframe[src*="recaptcha"], iframe[src*="referer_frame"], ' +
    'iframe[title*="reCAPTCHA" i], iframe[title*="recaptcha" i]';
  document.querySelectorAll(iframeSel).forEach((iframe) => {
    try {
      const src = iframe.getAttribute('src') || iframe.src || '';
      if (!src || src === 'about:blank') return;
      // Force reload: bump cache-buster then restore original if needed.
      try {
        const u = new URL(src, location.href);
        u.searchParams.set('_zaliver_reload', String(Date.now()));
        iframe.src = u.toString();
      } catch (e2) {
        iframe.src = src;
      }
      report.reloaded += 1;
    } catch (e) {}
  });
  return report;
}
"""


def reload_instagram_captcha_frame(page) -> None:
    """
    Сбросить состояние после неудачного RuCaptcha (inject/overrides)
    и перезагрузить iframe капчи перед ручным прохождением.
    """
    _log("Instagram: перезагружаю фрейм капчи перед ручным прохождением…")
    totals = {"cleared": 0, "reset": 0, "reloaded": 0}
    # Только main_frame — обход всех frames через evaluate иногда валит CDP.
    frames = []
    try:
        frames.append(page.main_frame)
    except Exception:
        pass
    try:
        for fr in page.frames:
            if fr in frames:
                continue
            try:
                url = (fr.url or "").lower()
            except Exception:
                url = ""
            if any(
                x in url
                for x in ("recaptcha", "captcha", "referer_frame")
            ):
                frames.append(fr)
    except Exception:
        pass

    for frame in frames:
        try:
            result = frame.evaluate(_RELOAD_CAPTCHA_FRAME_JS)
        except Exception:
            continue
        if not isinstance(result, dict):
            continue
        for k in totals:
            try:
                totals[k] += int(result.get(k) or 0)
            except Exception:
                pass

    _log(
        "Instagram: reload капчи — "
        f"cleared={totals['cleared']}, reset={totals['reset']}, "
        f"reloaded={totals['reloaded']}"
    )
    try:
        page.wait_for_timeout(2500)
    except Exception:
        return
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        try:
            if _is_confirmation_code_screen(page):
                return
            if _captcha_iframe_visible(page):
                _log("Instagram: фрейм капчи снова на месте.")
                return
            page.wait_for_timeout(400)
        except Exception:
            return
    _log("Instagram: после reload iframe капчи пока не видно — всё равно ждём вручную.")


def try_solve_instagram_captcha_rucaptcha(
    page,
    *,
    rucaptcha_api_key: str,
) -> bool:
    """
    2× Proxyless через RuCaptcha (без прокси/UA). True если токен вставлен
    и «Далее» нажата / уже экран кода.

    Запрос в RuCaptcha уходит только после полной прогрузки капчи во фрейме.
    """
    from zaliver.captcha.rucaptcha import (
        RuCaptchaError,
        solve_recaptcha_v2_proxyless,
    )

    api_key = (rucaptcha_api_key or "").strip()
    if not api_key:
        _log("Instagram: ключ RuCaptcha не задан — сразу ручная капча.")
        return False

    if _is_confirmation_code_screen(page):
        return True

    if not _wait_captcha_fully_loaded(page):
        _log(
            "Instagram: капча не прогрузилась — RuCaptcha не вызываем, "
            "ручное ожидание."
        )
        return False

    if _is_confirmation_code_screen(page):
        return True

    meta = _detect_captcha_meta(page)
    if not meta.get("sitekeys"):
        keys = _collect_sitekeys(page)
        if keys:
            meta = {
                "sitekeys": keys,
                "invisible": False,
                "enterprise": False,
                "apiDomain": "",
                "dataS": "",
            }

    sitekeys = list(meta.get("sitekeys") or [])
    if not sitekeys:
        _log("Instagram: sitekey не найден — RuCaptcha пропуск, ручная капча.")
        return False

    website_key = sitekeys[0]
    website_url = _instagram_captcha_page_url(page)
    prefer_invisible = bool(meta.get("invisible"))
    api_domain = (meta.get("apiDomain") or "").strip() or None
    enterprise_payload = None
    data_s = (meta.get("dataS") or "").strip()
    if data_s:
        enterprise_payload = {"s": data_s}

    _log(
        f"Instagram: RuCaptcha Proxyless — sitekey={website_key[:16]}… "
        f"url={website_url!r}, invisible={prefer_invisible}, "
        f"apiDomain={api_domain!r}"
    )

    token: str | None = None
    last_err: Exception | None = None
    try:
        token = solve_recaptcha_v2_proxyless(
            api_key,
            website_url=website_url,
            website_key=website_key,
            is_invisible=prefer_invisible,
            is_enterprise=True,
            api_domain=api_domain,
            enterprise_payload=enterprise_payload,
            retries=2,
            log=_log,
        )
    except RuCaptchaError as e:
        last_err = e
        if len(sitekeys) > 1:
            for alt_key in sitekeys[1:]:
                _log(f"Instagram: другой sitekey={alt_key[:16]}…")
                try:
                    token = solve_recaptcha_v2_proxyless(
                        api_key,
                        website_url=website_url,
                        website_key=alt_key,
                        is_invisible=prefer_invisible,
                        is_enterprise=True,
                        api_domain=api_domain,
                        enterprise_payload=enterprise_payload,
                        retries=1,
                        log=_log,
                    )
                    last_err = None
                    break
                except RuCaptchaError as e2:
                    last_err = e2

    if not token:
        _log(
            "Instagram: RuCaptcha не решила капчу"
            + (f" ({last_err})" if last_err else "")
            + " — переходим к ручному ожиданию."
        )
        return False

    _log(f"Instagram: RuCaptcha токен (len={len(token)}), вставляем…")
    try:
        _inject_recaptcha_token(page, token)
    except Exception as e:
        _log(f"Instagram: inject токена не удался: {e!r} — ручная капча.")
        return False

    # Ждём активации Next / экран кода. Чекбокс НЕ кликаем — только re-inject.
    wait_started = time.monotonic()
    wait_deadline = wait_started + 45.0
    reinject_at = (3.0, 8.0, 15.0)
    reinject_done: set[float] = set()
    challenge_logged = False
    while time.monotonic() < wait_deadline:
        if _is_confirmation_code_screen(page):
            _log("Instagram: после RuCaptcha сразу экран кода.")
            return True
        if _next_button_enabled(page):
            if _click_instagram_next(page):
                return True
            break
        elapsed = time.monotonic() - wait_started
        if _recaptcha_image_challenge_visible(page):
            if not challenge_logged:
                _log(
                    "Instagram: виден challenge с картинками — "
                    "это ломает callback; закрываю и re-inject."
                )
                challenge_logged = True
            try:
                _dismiss_recaptcha_image_challenge(page)
            except Exception:
                pass
        for t in reinject_at:
            if t in reinject_done or elapsed < t:
                continue
            reinject_done.add(t)
            try:
                _log(f"Instagram: повторный inject токена (~{t:.0f} с)…")
                _inject_recaptcha_token(page, token)
            except Exception as e:
                _log(f"Instagram: re-inject: {e!r}")
        page.wait_for_timeout(500)

    if _is_confirmation_code_screen(page):
        return True
    _log(
        "Instagram: после RuCaptcha «Далее» не активировалась — "
        "ручное ожидание."
    )
    return False



def _wait_captcha_or_code_screen(
    page,
    *,
    max_seconds: float = _CAPTCHA_OR_CODE_WAIT_MAX_S,
) -> str:
    """
    Ждать либо iframe капчи, либо экран кода подтверждения.

    Returns:
        ``\"captcha\"`` | ``\"code\"``
    """
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        accept_instagram_terms_if_present(page, max_seconds=8.0)
        if _is_confirmation_code_screen(page):
            _log("Instagram: экран кода подтверждения (без капчи).")
            return "code"
        if _captcha_iframe_visible(page):
            _log("Instagram: iframe капчи появился.")
            return "captcha"
        page.wait_for_timeout(500)
    raise RuntimeError(
        f"Instagram: ни капча, ни экран кода не появились за {max_seconds:.0f} с "
        f"(URL={page.url!r})"
    )


def _next_button_enabled(page) -> bool:
    """Next/Далее выглядит активной (не aria-disabled)."""
    js = """
() => {
  const re = /^(next|далее)$/i;
  const nodes = Array.from(document.querySelectorAll('[role="button"], button'));
  for (const el of nodes) {
    const t = (el.innerText || el.textContent || '').trim();
    if (!re.test(t)) continue;
    const disabled =
      el.getAttribute('aria-disabled') === 'true' ||
      el.getAttribute('disabled') !== null ||
      el.getAttribute('tabindex') === '-1';
    if (!disabled) return true;
  }
  const spans = Array.from(document.querySelectorAll('span'));
  for (const sp of spans) {
    const t = (sp.innerText || '').trim();
    if (!re.test(t)) continue;
    let p = sp;
    for (let i = 0; i < 8 && p; i++) {
      if (p.getAttribute && p.getAttribute('role') === 'button') {
        const disabled =
          p.getAttribute('aria-disabled') === 'true' ||
          p.getAttribute('tabindex') === '-1';
        if (!disabled) return true;
        break;
      }
      p = p.parentElement;
    }
  }
  return false;
}
"""
    try:
        return bool(page.evaluate(js))
    except Exception:
        return False


def wait_instagram_manual_captcha(
    page,
    *,
    reload_frame: bool = False,
    on_manual_captcha=None,
) -> None:
    """
    Бессрочно ждать, пока человек пройдёт капчу (кнопка «Далее»/Next активна),
    затем нажать её. Если уже экран кода — выйти без клика.

    ``reload_frame`` — сбросить/перезагрузить iframe после неудачного RuCaptcha.
    """
    if _is_confirmation_code_screen(page):
        _log("Instagram: капча не нужна — уже экран кода.")
        return

    if reload_frame:
        try:
            reload_instagram_captcha_frame(page)
        except Exception as e:
            _log(f"Instagram: reload фрейма капчи не удался: {e!r}")

    if _is_confirmation_code_screen(page):
        _log("Instagram: после reload уже экран кода.")
        return

    _log(
        "Instagram: капча — жду ручного прохождения "
        "(пока не станет активной кнопка «Далее»/Next)…"
    )
    if callable(on_manual_captcha):
        try:
            on_manual_captcha()
        except Exception as e:
            _log(f"Instagram: on_manual_captcha: {e!r}")

    started = time.monotonic()
    last_log = started
    while True:
        if _is_confirmation_code_screen(page):
            _log("Instagram: во время ожидания капчи открылся экран кода.")
            return
        if _next_button_enabled(page):
            _log("Instagram: «Далее» активна — капча пройдена вручную.")
            break
        now = time.monotonic()
        if now - last_log >= _MANUAL_CAPTCHA_LOG_EVERY_S:
            waited = int(now - started)
            _log(
                f"Instagram: всё ещё жду ручную капчу… ({waited} с). "
                "Пройдите капчу в окне браузера."
            )
            last_log = now
        page.wait_for_timeout(800)

    if _click_by_text(page, _NEXT_RE, prefer_link=False):
        _log("Instagram: нажали «Далее» после капчи.")
        page.wait_for_timeout(800)
        return

    # Кнопка активна, но текст не сматчился — ещё раз попробовать.
    try:
        page.get_by_role("button", name=_NEXT_RE).first.click(timeout=5000)
        _log("Instagram: нажали «Далее» (role=button) после капчи.")
        page.wait_for_timeout(800)
        return
    except Exception:
        pass
    raise RuntimeError(
        f"Instagram: «Далее» стала активной, но клик не удался (URL={page.url!r})"
    )


def wait_instagram_after_signup(
    page,
    *,
    rucaptcha_api_key: str = "",
    on_manual_captcha=None,
) -> None:
    """После «Отправить»: условия → RuCaptcha Proxyless (2×) или ручная капча / экран кода."""
    # Сразу после Submit иногда показывают «agree to our terms».
    page.wait_for_timeout(600)
    accept_instagram_terms_if_present(page, max_seconds=15.0)
    outcome = _wait_captcha_or_code_screen(page)
    if outcome == "code":
        return
    # createTask только спустя N секунд после появления iframe.
    settle_ms = int(max(0.0, float(_CAPTCHA_IFRAME_SETTLE_S)) * 1000)
    if settle_ms > 0:
        _log(
            f"Instagram: жду {_CAPTCHA_IFRAME_SETTLE_S:.0f} с после iframe "
            "перед запросом в RuCaptcha…"
        )
        page.wait_for_timeout(settle_ms)
        if _is_confirmation_code_screen(page):
            _log("Instagram: за время паузы уже экран кода.")
            return
    solved = try_solve_instagram_captcha_rucaptcha(
        page, rucaptcha_api_key=rucaptcha_api_key
    )
    if not solved:
        # После inject/RuCaptcha виджет часто «ломается» — обновим фрейм.
        wait_instagram_manual_captcha(
            page,
            reload_frame=True,
            on_manual_captcha=on_manual_captcha,
        )
    if _is_confirmation_code_screen(page):
        return
    wait_instagram_confirmation_code_screen(page)


def wait_instagram_confirmation_code_screen(
    page,
    *,
    max_seconds: float = _CONFIRM_SCREEN_MAX_S,
) -> None:
    """Ждать экран «Введите код подтверждения»."""
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        accept_instagram_terms_if_present(page, max_seconds=8.0)
        try:
            heading = page.get_by_text(_CONFIRM_CODE_HEADING_RE).first
            if heading.count() > 0 and heading.is_visible(timeout=400):
                _log("Instagram: экран кода подтверждения.")
                return
        except Exception:
            pass
        try:
            field = page.locator('input[maxlength="6"]').first
            if field.count() > 0 and field.is_visible(timeout=300):
                _log("Instagram: поле кода подтверждения видно.")
                return
        except Exception:
            pass
        page.wait_for_timeout(500)
    raise RuntimeError(
        f"Instagram: экран кода подтверждения не появился за {max_seconds:.0f} с "
        f"(URL={page.url!r})"
    )


def fill_instagram_confirmation_code(page, code: str) -> None:
    """Ввести 6-значный код в поле «Код подтверждения»."""
    code = (code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        raise RuntimeError(f"Instagram: ожидался 6-значный код, получили {code!r}")

    filled = False
    try:
        _fill_labeled_input(
            page,
            ["Код подтверждения", "Confirmation code", "Security code"],
            code,
        )
        filled = True
    except Exception as e:
        _log(f"Instagram: fill по label не сработал: {e!r}")

    if not filled:
        try:
            inp = page.locator('input[maxlength="6"]').first
            if inp.count() > 0 and inp.is_visible(timeout=2000):
                inp.fill(code, timeout=10_000)
                filled = True
        except Exception as e:
            raise RuntimeError(
                f"Instagram: не удалось ввести код подтверждения: {e!r}"
            ) from e

    if not filled:
        raise RuntimeError("Instagram: поле кода подтверждения не найдено.")
    _log(f"Instagram: ввели код {code}.")


def click_instagram_continue_after_code(
    page,
    *,
    max_seconds: float = 30.0,
) -> None:
    """Нажать «Продолжить» / Continue после ввода кода."""
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        if _click_by_text(page, _CONTINUE_RE, prefer_link=False):
            _log("Instagram: нажали «Продолжить».")
            page.wait_for_timeout(800)
            return
        # Bloks-кнопка с aria-label="Continue"
        try:
            btn = page.locator(
                '[role="button"][aria-label="Continue" i], '
                '[role="button"][aria-label="Продолжить" i]'
            ).first
            if btn.count() > 0 and btn.is_visible(timeout=300):
                disabled = (btn.get_attribute("aria-disabled") or "").lower() == "true"
                if not disabled:
                    btn.click(timeout=5000)
                    _log("Instagram: нажали Continue (aria-label).")
                    page.wait_for_timeout(800)
                    return
        except Exception:
            pass
        page.wait_for_timeout(400)
    raise RuntimeError(
        f"Instagram: кнопка «Продолжить» не найдена за {max_seconds:.0f} с "
        f"(URL={page.url!r})"
    )


def _is_human_confirm_intro_screen(page) -> bool:
    """Экран «Confirm you're human to use your account…» с кнопкой Continue."""
    try:
        heading = page.locator(
            '[role="heading"][aria-label*="Confirm you" i], '
            '[role="heading"][aria-label*="human" i]'
        ).first
        if heading.count() > 0 and heading.is_visible(timeout=300):
            label = (heading.get_attribute("aria-label") or "").strip()
            if _HUMAN_CONFIRM_RE.search(label) and "account" in label.lower():
                return True
    except Exception:
        pass
    try:
        text = page.get_by_text(_HUMAN_CONFIRM_RE).first
        if text.count() > 0 and text.is_visible(timeout=300):
            # Intro обычно с «to use your account» / Takes about 30 seconds.
            try:
                tip = page.get_by_text(
                    re.compile(r"takes\s+about\s+30\s+seconds|около\s+30\s+секунд", re.I)
                ).first
                if tip.count() > 0 and tip.is_visible(timeout=200):
                    return True
            except Exception:
                pass
            # Без image-captcha поля — считаем intro.
            if not _is_image_captcha_screen(page):
                body = ""
                try:
                    body = page.locator("body").inner_text(timeout=500) or ""
                except Exception:
                    pass
                if "to use your account" in body.lower() or "account," in body.lower():
                    return True
    except Exception:
        pass
    return False


def _is_image_captcha_screen(page) -> bool:
    """Экран картинки Facebook captcha + поле кода (bloks TextInput)."""
    try:
        ta = page.locator(
            'textarea[data-bloks-name="bk.components.TextInput"][maxlength="6"], '
            'textarea[type="tel"][maxlength="6"], '
            'textarea[placeholder*="code from the image" i], '
            'textarea[placeholder*="код с картинки" i], '
            'input[placeholder*="code from the image" i]'
        ).first
        if ta.count() > 0 and ta.is_visible(timeout=300):
            return True
    except Exception:
        pass
    try:
        img = page.locator(
            'img[src*="facebook.com/captcha/tfbimage"], '
            'img[src*="captcha_challenge"]'
        ).first
        if img.count() > 0 and img.is_visible(timeout=300):
            return True
    except Exception:
        pass
    try:
        # Подпись рядом с полем (placeholder у textarea часто пустой).
        label = page.get_by_text(_IMAGE_CAPTCHA_PLACEHOLDER_RE).first
        if label.count() > 0 and label.is_visible(timeout=300):
            return True
    except Exception:
        pass
    try:
        ph = page.get_by_placeholder(_IMAGE_CAPTCHA_PLACEHOLDER_RE).first
        if ph.count() > 0 and ph.is_visible(timeout=300):
            return True
    except Exception:
        pass
    return False


def _instagram_home_ready(page, username: str) -> bool:
    """Главная с ником уже видна (без ожидания)."""
    username = (username or "").strip().lstrip("@")
    if not username:
        return False
    href_re = re.compile(rf"/{re.escape(username)}/?$", re.IGNORECASE)
    name_re = re.compile(rf"^{re.escape(username)}$", re.IGNORECASE)
    try:
        link = page.locator(f'a[href="/{username}/"], a[href="/{username}"]').first
        if link.count() > 0 and link.is_visible(timeout=200):
            return True
    except Exception:
        pass
    try:
        link = page.locator("a[href*='instagram.com/']").filter(has_text=name_re).first
        if link.count() > 0 and link.is_visible(timeout=150):
            return True
    except Exception:
        pass
    try:
        url = (page.url or "").lower()
        if "emailsignup" in url or "accounts/signup" in url:
            return False
        text = page.get_by_text(name_re).first
        if text.count() > 0 and text.is_visible(timeout=150):
            if "instagram.com" in url and (
                "/accounts/" not in url
                or "onetap" in url
                or url.rstrip("/").endswith("instagram.com")
            ):
                return True
    except Exception:
        pass
    try:
        any_href = page.locator("a[href]").filter(
            has=page.locator(f'[href*="/{username}"]')
        )
        for i in range(min(any_href.count(), 8)):
            el = any_href.nth(i)
            href = (el.get_attribute("href") or "")
            if href_re.search(href.split("?")[0]) and el.is_visible(timeout=100):
                return True
    except Exception:
        pass
    return False


def click_instagram_human_confirm_continue(page, *, max_seconds: float = 30.0) -> bool:
    """Нажать Continue на intro «Confirm you're human…». True если кликнули."""
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        if not _is_human_confirm_intro_screen(page):
            return False
        try:
            btn = page.locator(
                '[role="button"][aria-label="Continue" i], '
                '[role="button"][aria-label="Продолжить" i]'
            ).first
            if btn.count() > 0 and btn.is_visible(timeout=400):
                disabled = (btn.get_attribute("aria-disabled") or "").lower() == "true"
                if not disabled and btn.get_attribute("disabled") is None:
                    btn.click(timeout=8000)
                    _log("Instagram: Confirm you're human — нажали Continue.")
                    page.wait_for_timeout(1000)
                    return True
        except Exception:
            pass
        if _click_by_text(page, _CONTINUE_RE, prefer_link=False):
            _log("Instagram: Confirm you're human — нажали Continue (текст).")
            page.wait_for_timeout(1000)
            return True
        page.wait_for_timeout(400)
    return False


def wait_instagram_image_captcha_manual(
    page,
    username: str = "",
    *,
    on_manual_captcha=None,
) -> None:
    """
    Image-капча (в т.ч. /accounts/suspended): уведомление + бессрочное ожидание,
    пока человек введёт код и экран капчи закроется.
    """
    if _image_captcha_passed(page, username):
        return

    if callable(on_manual_captcha):
        try:
            on_manual_captcha()
        except Exception as e:
            _log(f"Instagram: on_manual_captcha (image): {e!r}")

    _log(
        "Instagram: image captcha — жду ручного прохождения "
        "(код с картинки → Next)…"
    )
    started = time.monotonic()
    last_log = started
    while True:
        if _image_captcha_passed(page, username):
            _log("Instagram: image captcha пройдена (ручной ввод).")
            return
        if _next_button_enabled(page):
            _log("Instagram: Next активна после image captcha — нажимаем.")
            if _click_image_captcha_next(page):
                page.wait_for_timeout(1200)
                continue
        now = time.monotonic()
        if now - last_log >= _MANUAL_CAPTCHA_LOG_EVERY_S:
            waited = int(now - started)
            _log(
                f"Instagram: всё ещё жду image captcha… ({waited} с). "
                "Введите код с картинки в окне браузера."
            )
            last_log = now
        page.wait_for_timeout(800)


def _image_captcha_passed(page, username: str = "") -> bool:
    """Капча пройдена: главная / форма signup / ушли с suspended и поля кода."""
    uname = (username or "").strip().lstrip("@")
    if uname and _instagram_home_ready(page, uname):
        return True
    if _signup_form_visible(page):
        return True
    if _is_image_captcha_screen(page):
        return False
    if _is_accounts_suspended(page):
        return False
    return True


def _image_captcha_img_locator(page):
    return page.locator(
        'img[src*="facebook.com/captcha/tfbimage"], '
        'img[src*="captcha_challenge"]'
    ).first


def _image_captcha_input_locator(page):
    """
    Bloks TextInput: placeholder часто пустой, подпись в соседнем span
    («Enter the code from the image»), type=tel maxlength=6.
    """
    # 1) Точный bloks TextInput
    for sel in (
        'textarea[data-bloks-name="bk.components.TextInput"][maxlength="6"]',
        'textarea[type="tel"][maxlength="6"]',
        'textarea[data-bloks-name="bk.components.TextInput"]',
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=400):
                return loc
        except Exception:
            pass

    # 2) Textarea рядом с подписью (placeholder пустой)
    try:
        label = page.get_by_text(_IMAGE_CAPTCHA_PLACEHOLDER_RE).first
        if label.count() > 0 and label.is_visible(timeout=400):
            root = label.locator(
                "xpath=ancestor::div[.//textarea[@maxlength='6' or "
                "@data-bloks-name='bk.components.TextInput']][1]"
            )
            ta = root.locator(
                'textarea[data-bloks-name="bk.components.TextInput"], '
                'textarea[maxlength="6"], textarea'
            ).first
            if ta.count() > 0 and ta.is_visible(timeout=400):
                return ta
    except Exception:
        pass

    for sel in (
        'textarea[placeholder*="code from the image" i]',
        'textarea[placeholder*="код с картинки" i]',
        'textarea[maxlength="6"]',
        'input[maxlength="6"]',
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=300):
                return loc
        except Exception:
            pass
    return None


def _capture_image_captcha_base64(page) -> str:
    """Снять картинку капчи → clean base64 для RuCaptcha ImageToTextTask."""
    img = _image_captcha_img_locator(page)
    if img.count() == 0 or not img.is_visible(timeout=2000):
        raise RuntimeError("Instagram: не нашли img image captcha.")

    src = ""
    try:
        src = (img.get_attribute("src") or "").strip()
    except Exception:
        src = ""

    if src.startswith("http"):
        try:
            resp = page.request.get(src, timeout=30000)
            if resp.ok:
                raw = resp.body()
                if raw:
                    return base64.b64encode(raw).decode("ascii")
        except Exception as e:
            _log(f"Instagram: download captcha img failed, screenshot: {e!r}")

    png = img.screenshot(type="png")
    if not png:
        raise RuntimeError("Instagram: пустой screenshot image captcha.")
    return base64.b64encode(png).decode("ascii")


def _read_image_captcha_field_value(field) -> str:
    try:
        val = field.input_value(timeout=1000)
        if val is not None:
            return str(val)
    except Exception:
        pass
    try:
        return str(field.evaluate("el => el.value || el.textContent || ''") or "")
    except Exception:
        return ""


def _fill_image_captcha_code(page, code: str) -> None:
    """
    Ввод в bloks TextInput.

    Нельзя ставить el.value через JS — DOM меняется, а React/bloks state нет
    (счётчик пустой, Next не активируется). Нужны реальные клавиши.
    """
    raw = (code or "").strip()
    cleaned = re.sub(r"\D+", "", raw)
    if not cleaned:
        cleaned = re.sub(r"\s+", "", raw)
    _log(f"Instagram: RuCaptcha код raw={raw!r}, cleaned={cleaned!r}")
    if not cleaned:
        raise RuntimeError(f"Instagram: пустой код image captcha от RuCaptcha: {raw!r}")

    field = _image_captcha_input_locator(page)
    if field is None:
        raise RuntimeError("Instagram: не нашли поле кода image captcha.")

    try:
        meta = field.evaluate(
            """el => ({
                tag: el.tagName,
                type: el.getAttribute('type') || '',
                placeholder: el.getAttribute('placeholder') || '',
                bloks: el.getAttribute('data-bloks-name') || '',
                max: el.getAttribute('maxlength') || '',
                value: el.value || '',
            })"""
        )
        _log(f"Instagram: поле image captcha meta={meta!r}")
    except Exception as e:
        _log(f"Instagram: meta поля image captcha: {e!r}")

    # Родители с pointer-events:none — force click + явный focus.
    try:
        field.click(force=True, timeout=8000)
    except Exception as e:
        _log(f"Instagram: force click image captcha: {e!r}")
    try:
        field.evaluate("el => { el.focus(); el.click(); }")
    except Exception:
        pass
    page.wait_for_timeout(200)

    # Сброс DOM+state клавишами (не через fill/JS value).
    for combo in ("Control+A", "Meta+A"):
        try:
            page.keyboard.press(combo)
            break
        except Exception:
            pass
    try:
        page.keyboard.press("Backspace")
    except Exception:
        pass
    page.wait_for_timeout(100)

    # Реальный ввод — React/bloks слушает keydown/input.
    try:
        page.keyboard.type(cleaned, delay=120)
        _log(f"Instagram: keyboard.type отправил {cleaned!r}")
    except Exception as e:
        _log(f"Instagram: keyboard.type не сработал: {e!r}")
        try:
            field.press_sequentially(cleaned, delay=120)
            _log(f"Instagram: press_sequentially отправил {cleaned!r}")
        except Exception as e2:
            raise RuntimeError(
                f"Instagram: не удалось напечатать код image captcha: {e2!r}"
            ) from e2

    page.wait_for_timeout(300)
    actual = re.sub(r"\D+", "", _read_image_captcha_field_value(field).strip())
    _log(f"Instagram: в поле image captcha сейчас {actual!r} (ждали {cleaned!r})")
    if actual != cleaned:
        # Ещё одна попытка: посимвольно через keyboard.press
        _log("Instagram: повторный посимвольный ввод image captcha…")
        try:
            field.click(force=True, timeout=5000)
            field.evaluate("el => el.focus()")
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            for ch in cleaned:
                page.keyboard.press(ch)
                page.wait_for_timeout(80)
        except Exception as e:
            _log(f"Instagram: посимвольный ввод: {e!r}")
        actual = re.sub(r"\D+", "", _read_image_captcha_field_value(field).strip())
        _log(f"Instagram: после повтора в поле {actual!r}")
        if actual != cleaned:
            raise RuntimeError(
                f"Instagram: код не попал в bloks TextInput: "
                f"ждали {cleaned!r}, в поле {actual!r}"
            )
    _log(f"Instagram: ввели код image captcha {cleaned!r}.")


def _click_image_captcha_next(page) -> bool:
    """Нажать Next/Далее на экране image captcha."""
    if _click_instagram_next(page):
        return True
    try:
        btn = page.locator(
            '[role="button"][aria-label="Next" i], '
            '[role="button"][aria-label="Далее" i]'
        ).first
        if btn.count() > 0 and btn.is_visible(timeout=400):
            disabled = (
                (btn.get_attribute("aria-disabled") or "").lower() == "true"
                or btn.get_attribute("disabled") is not None
            )
            if not disabled:
                btn.click(timeout=5000)
                _log("Instagram: нажали Next (aria-label) после image captcha.")
                return True
    except Exception:
        pass
    return False


def _wait_image_captcha_ui(page, *, max_seconds: float = 30.0) -> bool:
    """Дождаться UI капчи на suspended / уже открытом экране."""
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        if _image_captcha_passed(page, ""):
            return False
        if _is_image_captcha_screen(page):
            return True
        if not _is_accounts_suspended(page):
            return False
        page.wait_for_timeout(400)
    return _is_image_captcha_screen(page)


def try_solve_instagram_image_captcha_rucaptcha(
    page,
    username: str = "",
    *,
    rucaptcha_api_key: str,
    retries: int = 2,
) -> bool:
    """
    Решить digit image captcha через RuCaptcha ImageToTextTask.
    True если ушли с экрана / открылась главная / signup.
    """
    from zaliver.captcha.rucaptcha import RuCaptchaError, solve_image_to_text

    api_key = (rucaptcha_api_key or "").strip()
    if not api_key:
        return False

    attempts = max(1, int(retries))
    for attempt in range(1, attempts + 1):
        if _image_captcha_passed(page, username):
            return True
        if not _is_image_captcha_screen(page):
            if _is_accounts_suspended(page):
                if not _wait_image_captcha_ui(page, max_seconds=15.0):
                    if _image_captcha_passed(page, username):
                        return True
                    _log(
                        "Instagram: suspended без UI капчи "
                        f"(попытка {attempt}/{attempts})."
                    )
                    continue
            else:
                return True

        _log(
            f"Instagram: image captcha → RuCaptcha ImageToText "
            f"попытка {attempt}/{attempts}…"
        )
        try:
            body_b64 = _capture_image_captcha_base64(page)
            text = solve_image_to_text(
                api_key,
                body_base64=body_b64,
                numeric=1,
                case=False,
                min_length=4,
                max_length=6,
                comment="цифры с картинки Instagram",
                retries=1,
                log=_log,
            )
            _log(f"Instagram: RuCaptcha вернула код image captcha: {text!r}")
        except (RuCaptchaError, RuntimeError) as e:
            _log(f"Instagram: RuCaptcha image captcha неудача: {e}")
            continue

        try:
            _fill_image_captcha_code(page, text)
        except Exception as e:
            _log(f"Instagram: не ввели код image captcha: {e!r}")
            continue

        # Ждём активации Next и клик / уход с экрана.
        wait_until = time.monotonic() + 20.0
        clicked = False
        while time.monotonic() < wait_until:
            if _image_captcha_passed(page, username):
                _log("Instagram: image captcha решена.")
                return True
            if _next_button_enabled(page):
                if _click_image_captcha_next(page):
                    clicked = True
                    page.wait_for_timeout(1500)
                    if _image_captcha_passed(page, username):
                        _log("Instagram: image captcha решена (после Next).")
                        return True
                    # Остались на image captcha — возможно неверный код.
                    if _is_image_captcha_screen(page) or _is_accounts_suspended(page):
                        break
            page.wait_for_timeout(400)

        if not clicked and (
            _is_image_captcha_screen(page) or _is_accounts_suspended(page)
        ):
            # Bloks-кнопка иногда без disabled — пробуем клик один раз.
            if _click_image_captcha_next(page):
                clicked = True
                page.wait_for_timeout(1500)
                if _image_captcha_passed(page, username):
                    return True

        if clicked and not _image_captcha_passed(page, username):
            _log(
                f"Instagram: после кода RuCaptcha всё ещё image captcha "
                f"(попытка {attempt}/{attempts})."
            )
            continue
        if not clicked:
            _log(
                f"Instagram: Next не нажалась после кода RuCaptcha "
                f"(попытка {attempt}/{attempts})."
            )

    return _image_captcha_passed(page, username)


def resolve_instagram_image_captcha(
    page,
    *,
    username: str = "",
    rucaptcha_api_key: str = "",
    on_manual_captcha=None,
) -> None:
    """Больше не решаем: image/SMS captcha → тег и остановка авторега."""
    del username, rucaptcha_api_key, on_manual_captcha
    abort_if_instagram_sms_image_captcha(page)


def handle_instagram_human_confirmation(
    page,
    username: str,
    *,
    rucaptcha_api_key: str = "",
    on_manual_captcha=None,
    appear_timeout_s: float = 45.0,
) -> None:
    """
    После кода из почты: опционально «Confirm you're human» → Continue →
    image captcha → стоп с тегом SMS (не решаем).
    """
    del rucaptcha_api_key, on_manual_captcha
    if _instagram_home_ready(page, username):
        return

    deadline = time.monotonic() + appear_timeout_s
    saw_intro = False
    saw_image = False
    while time.monotonic() < deadline:
        if _instagram_home_ready(page, username):
            return
        if _is_accounts_suspended(page) or _is_image_captcha_screen(page):
            saw_image = True
            break
        if _is_human_confirm_intro_screen(page):
            saw_intro = True
            break
        page.wait_for_timeout(500)

    if not saw_intro and not saw_image:
        _log(
            "Instagram: экран Confirm you're human не появился — "
            "идём к проверке главной."
        )
        return

    if saw_intro:
        _log("Instagram: экран Confirm you're human (intro).")
        if not click_instagram_human_confirm_continue(page):
            _log("Instagram: не удалось нажать Continue на intro — ждём image captcha.")
        wait_img = time.monotonic() + 60.0
        while time.monotonic() < wait_img:
            if _instagram_home_ready(page, username):
                return
            if _is_accounts_suspended(page) or _is_image_captcha_screen(page):
                saw_image = True
                break
            page.wait_for_timeout(500)

    if (
        saw_image
        or _is_accounts_suspended(page)
        or _is_image_captcha_screen(page)
    ):
        abort_if_instagram_sms_image_captcha(page)
        return

    _log("Instagram: после intro image captcha не появилась.")

def wait_instagram_home_with_username(
    page,
    username: str,
    *,
    max_seconds: float = _HOME_AFTER_CODE_MAX_S,
) -> None:
    """
    До ~1 мин ждать главную после подтверждения:
    виден ник / ссылка профиля /username/.
    """
    username = (username or "").strip().lstrip("@")
    if not username:
        raise RuntimeError("Instagram: пустой username для проверки главной.")

    href_re = re.compile(rf"/{re.escape(username)}/?$", re.IGNORECASE)
    name_re = re.compile(rf"^{re.escape(username)}$", re.IGNORECASE)
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        try:
            link = page.locator(f'a[href="/{username}/"], a[href="/{username}"]').first
            if link.count() > 0 and link.is_visible(timeout=300):
                _log(f"Instagram: главная — профиль /{username}/.")
                return
        except Exception:
            pass
        try:
            link = page.locator("a[href*='instagram.com/']").filter(has_text=name_re).first
            if link.count() > 0 and link.is_visible(timeout=200):
                _log(f"Instagram: главная — ссылка с ником {username!r}.")
                return
        except Exception:
            pass
        try:
            text = page.get_by_text(name_re).first
            if text.count() > 0 and text.is_visible(timeout=200):
                # На signup тоже может мелькать username — требуем не signup URL.
                url = (page.url or "").lower()
                if "emailsignup" not in url and "accounts/signup" not in url:
                    if "instagram.com" in url and (
                        "/accounts/" not in url
                        or "onetap" in url
                        or url.rstrip("/").endswith("instagram.com")
                    ):
                        _log(f"Instagram: главная — видим ник {username!r}.")
                        return
        except Exception:
            pass
        try:
            # Любая ссылка профиля с этим путём.
            any_href = page.locator("a[href]").filter(has=page.locator(f'[href*="/{username}"]'))
            for i in range(min(any_href.count(), 8)):
                el = any_href.nth(i)
                href = (el.get_attribute("href") or "")
                if href_re.search(href.split("?")[0]) and el.is_visible(timeout=150):
                    _log(f"Instagram: главная — href={href!r}.")
                    return
        except Exception:
            pass
        page.wait_for_timeout(1000)

    raise RuntimeError(
        f"Instagram: после кода не дождались главной с ником {username!r} "
        f"за {max_seconds:.0f} с (URL={page.url!r})"
    )


def complete_instagram_email_confirmation(
    ig_page,
    gmail_page,
    username: str,
    *,
    rucaptcha_api_key: str = "",
    on_manual_captcha=None,
) -> None:
    """Экран кода → Gmail (код) → ввод → Продолжить → (human check) → главная."""
    from zaliver.instagram_upload.gmail_confirmation_code import (
        fetch_instagram_confirmation_code_from_gmail,
    )

    wait_instagram_confirmation_code_screen(ig_page)
    code = fetch_instagram_confirmation_code_from_gmail(gmail_page)
    try:
        ig_page.bring_to_front()
    except Exception:
        pass
    ig_page.wait_for_timeout(500)
    wait_instagram_confirmation_code_screen(ig_page, max_seconds=30.0)
    fill_instagram_confirmation_code(ig_page, code)
    click_instagram_continue_after_code(ig_page)
    handle_instagram_human_confirmation(
        ig_page,
        username,
        rucaptcha_api_key=rucaptcha_api_key,
        on_manual_captcha=on_manual_captcha,
    )
    wait_instagram_home_with_username(ig_page, username)
    _log(f"Instagram: регистрация подтверждена, username={username!r}.")


def _page_url(page) -> str:
    try:
        return (page.url or "").strip()
    except Exception:
        return ""


def _is_instagram_url(url: str) -> bool:
    u = (url or "").strip().lower()
    return "instagram.com" in u and not u.startswith("about:")


def _navigate_page_to(page, url: str, *, label: str = "Instagram") -> None:
    """Надёжный goto для CDP/антидетекта (часто new_page зависает на about:blank)."""
    last_err: Exception | None = None
    strategies = (
        ("commit", lambda: page.goto(url, wait_until="commit", timeout=90_000)),
        (
            "domcontentloaded",
            lambda: page.goto(url, wait_until="domcontentloaded", timeout=90_000),
        ),
        (
            "location.assign",
            lambda: page.evaluate("(u) => { location.assign(u); }", url),
        ),
        (
            "location.href",
            lambda: page.evaluate("(u) => { window.location.href = u; }", url),
        ),
    )
    for name, action in strategies:
        try:
            try:
                page.bring_to_front()
            except Exception:
                pass
            _log(f"{label}: навигация ({name}) → {url}")
            action()
            try:
                page.wait_for_load_state("domcontentloaded", timeout=60_000)
            except Exception as e:
                _log(f"{label}: wait domcontentloaded после {name}: {e!r}")
            page.wait_for_timeout(800)
            cur = _page_url(page)
            if _is_instagram_url(cur):
                _log(f"{label}: OK, URL={cur!r}")
                return
            if cur and cur.lower() != "about:blank":
                # Уже не blank (редирект/логин и т.п.) — считаем успехом.
                _log(f"{label}: загрузилось URL={cur!r}")
                return
            _log(f"{label}: после {name} всё ещё {cur!r}")
        except Exception as e:
            last_err = e
            _log(f"{label}: {name} не удалось: {e!r}")
    cur = _page_url(page)
    raise RuntimeError(
        f"{label}: не удалось открыть {url!r}, текущий URL={cur!r}"
        + (f" ({last_err!r})" if last_err else "")
    )


def open_instagram_signup_tab(
    gmail_page,
    *,
    password: str = "",
    rucaptcha_api_key: str = "",
    on_manual_captcha=None,
):
    """Вторая вкладка: Instagram → сохранённый профиль / «Создать новый аккаунт»."""
    context = gmail_page.context
    _log(f"Instagram: открываем вкладку {INSTAGRAM_URL}")

    ig_page = None
    # Сначала Playwright new_page + goto — без лишней about:blank от window.open.
    try:
        ig_page = context.new_page()
        _navigate_page_to(ig_page, INSTAGRAM_URL)
    except Exception as e:
        _log(f"Instagram: new_page не сработал ({e!r}), пробуем window.open…")
        failed = ig_page
        ig_page = None
        if failed is not None:
            try:
                failed.close()
            except Exception:
                pass
        try:
            try:
                gmail_page.bring_to_front()
            except Exception:
                pass
            with context.expect_page(timeout=25_000) as page_info:
                gmail_page.evaluate(
                    """(url) => {
                        const w = window.open(url, '_blank');
                        if (!w) throw new Error('window.open blocked');
                    }""",
                    INSTAGRAM_URL,
                )
            ig_page = page_info.value
            try:
                ig_page.wait_for_load_state("domcontentloaded", timeout=90_000)
            except Exception as e2:
                _log(f"Instagram: wait после window.open: {e2!r}")
            ig_page.wait_for_timeout(1000)
            cur = _page_url(ig_page)
            _log(f"Instagram: вкладка через window.open, URL={cur!r}")
            if not _is_instagram_url(cur):
                _navigate_page_to(ig_page, INSTAGRAM_URL)
        except Exception as e2:
            raise RuntimeError(
                f"Instagram: не удалось открыть вкладку "
                f"(new_page: {e!r}; window.open: {e2!r})"
            ) from e2

    if ig_page is None:
        raise RuntimeError("Instagram: не удалось создать вкладку.")

    try:
        ig_page.bring_to_front()
    except Exception:
        pass

    # /consent или модалка «Allow the use of cookies» — Accept all → главная.
    accept_instagram_cookie_consent_if_present(ig_page)

    # Сразу на suspended — циферная капча: стоп с тегом SMS.
    abort_if_instagram_sms_image_captcha(ig_page)
    # Уже залогинены (лента) — регистрация не нужна.
    _raise_if_already_logged_in(ig_page)

    # Сохранённый профиль: Continue → пароль → Log in → Save info.
    pwd = (password or "").strip()
    if pwd and _is_saved_profile_chooser_screen(ig_page):
        uname = try_instagram_saved_profile_login(ig_page, pwd)
        if uname:
            raise InstagramAlreadyLoggedInError(
                username=uname,
                detail="вход через сохранённый профиль (Continue)",
            )
        _log(
            "Instagram: вход через сохранённый профиль не удался — "
            "продолжаем к регистрации."
        )

    if not _signup_form_visible(ig_page):
        if not _click_by_text(ig_page, _CREATE_ACCOUNT_RE, prefer_link=True):
            # Прямой переход на signup, если кнопки нет (уже на login с другой вёрсткой).
            signup = "https://www.instagram.com/accounts/emailsignup/"
            _log(f"Instagram: кнопка создания не найдена — goto {signup}")
            _navigate_page_to(ig_page, signup)
            accept_instagram_cookie_consent_if_present(ig_page)
            # Залогиненная сессия часто редиректит emailsignup → главная.
            # Коротко ждём отрисовки ленты, не 90 с формы signup.
            settle = time.monotonic() + 8.0
            while time.monotonic() < settle:
                if _signup_form_visible(ig_page):
                    break
                if _instagram_already_logged_in(ig_page):
                    _raise_if_already_logged_in(ig_page)
                try:
                    ig_page.wait_for_timeout(500)
                except Exception:
                    break
            if not _signup_form_visible(ig_page):
                # Только если реально сессия / навбар залогиненного UI.
                _raise_if_already_logged_in(ig_page)
        else:
            _log("Instagram: нажали «Создать новый аккаунт».")

    _wait_signup_form(
        ig_page,
        rucaptcha_api_key=rucaptcha_api_key,
        on_manual_captcha=on_manual_captcha,
    )
    return ig_page


def fill_instagram_signup_form(
    page,
    credentials: GoogleLoginCredentials,
) -> str:
    """
    Заполнить форму регистрации и нажать «Отправить».
    Возвращает итоговое имя пользователя.
    """
    email = (credentials.email or "").strip()
    password = (credentials.password or "").strip()
    if not email:
        raise RuntimeError("Instagram: пустой yt_login (email) для регистрации.")
    if not password:
        raise RuntimeError("Instagram: пустой yt_password для регистрации.")

    local = email_local_part(email)
    if not local:
        raise RuntimeError(f"Instagram: не удалось взять локальную часть email {email!r}")

    _log(f"Instagram: email={email!r}, username/name={local!r}")

    _fill_labeled_input(
        page,
        [
            "Мобильный телефон или электронный адрес",
            "Номер мобильного телефона или электронный адрес",
            "Mobile number or email",
            "email",
        ],
        email,
    )
    page.wait_for_timeout(400)

    _fill_labeled_input(
        page,
        ["Пароль", "Password"],
        password,
    )
    page.wait_for_timeout(400)

    _fill_birthday(page)
    page.wait_for_timeout(400)

    _fill_labeled_input(
        page,
        ["Имя и фамилия", "Название", "Full name", "Name"],
        local,
    )
    page.wait_for_timeout(400)

    _fill_labeled_input(
        page,
        ["Имя пользователя", "Username"],
        local,
    )
    username = local

    # Проверка «имя занято» через ~5 с.
    _log("Instagram: ждём 5 с проверку имени пользователя…")
    page.wait_for_timeout(5000)
    if _username_taken_visible(page):
        suffix = random.randint(1, 1000)
        username = f"{local}{suffix}"
        _log(f"Instagram: имя занято — пробуем {username!r}")
        _fill_labeled_input(
            page,
            ["Имя пользователя", "Username"],
            username,
        )
        page.wait_for_timeout(5000)
        if _username_taken_visible(page):
            suffix = random.randint(1, 1000)
            username = f"{local}{suffix}"
            _log(f"Instagram: снова занято — пробуем {username!r}")
            _fill_labeled_input(
                page,
                ["Имя пользователя", "Username"],
                username,
            )
            page.wait_for_timeout(2500)

    if not _click_by_text(page, _SUBMIT_RE, prefer_link=False):
        # aria / текст «Отправить»
        try:
            page.get_by_text(
                re.compile(r"^отправить$|^submit$|^sign\s*up$", re.I)
            ).first.click(timeout=8000)
        except Exception as e:
            raise RuntimeError(
                f"Instagram: не нашли кнопку «Отправить»: {e!r}"
            ) from e
    _log("Instagram: нажали «Отправить».")
    return username


def run_instagram_registration_after_gmail(
    gmail_page,
    credentials: GoogleLoginCredentials | None,
    *,
    rucaptcha_api_key: str = "",
    on_manual_captcha=None,
) -> str:
    """
    Gmail уже открыт → Instagram signup → RuCaptcha/ручная капча → код из почты.
    Возвращает зарегистрированный username.

    Если в профиле уже выполнен вход в Instagram — считаем успехом
    (новый аккаунт создавать не нужно).
    """
    if credentials is None:
        raise RuntimeError("Instagram: нет credentials профиля (yt_login / yt_password).")
    try:
        ig_page = open_instagram_signup_tab(
            gmail_page,
            password=(credentials.password or "").strip(),
            rucaptcha_api_key=rucaptcha_api_key,
            on_manual_captcha=on_manual_captcha,
        )
    except InstagramAlreadyLoggedInError as e:
        username = (e.username or "").strip().lstrip("@")
        if not username:
            username = email_local_part(credentials.email or "") or "already_logged_in"
        _log(
            "Instagram: аккаунт уже доступен"
            + (f" (@{username})" if username else "")
            + " — регистрацию пропускаем, успех."
        )
        return username
    try:
        username = fill_instagram_signup_form(ig_page, credentials)
        wait_instagram_after_signup(
            ig_page,
            rucaptcha_api_key=rucaptcha_api_key,
            on_manual_captcha=on_manual_captcha,
        )
        complete_instagram_email_confirmation(
            ig_page,
            gmail_page,
            username,
            rucaptcha_api_key=rucaptcha_api_key,
            on_manual_captcha=on_manual_captcha,
        )
        return username
    except Exception:
        # Оставляем вкладку открытой для разбора.
        raise
