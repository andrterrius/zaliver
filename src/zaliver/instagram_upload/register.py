"""Регистрация аккаунта Instagram (после входа в Gmail)."""

from __future__ import annotations

import base64
import calendar
import random
import re
import time

from zaliver.instagram_upload.logutil import emit_instagram_log, instagram_entrypoint
from zaliver.youtube_upload.google_login import (
    GoogleLoginCredentials,
    random_birthday,
)
from zaliver.antydetect.profile_tags import (
    IG_REGISTER_ERROR_TAG,
    IG_REGISTER_SMS_ERROR_TAG,
)

INSTAGRAM_URL = "https://www.instagram.com/"
_IG_CAPTCHA_FALLBACK_URLS = (
    "https://www.instagram.com/accounts/emailsignup/",
    "https://www.instagram.com/",
    "https://instagram.com/accounts/emailsignup/",
    "https://instagram.com/",
    "https://www.facebook.com/",
    "https://facebook.com/",
)

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


class InstagramRegistrationFailedError(RuntimeError):
    """
    Явный баннер ошибки на форме signup
    («An error occurred during your registration…») — закрываем профиль.
    """

    def __init__(self, detail: str = "") -> None:
        base = IG_REGISTER_ERROR_TAG
        msg = f"{base}: {detail}" if (detail or "").strip() else base
        super().__init__(msg)

    @classmethod
    def matches(cls, err: str) -> bool:
        text = err or ""
        return IG_REGISTER_ERROR_TAG in text and IG_REGISTER_SMS_ERROR_TAG not in text


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
    """
    Human-check intro / /accounts/suspended / циферная image-капча —
    сразу ошибка с тегом SMS (авторег не продолжаем).
    """
    if not (
        _is_accounts_suspended(page)
        or _is_image_captcha_screen(page)
        or _is_human_confirm_intro_screen(page)
    ):
        return
    url = ""
    try:
        url = page.url or ""
    except Exception:
        url = ""
    _log(
        f"Instagram: human/SMS captcha — авторег остановлен "
        f"({IG_REGISTER_SMS_ERROR_TAG}), URL={url!r}"
    )
    raise InstagramSmsCaptchaError(f"URL={url!r}")


def _is_registration_failed_banner(page) -> bool:
    """Баннер «An error occurred during your registration…» на форме signup."""
    try:
        loc = page.get_by_text(_REGISTRATION_FAILED_BANNER_RE).first
        if loc.count() > 0 and loc.is_visible(timeout=400):
            return True
    except Exception:
        pass
    try:
        body = page.locator("body").inner_text(timeout=600) or ""
    except Exception:
        return False
    return bool(_REGISTRATION_FAILED_BANNER_RE.search(body))


def abort_if_instagram_registration_failed(page) -> None:
    """Явная ошибка регистрации на форме → обычный error-тег."""
    if not _is_registration_failed_banner(page):
        return
    url = ""
    try:
        url = page.url or ""
    except Exception:
        url = ""
    _log(
        f"Instagram: баннер ошибки регистрации — стоп "
        f"({IG_REGISTER_ERROR_TAG}), URL={url!r}"
    )
    raise InstagramRegistrationFailedError(f"URL={url!r}")


_CREATE_ACCOUNT_RE = re.compile(
    r"создать\s+новый\s+аккаунт|create\s+(new\s+)?account|sign\s*up",
    re.IGNORECASE,
)
_SUBMIT_RE = re.compile(r"^отправить$|^submit$|^sign\s*up$", re.IGNORECASE)
_NEXT_RE = re.compile(r"^далее$|^next$", re.IGNORECASE)
_CONTINUE_RE = re.compile(r"^продолжить$|^continue$", re.IGNORECASE)
_LOG_IN_RE = re.compile(r"^войти$|^log\s*in$", re.IGNORECASE)
_SAVE_INFO_RE = re.compile(
    r"^save(\s*info)?$|^сохранить(\s+данные)?$|^сохранить\s+информацию$",
    re.IGNORECASE,
)
_NOT_NOW_RE = re.compile(r"^не\s+сейчас$|^not\s+now$", re.IGNORECASE)
_CLOSE_BTN_RE = re.compile(r"^(Close|Закрыть)$", re.I)
_SCRAPING_WARNING_RE = re.compile(
    r"автоматизированн\w*\s+действи|"
    r"automated\s+behaviou?r|"
    r"suspected\s+automated|"
    r"we\s+suspect\s+automated",
    re.I,
)
_SAVE_LOGIN_INFO_HEADING_RE = re.compile(
    r"save\s+your\s+login\s+info|"
    r"сохраните\s+пароль|"
    r"сохранить\s+данные\s+для\s+входа|"
    r"сохранить\s+(данные\s+для\s+)?входа|"
    r"сохранить\s+информацию\s+для\s+входа",
    re.IGNORECASE,
)
# «Введены неверные данные для входа» / Incorrect login credentials
_WRONG_LOGIN_CREDENTIALS_RE = re.compile(
    r"incorrect\s+(login\s+)?(credentials|information|details)|"
    r"the\s+(password|login\s+info(rmation)?)\s+you\s+entered\s+is\s+incorrect|"
    r"login\s+info(rmation)?\s+you\s+entered\s+is\s+incorrect|"
    r"entered\s+an?\s+incorrect|"
    r"введены\s+неверные\s+данные(\s+для\s+входа)?|"
    r"неверн(ый|ые|ое)\s+(пароль|данные(\s+для\s+входа)?)|"
    r"find\s+your\s+account\s+and\s+log\s+in",
    re.IGNORECASE,
)
_LOGIN_2FA_CHALLENGE_RE = re.compile(
    r"go\s+to\s+your\s+authentication\s+app|"
    r"enter\s+the\s+6[- ]digit\s+code\s+for\s+this\s+account|"
    r"two[- ]factor\s+authentication\s+app|"
    r"перейд(ите|и)\s+в\s+(сво[её]\s+)?приложение\s+(для\s+)?аутентифика|"
    r"введите\s+6[- ]значный\s+код|"
    r"приложение\s+двухфакторной\s+аутентификации",
    re.IGNORECASE,
)
_TRUST_DEVICE_RE = re.compile(
    r"trust\s+this\s+device|"
    r"skip\s+this\s+step\s+from\s+now\s+on|"
    r"доверять\s+этому\s+устройству|"
    r"запомнить\s+это\s+устройство|"
    r"пропускать\s+этот\s+шаг",
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
    r"allow\s+all\s+cookies|"
    r"разрешить\s+все\s+(файлы\s+)?cookie|"
    r"принять\s+все\s+(файлы\s+)?cookie|"
    r"разрешить\s+все\b",
    re.IGNORECASE,
)
_DECLINE_OPTIONAL_COOKIES_RE = re.compile(
    r"decline\s+optional(\s+cookies)?|"
    r"отклонить\s+необязательные(\s+файлы\s+cookie)?",
    re.IGNORECASE,
)
_COOKIE_CONSENT_HEADING_RE = re.compile(
    r"allow\s+the\s+use\s+of\s+cookies|"
    r"разрешить\s+использование\s+(файлов\s+)?cookie|"
    r"разрешить\s+использование\s+файлов\s+cookie\s+от\s+instagram|"
    r"использовать\s+файлы\s+cookie|"
    r"cookie[s]?\s+(by|from)\s+instagram|"
    r"cookies?\s+from\s+instagram\s+on\s+this\s+browser|"
    r"файлов\s+cookie\s+от\s+instagram\s+в\s+этом\s+браузере",
    re.IGNORECASE,
)
# /consent/?flow=user_cookie_choice_v2 — primary Accept/Allow, не accordion.
_COOKIE_WORD_BUTTON_RE = re.compile(
    r"(allow|accept|разреш|принят).{0,40}cookie|cookie.{0,20}(allow|accept)",
    re.IGNORECASE,
)
# Accordion / Learn more на cookie-экране — не кликать.
_COOKIE_NON_ACTION_RE = re.compile(
    r"how\s+we\s+use|"
    r"what\s+are\s+cookies|"
    r"why\s+do\s+we\s+use|"
    r"learn\s+more|"
    r"see\s+more|"
    r"expand|"
    r"choose\s+cookies|"
    r"select\s+all|"
    r"optional\s+cookies|"
    r"your\s+cookie\s+choices|"
    r"meta\s+products|"
    r"подробнее|"
    r"узнать\s+больше|"
    r"как\s+мы\s+используем|"
    r"что\s+такое\s+cookie",
    re.IGNORECASE,
)
# Только код из письма. НЕ «enter the code» — совпадает с image captcha
# («Enter the code from the image»).
_CONFIRM_CODE_HEADING_RE = re.compile(
    r"введите\s+код\s+подтверждения|"
    r"проверьте\s+электронную\s+почту|"
    r"проверьте\s+(свою\s+)?почту|"
    r"check\s+your\s+email|"
    r"мы\s+отправили\s+код\s+сюда|"
    r"enter\s+(the\s+)?confirmation\s+code|"
    r"enter\s+(the\s+)?security\s+code|"
    r"confirmation\s+code",
    re.IGNORECASE,
)
_EMAIL_CODE_REJECTED_RE = re.compile(
    r"that\s+code\s+isn.?t\s+valid|"
    r"code\s+isn.?t\s+valid|"
    r"incorrect\s+(confirmation\s+)?code|"
    r"invalid\s+(confirmation\s+)?code|"
    r"wrong\s+(confirmation\s+)?code|"
    r"check\s+the\s+code\s+and\s+try\s+again|"
    r"please\s+check\s+the\s+code|"
    r"неверн(ый|ым)\s+код(\s+подтверждения)?|"
    r"код\s+недействителен|"
    r"проверьте\s+код|"
    r"запросите\s+новый\s+код",
    re.IGNORECASE,
)
_HUMAN_CONFIRM_RE = re.compile(
    r"confirm\s+you.?re\s+human|"
    r"подтвердите[,\s]+что\s+вы[\s\u00a0]*[—–\-]*[\s\u00a0]*человек|"
    r"подтвердите[,\s]+что\s+вы\s+не\s+робот|"
    r"чтобы\s+использовать\s+свой\s+аккаунт",
    re.IGNORECASE,
)
_HUMAN_CONFIRM_TIP_RE = re.compile(
    r"takes\s+about\s+30\s+seconds|"
    r"займет\s+около\s+30[\s\u00a0]*секунд|"
    r"займёт\s+около\s+30[\s\u00a0]*секунд|"
    r"около\s+30[\s\u00a0]*секунд",
    re.IGNORECASE,
)
_REGISTRATION_FAILED_BANNER_RE = re.compile(
    r"an\s+error\s+occurred\s+during\s+your\s+registration|"
    r"error\s+occurred\s+during\s+(your\s+)?registration|"
    r"во\s+время\s+регистрации\s+произошл[ао]\s+ошибк|"
    r"при\s+регистрации\s+произошл[ао]\s+ошибк|"
    r"ошибка\s+при\s+регистрации|"
    r"не\s+удалось\s+(завершить\s+)?регистрац|"
    r"registration\s+failed|"
    r"couldn.?t\s+complete\s+(your\s+)?registration",
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
# Пауза после появления iframe — дать расширению AntiCaptcha подняться.
_CAPTCHA_IFRAME_SETTLE_S = 10.0
_CONFIRM_SCREEN_MAX_S = 300.0
_HOME_AFTER_CODE_MAX_S = 60.0
# Ожидание авторешения капчи расширением AntiCaptcha в браузере.
_EXTENSION_CAPTCHA_MAX_S = 180.0
# Сколько раз пробовать код из почты, если экран не сменился.
_EMAIL_CODE_ATTEMPTS = 5
# Пауза после «Продолжить», чтобы понять: ушли с экрана кода или нет.
_AFTER_EMAIL_CODE_SETTLE_S = 15.0
_MANUAL_CAPTCHA_LOG_EVERY_S = 30.0
# Если руками долго не проходят — клик по капче (может пройти само / будит расширение).
_MANUAL_CAPTCHA_NUDGE_AFTER_S = 60.0
_MANUAL_CAPTCHA_NUDGE_EVERY_S = 45.0
# После обновления iframe — ждать прогрузки перед кликом.
_CAPTCHA_RELOAD_READY_MAX_S = 25.0
_CAPTCHA_IFRAME_SEL = (
    'iframe#captcha-recaptcha, iframe[src*="captcha"], '
    'iframe[src*="recaptcha"], iframe[src*="referer_frame"], '
    'iframe[title*="reCAPTCHA"], iframe[title*="recaptcha"]'
)
_RECAPTCHA_ANCHOR_SEL = (
    "#recaptcha-anchor, .recaptcha-checkbox-border, "
    ".rc-anchor-checkbox, span[role='checkbox']"
)


def _log(message: str) -> None:
    emit_instagram_log(message, tag="[instagram]")


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
    """URL /accounts/suspended — human-check / циферная капча Instagram."""
    url = ""
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    return "/accounts/suspended" in url


def _is_scraping_warning_url(url: str = "", page=None) -> bool:
    """URL /accounts/scraping_warning — предупреждение об автоматизации."""
    u = (url or "").lower()
    if not u and page is not None:
        try:
            u = (page.url or "").lower()
        except Exception:
            u = ""
    return "/accounts/scraping_warning" in u


def _scraping_warning_visible(page) -> bool:
    """Экран «We suspected automated actions» / RU-аналог."""
    if _is_scraping_warning_url(page=page):
        return True
    try:
        loc = page.get_by_text(_SCRAPING_WARNING_RE).first
        return loc.count() > 0 and loc.is_visible(timeout=400)
    except Exception:
        return False


def dismiss_instagram_scraping_warning_if_present(page) -> bool:
    """
    После входа: /accounts/scraping_warning → «Закрыть» / Close.
    True если нажали.
    """
    if not _scraping_warning_visible(page):
        return False
    _log(
        "Instagram: предупреждение об автоматизации "
        f"(URL={_page_url(page)!r}) — жмём «Закрыть»…"
    )
    try:
        btn = page.get_by_role("button", name=_CLOSE_BTN_RE).first
        if btn.count() > 0 and btn.is_visible(timeout=800):
            btn.click(timeout=8000)
            _log("Instagram: scraping_warning — нажали «Закрыть».")
            page.wait_for_timeout(1000)
            return True
    except Exception:
        pass
    try:
        btn = page.locator("button").filter(has_text=_CLOSE_BTN_RE).first
        if btn.count() > 0 and btn.is_visible(timeout=500):
            btn.click(timeout=8000, force=True)
            _log("Instagram: scraping_warning — нажали «Закрыть» (button).")
            page.wait_for_timeout(1000)
            return True
    except Exception:
        pass
    if _click_by_text(page, _CLOSE_BTN_RE, prefer_link=False):
        _log("Instagram: scraping_warning — нажали «Закрыть» (текст).")
        page.wait_for_timeout(1000)
        return True
    _log("Instagram: scraping_warning виден, но «Закрыть» не нажалась.")
    return False


def _human_confirm_mentions_account(text: str) -> bool:
    t = (text or "").lower()
    return "account" in t or "аккаунт" in t


def _signup_form_visible(page) -> bool:
    """Форма регистрации видна / URL emailsignup без suspended и без cookie-диалога."""
    try:
        if _is_cookie_consent_screen(page):
            return False
    except Exception:
        pass
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


_SIGN_UP_BTN_RE = re.compile(
    r"^зарегистрироваться$|^sign\s*up$|^create\s+new\s+account$|"
    r"^создать\s+новый\s+аккаунт$",
    re.IGNORECASE,
)
_OPEN_IG_APP_RE = re.compile(
    r"^открыть\s+instagram$|^open\s+(the\s+)?instagram(\s+app)?$",
    re.IGNORECASE,
)


def _instagram_login_form_visible(page) -> bool:
    """Экран входа (ещё не залогинены)."""
    if _is_classic_login_form_visible(page):
        return True
    if _is_mobile_logged_out_landing(page):
        return True
    try:
        if page.get_by_text(
            re.compile(
                r"phone\s+number,\s+username,\s+or\s+email|"
                r"номер\s+телефона,\s+имя\s+пользователя\s+или\s+эл|"
                r"имя\s+пользователя,\s+эл\.?\s*адрес|"
                r"username,\s+email\s+or\s+mobile",
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


def _is_mobile_logged_out_landing(page) -> bool:
    """
    Mobile splash без сохранённого профиля:
    «Войти» + «зарегистрироваться» (+ часто «Открыть Instagram»).
    """
    if _is_classic_login_form_visible(page) or _is_saved_profile_chooser_screen(page):
        return False
    try:
        login_btn = page.locator("button").filter(has_text=_LOG_IN_RE).first
        signup_btn = page.locator("button").filter(has_text=_SIGN_UP_BTN_RE).first
        if login_btn.count() > 0 and signup_btn.count() > 0:
            return True
    except Exception:
        pass
    try:
        login_btn = page.get_by_role("button", name=_LOG_IN_RE).first
        signup_btn = page.get_by_role("button", name=_SIGN_UP_BTN_RE).first
        if login_btn.count() > 0 and signup_btn.count() > 0:
            return True
    except Exception:
        pass
    return False


def _click_mobile_landing_log_in(page) -> bool:
    """На splash нажать «Войти» (не «Открыть Instagram»)."""
    try:
        buttons = page.locator("button")
        n = int(buttons.count())
        for i in range(min(n, 12)):
            target = buttons.nth(i)
            try:
                text = (target.inner_text(timeout=300) or "").strip()
            except Exception:
                continue
            if not text or _OPEN_IG_APP_RE.search(text):
                continue
            if not _LOG_IN_RE.fullmatch(text):
                continue
            try:
                target.click(timeout=8000)
                return True
            except Exception:
                try:
                    target.click(timeout=8000, force=True)
                    return True
                except Exception:
                    continue
    except Exception:
        pass
    try:
        btn = page.get_by_role("button", name=_LOG_IN_RE).first
        if btn.count() > 0:
            btn.click(timeout=8000, force=True)
            return True
    except Exception:
        pass
    return False


def _instagram_session_cookie_present(page) -> bool:
    """Настоящая сессия: cookie sessionid / ds_user_id."""
    try:
        try:
            cookies = page.context.cookies(["https://instagram.com"])
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
    """UI залогиненного приложения (Direct / Profile / Home+Create).

    На фоновых вкладках Chromium часто не считает элементы visible —
    достаточно наличия в DOM (count > 0).
    """
    strong = (
        'a[href="/direct/inbox/"], a[href*="/direct/inbox"]',
        'svg[aria-label="Profile"], svg[aria-label="Профиль"]',
        'a[aria-label="Profile" i], a[aria-label="Профиль" i]',
    )
    for sel in strong:
        try:
            loc = page.locator(sel)
            if int(loc.count()) > 0:
                return True
        except Exception:
            continue
    # Home + Create вместе — тоже сильный сигнал.
    home_ok = False
    create_ok = False
    try:
        h = page.locator(
            'svg[aria-label="Home"], svg[aria-label="Главная"]'
        )
        home_ok = int(h.count()) > 0
    except Exception:
        pass
    try:
        c = page.locator(
            'svg[aria-label="Новая публикация"], '
            'svg[aria-label="New post"], svg[aria-label="Create"], '
            'svg[aria-label="Создать"]'
        )
        create_ok = int(c.count()) > 0
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
            "/accounts/scraping_warning",
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


_SAVED_PROFILE_ALT_BTN = (
    '[role="button"][aria-label*="Использовать другой профиль" i], '
    '[role="button"][aria-label*="Use another profile" i], '
    '[role="button"][aria-label*="Создать новый аккаунт" i], '
    '[role="button"][aria-label*="Create new account" i], '
    '[role="button"][aria-label*="Create new account"], '
    'a[aria-label*="Create new account" i], '
    'a[href*="emailsignup"]'
)
_SAVED_PROFILE_CONTINUE_EXACT = (
    '[role="button"][aria-label="Продолжить" i], '
    '[role="button"][aria-label="Continue" i]'
)
_SAVED_PROFILE_CONTINUE_WITH_USER = (
    '[role="button"][aria-label^="Continue " i], '
    '[role="button"][aria-label^="Продолжить " i]'
)
_SAVED_PROFILE_AVATAR_RE = re.compile(
    r"^([A-Za-z0-9._]{2,30})\s*,\s*(фото\s+профиля|profile\s+photo)\s*$",
    re.IGNORECASE,
)


def _is_saved_profile_chooser_screen(page) -> bool:
    """
    Экран сохранённого профиля:
    - desktop: «Continue <username>» / «Продолжить <username>»;
    - mobile Bloks: aria-label=\"Продолжить\" + аватар / «другой профиль».
    """
    try:
        btn = page.locator(_SAVED_PROFILE_CONTINUE_WITH_USER).first
        if btn.count() > 0 and btn.is_visible(timeout=400):
            return True
    except Exception:
        pass
    # Mobile Bloks: точное «Продолжить» рядом с «другой профиль» / «создать аккаунт».
    try:
        cont = page.locator(_SAVED_PROFILE_CONTINUE_EXACT).first
        alt = page.locator(_SAVED_PROFILE_ALT_BTN).first
        if cont.count() > 0 and alt.count() > 0:
            return True
    except Exception:
        pass
    try:
        cont = page.locator('[role="button"]').filter(has_text=_CONTINUE_RE).first
        alt = page.locator(_SAVED_PROFILE_ALT_BTN).first
        if (
            cont.count() > 0
            and cont.is_visible(timeout=300)
            and alt.count() > 0
        ):
            return True
    except Exception:
        pass
    # Аватар «username, фото профиля» + кнопка Продолжить (mobile).
    try:
        avatar = page.locator(
            '[role="button"][aria-label*=", фото профиля" i], '
            '[role="button"][aria-label*=", profile photo" i]'
        ).first
        cont = page.locator(_SAVED_PROFILE_CONTINUE_EXACT).first
        if avatar.count() > 0 and cont.count() > 0:
            return True
    except Exception:
        pass
    return False


def _extract_saved_profile_username(page) -> str:
    """Ник с экрана сохранённого профиля (Continue <user> / аватар / заголовок)."""
    try:
        btn = page.locator(_SAVED_PROFILE_CONTINUE_WITH_USER).first
        if btn.count() > 0:
            label = (btn.get_attribute("aria-label") or "").strip()
            for prefix in ("Continue ", "Продолжить "):
                if label.lower().startswith(prefix.lower()):
                    name = label[len(prefix) :].strip().lstrip("@")
                    if name and re.fullmatch(r"[A-Za-z0-9._]{2,30}", name):
                        return name
    except Exception:
        pass
    # Mobile Bloks: aria-label="james…, фото профиля"
    try:
        avatar = page.locator(
            '[role="button"][aria-label*=", фото профиля" i], '
            '[role="button"][aria-label*=", profile photo" i]'
        ).first
        if avatar.count() > 0:
            label = (avatar.get_attribute("aria-label") or "").strip()
            m = _SAVED_PROFILE_AVATAR_RE.match(label)
            if m:
                return m.group(1)
    except Exception:
        pass
    # Mobile Bloks: крупный ник в h2 / heading.
    try:
        heading = page.locator('h2 [role="heading"], h2 span, [role="heading"]').first
        if heading.count() > 0:
            text = (heading.inner_text(timeout=500) or "").strip().lstrip("@")
            if text and re.fullmatch(r"[A-Za-z0-9._]{2,30}", text):
                return text
    except Exception:
        pass
    return ""


def _click_saved_profile_continue(page) -> bool:
    """Нажать Continue / Продолжить на экране сохранённого профиля."""
    try:
        btn = page.locator(_SAVED_PROFILE_CONTINUE_WITH_USER).first
        if btn.count() > 0 and btn.is_visible(timeout=800):
            btn.click(timeout=8000)
            return True
    except Exception:
        pass
    # Mobile Bloks: aria-label ровно «Продолжить» / «Continue».
    try:
        btn = page.locator(_SAVED_PROFILE_CONTINUE_EXACT).first
        if btn.count() > 0:
            try:
                visible = btn.is_visible(timeout=500)
            except Exception:
                visible = True
            if visible or btn.count() > 0:
                disabled = (btn.get_attribute("aria-disabled") or "").lower() == "true"
                if not disabled:
                    btn.click(timeout=8000, force=True)
                    return True
    except Exception:
        pass
    # Continue с текстом внутри (не intro «Confirm you're human»).
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


def _is_classic_login_form_visible(page) -> bool:
    """Форма входа: desktop form#login_form или mobile Bloks username+password."""
    try:
        form = page.locator("form#login_form").first
        if form.count() > 0 and form.is_visible(timeout=500):
            email = form.locator('input[name="email"], input[type="text"]').first
            pwd = form.locator('input[name="pass"], input[type="password"]').first
            if (
                email.count() > 0
                and pwd.count() > 0
                and email.is_visible(timeout=300)
                and pwd.is_visible(timeout=300)
            ):
                return True
    except Exception:
        pass
    # Mobile Bloks: input[name=username] + input[name=password] + Войти
    try:
        user = page.locator('input[name="username"]').first
        pwd = page.locator(
            'input[name="password"][type="password"], '
            'input[aria-label="Пароль" i][type="password"]'
        ).first
        if user.count() > 0 and pwd.count() > 0:
            return True
    except Exception:
        pass
    try:
        heading = page.get_by_text(
            re.compile(r"^log\s+in\s+to\s+instagram$|^вход\s+в\s+instagram$", re.I)
        ).first
        if heading.count() > 0 and heading.is_visible(timeout=400):
            email = page.locator(
                'input[name="email"], input[name="username"]'
            ).first
            pwd = page.locator(
                'input[name="pass"][type="password"], '
                'input[name="password"][type="password"]'
            ).first
            if (
                email.count() > 0
                and pwd.count() > 0
                and email.is_visible(timeout=300)
                and pwd.is_visible(timeout=300)
            ):
                return True
    except Exception:
        pass
    return False


def _onetap_password_visible(page) -> bool:
    """Только пароль сохранённого профиля (не классическая login_form)."""
    if _is_classic_login_form_visible(page):
        return False
    # Полная форма с username — не onetap.
    try:
        user = page.locator('input[name="username"]').first
        if user.count() > 0:
            return False
    except Exception:
        pass
    try:
        inp = page.locator(
            "form#aymh_password_entry_view input[type='password'][name='pass']"
        ).first
        if inp.count() > 0 and inp.is_visible(timeout=400):
            return True
    except Exception:
        pass
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


def _fill_classic_login_form(page, login: str, password: str) -> None:
    login = (login or "").strip()
    password = password or ""
    if not login:
        raise RuntimeError(
            "Instagram: пустой логин для формы входа "
            "(нужен inst_login в данных учётки)."
        )
    if not password:
        raise RuntimeError(
            "Instagram: пустой пароль для формы входа "
            "(нужен inst_password или gmail_password)."
        )
    form = page.locator("form#login_form").first
    email_candidates = (
        page.locator('input[name="username"]').first,
        form.locator('input[name="email"]').first,
        page.locator('input[name="email"]').first,
        page.locator(
            'input[aria-label*="имя пользователя" i], '
            'input[aria-label*="эл. адрес" i], '
            'input[aria-label*="username" i]'
        ).first,
        page.get_by_label(
            re.compile(
                r"mobile\s+number|username|email|номер\s+телефона|"
                r"имя\s+пользователя|эл\.?\s*почт|эл\.\s*адрес",
                re.I,
            )
        ).first,
    )
    pwd_candidates = (
        page.locator('input[name="password"][type="password"]').first,
        form.locator('input[name="pass"][type="password"]').first,
        page.locator('input[name="pass"][type="password"]').first,
        page.locator('input[aria-label="Пароль" i][type="password"]').first,
        page.get_by_label(re.compile(r"^password$|^пароль$", re.I)).first,
    )
    filled_login = False
    last_err: Exception | None = None
    for inp in email_candidates:
        try:
            if inp.count() <= 0:
                continue
            try:
                visible = inp.is_visible(timeout=500)
            except Exception:
                visible = True
            if not visible and inp.count() <= 0:
                continue
            inp.click(timeout=4000)
            inp.fill(login, timeout=10_000)
            filled_login = True
            break
        except Exception as e:
            last_err = e
            continue
    if not filled_login:
        raise RuntimeError(
            "Instagram: не найдено поле логина на форме входа"
            + (f": {last_err!r}" if last_err else "")
        )
    filled_pwd = False
    for inp in pwd_candidates:
        try:
            if inp.count() <= 0:
                continue
            try:
                visible = inp.is_visible(timeout=500)
            except Exception:
                visible = True
            if not visible and inp.count() <= 0:
                continue
            inp.click(timeout=4000)
            inp.fill(password, timeout=10_000)
            filled_pwd = True
            break
        except Exception as e:
            last_err = e
            continue
    if not filled_pwd:
        raise RuntimeError(
            "Instagram: не найдено поле Password на форме входа"
            + (f": {last_err!r}" if last_err else "")
        )


def _click_classic_log_in(page) -> bool:
    try:
        btn = page.locator(
            'form#login_form [role="button"][aria-label="Log in" i], '
            'form#login_form [role="button"][aria-label="Войти" i], '
            '[role="button"][aria-label="Войти" i], '
            '[role="button"][aria-label="Log in" i]'
        ).first
        if btn.count() > 0:
            disabled = (btn.get_attribute("aria-disabled") or "").lower() == "true"
            if not disabled:
                btn.click(timeout=8000, force=True)
                return True
    except Exception:
        pass
    return _click_onetap_log_in(page)


def _fill_onetap_password(page, password: str) -> None:
    password = password or ""
    if not password:
        raise RuntimeError(
            "Instagram: пустой пароль для входа в сохранённый профиль "
            "(нужен inst_password или gmail_password)."
        )
    candidates = (
        page.locator('input[type="password"][name="pass"]').first,
        page.locator("form#aymh_password_entry_view input[type='password']").first,
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
        btn = page.locator(
            '[role="button"][aria-label="Войти" i], '
            '[role="button"][aria-label="Log in" i], '
            '[role="button"][aria-label="Log In" i]'
        ).first
        if btn.count() > 0:
            disabled = (btn.get_attribute("aria-disabled") or "").lower() == "true"
            if not disabled:
                btn.click(timeout=8000, force=True)
                return True
    except Exception:
        pass
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


def _wrong_login_credentials_visible(page) -> bool:
    try:
        if page.get_by_text(_WRONG_LOGIN_CREDENTIALS_RE).first.is_visible(timeout=500):
            return True
    except Exception:
        pass
    return False


def _is_login_2fa_challenge_screen(page) -> bool:
    try:
        if page.get_by_text(_LOGIN_2FA_CHALLENGE_RE).first.is_visible(timeout=500):
            return True
    except Exception:
        pass
    try:
        if page.get_by_text(_TRUST_DEVICE_RE).first.is_visible(timeout=400):
            code = page.get_by_label(re.compile(r"^code$|^код$", re.I)).first
            if code.count() > 0 and code.is_visible(timeout=300):
                return True
    except Exception:
        pass
    return False


def _ensure_trust_device_checked(page) -> None:
    """Оставить галочку «Trust this device…» включённой."""
    try:
        label = page.locator("label").filter(has_text=_TRUST_DEVICE_RE).first
        if label.count() <= 0 or not label.is_visible(timeout=600):
            return
        cb = label.locator('input[type="checkbox"]').first
        if cb.count() <= 0:
            return
        checked = (cb.get_attribute("aria-checked") or "").lower()
        if checked == "true" or cb.is_checked():
            return
        try:
            cb.check(timeout=3000)
        except Exception:
            label.click(timeout=3000)
        _log("Instagram: включили «Trust this device».")
    except Exception:
        pass


def _fill_login_2fa_code(page, code: str) -> bool:
    code = (code or "").strip()
    if not code:
        return False
    candidates = (
        page.get_by_label(re.compile(r"^code$|^код$", re.I)).first,
        page.locator(
            'input[autocomplete="one-time-code"], '
            'input[inputmode="numeric"], '
            'input[maxlength="6"], '
            'input[type="text"]'
        ).first,
    )
    for inp in candidates:
        try:
            if inp.count() <= 0 or not inp.is_visible(timeout=600):
                continue
            inp.click(timeout=4000)
            inp.fill("")
            inp.fill(code, timeout=10_000)
            return True
        except Exception:
            continue
    return False


def _click_login_2fa_continue(page) -> bool:
    try:
        btn = page.locator('[role="button"]').filter(has_text=_CONTINUE_RE).first
        if btn.count() > 0 and btn.is_visible(timeout=800):
            disabled = (btn.get_attribute("aria-disabled") or "").lower() == "true"
            if not disabled:
                btn.click(timeout=8000)
                return True
    except Exception:
        pass
    return _click_by_text(page, _CONTINUE_RE, prefer_link=False)


def _handle_login_2fa_challenge(page, twofa_secret: str, *, max_seconds: float = 60.0) -> None:
    """Экран authenticator app после пароля: trust device + TOTP → Continue."""
    from zaliver.youtube_upload.totp import get_totp_token

    secret = (twofa_secret or "").strip().replace(" ", "")
    if not secret:
        raise RuntimeError(
            "Instagram: требуется 2FA при входе, но inst_2fa не задан "
            "в данных учётки профиля."
        )
    _ensure_trust_device_checked(page)
    otp = get_totp_token(secret)
    _log("Instagram: вводим TOTP-код на экране 2FA входа…")
    if not _fill_login_2fa_code(page, otp):
        raise RuntimeError(
            "Instagram: не найдено поле Code на экране 2FA входа."
        )
    page.wait_for_timeout(400)
    deadline = time.monotonic() + max(10.0, float(max_seconds))
    while time.monotonic() < deadline:
        _ensure_trust_device_checked(page)
        if _click_login_2fa_continue(page):
            _log("Instagram: нажали Continue после 2FA.")
            return
        page.wait_for_timeout(350)
    raise RuntimeError(
        "Instagram: не удалось нажать Continue на экране 2FA входа."
    )


def _is_save_login_info_screen(page) -> bool:
    """«Save your login info?» / mobile «Сохраните пароль» / «Сохранить данные для входа»."""
    try:
        dlg = page.locator(
            '[role="dialog"][aria-label*="Сохраните пароль" i], '
            '[role="dialog"][aria-label*="Save your password" i], '
            '[role="dialog"][aria-label*="Save your login" i], '
            '[role="dialog"][aria-label*="login info" i]'
        ).first
        if dlg.count() > 0:
            return True
    except Exception:
        pass
    try:
        heading = page.get_by_text(_SAVE_LOGIN_INFO_HEADING_RE).first
        if heading.count() > 0 and heading.is_visible(timeout=400):
            return True
    except Exception:
        pass
    try:
        # Есть и «Сохранить», и «Не сейчас».
        save_btn = page.get_by_role("button", name=_SAVE_INFO_RE).first
        not_now = page.get_by_role("button", name=_NOT_NOW_RE).first
        if save_btn.count() > 0 and not_now.count() > 0:
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


def _click_save_login_info_not_now(page) -> bool:
    """На экране сохранения пароля нажать «Не сейчас» / Not now."""
    # В mobile-диалоге бывают скрытые/disabled дубликаты — берём активную.
    try:
        dlg = page.locator(
            '[role="dialog"][aria-label*="Сохраните пароль" i], '
            '[role="dialog"][aria-label*="Save your password" i], '
            '[role="dialog"][aria-label*="Save your login" i], '
            '[role="dialog"]'
        ).first
        scope = dlg if dlg.count() > 0 else page
        candidates = (
            scope.get_by_role("button", name=_NOT_NOW_RE),
            scope.locator('[role="button"]').filter(has_text=_NOT_NOW_RE),
            page.get_by_role("button", name=_NOT_NOW_RE),
            page.locator('[role="button"]').filter(has_text=_NOT_NOW_RE),
        )
        for loc in candidates:
            try:
                n = int(loc.count())
            except Exception:
                n = 0
            for i in range(min(n, 4)):
                btn = loc.nth(i)
                try:
                    disabled = (btn.get_attribute("aria-disabled") or "").lower()
                    if disabled in ("true", "1"):
                        continue
                    tabindex = (btn.get_attribute("tabindex") or "").strip()
                    if tabindex == "-1":
                        continue
                    btn.click(timeout=8000, force=True)
                    return True
                except Exception:
                    continue
    except Exception:
        pass
    return _click_by_text(page, _NOT_NOW_RE, prefer_link=False)


def _click_save_login_info(page) -> bool:
    """
    Закрыть экран «Save your login info?» / «Сохраните пароль».
    Предпочтительно «Не сейчас» (mobile), иначе Save.
    """
    if _click_save_login_info_not_now(page):
        return True
    try:
        btn = page.get_by_role("button", name=_SAVE_INFO_RE).first
        if btn.count() > 0 and btn.is_visible(timeout=800):
            disabled = (btn.get_attribute("aria-disabled") or "").lower()
            if disabled not in ("true", "1"):
                btn.click(timeout=8000)
                return True
    except Exception:
        pass
    try:
        btn = page.locator('[role="button"]').filter(has_text=_SAVE_INFO_RE).first
        if btn.count() > 0 and btn.is_visible(timeout=500):
            btn.click(timeout=8000, force=True)
            return True
    except Exception:
        pass
    return _click_by_text(page, _SAVE_INFO_RE, prefer_link=False)


def try_instagram_saved_profile_login(page, password: str) -> str | None:
    """
    Экран сохранённого профиля: Continue → пароль → Log in → Save info.

    Мягкий путь для регистрации: при неудаче возвращает None
    (можно продолжить signup). Без 2FA и без ошибки на неверный пароль.
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

    _log("Instagram: вводим пароль на экране сохранённого профиля…")
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

    # Save your login info? → Save
    save_deadline = time.monotonic() + 25.0
    while time.monotonic() < save_deadline:
        if dismiss_instagram_scraping_warning_if_present(page):
            continue
        if _is_save_login_info_screen(page):
            if _click_save_login_info(page):
                _log("Instagram: «Сохраните пароль» — нажали «Не сейчас».")
                page.wait_for_timeout(1000)
            break
        if _instagram_already_logged_in(page):
            break
        page.wait_for_timeout(400)

    home_deadline = time.monotonic() + 45.0
    while time.monotonic() < home_deadline:
        if dismiss_instagram_scraping_warning_if_present(page):
            continue
        if _is_save_login_info_screen(page):
            if _click_save_login_info(page):
                _log("Instagram: «Сохраните пароль» — нажали «Не сейчас».")
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


def ensure_instagram_session_relogin(
    page,
    *,
    password: str,
    twofa_secret: str = "",
    login: str = "",
    max_seconds: float = 90.0,
    login_credentials=None,
) -> str | None:
    """
    Re-login (все алгоритмы кроме регистрации).

    Варианты:
    1) Mobile splash без профиля: Войти → форма username/password
    2) Сохранённый профиль: Continue → пароль → Log in
    3) Классическая / Bloks форма: логин + пароль → Log in

    Далее: (опц. 2FA + trust device) → Save → главная.

    Returns:
        username при успешном входе, None если экрана разлогина нет.
    Raises:
        RuntimeError при неверном пароле / отсутствии данных / сбое входа.
    """
    landing = _is_mobile_logged_out_landing(page)
    classic = _is_classic_login_form_visible(page)
    chooser = _is_saved_profile_chooser_screen(page)
    onetap = _onetap_password_visible(page)
    if not (landing or classic or chooser or onetap):
        return None

    pwd = (password or "").strip()
    user_login = (login or "").strip()
    if not pwd:
        raise RuntimeError(
            "Instagram: сессия разлогинена, но нет пароля "
            "(inst_password или gmail_password) в данных учётки."
        )

    username = _extract_saved_profile_username(page) or user_login

    if landing and not classic:
        if not user_login:
            raise RuntimeError(
                "Instagram: экран «Войти», но нет inst_login в данных учётки."
            )
        _log("Instagram: разлогин — mobile splash без профиля, жмём «Войти»…")
        if not _click_mobile_landing_log_in(page):
            raise RuntimeError(
                "Instagram: не удалось нажать «Войти» на стартовом экране."
            )
        page.wait_for_timeout(800)
        form_deadline = time.monotonic() + min(25.0, max(10.0, float(max_seconds)))
        while time.monotonic() < form_deadline:
            if _is_classic_login_form_visible(page):
                classic = True
                break
            if _is_saved_profile_chooser_screen(page):
                return ensure_instagram_session_relogin(
                    page,
                    password=pwd,
                    twofa_secret=twofa_secret,
                    login=user_login,
                    max_seconds=max(20.0, form_deadline - time.monotonic()),
                    login_credentials=login_credentials,
                )
            page.wait_for_timeout(350)
        if not classic:
            raise RuntimeError(
                "Instagram: после «Войти» на splash не появилась форма логина "
                f"(URL={(page.url or '')!r})."
            )

    if classic:
        if not user_login:
            raise RuntimeError(
                "Instagram: форма входа, но нет inst_login в данных учётки."
            )
        _log(
            "Instagram: разлогин — форма входа "
            f"(login={user_login!r})…"
        )
        _fill_classic_login_form(page, user_login, pwd)
        page.wait_for_timeout(400)
        login_deadline = time.monotonic() + 15.0
        clicked = False
        while time.monotonic() < login_deadline:
            if _click_classic_log_in(page):
                clicked = True
                break
            page.wait_for_timeout(350)
        if not clicked:
            raise RuntimeError("Instagram: не удалось нажать «Log in» / «Войти».")
        _log("Instagram: нажали «Log in».")
        page.wait_for_timeout(1000)
    else:
        if chooser:
            _log(
                "Instagram: разлогин — экран сохранённого профиля"
                + (f" (@{username})" if username else "")
                + " — жмём Continue…"
            )
            if not _click_saved_profile_continue(page):
                raise RuntimeError(
                    "Instagram: не удалось нажать «Продолжить» на экране "
                    "сохранённого профиля."
                )
            page.wait_for_timeout(800)

        deadline = time.monotonic() + max(15.0, float(max_seconds))
        while time.monotonic() < deadline:
            if _onetap_password_visible(page):
                break
            if _is_classic_login_form_visible(page):
                # После Continue иногда уходят на полную форму входа.
                return ensure_instagram_session_relogin(
                    page,
                    password=pwd,
                    twofa_secret=twofa_secret,
                    login=user_login,
                    max_seconds=max(20.0, deadline - time.monotonic()),
                )
            if _instagram_already_logged_in(page):
                uname = (
                    username or _extract_logged_in_username(page) or "saved_profile"
                )
                _log(f"Instagram: после Continue сразу вошли (@{uname}).")
                return uname
            if _is_login_2fa_challenge_screen(page):
                break
            if _is_confirmation_code_screen(page):
                break
            try:
                from zaliver.instagram_upload.setup_2fa import (
                    _email_check_screen_visible as _email_scr,
                )

                if _email_scr(page):
                    break
            except Exception:
                pass
            page.wait_for_timeout(400)
        else:
            raise RuntimeError(
                "Instagram: после «Продолжить» не появилось поле пароля / 2FA / код почты."
            )

        if _onetap_password_visible(page):
            _log("Instagram: вводим пароль (inst_password / gmail_password)…")
            _fill_onetap_password(page, pwd)
            page.wait_for_timeout(400)
            login_deadline = time.monotonic() + 15.0
            clicked = False
            while time.monotonic() < login_deadline:
                if _click_onetap_log_in(page):
                    clicked = True
                    break
                page.wait_for_timeout(350)
            if not clicked:
                raise RuntimeError("Instagram: не удалось нажать «Войти».")
            _log("Instagram: нажали «Войти».")
            page.wait_for_timeout(1000)

    # После пароля: ошибка / 2FA / код почты / Save / лента
    settle_deadline = time.monotonic() + max(20.0, float(max_seconds))
    while time.monotonic() < settle_deadline:
        if _wrong_login_credentials_visible(page):
            raise RuntimeError(
                "Instagram: введены неверные данные для входа "
                "(неверный логин или пароль)."
            )
        if _is_login_2fa_challenge_screen(page):
            _handle_login_2fa_challenge(
                page,
                twofa_secret,
                max_seconds=min(60.0, settle_deadline - time.monotonic()),
            )
            page.wait_for_timeout(1000)
            continue
        # Mobile Bloks: «Проверьте электронную почту» → код из Gmail → Продолжить.
        email_scr = _is_confirmation_code_screen(page)
        email_handler = None
        try:
            from zaliver.instagram_upload.setup_2fa import (
                _email_check_screen_visible,
                _handle_email_verification_if_needed,
            )

            email_scr = email_scr or _email_check_screen_visible(page)
            email_handler = _handle_email_verification_if_needed
        except Exception as e_imp:
            _log(f"Instagram: import email handler: {e_imp!r}")
        if email_scr:
            if email_handler is None:
                raise RuntimeError(
                    "Instagram: экран кода из почты, но обработчик недоступен "
                    f"(URL={(page.url or '')!r})."
                )
            # Gmail + ожидание письма могут занять до ~2 мин.
            email_deadline = max(settle_deadline, time.monotonic() + 150.0)
            settle_deadline = max(settle_deadline, email_deadline)
            email_handler(
                page,
                login_credentials=login_credentials,
                deadline=email_deadline,
            )
            page.wait_for_timeout(1000)
            continue
        # После кода почты: «Создайте новый пароль» → Пропустить.
        try:
            from zaliver.instagram_upload.setup_2fa import (
                dismiss_new_password_screen_if_present,
            )

            if dismiss_new_password_screen_if_present(page):
                page.wait_for_timeout(1000)
                continue
        except Exception as e_skip:
            _log(f"Instagram: skip new password: {e_skip!r}")
        if dismiss_instagram_scraping_warning_if_present(page):
            continue
        if _is_save_login_info_screen(page):
            if _click_save_login_info(page):
                _log("Instagram: «Сохраните пароль» — нажали «Не сейчас».")
                page.wait_for_timeout(1000)
            continue
        if _instagram_already_logged_in(page):
            uname = (
                username
                or _extract_logged_in_username(page)
                or user_login
                or "saved_profile"
            )
            _log(f"Instagram: re-login успешен (@{uname}).")
            return uname
        if _onetap_password_visible(page) or _is_classic_login_form_visible(page):
            page.wait_for_timeout(400)
            continue
        page.wait_for_timeout(400)

    if _wrong_login_credentials_visible(page):
        raise RuntimeError(
            "Instagram: введены неверные данные для входа "
            "(неверный логин или пароль)."
        )
    raise RuntimeError(
        "Instagram: после ввода пароля не удалось войти в аккаунт "
        f"(URL={(page.url or '')!r})."
    )


def _wait_signup_form(
    page,
    *,
    max_seconds: float = _SIGNUP_READY_MAX_S,
) -> None:
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        accept_instagram_cookie_consent_if_present(page)
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
    """Уже экран ввода кода из почты (не image captcha / human check)."""
    # Image captcha тоже про «code» + maxlength=6 — не путать с почтой.
    if _is_image_captcha_screen(page) or _is_human_confirm_intro_screen(page):
        return False
    try:
        heading = page.get_by_text(_CONFIRM_CODE_HEADING_RE).first
        if heading.count() > 0 and heading.is_visible(timeout=300):
            return True
    except Exception:
        pass
    try:
        # Mobile Bloks: h2 aria-label="Проверьте электронную почту"
        heading = page.get_by_role(
            "heading",
            name=re.compile(
                r"проверьте\s+электронную\s+почту|check\s+your\s+email",
                re.I,
            ),
        ).first
        if heading.count() > 0 and heading.is_visible(timeout=300):
            return True
    except Exception:
        pass
    try:
        # Код из почты — input; image captcha — textarea.
        field = page.locator(
            'input[maxlength="6"], '
            'input[aria-label="Введите код" i], '
            'input[aria-label="Enter code" i]'
        ).first
        if field.count() > 0 and field.is_visible(timeout=300):
            # Без заголовка «код/почта» не считаем экраном (избежать ложных срабатываний).
            if page.get_by_text(_CONFIRM_CODE_HEADING_RE).count() > 0:
                return True
            if page.locator(
                'h2[aria-label*="почт" i], h2[aria-label*="email" i]'
            ).count() > 0:
                return True
    except Exception:
        pass
    return False


def _email_confirmation_code_rejected(page) -> bool:
    """Instagram показал ошибку неверного кода из письма."""
    try:
        loc = page.get_by_text(_EMAIL_CODE_REJECTED_RE).first
        if loc.count() > 0 and loc.is_visible(timeout=300):
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


def _cookie_dialog(page):
    """Модалка cookies: role=dialog с заголовком Allow the use of cookies…"""
    return page.locator('[role="dialog"][aria-modal="true"]').filter(
        has_text=_COOKIE_CONSENT_HEADING_RE
    )


def _cookie_allow_buttons(page):
    """
    Кнопки Accept all / «Разрешить все cookie».
    Новый UI (/consent): div[role=button] в dialog.
    Старый UI: <button class="_a9-- _asz1">.
    user_cookie_choice_v2: любая кнопка со словом cookie (не Decline).
    """
    dialog = _cookie_dialog(page)
    locs = [
        # Новый Meta-dialog — приоритет.
        dialog.locator('[role="button"]').filter(has_text=_ALLOW_ALL_COOKIES_RE),
        dialog.get_by_role("button", name=re.compile(r"^allow\s+all\s+cookies$", re.I)),
        dialog.get_by_role(
            "button", name=re.compile(r"^разрешить\s+все\s+(файлы\s+)?cookie[s]?$", re.I)
        ),
        page.get_by_role("button", name=re.compile(r"^allow\s+all\s+cookies$", re.I)),
        page.get_by_role(
            "button", name=re.compile(r"^разрешить\s+все\s+(файлы\s+)?cookie[s]?$", re.I)
        ),
        page.locator('[role="dialog"] [role="button"]').filter(
            has_text=_ALLOW_ALL_COOKIES_RE
        ),
        page.locator('[role="button"]').filter(has_text=_ALLOW_ALL_COOKIES_RE),
        # Старый баннер.
        page.locator("button._asz1").filter(has_text=_ALLOW_ALL_COOKIES_RE),
        page.locator("button._a9--").filter(has_text=_ALLOW_ALL_COOKIES_RE),
        page.locator("div._abdc button").filter(has_text=_ALLOW_ALL_COOKIES_RE),
        page.locator("button").filter(has_text=_ALLOW_ALL_COOKIES_RE),
        page.get_by_text(re.compile(r"^allow\s+all\s+cookies$", re.I)),
        page.get_by_text(
            re.compile(r"^разрешить\s+все\s+(файлы\s+)?cookie[s]?$", re.I)
        ),
    ]
    # Широкий матч Accept/Allow+cookie — только на /consent (не accordion).
    try:
        if _is_cookie_consent_url(_page_url(page)):
            locs.append(
                page.locator('button, [role="button"]')
                .filter(has_text=_COOKIE_WORD_BUTTON_RE)
                .filter(has_not_text=_DECLINE_OPTIONAL_COOKIES_RE)
                .filter(has_not_text=_COOKIE_NON_ACTION_RE)
            )
    except Exception:
        pass
    return tuple(locs)


def _cookie_allow_button_present(page) -> bool:
    for loc in _cookie_allow_buttons(page):
        try:
            if loc.count() > 0:
                return True
        except Exception:
            continue
    return False


def _is_cookie_consent_screen(page) -> bool:
    """Диалог cookies: EN Allow all / RU «Разрешить все cookie» (dialog / _abdc / /consent)."""
    try:
        if _is_cookie_consent_url(_page_url(page)):
            return True
    except Exception:
        pass
    try:
        dialog = _cookie_dialog(page).first
        if dialog.count() > 0 and dialog.is_visible(timeout=400):
            return True
    except Exception:
        pass
    try:
        heading = page.get_by_text(_COOKIE_CONSENT_HEADING_RE).first
        if heading.count() > 0 and heading.is_visible(timeout=400):
            return True
    except Exception:
        pass
    if _cookie_allow_button_present(page):
        return True
    try:
        decline = page.locator('[role="button"], button').filter(
            has_text=_DECLINE_OPTIONAL_COOKIES_RE
        ).first
        if decline.count() > 0:
            return True
    except Exception:
        pass
    try:
        banner = page.locator("div._abdc").first
        if banner.count() > 0 and banner.is_visible(timeout=300):
            if banner.get_by_text(_COOKIE_CONSENT_HEADING_RE).count() > 0:
                return True
            if banner.locator("button._asz1, button._a9--").count() > 0:
                return True
    except Exception:
        pass
    return False


def _click_allow_all_cookies_via_js(page) -> bool:
    """Прямой DOM-клик — dialog div[role=button] и sticky button._asz1."""
    try:
        clicked = page.evaluate(
            """() => {
                const needles = [
                    'allow all cookies',
                    'разрешить все cookie',
                    'разрешить все файлы cookie',
                    'принять все cookie',
                    'принять все файлы cookie',
                ];
                const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const labelOf = (el) => norm(
                    el.getAttribute('aria-label') || el.innerText || el.textContent || ''
                );
                const isDecline = (t) =>
                    /decline|отклонить|reject|отказаться|не\\s+сейчас|not\\s+now/.test(t);
                // Accordion / FAQ — «How we use these cookies», Learn more и т.п.
                const isNonAction = (el, t) => {
                    const al = norm(el.getAttribute('aria-label') || '');
                    if (/\\bexpand\\b|\\bcollapse\\b|learn\\s+more|see\\s+more|подробнее/.test(al))
                        return true;
                    if (/\\bexpand\\b|\\bcollapse\\b|learn\\s+more|see\\s+more/.test(t))
                        return true;
                    if (/how\\s+we\\s+use|what\\s+are\\s+cookies|why\\s+do\\s+we\\s+use/.test(t))
                        return true;
                    if (/choose\\s+cookies|select\\s+all|your\\s+cookie\\s+choices/.test(t))
                        return true;
                    if (/как\\s+мы\\s+используем|что\\s+такое\\s+cookie|узнать\\s+больше/.test(t))
                        return true;
                    // Длинный текст — не primary CTA (Allow all обычно короткая кнопка).
                    if (t.length > 60) return true;
                    return false;
                };
                const isExactAllow = (t) => needles.some((n) => t === n || t.startsWith(n));
                // Только Accept/Allow + cookie. Без fallback на любой текст с «cookie»
                // (иначе кликаем accordion «How we use these cookies»).
                const isCookieWordAllow = (t) => {
                    if (!t || isDecline(t)) return false;
                    if (!/cookie/.test(t)) return false;
                    if (/optional|необязательн/.test(t) && !/allow\\s+all|разрешить\\s+все/.test(t))
                        return false;
                    return /allow|accept|разреш|принят|соглас|enable|включ/.test(t);
                };
                const onConsent = /instagram\\.com.*\\/consent/i.test(
                    location.href || ''
                );

                const score = (el) => {
                    const t = labelOf(el);
                    let s = (el.className || '').includes('_asz1') ? 0 : 10;
                    if (isExactAllow(t)) s -= 20;
                    if (/^allow\\s+all\\s+cookies$|^разрешить\\s+все/.test(t)) s -= 15;
                    if (/allow\\s+all|разрешить\\s+все|принять\\s+все/.test(t)) s -= 8;
                    if (isDecline(t) || isNonAction(el, t)) s += 100;
                    return s;
                };

                const tryClick = (nodes, prefix) => {
                    const sorted = Array.from(nodes).sort((a, b) => score(a) - score(b));
                    for (const el of sorted) {
                        const t = labelOf(el);
                        if (isNonAction(el, t)) continue;
                        if (isExactAllow(t)) {
                            // ok
                        } else if (onConsent && isCookieWordAllow(t)) {
                            // ok
                        } else {
                            continue;
                        }
                        try { el.scrollIntoView({block: 'center', inline: 'nearest'}); } catch (e) {}
                        el.click();
                        return (prefix || '') + t;
                    }
                    return null;
                };

                // 1) Сначала внутри aria-modal dialog (exact Allow all — выше по score).
                const dialogs = Array.from(
                    document.querySelectorAll('[role="dialog"][aria-modal="true"]')
                );
                for (const dlg of dialogs) {
                    const nodes = dlg.querySelectorAll('button, [role="button"]');
                    const hit = tryClick(nodes, 'dialog:');
                    if (hit) return hit;
                }

                // 2) По всей странице.
                return tryClick(
                    document.querySelectorAll('button, [role="button"]'),
                    ''
                );
            }"""
        )
        if clicked:
            clicked_l = str(clicked).lower()
            if re.search(
                r"how\s+we\s+use|learn\s+more|see\s+more|expand|choose\s+cookies",
                clicked_l,
            ):
                _log(
                    f"Instagram: cookie consent — JS попал не в Allow "
                    f"({clicked!r}), пропускаем."
                )
                return False
            if not re.search(
                r"allow\s+all|разрешить\s+все|принять\s+все|"
                r"allow.*cookie|accept.*cookie|разреш.*cookie|принят.*cookie",
                clicked_l,
            ):
                _log(
                    f"Instagram: cookie consent — JS click без Allow "
                    f"({clicked!r}), пропускаем."
                )
                return False
            _log(f"Instagram: cookie consent — JS click ({clicked!r})")
            return True
    except Exception as e:
        _log(f"Instagram: cookie JS click failed: {e!r}")
    return False


def _click_allow_all_cookies_button(page) -> bool:
    """Клик по Allow all cookies (dialog div / button._asz1 / JS)."""
    # Сначала JS по dialog — самый надёжный путь для нового UI.
    if _click_allow_all_cookies_via_js(page):
        return True
    for loc in _cookie_allow_buttons(page):
        try:
            btn = loc.first
            if btn.count() <= 0:
                continue
            try:
                btn.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            page.wait_for_timeout(150)
            try:
                btn.click(timeout=8000)
                return True
            except Exception:
                pass
            try:
                btn.click(timeout=5000, force=True)
                return True
            except Exception:
                pass
            try:
                box = btn.bounding_box()
                if box:
                    page.mouse.click(
                        box["x"] + box["width"] / 2,
                        box["y"] + box["height"] / 2,
                    )
                    return True
            except Exception:
                continue
        except Exception:
            continue
    return _click_by_text(page, _ALLOW_ALL_COOKIES_RE, prefer_link=False)


def _cookie_consent_dismissed(page) -> bool:
    """True если ушли с /consent и диалог/кнопки Allow all больше нет."""
    try:
        url = _page_url(page)
    except Exception:
        url = ""
    if _is_cookie_consent_url(url):
        return False
    try:
        dialog = _cookie_dialog(page).first
        if dialog.count() > 0 and dialog.is_visible(timeout=200):
            return False
    except Exception:
        pass
    if _cookie_allow_button_present(page):
        return False
    try:
        if page.get_by_text(_COOKIE_CONSENT_HEADING_RE).first.is_visible(timeout=200):
            return False
    except Exception:
        pass
    return True


def click_instagram_allow_all_cookies(page, *, max_seconds: float = 30.0) -> bool:
    """
    Нажать «Allow all cookies» / «Разрешить все cookie».
    True если кликнули или диалог уже исчез.
    """
    if not _is_cookie_consent_screen(page):
        return False

    deadline = time.monotonic() + max_seconds
    clicked_once = False
    while time.monotonic() < deadline:
        url = _page_url(page)
        if (
            not _is_cookie_consent_url(url)
            and _cookie_consent_dismissed(page)
        ):
            if clicked_once:
                _log(f"Instagram: после cookies URL={url!r}")
            return True

        # Ждём кнопку Allow all / dialog — не только старый button._asz1.
        if not _cookie_allow_button_present(page):
            try:
                page.locator(
                    '[role="dialog"][aria-modal="true"] [role="button"], '
                    "button._asz1, button._a9--"
                ).filter(has_text=_ALLOW_ALL_COOKIES_RE).first.wait_for(
                    state="attached", timeout=1500
                )
            except Exception:
                try:
                    _cookie_dialog(page).first.wait_for(state="visible", timeout=800)
                except Exception:
                    page.wait_for_timeout(400)
            if not _cookie_allow_button_present(page):
                # Dialog есть, кнопки ещё нет — JS всё равно попробуем позже.
                if _is_cookie_consent_url(url) or _cookie_dialog(page).count() > 0:
                    if _click_allow_all_cookies_via_js(page):
                        clicked_once = True
                        _log("Instagram: cookie consent — JS click (ожидание).")
                        page.wait_for_timeout(800)
                        continue
                page.wait_for_timeout(400)
                continue

        if _click_allow_all_cookies_button(page):
            clicked_once = True
            _log("Instagram: cookie consent — нажали «Allow all cookies».")
            settle = time.monotonic() + 15.0
            while time.monotonic() < settle:
                url = _page_url(page)
                if not _is_cookie_consent_url(url) and _cookie_consent_dismissed(page):
                    _log(f"Instagram: после cookies URL={url!r}")
                    return True
                try:
                    page.wait_for_timeout(400)
                except Exception:
                    break
            if not _is_cookie_consent_url(_page_url(page)) and _cookie_consent_dismissed(
                page
            ):
                _log(f"Instagram: cookie dialog закрыт, URL={_page_url(page)!r}")
                return True
            _log("Instagram: cookie клик был, но экран ещё виден — повторяем…")
            continue
        page.wait_for_timeout(400)

    _log("Instagram: cookie consent виден, но «Allow all cookies» не нажалась.")
    return False


def accept_instagram_cookie_consent_if_present(
    page, *, max_seconds: float = 30.0, appear_seconds: float = 0.0
) -> bool:
    """
    Если есть /consent или диалог cookies — Accept all.
    appear_seconds > 0 — подождать появления диалога (после навигации).
    """
    deadline = time.monotonic() + max(0.0, appear_seconds)
    while True:
        if _is_cookie_consent_screen(page):
            _log(
                f"Instagram: экран согласия на cookies "
                f"(URL={_page_url(page)!r})."
            )
            return click_instagram_allow_all_cookies(page, max_seconds=max_seconds)
        if time.monotonic() >= deadline:
            return False
        # На /consent всегда ждём кнопку, early-skip не применяем.
        try:
            if _is_cookie_consent_url(_page_url(page)):
                page.wait_for_timeout(300)
                continue
        except Exception:
            pass
        if _page_looks_past_cookie_gate(page):
            return False
        try:
            page.wait_for_timeout(300)
        except Exception:
            return False


def _page_looks_past_cookie_gate(page) -> bool:
    """Есть UI логина/регистрации/ленты — ждать cookie-диалог не нужно."""
    try:
        if _is_cookie_consent_url(_page_url(page)):
            return False
    except Exception:
        pass
    try:
        if _cookie_dialog(page).count() > 0:
            return False
    except Exception:
        pass
    try:
        if page.get_by_text(_CREATE_ACCOUNT_RE).first.is_visible(timeout=200):
            return True
    except Exception:
        pass
    try:
        if page.get_by_role("button", name=_LOG_IN_RE).first.is_visible(timeout=200):
            return True
    except Exception:
        pass
    try:
        inp = page.locator(
            'input[name="username"], input[name="email"], '
            'input[aria-label*="email" i], input[aria-label*="пароль" i], '
            'input[aria-label*="password" i]'
        ).first
        if inp.count() > 0 and inp.is_visible(timeout=200):
            return True
    except Exception:
        pass
    try:
        if _instagram_already_logged_in(page):
            return True
    except Exception:
        pass
    return False


def _captcha_iframe_visible(page) -> bool:
    try:
        frame = page.locator(_CAPTCHA_IFRAME_SEL).first
        return frame.count() > 0 and frame.is_visible(timeout=500)
    except Exception:
        return False


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


def _recaptcha_has_response(page) -> bool:
    """Есть ли уже токен в textarea / grecaptcha.getResponse."""
    js = """
() => {
  const areas = document.querySelectorAll(
    '#g-recaptcha-response, textarea[name="g-recaptcha-response"], ' +
      'textarea[id^="g-recaptcha-response"]'
  );
  for (const el of areas) {
    if ((el.value || '').trim().length > 20) return true;
  }
  try {
    if (typeof grecaptcha !== 'undefined') {
      if (grecaptcha.enterprise && typeof grecaptcha.enterprise.getResponse === 'function') {
        const t = (grecaptcha.enterprise.getResponse() || '').trim();
        if (t.length > 20) return true;
      }
      if (typeof grecaptcha.getResponse === 'function') {
        const t = (grecaptcha.getResponse() || '').trim();
        if (t.length > 20) return true;
      }
    }
  } catch (e) {}
  return false;
}
"""
    for frame in list(page.frames):
        try:
            if frame.evaluate(js):
                return True
        except Exception:
            continue
    return False


def _captcha_passed_locally(page) -> bool:
    """Капча уже пройдена: токен / Next / экран кода."""
    if _is_confirmation_code_screen(page):
        return True
    if _next_button_enabled(page):
        return True
    if _recaptcha_has_response(page):
        return True
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
        abort_if_instagram_registration_failed(page)
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


def _extension_captcha_solved_visible(page) -> bool:
    """Бейдж AntiCaptcha .antigate_solver.solved на странице."""
    try:
        loc = page.locator(".antigate_solver.solved").first
        return loc.count() > 0 and loc.is_visible(timeout=300)
    except Exception:
        return False


def _reload_captcha_iframe(page) -> bool:
    """
    Обновить iframe капчи (переназначить src / grecaptcha.reset).
    True если удалось инициировать reload.
    """
    js = """
() => {
  const sel =
    'iframe#captcha-recaptcha, iframe[src*="captcha"], ' +
    'iframe[src*="recaptcha"], iframe[src*="referer_frame"], ' +
    'iframe[title*="reCAPTCHA"], iframe[title*="recaptcha"]';
  const frames = Array.from(document.querySelectorAll(sel));
  let n = 0;
  for (const iframe of frames) {
    try {
      const src = iframe.getAttribute('src') || iframe.src || '';
      if (!src) continue;
      const u = new URL(src, location.href);
      u.searchParams.set('_zaliver_ts', String(Date.now()));
      iframe.src = u.toString();
      n += 1;
    } catch (e) {
      try {
        const src = iframe.src;
        if (src) {
          iframe.src = src;
          n += 1;
        }
      } catch (e2) {}
    }
  }
  try {
    if (typeof grecaptcha !== 'undefined') {
      if (grecaptcha.enterprise && typeof grecaptcha.enterprise.reset === 'function') {
        grecaptcha.enterprise.reset();
        n += 1;
      } else if (typeof grecaptcha.reset === 'function') {
        grecaptcha.reset();
        n += 1;
      }
    }
  } catch (e) {}
  return n;
}
"""
    total = 0
    for frame in list(page.frames):
        try:
            n = frame.evaluate(js)
            if isinstance(n, (int, float)) and n > 0:
                total += int(n)
        except Exception:
            continue
    if total > 0:
        _log(f"Instagram: обновил iframe капчи ({total}).")
        return True
    return False


def _recaptcha_checkbox_ready(page) -> bool:
    """Виден ли чекбокс reCAPTCHA в каком-либо фрейме."""
    try:
        for frame in list(page.frames):
            try:
                url = (frame.url or "").lower()
            except Exception:
                url = ""
            if url and "recaptcha" not in url and "captcha" not in url:
                if url not in ("", "about:blank"):
                    continue
            try:
                box = frame.locator(_RECAPTCHA_ANCHOR_SEL).first
                if box.count() > 0 and box.is_visible(timeout=400):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    try:
        fl = page.frame_locator(_CAPTCHA_IFRAME_SEL).first
        box = fl.locator(_RECAPTCHA_ANCHOR_SEL).first
        return box.count() > 0 and box.is_visible(timeout=400)
    except Exception:
        return False


def _wait_captcha_ready_after_reload(
    page,
    *,
    max_seconds: float = _CAPTCHA_RELOAD_READY_MAX_S,
) -> bool:
    """Ждать, пока после reload снова виден iframe / чекбокс капчи."""
    deadline = time.monotonic() + float(max_seconds)
    _log(
        f"Instagram: жду прогрузку капчи после обновления "
        f"(до {max_seconds:.0f} с)…"
    )
    # Краткая пауза: старый iframe успеет уйти.
    page.wait_for_timeout(800)
    while time.monotonic() < deadline:
        if _is_confirmation_code_screen(page) or _captcha_passed_locally(page):
            return True
        if _recaptcha_checkbox_ready(page):
            _log("Instagram: капча прогрузилась (чекбокс виден).")
            return True
        if _captcha_iframe_visible(page):
            # iframe есть — ещё чуть подождать чекбокс, но уже почти готово
            page.wait_for_timeout(600)
            if _recaptcha_checkbox_ready(page) or _captcha_iframe_visible(page):
                _log("Instagram: капча прогрузилась (iframe виден).")
                return True
        page.wait_for_timeout(400)
    _log("Instagram: капча после обновления не прогрузилась за отведённое время.")
    return False


def _nudge_captcha_click(page) -> bool:
    """
    Обновить капчу, дождаться прогрузки, затем кликнуть по ней.
    Иногда после клика challenge проходит сам или просыпается расширение.
    """
    reloaded = _reload_captcha_iframe(page)
    if reloaded:
        if not _wait_captcha_ready_after_reload(page):
            # Всё равно пробуем клик — iframe мог быть без чекбокса.
            _log("Instagram: кликаю по капче без подтверждения прогрузки…")
    else:
        _log("Instagram: iframe капчи для обновления не найден — кликаю как есть.")

    # 1) Чекбокс внутри фреймов (anchor iframe Google).
    try:
        for frame in list(page.frames):
            try:
                url = (frame.url or "").lower()
            except Exception:
                url = ""
            if url and "recaptcha" not in url and "captcha" not in url:
                # Пустой about:blank и т.п. — тоже пробуем (вложенные фреймы).
                if url not in ("", "about:blank"):
                    continue
            try:
                box = frame.locator(_RECAPTCHA_ANCHOR_SEL).first
                if box.count() == 0:
                    continue
                if not box.is_visible(timeout=400):
                    continue
                box.click(timeout=2500, force=True)
                _log("Instagram: клик по чекбоксу reCAPTCHA (nudge).")
                return True
            except Exception:
                continue
    except Exception:
        pass

    # 2) frame_locator по видимому iframe капчи.
    try:
        fl = page.frame_locator(_CAPTCHA_IFRAME_SEL).first
        box = fl.locator(_RECAPTCHA_ANCHOR_SEL).first
        box.click(timeout=2500, force=True)
        _log("Instagram: клик по reCAPTCHA через frame_locator (nudge).")
        return True
    except Exception:
        pass

    # 3) Клик по самому iframe (центр) — иногда будит challenge/расширение.
    try:
        iframe = page.locator(_CAPTCHA_IFRAME_SEL).first
        if iframe.count() > 0 and iframe.is_visible(timeout=500):
            iframe.click(timeout=2500, force=True)
            _log("Instagram: клик по iframe капчи (nudge).")
            return True
    except Exception:
        pass

    # 4) Бейдж/кнопка AntiCaptcha на странице.
    try:
        for sel in (
            ".antigate_solver:not(.solved)",
            ".antigate_solver",
            "[class*='antigate']",
        ):
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            if not loc.is_visible(timeout=300):
                continue
            loc.click(timeout=2000, force=True)
            _log(f"Instagram: клик по AntiCaptcha UI ({sel}) (nudge).")
            return True
    except Exception:
        pass

    return False


def _wait_anticaptcha_extension_solve(
    page,
    *,
    max_seconds: float = _EXTENSION_CAPTCHA_MAX_S,
) -> bool:
    """
    Ждать авторешения капчи браузерным расширением AntiCaptcha
    (без HTTP API Zaliver и без reload фрейма).
    """
    deadline = time.monotonic() + float(max_seconds)
    last_log = 0.0
    _log(
        f"Instagram: жду расширение AntiCaptcha до {max_seconds:.0f} с "
        "(бейдж .antigate_solver.solved / «Далее» / экран кода)…"
    )
    while time.monotonic() < deadline:
        accept_instagram_terms_if_present(page, max_seconds=5.0)
        if _is_confirmation_code_screen(page):
            _log("Instagram: расширение — уже экран кода.")
            return True
        if _extension_captcha_solved_visible(page):
            _log("Instagram: расширение отметило капчу как solved.")
            if _next_button_enabled(page) and _click_instagram_next(page):
                return True
            if _is_confirmation_code_screen(page):
                return True
            # Токен мог подставиться без авто-submit — пробуем Next ещё раз.
            page.wait_for_timeout(1500)
            if _next_button_enabled(page) and _click_instagram_next(page):
                return True
            if _is_confirmation_code_screen(page):
                return True
            return True
        if _captcha_passed_locally(page) or _next_button_enabled(page):
            if _click_instagram_next(page) or _is_confirmation_code_screen(page):
                _log("Instagram: капча пройдена (Next активна) — вероятно расширение.")
                return True
        now = time.monotonic()
        if now - last_log >= _MANUAL_CAPTCHA_LOG_EVERY_S:
            _log(
                f"Instagram: всё ещё жду расширение… "
                f"({int(now - (deadline - max_seconds))} с)."
            )
            last_log = now
        page.wait_for_timeout(800)
    _log("Instagram: расширение не решило капчу за отведённое время.")
    return False


def wait_instagram_manual_captcha(
    page,
    *,
    on_manual_captcha=None,
) -> None:
    """
    Бессрочно ждать, пока человек пройдёт капчу (кнопка «Далее»/Next активна),
    затем нажать её. Если уже экран кода — выйти без клика.

    Если долго нет прогресса — периодически кликаем по капче (nudge):
    иногда challenge проходит сам или просыпается AntiCaptcha.
    """
    if _is_confirmation_code_screen(page):
        _log("Instagram: капча не нужна — уже экран кода.")
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
    last_nudge = 0.0
    while True:
        if _is_confirmation_code_screen(page):
            _log("Instagram: во время ожидания капчи открылся экран кода.")
            return
        if _next_button_enabled(page):
            _log("Instagram: «Далее» активна — капча пройдена вручную.")
            break
        if _captcha_passed_locally(page):
            _log("Instagram: капча пройдена (токен/Next) во время ожидания.")
            break
        if _extension_captcha_solved_visible(page):
            _log("Instagram: расширение отметило solved во время ручного ожидания.")
            break
        now = time.monotonic()
        waited = now - started
        if waited >= _MANUAL_CAPTCHA_NUDGE_AFTER_S and (
            last_nudge <= 0.0
            or (now - last_nudge) >= _MANUAL_CAPTCHA_NUDGE_EVERY_S
        ):
            _log(
                f"Instagram: ручная капча без прогресса {int(waited)} с — "
                "обновляю капчу и кликаю (nudge)…"
            )
            if _nudge_captcha_click(page):
                page.wait_for_timeout(1500)
                if _captcha_passed_locally(page) or _extension_captcha_solved_visible(
                    page
                ):
                    _log("Instagram: после nudge капча выглядит пройденной.")
                    break
                if _is_confirmation_code_screen(page):
                    _log("Instagram: после nudge уже экран кода.")
                    return
            else:
                _log("Instagram: nudge — элемент капчи для клика не найден.")
            last_nudge = time.monotonic()
        if now - last_log >= _MANUAL_CAPTCHA_LOG_EVERY_S:
            _log(
                f"Instagram: всё ещё жду ручную капчу… ({int(waited)} с). "
                "Пройдите капчу в окне браузера."
            )
            last_log = now
        page.wait_for_timeout(800)

    # После nudge/токена Next может появиться с задержкой.
    for _ in range(8):
        if _is_confirmation_code_screen(page):
            _log("Instagram: экран кода после прохождения капчи.")
            return
        if _next_button_enabled(page):
            break
        page.wait_for_timeout(500)

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
    if _is_confirmation_code_screen(page):
        return
    raise RuntimeError(
        f"Instagram: «Далее» стала активной, но клик не удался (URL={page.url!r})"
    )


def wait_instagram_after_signup(
    page,
    *,
    on_manual_captcha=None,
) -> None:
    """После «Отправить»: расширение AntiCaptcha → иначе ручное ожидание."""
    # Сразу после Submit иногда показывают «agree to our terms».
    page.wait_for_timeout(600)
    abort_if_instagram_registration_failed(page)
    accept_instagram_terms_if_present(page, max_seconds=15.0)
    abort_if_instagram_registration_failed(page)
    outcome = _wait_captcha_or_code_screen(page)
    if outcome == "code":
        return
    # Дать iframe/расширению время подняться.
    settle_ms = int(max(0.0, float(_CAPTCHA_IFRAME_SETTLE_S)) * 1000)
    if settle_ms > 0:
        _log(
            f"Instagram: жду {_CAPTCHA_IFRAME_SETTLE_S:.0f} с после iframe "
            "перед решением капчи…"
        )
        page.wait_for_timeout(settle_ms)
        abort_if_instagram_registration_failed(page)
        if _is_confirmation_code_screen(page):
            _log("Instagram: за время паузы уже экран кода.")
            return

    if _wait_anticaptcha_extension_solve(page):
        abort_if_instagram_registration_failed(page)
        if _is_confirmation_code_screen(page):
            return
        wait_instagram_confirmation_code_screen(page)
        return

    wait_instagram_manual_captcha(
        page,
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
    """Ждать экран «Введите код подтверждения» (код из почты)."""
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        accept_instagram_terms_if_present(page, max_seconds=8.0)
        if _is_confirmation_code_screen(page):
            _log("Instagram: экран кода подтверждения.")
            return
        page.wait_for_timeout(500)
    raise RuntimeError(
        f"Instagram: экран кода подтверждения не появился за {max_seconds:.0f} с "
        f"(URL={page.url!r})"
    )


def fill_instagram_confirmation_code(page, code: str) -> None:
    """Ввести код в поле «Код подтверждения» / Bloks «Введите код»."""
    code = (code or "").strip()
    if not re.fullmatch(r"\d{6,8}", code):
        raise RuntimeError(f"Instagram: ожидался 6–8-значный код, получили {code!r}")

    filled = False
    try:
        _fill_labeled_input(
            page,
            [
                "Код подтверждения",
                "Confirmation code",
                "Security code",
                "Введите код",
                "Enter code",
            ],
            code,
        )
        filled = True
    except Exception as e:
        _log(f"Instagram: fill по label не сработал: {e!r}")

    if not filled:
        try:
            inp = page.locator(
                'input[aria-label="Введите код" i], '
                'input[aria-label="Enter code" i], '
                'input[maxlength="6"], '
                'input[maxlength="8"], '
                'input[inputmode="numeric"]'
            ).first
            if inp.count() > 0 and inp.is_visible(timeout=2000):
                inp.click(timeout=4000)
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
    """
    Экран «Confirm you're human…» / «подтвердите, что вы — человек»
    с кнопкой Continue / Продолжить (часто на /accounts/suspended).
    """
    try:
        heading = page.locator(
            '[role="heading"][aria-label*="Confirm you" i], '
            '[role="heading"][aria-label*="human" i], '
            '[role="heading"][aria-label*="человек" i], '
            '[role="heading"][aria-label*="аккаунт" i]'
        ).first
        if heading.count() > 0 and heading.is_visible(timeout=300):
            label = (heading.get_attribute("aria-label") or "").strip()
            if _HUMAN_CONFIRM_RE.search(label) and _human_confirm_mentions_account(
                label
            ):
                return True
    except Exception:
        pass
    try:
        text = page.get_by_text(_HUMAN_CONFIRM_RE).first
        if text.count() > 0 and text.is_visible(timeout=300):
            try:
                tip = page.get_by_text(_HUMAN_CONFIRM_TIP_RE).first
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
                if _human_confirm_mentions_account(body) or _HUMAN_CONFIRM_TIP_RE.search(
                    body
                ):
                    return True
    except Exception:
        pass
    # На /accounts/suspended: Continue + «~30 секунд» / human-текст.
    if _is_accounts_suspended(page):
        try:
            tip = page.get_by_text(_HUMAN_CONFIRM_TIP_RE).first
            if tip.count() > 0 and tip.is_visible(timeout=200):
                return True
        except Exception:
            pass
        try:
            body = page.locator("body").inner_text(timeout=400) or ""
            if _HUMAN_CONFIRM_RE.search(body):
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


def resolve_instagram_image_captcha(page) -> None:
    """Image/SMS captcha → тег и остановка авторега (не решаем)."""
    abort_if_instagram_sms_image_captcha(page)


def handle_instagram_human_confirmation(
    page,
    username: str,
    *,
    appear_timeout_s: float = 45.0,
) -> None:
    """
    После кода из почты: если «Confirm you're human» / /accounts/suspended /
    image captcha — сразу SMS-ошибка (Continue не жмём).
    """
    accept_instagram_cookie_consent_if_present(page, appear_seconds=2.0)
    dismiss_instagram_scraping_warning_if_present(page)
    if _instagram_home_ready(page, username):
        return

    # Уже на экране после кода — стоп без ожидания.
    abort_if_instagram_sms_image_captcha(page)

    deadline = time.monotonic() + max(0.0, float(appear_timeout_s))
    while time.monotonic() < deadline:
        accept_instagram_cookie_consent_if_present(page)
        dismiss_instagram_scraping_warning_if_present(page)
        if _instagram_home_ready(page, username):
            return
        abort_if_instagram_sms_image_captcha(page)
        page.wait_for_timeout(500)

    _log(
        "Instagram: экран Confirm you're human / suspended не появился — "
        "идём к проверке главной."
    )


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
        # После кода часто редирект на /consent/?flow=user_cookie_choice_v2.
        accept_instagram_cookie_consent_if_present(page)
        dismiss_instagram_scraping_warning_if_present(page)
        abort_if_instagram_sms_image_captcha(page)
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


def _left_email_confirmation_screen(page, username: str) -> bool:
    """True если уже не экран кода из почты (human check / главная / SMS и т.п.)."""
    if _is_cookie_consent_screen(page) or _is_cookie_consent_url(_page_url(page)):
        return True
    if _is_human_confirm_intro_screen(page):
        return True
    if _is_accounts_suspended(page) or _is_image_captcha_screen(page):
        return True
    if _is_terms_agree_screen(page):
        return True
    if _instagram_home_ready(page, username):
        return True
    # URL ушёл с signup/confirm email — считаем, что код принят.
    try:
        url = (page.url or "").strip().lower()
        if "instagram.com" in url:
            left_signup = all(
                token not in url
                for token in (
                    "emailsignup",
                    "accounts/signup",
                    "confirm_email",
                    "challenge",
                )
            )
            if left_signup and ("onetap" in url or "/accounts/" not in url):
                return True
    except Exception:
        pass
    return not _is_confirmation_code_screen(page)


def complete_instagram_email_confirmation(
    ig_page,
    gmail_page,
    username: str,
) -> None:
    """
    Экран кода → Gmail (код) → ввод → Продолжить →
    (/consent cookie choice) → (human check) → главная.

    Новое письмо ищем только если код явно отклонён или всё ещё
    именно экран кода из почты (не image captcha / human check).
    """
    from zaliver.instagram_upload.gmail_confirmation_code import (
        fetch_instagram_confirmation_code_from_gmail,
    )

    wait_instagram_confirmation_code_screen(ig_page)
    # Дать Instagram время отправить письмо, прежде чем идти в Gmail.
    _log("Instagram: экран кода — ждём 3 с перед переходом на почту…")
    ig_page.wait_for_timeout(3000)
    used_codes: set[str] = set()
    last_code = ""

    for attempt in range(1, _EMAIL_CODE_ATTEMPTS + 1):
        code = fetch_instagram_confirmation_code_from_gmail(
            gmail_page,
            exclude_codes=used_codes,
        )
        last_code = code
        used_codes.add(code)
        try:
            ig_page.bring_to_front()
        except Exception:
            pass
        ig_page.wait_for_timeout(500)
        if attempt > 1 and _left_email_confirmation_screen(ig_page, username):
            _log(
                "Instagram: пока ходили в Gmail, экран почтового кода уже сменился — "
                "новое письмо не нужно."
            )
            break
        wait_instagram_confirmation_code_screen(ig_page, max_seconds=30.0)
        fill_instagram_confirmation_code(ig_page, code)
        click_instagram_continue_after_code(ig_page)

        settle_deadline = time.monotonic() + _AFTER_EMAIL_CODE_SETTLE_S
        while time.monotonic() < settle_deadline:
            # Сразу после кода часто /consent/?flow=user_cookie_choice_v2.
            if _is_cookie_consent_url(_page_url(ig_page)) or _is_cookie_consent_screen(
                ig_page
            ):
                accept_instagram_cookie_consent_if_present(
                    ig_page, appear_seconds=1.0, max_seconds=25.0
                )
            if _left_email_confirmation_screen(ig_page, username):
                break
            if _email_confirmation_code_rejected(ig_page):
                break
            ig_page.wait_for_timeout(500)

        if _left_email_confirmation_screen(ig_page, username):
            _log(
                f"Instagram: код {code} принят — экран почтового кода сменился "
                f"(URL={ig_page.url!r})."
            )
            break

        rejected = _email_confirmation_code_rejected(ig_page)
        still_email_code = _is_confirmation_code_screen(ig_page)
        if not rejected and not still_email_code:
            # Не email-code и не ошибка — ушли на другой шаг (captcha/human/…).
            _log(
                f"Instagram: после кода {code} больше не экран почтового кода "
                f"(URL={ig_page.url!r}) — не ждём новое письмо."
            )
            break

        _log(
            f"Instagram: после кода {code} "
            + (
                "код отклонён"
                if rejected
                else "всё ещё экран подтверждения из почты"
            )
            + f" (попытка {attempt}/{_EMAIL_CODE_ATTEMPTS}) — "
            "открываем Gmail #inbox/ и ищем новое письмо."
        )
    else:
        raise RuntimeError(
            f"Instagram: после {_EMAIL_CODE_ATTEMPTS} кодов из почты "
            f"(последний {last_code!r}) экран подтверждения не сменился "
            f"(URL={ig_page.url!r})"
        )

    # После кода: /consent/?flow=user_cookie_choice_v2 → кнопка со словом cookie.
    accept_instagram_cookie_consent_if_present(
        ig_page, appear_seconds=8.0, max_seconds=30.0
    )
    handle_instagram_human_confirmation(ig_page, username)
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


def _navigate_page_via_cdp_background(page, url: str, *, label: str) -> bool:
    """
    Page.navigate по CDP без Target.activateTarget — вкладка не выходит на передний план.
    Нужно для Yt+Inst, чтобы Studio не теряла мастер загрузки.
    """
    cdp = None
    try:
        cdp = page.context.new_cdp_session(page)
        _log(f"{label}: навигация (CDP Page.navigate, фон) → {url}")
        cdp.send("Page.navigate", {"url": url})
        deadline = time.monotonic() + 90.0
        while time.monotonic() < deadline:
            try:
                page.wait_for_timeout(200)
            except Exception:
                time.sleep(0.2)
            cur = _page_url(page)
            if not cur or cur.lower() == "about:blank":
                continue
            if _is_instagram_url(cur) or cur.lower() != "about:blank":
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=30_000)
                except Exception as e:
                    _log(f"{label}: wait domcontentloaded после CDP navigate: {e!r}")
                _log(f"{label}: OK (фон), URL={cur!r}")
                if _is_cookie_consent_url(cur):
                    accept_instagram_cookie_consent_if_present(
                        page, appear_seconds=8.0, max_seconds=30.0
                    )
                return True
        return False
    except Exception as e:
        _log(f"{label}: CDP Page.navigate не удалось: {e!r}")
        return False
    finally:
        if cdp is not None:
            try:
                cdp.detach()
            except Exception:
                pass


def _navigate_page_to(
    page, url: str, *, label: str = "Instagram", keep_in_background: bool = False
) -> None:
    """Надёжный goto для CDP/антидетекта (часто new_page зависает на about:blank)."""
    if keep_in_background:
        if _navigate_page_via_cdp_background(page, url, label=label):
            return
        _log(
            f"{label}: фоновый CDP navigate не сработал — "
            "fallback без bring_to_front."
        )

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
            # Yt+Inst: не перехватывать фокус у Studio — иначе при /reels/
            # вкладка Instagram выходит на передний план, а возврат на YouTube
            # перезагружает мастер загрузки.
            if not keep_in_background:
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
                if _is_cookie_consent_url(cur):
                    accept_instagram_cookie_consent_if_present(
                        page, appear_seconds=8.0, max_seconds=30.0
                    )
                return
            if cur and cur.lower() != "about:blank":
                # Уже не blank (редирект/логин и т.п.) — считаем успехом.
                _log(f"{label}: загрузилось URL={cur!r}")
                if _is_cookie_consent_url(cur):
                    accept_instagram_cookie_consent_if_present(
                        page, appear_seconds=8.0, max_seconds=30.0
                    )
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
    # Диалог часто появляется с задержкой после навигации.
    accept_instagram_cookie_consent_if_present(ig_page, appear_seconds=12.0)

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
            accept_instagram_cookie_consent_if_present(
                ig_page, appear_seconds=10.0
            )
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

    _wait_signup_form(ig_page)
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

    _log(f"Instagram: email={email!r}, username={local!r} (full name пропускаем)")

    # Cookie-диалог может всплыть уже на форме signup.
    accept_instagram_cookie_consent_if_present(page, appear_seconds=5.0)

    _fill_labeled_input(
        page,
        [
            "Мобильный телефон или электронный адрес",
            "Номер мобильного телефона или электронный адрес",
            "Mobile number or email",
            "электронный адрес",
            "телефон",
            "email",
            "mobile",
            "number",
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
    # Баннер «An error occurred during your registration…» часто сразу после Submit.
    page.wait_for_timeout(1200)
    abort_if_instagram_registration_failed(page)
    return username


@instagram_entrypoint
def run_instagram_registration_after_gmail(
    gmail_page,
    credentials: GoogleLoginCredentials | None,
    *,
    on_manual_captcha=None,
    profile_id: str | None = None,
) -> str:
    """
    Gmail уже открыт → Instagram signup → расширение AntiCaptcha / ручная капча →
    код из почты. Возвращает зарегистрированный username.

    Если в профиле уже выполнен вход в Instagram — считаем успехом
    (новый аккаунт создавать не нужно).
    """
    if credentials is None:
        raise RuntimeError("Instagram: нет credentials профиля (yt_login / yt_password).")
    try:
        ig_page = open_instagram_signup_tab(
            gmail_page,
            password=(credentials.password or "").strip(),
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
            on_manual_captcha=on_manual_captcha,
        )
        complete_instagram_email_confirmation(
            ig_page,
            gmail_page,
            username,
        )
        return username
    except Exception:
        # Оставляем вкладку открытой для разбора.
        raise
