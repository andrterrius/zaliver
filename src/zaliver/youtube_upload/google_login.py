from __future__ import annotations

import calendar
import random
import re
import time
from dataclasses import dataclass

from zaliver.youtube_upload.totp import get_totp_token

YT_LOGIN_KEY = "yt_login"
YT_PASSWORD_KEY = "yt_password"
YT_2FA_KEY = "yt_2fa"
YT_OLDEST_NAME_KEY = "yt_oldest_name"
GMAIL_LOGIN_KEY = "gmail_login"
GMAIL_PASSWORD_KEY = "gmail_password"
GMAIL_2FA_KEY = "gmail_2fa"

_IDENTITY_HEADING_RE = re.compile(
    r"подтвердите\s+свою\s+личность|confirm\s+your\s+identity",
    re.I,
)
_2FA_HEADING_RE = re.compile(
    r"двухэтапн|two[- ]step|2[- ]step",
    re.I,
)
_2FA_AUTHENTICATOR_CHALLENGE_RE = re.compile(
    r"google\s+authenticator|"
    r"создайте\s+код\s+в\s+приложении|"
    r"create\s+code\s+in\s+the(?:\s+\w+){0,4}\s+authenticator",
    re.I,
)
_PASSKEY_ENROLLMENT_HEADING_RE = re.compile(
    r"входите\s+в\s+аккаунт\s+быстрее|sign\s+in\s+faster|faster\s+sign[- ]in",
    re.I,
)
_SELFIE_ENROLLMENT_HEADING_RE = re.compile(
    r"добавьте\s+селфи|add\s+(a\s+)?selfie|video\s*selfie",
    re.I,
)
_NEXT_BTN_RE = re.compile(r"^далее$|^next$", re.I)
_NOT_NOW_BTN_RE = re.compile(r"^не\s+сейчас$|^not\s+now$", re.I)
_DONT_ASK_RE = re.compile(
    r"don['\u2019]?t\s+ask\s+again|больше\s+не\s+спрашивать",
    re.I,
)
_NO_SUBSCRIBERS_RE = re.compile(
    r"no\s+subscribers|нет\s+подписчиков|без\s+подписчиков|0\s+подписчик",
    re.I,
)
_SUBSCRIBER_WORD_RE = re.compile(r"subscriber|subscribers|подписчик", re.I)
# 1.2K subscribers / 12 тыс. подписчиков / 1,2 млн подписчиков
_SUBSCRIBER_SCALED_RE = re.compile(
    r"(?P<num>[\d][\d\s.,\u00a0\u202f]*)\s*"
    r"(?P<scale>k|m|тыс\.?|млн\.?|million|thousand)\b\.?\s*"
    r"(?:(?:\w+)\s+){0,3}"
    r"(?:subscriber|subscribers|подписчик\w*)",
    re.I,
)
# 1 subscriber / 1 234 подписчиков / 1,234 subscribers
_SUBSCRIBER_PLAIN_RE = re.compile(
    r"(?P<num>[\d][\d\s.,\u00a0\u202f]*)\s+"
    r"(?:subscriber|subscribers|подписчик\w*)",
    re.I,
)
_SCALE_MULTIPLIERS: dict[str, float] = {
    "k": 1_000.0,
    "m": 1_000_000.0,
    "тыс": 1_000.0,
    "млн": 1_000_000.0,
    "million": 1_000_000.0,
    "thousand": 1_000.0,
}
_CHANNEL_PICKER_TITLE_RE = re.compile(
    r"select\s+a\s+channel|выберите\s+канал|choose\s+a\s+channel",
    re.I,
)
_ACCOUNT_CHOOSER_HEADING_RE = re.compile(
    r"выберите\s+аккаунт|choose\s+(an\s+)?account",
    re.I,
)
_USE_ANOTHER_ACCOUNT_RE = re.compile(
    r"использовать\s+другой\s+аккаунт|use\s+another\s+account",
    re.I,
)
_RECOVERY_INFO_HEADING_RE = re.compile(
    r"убедитесь,\s*что\s+вы\s+всегда\s+сможете\s+войти|"
    r"make\s+sure\s+you\s+(can\s+)?always\s+sign\s+in",
    re.I,
)
_RECOVERY_PHONE_HEADING_RE = re.compile(
    r"укажите\s+номер\s+телефона|add\s+(a\s+)?phone\s+number|enter\s+(your\s+)?phone",
    re.I,
)
_HOME_ADDRESS_HEADING_RE = re.compile(
    r"set\s+a\s+home\s+address|укажите\s+домашний\s+адрес|"
    r"домашний\s+адрес",
    re.I,
)
_SKIP_BTN_RE = re.compile(r"^skip$|^пропустить$", re.I)
_BIRTHDAY_HEADING_RE = re.compile(
    r"add\s+your\s+birthday|добавьте\s+дату\s+рождения|"
    r"укажите\s+дату\s+рождения|"
    r"date\s+of\s+birth\s+is\s+missing|дата\s+рождения\s+не\s+указана|"
    r"дат[ауы]?\s+рождения",
    re.I,
)
_SAVE_BTN_RE = re.compile(r"^save$|^сохранить$", re.I)
_BIRTHDAY_CONFIRM_HEADING_RE = re.compile(
    r"confirm\s+birthday|подтвердите\s+дату\s+рождения|"
    r"confirm\s+date\s+of\s+birth",
    re.I,
)
_CONFIRM_BTN_RE = re.compile(r"^confirm$|^подтвердить$", re.I)
_BIRTHDAY_SUCCESS_HEADING_RE = re.compile(r"thank\s+you|спасибо", re.I)
_BIRTHDAY_SUCCESS_BODY_RE = re.compile(
    r"your\s+birthday\s+helps\s+confirm|"
    r"дата\s+рождения\s+помогает|"
    r"won'?t\s+make\s+your\s+birthday\s+public|"
    r"не\s+будет\s+общедоступн",
    re.I,
)
_DONE_BTN_RE = re.compile(r"^done$|^готово$", re.I)
_BIRTHDAY_YEAR_MIN = 1970
_BIRTHDAY_YEAR_MAX = 1990
_BIRTHDAY_MONTH_NAMES_RU = (
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

_GOOGLE_LOGIN_MAX_S = 180.0


def _birthday_form_root(scope):
    return scope.locator('div[data-year-required="true"]').first


def _birthday_day_input(scope):
    root = _birthday_form_root(scope)
    return root.locator(
        'input[placeholder="DD"], input[placeholder="ДД"], '
        'input[aria-label*="day" i], input[aria-label*="дату" i], '
        'input[inputmode="numeric"]'
    ).first


def _birthday_year_input(scope):
    root = _birthday_form_root(scope)
    return root.locator(
        'input[placeholder="YYYY"], input[placeholder="ГГГГ"], '
        'input[aria-label*="year" i], input[aria-label*="год" i], '
        'input[inputmode="numeric"]'
    ).last


def _birthday_month_combo(scope):
    root = _birthday_form_root(scope)
    return root.locator('[role="combobox"]').or_(
        scope.locator('[aria-label*="month" i][role="combobox"]')
    ).or_(
        scope.locator('[aria-label*="месяц" i][role="combobox"]')
    ).first


def _random_birthday() -> tuple[int, int, int]:
    year = random.randint(_BIRTHDAY_YEAR_MIN, _BIRTHDAY_YEAR_MAX)
    month = random.randint(1, 12)
    _, max_day = calendar.monthrange(year, month)
    day = random.randint(1, max_day)
    return day, month, year


def random_birthday() -> tuple[int, int, int]:
    """Публичный генератор даты рождения (день, месяц 1–12, год)."""
    return _random_birthday()


class GoogleLoginCredentialsMissingError(RuntimeError):
    """В данных учётки нет данных для входа — профиль пропускаем, остальные продолжают."""


class GoogleLoginPasswordMissingError(GoogleLoginCredentialsMissingError):
    """В custom_data профиля нет yt_password — профиль пропускаем, остальные продолжают."""


@dataclass(frozen=True, slots=True)
class GoogleLoginCredentials:
    email: str = ""
    password: str = ""
    twofa_token: str = ""


def has_login_credentials(
    credentials: GoogleLoginCredentials | None,
) -> bool:
    if credentials is None:
        return False
    return bool(
        (credentials.email or "").strip()
        or (credentials.password or "").strip()
        or (credentials.twofa_token or "").strip()
    )


def credentials_from_custom_data(
    custom_data: dict[str, object] | None,
) -> GoogleLoginCredentials | None:
    if not isinstance(custom_data, dict):
        return None
    email = str(custom_data.get(YT_LOGIN_KEY) or "").strip()
    password = str(custom_data.get(YT_PASSWORD_KEY) or "").strip()
    twofa = str(custom_data.get(YT_2FA_KEY) or "").strip()
    if not email and not password and not twofa:
        return None
    return GoogleLoginCredentials(email=email, password=password, twofa_token=twofa)


def gmail_or_yt_credentials_from_custom_data(
    custom_data: dict[str, object] | None,
) -> GoogleLoginCredentials | None:
    """
    Для входа в Gmail: если в custom_data есть gmail_* — только они,
    иначе yt_login / yt_password / yt_2fa.
    """
    if not isinstance(custom_data, dict):
        return None
    email = str(custom_data.get(GMAIL_LOGIN_KEY) or "").strip()
    password = str(custom_data.get(GMAIL_PASSWORD_KEY) or "")
    twofa = str(custom_data.get(GMAIL_2FA_KEY) or "").strip()
    if email or password or twofa:
        return GoogleLoginCredentials(
            email=email,
            password=password,
            twofa_token=twofa,
        )
    return credentials_from_custom_data(custom_data)


def oldest_name_from_custom_data(custom_data: dict[str, object] | None) -> str:
    if not isinstance(custom_data, dict):
        return ""
    return str(custom_data.get(YT_OLDEST_NAME_KEY) or "").strip()


def _log(message: str) -> None:
    from zaliver.youtube_upload import studio as _studio

    _studio._log(message)


def _page_url_lower(page) -> str:
    try:
        return (page.url or "").lower()
    except Exception:
        return ""


def _scope_url_lower(scope) -> str:
    try:
        return (getattr(scope, "url", None) or "").lower()
    except Exception:
        return ""


def _google_auth_scopes(page):
    """Страница и фреймы, где может отображаться UI входа Google."""
    seen: set[int] = set()
    scopes: list = [page]
    try:
        scopes.extend(page.frames)
    except Exception:
        pass
    for scope in scopes:
        sid = id(scope)
        if sid in seen:
            continue
        seen.add(sid)
        yield scope


def _use_another_account_locator(scope):
    return (
        scope.locator('[jsname="rwl3qc"]')
        .or_(scope.locator("div.riDSKb", has_text=_USE_ANOTHER_ACCOUNT_RE))
        .or_(scope.get_by_role("link", name=_USE_ANOTHER_ACCOUNT_RE))
        .or_(scope.get_by_text(_USE_ANOTHER_ACCOUNT_RE))
    )


def google_auth_interaction_visible(page) -> bool:
    """Один из шагов входа Google / выбора канала YouTube."""
    if "accounts.google.com" in _page_url_lower(page):
        return True
    if _account_chooser_step_visible(page):
        return True
    if _identifier_step_visible(page):
        return True
    try:
        if page.locator("ytd-channel-switcher-renderer").first.is_visible(timeout=300):
            return True
    except Exception:
        pass
    try:
        if page.locator('input[name="Passwd"]').first.is_visible(timeout=300):
            return True
    except Exception:
        pass
    try:
        if page.locator('input[name="totpPin"], #totpPin').first.is_visible(timeout=300):
            return True
    except Exception:
        pass
    try:
        h = page.locator("#headingText").first
        if h.is_visible(timeout=300):
            txt = (h.inner_text(timeout=500) or "").strip()
            if _IDENTITY_HEADING_RE.search(txt):
                return True
            if _2FA_AUTHENTICATOR_CHALLENGE_RE.search(txt):
                return True
            if _PASSKEY_ENROLLMENT_HEADING_RE.search(txt):
                return True
    except Exception:
        pass
    if _passkey_enrollment_visible(page):
        return True
    if _selfie_enrollment_visible(page):
        return True
    if _recovery_info_step_visible(page):
        return True
    if _home_address_step_visible(page):
        return True
    if _birthday_confirm_step_visible(page):
        return True
    if _birthday_success_step_visible(page):
        return True
    if _birthday_step_visible(page):
        return True
    if _2fa_challenge_picker_visible(page):
        return True
    return False


def _account_chooser_step_visible(page) -> bool:
    """Экран «Выберите аккаунт» перед шагом ввода email."""
    for scope in _google_auth_scopes(page):
        url = _scope_url_lower(scope)
        if "accountchooser" in url:
            return True
        try:
            if scope.locator('[data-p*="identity-signin-account-chooser"]').count() > 0:
                return True
        except Exception:
            pass
        try:
            h = scope.locator("#headingText").first
            if h.count() > 0 and h.is_visible(timeout=300):
                if _ACCOUNT_CHOOSER_HEADING_RE.search(
                    (h.inner_text(timeout=500) or "").strip()
                ):
                    return True
        except Exception:
            pass
        try:
            if scope.get_by_text(_ACCOUNT_CHOOSER_HEADING_RE).first.is_visible(timeout=300):
                return True
        except Exception:
            pass
        try:
            btn = _use_another_account_locator(scope)
            if btn.count() > 0 and btn.first.is_visible(timeout=300):
                return True
        except Exception:
            pass
    return False


def _click_use_another_account_js(scope) -> bool:
    try:
        return bool(
            scope.evaluate(
                """() => {
                    const labels = [
                        'Использовать другой аккаунт',
                        'Use another account',
                    ];
                    const matches = (el) => {
                        const t = (el.innerText || el.textContent || '').trim();
                        return labels.some((x) => t === x || t.includes(x));
                    };
                    const tryClick = (root) => {
                        if (!root) return false;
                        const direct = root.querySelector('[jsname="rwl3qc"]');
                        if (direct) {
                            direct.click();
                            return true;
                        }
                        for (const el of root.querySelectorAll('[role="link"], button, div, span')) {
                            if (matches(el)) {
                                el.click();
                                return true;
                            }
                        }
                        for (const host of root.querySelectorAll('*')) {
                            if (host.shadowRoot && tryClick(host.shadowRoot)) {
                                return true;
                            }
                        }
                        return false;
                    };
                    return tryClick(document);
                }"""
            )
        )
    except Exception:
        return False


def _click_use_another_account(page) -> None:
    _log("Google: «Выберите аккаунт» — нажимаем «Использовать другой аккаунт»…")
    try:
        page.wait_for_load_state("domcontentloaded", timeout=8_000)
    except Exception:
        pass

    clicked = False
    last_err: str = ""
    for scope in _google_auth_scopes(page):
        btn = _use_another_account_locator(scope)
        try:
            if btn.count() == 0:
                continue
            target = btn.first
            target.wait_for(state="visible", timeout=20_000)
            try:
                target.scroll_into_view_if_needed(timeout=5_000)
            except Exception:
                pass
            try:
                target.click(timeout=30_000)
            except Exception as e:
                last_err = repr(e)
                target.click(timeout=30_000, force=True)
            clicked = True
            break
        except Exception as e:
            last_err = repr(e)
            continue

    if not clicked:
        for scope in _google_auth_scopes(page):
            if _click_use_another_account_js(scope):
                clicked = True
                _log("Google: клик «Использовать другой аккаунт» через JS.")
                break

    if not clicked:
        raise RuntimeError(
            "Google: кнопка «Использовать другой аккаунт» не найдена или не видна. "
            f"URL={page.url!r}. {last_err}"
        )

    page.wait_for_timeout(900)
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if _identifier_step_visible(page):
            return
        page.wait_for_timeout(250)


def _use_another_account_present_js(scope) -> bool:
    try:
        return bool(
            scope.evaluate(
                """() => {
                    const has = (root) => {
                        if (!root) return false;
                        if (root.querySelector('[jsname="rwl3qc"]')) return true;
                        for (const host of root.querySelectorAll('*')) {
                            if (host.shadowRoot && has(host.shadowRoot)) return true;
                        }
                        return false;
                    };
                    return has(document);
                }"""
            )
        )
    except Exception:
        return False


def _try_use_another_account_if_present(page) -> bool:
    """Запасной путь: ищем кнопку напрямую (в т.ч. во фреймах и shadow DOM)."""
    for scope in _google_auth_scopes(page):
        try:
            btn = _use_another_account_locator(scope)
            if btn.count() > 0 and btn.first.is_visible(timeout=300):
                _click_use_another_account(page)
                return True
        except Exception:
            continue
        if _use_another_account_present_js(scope):
            _click_use_another_account(page)
            return True
    return False


def _identifier_field_locator(page):
    """Только видимое поле email на первом шаге (не #hiddenEmail на шаге пароля)."""
    return page.locator("#identifierId")


def _identifier_step_visible(page) -> bool:
    """Первый шаг входа: «Вход» — поле email/телефона (#identifierId)."""
    if _password_step_visible(page):
        return False
    try:
        if _identifier_field_locator(page).first.is_visible(timeout=400):
            return True
    except Exception:
        pass
    return False


def _auth_heading_text(page) -> str:
    for scope in _google_auth_scopes(page):
        try:
            h = scope.locator("#headingText").first
            if h.count() > 0 and h.is_visible(timeout=200):
                return (h.inner_text(timeout=400) or "").strip()
        except Exception:
            pass
    return ""


def _totp_input_locator(scope):
    return (
        scope.locator('input[name="totpPin"], #totpPin')
        .or_(scope.locator('input[name="idvPin"], #idvPin'))
        .or_(scope.locator('input[autocomplete="one-time-code"]'))
        .or_(scope.locator('input[type="tel"][aria-label]'))
        .or_(scope.locator('[data-challengeid] input[type="tel"]'))
    )


def _identity_confirm_visible(page) -> bool:
    if _totp_step_visible(page):
        return False
    if _2fa_challenge_picker_visible(page):
        return False
    heading = _auth_heading_text(page)
    if _2FA_AUTHENTICATOR_CHALLENGE_RE.search(heading):
        return False
    if _2FA_HEADING_RE.search(heading):
        return False
    url = _page_url_lower(page)
    if any(
        token in url
        for token in (
            "challenge/totp",
            "challenge/ipp",
            "totpauthorization",
            "challenge/az",
        )
    ):
        return False
    if "confirmidentifier" in url or "confirm-identifier" in url:
        return True
    if heading and _IDENTITY_HEADING_RE.search(heading):
        return True
    try:
        if page.locator('[data-p*="identity-signin-confirm-identifier"]').count() > 0:
            return True
    except Exception:
        pass
    return False


def _password_step_visible(page) -> bool:
    try:
        return page.locator('input[name="Passwd"]').first.is_visible(timeout=400)
    except Exception:
        return False


def _totp_step_visible(page) -> bool:
    url = _page_url_lower(page)
    if any(
        token in url
        for token in (
            "challenge/totp",
            "challenge/ipp",
            "totpauthorization",
        )
    ):
        return True
    for scope in _google_auth_scopes(page):
        try:
            loc = _totp_input_locator(scope)
            if loc.count() > 0 and loc.first.is_visible(timeout=400):
                return True
        except Exception:
            pass
        try:
            if scope.locator("#totpNext").count() > 0 and scope.locator(
                "#totpNext"
            ).first.is_visible(timeout=200):
                heading = _auth_heading_text(page)
                if _2FA_AUTHENTICATOR_CHALLENGE_RE.search(heading):
                    return True
        except Exception:
            pass
    heading = _auth_heading_text(page)
    if _2FA_AUTHENTICATOR_CHALLENGE_RE.search(heading):
        return True
    for scope in _google_auth_scopes(page):
        try:
            if scope.get_by_text(_2FA_AUTHENTICATOR_CHALLENGE_RE).first.is_visible(
                timeout=300
            ):
                if _totp_input_locator(scope).count() > 0:
                    return True
                if scope.locator("#totpNext").count() > 0:
                    return True
        except Exception:
            pass
    return False


def _google_authenticator_challenge_locator(scope):
    return (
        scope.locator(
            '[data-action="selectchallenge"][data-challengetype="6"]'
            ':not([data-challengeunavailable="true"])'
        )
        .or_(
            scope.locator(
                '[jsname="EBHGs"][data-challengetype="6"]:not([data-challengeunavailable="true"])'
            )
        )
        .or_(
            scope.locator('[data-action="selectchallenge"]', has_text=_2FA_AUTHENTICATOR_CHALLENGE_RE)
        )
        .or_(scope.get_by_role("link", name=_2FA_AUTHENTICATOR_CHALLENGE_RE))
    )


def _2fa_challenge_picker_visible(page) -> bool:
    """Экран «Выберите способ входа» перед полем кода Google Authenticator."""
    if _totp_step_visible(page):
        return False
    for scope in _google_auth_scopes(page):
        try:
            locator = _google_authenticator_challenge_locator(scope)
            if locator.count() > 0 and locator.first.is_visible(timeout=400):
                return True
        except Exception:
            pass
    return False


def _click_google_authenticator_challenge(page) -> None:
    _log("Google: 2FA — выбираем «Создайте код в Google Authenticator»…")
    clicked = False
    last_err = ""
    for scope in _google_auth_scopes(page):
        btn = _google_authenticator_challenge_locator(scope)
        try:
            if btn.count() == 0:
                continue
            target = btn.first
            target.wait_for(state="visible", timeout=20_000)
            try:
                target.scroll_into_view_if_needed(timeout=5_000)
            except Exception:
                pass
            try:
                target.click(timeout=30_000)
            except Exception as e:
                last_err = repr(e)
                target.click(timeout=30_000, force=True)
            clicked = True
            break
        except Exception as e:
            last_err = repr(e)
            continue
    if not clicked:
        raise RuntimeError(
            "Google: пункт «Google Authenticator» на экране 2FA не найден или недоступен. "
            f"URL={page.url!r}. {last_err}"
        )
    page.wait_for_timeout(900)
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if _totp_step_visible(page):
            return
        page.wait_for_timeout(250)


def _passkey_enrollment_visible(page) -> bool:
    """Предложение создать ключ доступа: «Входите в аккаунт быстрее»."""
    url = _page_url_lower(page)
    if "passkeyenrollment" in url or ("speedbump" in url and "passkey" in url):
        return True
    try:
        if page.locator('figure[data-illustration="passkeyEnrollment"]').first.is_visible(
            timeout=400
        ):
            return True
    except Exception:
        pass
    try:
        h = page.locator("#headingText").first
        if h.is_visible(timeout=400):
            if _PASSKEY_ENROLLMENT_HEADING_RE.search(
                (h.inner_text(timeout=500) or "").strip()
            ):
                return True
    except Exception:
        pass
    try:
        skip = page.locator('div[jsname="eBSUOb"] button').first
        if skip.is_visible(timeout=400):
            label = (skip.inner_text(timeout=500) or "").strip()
            if _NOT_NOW_BTN_RE.search(label):
                return True
    except Exception:
        pass
    return False


def _click_passkey_enrollment_not_now(page) -> None:
    _log("Google: окно ключа доступа — нажимаем «Не сейчас»…")
    skip_btn = (
        page.locator('div[jsname="eBSUOb"] button[jsname="LgbsSe"]')
        .or_(page.locator("#eBSUOb button"))
        .or_(page.locator('div.JYXaTc[data-secondary-action-label] button').nth(1))
        .or_(page.get_by_role("button", name=_NOT_NOW_BTN_RE))
    )
    skip_btn.first.wait_for(state="visible", timeout=20_000)
    skip_btn.first.click(timeout=30_000)
    page.wait_for_timeout(900)


def _selfie_enrollment_visible(page) -> bool:
    """Предложение добавить видеоселфи: «Добавьте селфи для входа в аккаунт»."""
    url = _page_url_lower(page)
    if "video-verification" in url or "video_verification" in url:
        return True
    for scope in _google_auth_scopes(page):
        try:
            h = scope.locator("h1.SgEu9c, h1#headingText, #headingText").first
            if h.count() > 0 and h.is_visible(timeout=400):
                if _SELFIE_ENROLLMENT_HEADING_RE.search(
                    (h.inner_text(timeout=500) or "").strip()
                ):
                    return True
        except Exception:
            pass
        try:
            if scope.get_by_text(_SELFIE_ENROLLMENT_HEADING_RE).first.is_visible(
                timeout=400
            ):
                return True
        except Exception:
            pass
        try:
            if scope.locator('img[src*="selfie-scene"]').count() > 0:
                skip = scope.locator('[jsname="gQ2Xie"]').first
                if skip.is_visible(timeout=400):
                    return True
        except Exception:
            pass
    return False


def _click_selfie_enrollment_not_now(page) -> None:
    _log("Google: окно видеоселфи — нажимаем «Не сейчас»…")
    skip_btn = (
        page.locator('[jsname="gQ2Xie"]')
        .or_(page.locator('[jsname="gQ2Xie"] a[aria-label]'))
        .or_(page.locator('a[aria-label="Не сейчас"]'))
        .or_(page.locator('a[aria-label="Not now"]'))
        .or_(page.get_by_role("button", name=_NOT_NOW_BTN_RE))
        .or_(page.get_by_role("link", name=_NOT_NOW_BTN_RE))
    )
    skip_btn.first.wait_for(state="visible", timeout=20_000)
    try:
        skip_btn.first.click(timeout=30_000)
    except Exception:
        skip_btn.first.click(timeout=30_000, force=True)
    page.wait_for_timeout(900)


def _recovery_info_save_locator(scope):
    return (
        scope.locator('[jsname="M2UYVd"]')
        .or_(scope.locator('button[aria-label="Сохранить"]'))
        .or_(scope.locator('button[aria-label="Save"]'))
        .or_(scope.get_by_role("button", name=_SAVE_BTN_RE))
    )


def _recovery_info_step_visible(page) -> bool:
    """Экран «Убедитесь, что вы всегда сможете войти» — запрос телефона для восстановления."""
    for scope in _google_auth_scopes(page):
        has_heading = False
        has_phone = False
        try:
            h = scope.locator(".RY3zi, [role='heading'][aria-level='1']").first
            if h.count() > 0 and h.is_visible(timeout=300):
                txt = (h.inner_text(timeout=500) or "").strip()
                if _RECOVERY_INFO_HEADING_RE.search(txt):
                    has_heading = True
        except Exception:
            pass
        if not has_heading:
            try:
                if scope.get_by_text(_RECOVERY_INFO_HEADING_RE).first.is_visible(timeout=300):
                    has_heading = True
            except Exception:
                pass
        try:
            phone_h = scope.locator(".Fo3vmc[role='heading'], div.Fo3vmc").filter(
                has_text=_RECOVERY_PHONE_HEADING_RE
            )
            if phone_h.count() > 0 and phone_h.first.is_visible(timeout=300):
                has_phone = True
        except Exception:
            pass
        if not has_phone:
            try:
                if scope.get_by_text(_RECOVERY_PHONE_HEADING_RE).first.is_visible(timeout=300):
                    has_phone = True
            except Exception:
                pass
        if not (has_heading or has_phone):
            continue
        try:
            save = _recovery_info_save_locator(scope)
            if save.count() > 0 and save.first.is_visible(timeout=300):
                return True
        except Exception:
            pass
    return False


def _click_recovery_info_save_js(scope) -> bool:
    try:
        return bool(
            scope.evaluate(
                """() => {
                    const labels = ['Сохранить', 'Save'];
                    const tryClick = (root) => {
                        if (!root) return false;
                        const direct = root.querySelector('[jsname="M2UYVd"]');
                        if (direct) {
                            direct.click();
                            return true;
                        }
                        for (const el of root.querySelectorAll('button')) {
                            const t = (el.innerText || el.textContent || '').trim();
                            const aria = (el.getAttribute('aria-label') || '').trim();
                            if (labels.some((x) => t === x || aria === x)) {
                                el.click();
                                return true;
                            }
                        }
                        for (const host of root.querySelectorAll('*')) {
                            if (host.shadowRoot && tryClick(host.shadowRoot)) {
                                return true;
                            }
                        }
                        return false;
                    };
                    return tryClick(document);
                }"""
            )
        )
    except Exception:
        return False


def _click_recovery_info_save(page) -> None:
    _log("Google: восстановление доступа — нажимаем «Сохранить»…")
    clicked = False
    last_err = ""
    for scope in _google_auth_scopes(page):
        btn = _recovery_info_save_locator(scope)
        try:
            if btn.count() == 0:
                continue
            target = btn.first
            target.wait_for(state="visible", timeout=20_000)
            try:
                target.scroll_into_view_if_needed(timeout=5_000)
            except Exception:
                pass
            try:
                target.click(timeout=30_000)
            except Exception as e:
                last_err = repr(e)
                target.click(timeout=30_000, force=True)
            clicked = True
            break
        except Exception as e:
            last_err = repr(e)
            continue
    if not clicked:
        for scope in _google_auth_scopes(page):
            if _click_recovery_info_save_js(scope):
                clicked = True
                _log("Google: клик «Сохранить» через JS.")
                break
    if not clicked:
        raise RuntimeError(
            "Google: кнопка «Сохранить» на экране восстановления доступа не найдена. "
            f"URL={page.url!r}. {last_err}"
        )
    page.wait_for_timeout(900)


def _home_address_skip_locator(scope):
    return (
        scope.locator('button[jsname="ZUkOIc"][aria-label="Skip"]')
        .or_(scope.locator('button[jsname="ZUkOIc"][aria-label="Пропустить"]'))
        .or_(scope.get_by_role("button", name=_SKIP_BTN_RE))
    )


def _home_address_step_visible_in_scope(scope) -> bool:
    has_heading = False
    try:
        h = scope.locator(".RY3zi, [role='heading'][aria-level='1']").first
        if h.count() > 0 and h.is_visible(timeout=300):
            txt = (h.inner_text(timeout=500) or "").strip()
            if _HOME_ADDRESS_HEADING_RE.search(txt):
                has_heading = True
    except Exception:
        pass
    if not has_heading:
        try:
            if scope.get_by_text(_HOME_ADDRESS_HEADING_RE).first.is_visible(timeout=300):
                has_heading = True
        except Exception:
            pass
    if not has_heading:
        return False
    try:
        skip = _home_address_skip_locator(scope).first
        if skip.count() > 0 and skip.is_visible(timeout=300):
            return True
    except Exception:
        pass
    return False


def _home_address_form_scope(page):
    for scope in _google_auth_scopes(page):
        if _home_address_step_visible_in_scope(scope):
            return scope
    return page


def _home_address_step_visible(page) -> bool:
    """Экран «Set a home address» — предложение указать домашний адрес."""
    for scope in _google_auth_scopes(page):
        if _home_address_step_visible_in_scope(scope):
            return True
    return False


def _click_home_address_skip_js(scope) -> bool:
    try:
        return bool(
            scope.evaluate(
                """() => {
                    const labels = ['Skip', 'Пропустить'];
                    const tryClick = (root) => {
                        if (!root) return false;
                        for (const el of root.querySelectorAll('button[jsname="ZUkOIc"]')) {
                            const t = (el.innerText || el.textContent || '').trim();
                            const aria = (el.getAttribute('aria-label') || '').trim();
                            if (labels.some((x) => t === x || aria === x)) {
                                el.click();
                                return true;
                            }
                        }
                        for (const host of root.querySelectorAll('*')) {
                            if (host.shadowRoot && tryClick(host.shadowRoot)) {
                                return true;
                            }
                        }
                        return false;
                    };
                    return tryClick(document);
                }"""
            )
        )
    except Exception:
        return False


def _click_home_address_skip(page) -> None:
    _log("Google: домашний адрес — нажимаем «Пропустить»…")
    clicked = False
    last_err = ""
    for scope in _google_auth_scopes(page):
        btn = _home_address_skip_locator(scope)
        try:
            if btn.count() == 0:
                continue
            target = btn.first
            target.wait_for(state="visible", timeout=20_000)
            try:
                target.scroll_into_view_if_needed(timeout=5_000)
            except Exception:
                pass
            try:
                target.click(timeout=30_000)
            except Exception as e:
                last_err = repr(e)
                target.click(timeout=30_000, force=True)
            clicked = True
            break
        except Exception as e:
            last_err = repr(e)
            continue
    if not clicked:
        for scope in _google_auth_scopes(page):
            if _click_home_address_skip_js(scope):
                clicked = True
                _log("Google: клик «Пропустить» через JS.")
                break
    if not clicked:
        raise RuntimeError(
            "Google: кнопка «Пропустить» на экране домашнего адреса не найдена. "
            f"URL={page.url!r}. {last_err}"
        )
    page.wait_for_timeout(900)


def _birthday_step_visible_in_scope(scope) -> bool:
    try:
        h = scope.locator("h1.qQnGVb").first
        if h.count() > 0 and h.is_visible(timeout=300):
            txt = (h.inner_text(timeout=500) or "").strip()
            if _BIRTHDAY_HEADING_RE.search(txt):
                return True
    except Exception:
        pass
    try:
        if scope.get_by_text(_BIRTHDAY_HEADING_RE).first.is_visible(timeout=300):
            return True
    except Exception:
        pass
    try:
        day = _birthday_day_input(scope)
        year = _birthday_year_input(scope)
        if day.count() > 0 and year.count() > 0:
            if day.is_visible(timeout=300) and year.is_visible(timeout=300):
                return True
    except Exception:
        pass
    return False


def _birthday_form_scope(page):
    for scope in _google_auth_scopes(page):
        if _birthday_step_visible_in_scope(scope):
            return scope
    return page


def _birthday_confirm_step_visible_in_scope(scope) -> bool:
    try:
        dlg = scope.locator('[role="dialog"][aria-modal="true"]').filter(
            has=scope.locator("h2").filter(has_text=_BIRTHDAY_CONFIRM_HEADING_RE)
        )
        if dlg.count() > 0 and dlg.first.is_visible(timeout=300):
            return True
    except Exception:
        pass
    try:
        h = scope.locator("h2.VfPpkd-k2Wrsb, h2").filter(
            has_text=_BIRTHDAY_CONFIRM_HEADING_RE
        )
        if h.count() > 0 and h.first.is_visible(timeout=300):
            return True
    except Exception:
        pass
    try:
        if scope.get_by_text(_BIRTHDAY_CONFIRM_HEADING_RE).first.is_visible(timeout=300):
            return True
    except Exception:
        pass
    return False


def _birthday_confirm_form_scope(page):
    for scope in _google_auth_scopes(page):
        if _birthday_confirm_step_visible_in_scope(scope):
            return scope
    return page


def _birthday_confirm_step_visible(page) -> bool:
    """Модальное окно «Confirm birthday» после ввода даты рождения."""
    for scope in _google_auth_scopes(page):
        if _birthday_confirm_step_visible_in_scope(scope):
            return True
    return False


def _click_birthday_confirm(page) -> None:
    _log("Google: подтверждение даты рождения — «Confirm»…")
    scope = _birthday_confirm_form_scope(page)
    confirm_btn = (
        scope.locator('button[data-mdc-dialog-action="ok"]')
        .or_(scope.get_by_role("button", name=_CONFIRM_BTN_RE))
    ).first
    confirm_btn.wait_for(state="visible", timeout=10_000)
    confirm_btn.click(timeout=30_000)
    page.wait_for_timeout(900)


def _birthday_success_done_locator(scope):
    return (
        scope.locator('div.iSxK8e button[jsname="AHldd"]')
        .or_(scope.locator('button[jsname="AHldd"]:has(span.VfPpkd-vQzf8d)'))
        .or_(scope.locator('button[jsname="AHldd"]'))
        .or_(scope.get_by_role("button", name=_DONE_BTN_RE))
    )


def _birthday_success_step_visible_in_scope(scope) -> bool:
    try:
        done_btn = _birthday_success_done_locator(scope).first
        if done_btn.count() == 0 or not done_btn.is_visible(timeout=300):
            return False
    except Exception:
        return False
    try:
        h = scope.locator("h1.qQnGVb").first
        if h.count() > 0 and h.is_visible(timeout=300):
            txt = (h.inner_text(timeout=500) or "").strip()
            if _BIRTHDAY_SUCCESS_HEADING_RE.search(txt):
                return True
    except Exception:
        pass
    try:
        if scope.locator('img[src*="add-birthday-success"]').first.is_visible(timeout=300):
            return True
    except Exception:
        pass
    try:
        if scope.get_by_text(_BIRTHDAY_SUCCESS_BODY_RE).first.is_visible(timeout=300):
            return True
    except Exception:
        pass
    return False


def _birthday_success_form_scope(page):
    for scope in _google_auth_scopes(page):
        if _birthday_success_step_visible_in_scope(scope):
            return scope
    return page


def _birthday_success_step_visible(page) -> bool:
    """Экран «Thank you» после сохранения даты рождения."""
    for scope in _google_auth_scopes(page):
        if _birthday_success_step_visible_in_scope(scope):
            return True
    return False


def _click_birthday_success_done_js(scope) -> bool:
    try:
        return bool(
            scope.evaluate(
                """() => {
                    const labels = ['Done', 'Готово'];
                    const tryClick = (root) => {
                        if (!root) return false;
                        for (const el of root.querySelectorAll('button[jsname="AHldd"]')) {
                            const span = el.querySelector('span.VfPpkd-vQzf8d');
                            const t = ((span && span.innerText) || el.innerText || el.textContent || '').trim();
                            if (labels.some((x) => t === x)) {
                                el.click();
                                return true;
                            }
                        }
                        for (const host of root.querySelectorAll('*')) {
                            if (host.shadowRoot && tryClick(host.shadowRoot)) {
                                return true;
                            }
                        }
                        return false;
                    };
                    return tryClick(document);
                }"""
            )
        )
    except Exception:
        return False


def _click_birthday_success_done(page) -> None:
    _log("Google: дата рождения сохранена — «Done» / «Готово»…")
    clicked = False
    last_err = ""
    for scope in _google_auth_scopes(page):
        btn = _birthday_success_done_locator(scope)
        try:
            if btn.count() == 0:
                continue
            target = btn.first
            target.wait_for(state="visible", timeout=20_000)
            try:
                target.scroll_into_view_if_needed(timeout=5_000)
            except Exception:
                pass
            try:
                target.click(timeout=30_000)
            except Exception as e:
                last_err = repr(e)
                target.click(timeout=30_000, force=True)
            clicked = True
            break
        except Exception as e:
            last_err = repr(e)
            continue
    if not clicked:
        for scope in _google_auth_scopes(page):
            if _click_birthday_success_done_js(scope):
                clicked = True
                _log("Google: клик «Done» через JS.")
                break
    if not clicked:
        raise RuntimeError(
            "Google: кнопка «Done» после сохранения даты рождения не найдена. "
            f"URL={page.url!r}. {last_err}"
        )
    page.wait_for_timeout(900)


def _birthday_step_visible(page) -> bool:
    """Экран «Add your birthday» — запрос даты рождения."""
    for scope in _google_auth_scopes(page):
        if _birthday_step_visible_in_scope(scope):
            return True
    return False


def _fill_birthday_and_save(page) -> None:
    day, month, year = _random_birthday()
    _log(
        f"Google: дата рождения — {day:02d}."
        f"{month:02d}.{year} и «Сохранить»…"
    )
    scope = _birthday_form_scope(page)

    month_combo = _birthday_month_combo(scope)
    month_combo.wait_for(state="visible", timeout=20_000)
    month_combo.click(timeout=30_000)
    page.wait_for_timeout(450)

    month_name_en = calendar.month_name[month]
    month_name_ru = _BIRTHDAY_MONTH_NAMES_RU[month]
    month_option = (
        scope.locator(f'li[role="option"][data-value="{month}"]')
        .or_(
            scope.get_by_role(
                "option", name=re.compile(rf"^{re.escape(month_name_en)}$", re.I)
            )
        )
        .or_(
            scope.get_by_role(
                "option", name=re.compile(rf"^{re.escape(month_name_ru)}$", re.I)
            )
        )
    ).first
    month_option.wait_for(state="visible", timeout=10_000)
    month_option.click(timeout=30_000)
    page.wait_for_timeout(350)

    day_input = _birthday_day_input(scope)
    day_input.wait_for(state="visible", timeout=10_000)
    day_input.fill(str(day))

    year_input = _birthday_year_input(scope)
    year_input.wait_for(state="visible", timeout=10_000)
    year_input.fill(str(year))
    page.wait_for_timeout(350)

    save_btn = (
        scope.locator('[jsname="x8hlje"]')
        .or_(scope.get_by_role("button", name=_SAVE_BTN_RE))
    ).first
    save_btn.wait_for(state="visible", timeout=10_000)
    save_btn.click(timeout=30_000)
    page.wait_for_timeout(900)
    for _ in range(40):
        if _birthday_confirm_step_visible(page):
            _click_birthday_confirm(page)
            break
        page.wait_for_timeout(250)
    for _ in range(40):
        if _birthday_success_step_visible(page):
            _click_birthday_success_done(page)
            return
        page.wait_for_timeout(250)


def _channel_switcher_root(page):
    return page.locator("ytd-channel-switcher-renderer").or_(
        page.locator("ytd-multi-page-menu-guide-section-renderer ytd-channel-switcher")
    )


def _channel_switcher_visible(page) -> bool:
    try:
        root = _channel_switcher_root(page)
        if root.count() > 0 and root.first.is_visible(timeout=500):
            return True
    except Exception:
        pass
    try:
        title = page.locator("ytd-simple-menu-header-renderer yt-formatted-string").filter(
            has_text=_CHANNEL_PICKER_TITLE_RE
        )
        if title.count() > 0 and title.first.is_visible(timeout=500):
            return True
    except Exception:
        pass
    try:
        items = page.locator("ytd-channel-switcher-renderer ytd-account-item-renderer")
        if items.count() > 0 and items.first.is_visible(timeout=500):
            return True
    except Exception:
        pass
    return False


def handle_channel_switcher_if_present(page) -> bool:
    """Выбор канала YouTube (подписчики / последний в списке)."""
    if not _channel_switcher_visible(page):
        return False
    _handle_channel_switcher(page)
    return True


def _google_next_button_locator(page, *, include_totp: bool = True):
    loc = (
        page.locator("#identifierNext button")
        .or_(page.locator("#passwordNext button"))
        .or_(
            page.locator('div[jsname="Njthtb"] button[jsname="LgbsSe"]')
        )
        .or_(page.get_by_role("button", name=_NEXT_BTN_RE))
    )
    if include_totp:
        loc = loc.or_(page.locator("#totpNext button"))
    return loc


def _click_google_next(page, *, include_totp: bool = True) -> None:
    btn = _google_next_button_locator(page, include_totp=include_totp)
    btn.first.wait_for(state="visible", timeout=60_000)
    try:
        btn.first.click(timeout=15_000)
    except Exception:
        btn.first.click(timeout=15_000, force=True)
    page.wait_for_timeout(1000)


def _click_identity_confirm_next(page) -> None:
    _click_google_next(page, include_totp=False)


def _wait_after_identifier_submit(page, *, timeout_s: float = 30.0) -> None:
    """Ждём переход со шага email на пароль / подтверждение личности."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _password_step_visible(page) or _identity_confirm_visible(page):
            return
        try:
            if not _identifier_field_locator(page).first.is_visible(timeout=200):
                return
        except Exception:
            return
        page.wait_for_timeout(250)


def _fill_input_and_click_next(
    page, locator, value: str, *, use_totp_next: bool = False
) -> None:
    field = locator.first
    field.wait_for(state="visible", timeout=20_000)
    field.fill(value, timeout=15_000)
    page.wait_for_timeout(200)
    try:
        field.press("Enter")
        page.wait_for_timeout(800)
    except Exception:
        _click_google_next(page, include_totp=use_totp_next)
        return
    try:
        if field.is_visible(timeout=400):
            _click_google_next(page, include_totp=use_totp_next)
    except Exception:
        pass


def _fill_identifier_and_continue(page, email: str) -> None:
    field = _identifier_field_locator(page).first
    field.wait_for(state="visible", timeout=20_000)
    field.fill(email, timeout=15_000)
    page.wait_for_timeout(200)
    try:
        field.press("Enter")
    except Exception:
        _click_google_next(page)
    else:
        page.wait_for_timeout(800)
        try:
            if field.is_visible(timeout=400):
                _click_google_next(page)
        except Exception:
            pass
    _wait_after_identifier_submit(page)


def _generate_totp_code(twofa_secret: str) -> str:
    secret = (twofa_secret or "").strip()
    if not secret:
        raise RuntimeError("YouTube/Google 2FA: не задан yt_2fa в данных учётки профиля.")
    otp = get_totp_token(secret)
    _log("Google 2FA: сгенерирован OTP локально.")
    return otp


def _normalize_count_text(text: str) -> str:
    t = (text or "").replace("\u00a0", " ").replace("\u202f", " ")
    return re.sub(r"\s+", " ", t).strip()


def _parse_count_number_token(raw: str) -> float | None:
    """«1,234» / «1.234» / «1,2» → float."""
    s = re.sub(r"\s+", "", (raw or "").strip())
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        left, _, right = s.partition(",")
        if right.isdigit() and len(right) == 3 and len(left) <= 3:
            s = left + right
        else:
            s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _apply_scale(value: float, scale: str) -> int:
    key = (scale or "").lower().rstrip(".")
    mult = _SCALE_MULTIPLIERS.get(key, 1.0)
    return max(0, int(round(value * mult)))


def _parse_subscriber_count(text: str) -> int:
    """
    Подписчики из подписи канала в switcher (EN/RU, K/M/тыс/млн).
    Возвращает -1, если в строке нет явного счётчика подписчиков.
    """
    t = _normalize_count_text(text)
    if not t:
        return -1
    if _NO_SUBSCRIBERS_RE.search(t):
        return 0
    if not _SUBSCRIBER_WORD_RE.search(t):
        return -1

    best: float | None = None
    for pat in (_SUBSCRIBER_SCALED_RE, _SUBSCRIBER_PLAIN_RE):
        for m in pat.finditer(t):
            num = _parse_count_number_token(m.group("num"))
            if num is None:
                continue
            scale = m.groupdict().get("scale")
            if scale:
                val = float(_apply_scale(num, scale))
            else:
                val = num
            if best is None or val > best:
                best = val

    if best is None:
        return -1
    return int(best)


def _item_channel_name(item) -> str:
    for sel in (
        "yt-formatted-string:not([secondary])",
        "#primary-text yt-formatted-string",
        "yt-formatted-string#channel-title",
    ):
        loc = item.locator(sel)
        try:
            if loc.count() > 0:
                name = (loc.first.inner_text(timeout=2_000) or "").strip()
                if name:
                    return name
        except Exception:
            continue
    try:
        lines = [
            ln.strip()
            for ln in (item.inner_text(timeout=2_000) or "").splitlines()
            if ln.strip()
        ]
        if lines:
            return lines[0]
    except Exception:
        pass
    return ""


def _item_subscriber_label(item) -> str:
    """Только строка с подписчиками, без имени канала."""
    for sel in (
        "yt-formatted-string[secondary]",
        "tp-yt-paper-item-body yt-formatted-string[secondary]",
    ):
        loc = item.locator(sel)
        try:
            if loc.count() > 0:
                label = (loc.first.inner_text(timeout=2_000) or "").strip()
                if label:
                    return label
        except Exception:
            continue
    try:
        body_lines = item.locator("tp-yt-paper-item-body yt-formatted-string")
        if body_lines.count() >= 2:
            label = (body_lines.last.inner_text(timeout=2_000) or "").strip()
            if label and _SUBSCRIBER_WORD_RE.search(label):
                return label
    except Exception:
        pass
    try:
        aria = (
            item.locator("tp-yt-paper-icon-item").first.get_attribute("aria-label") or ""
        ).strip()
        if aria and _SUBSCRIBER_WORD_RE.search(aria):
            return aria
    except Exception:
        pass
    return ""


def _collect_channel_switcher_options(page, root) -> list[tuple[int, int, str, str]]:
    """
    Список каналов: (индекс, подписчики или -1, имя, подпись для лога).
    """
    items = root.locator("ytd-account-item-renderer")
    count = items.count()
    options: list[tuple[int, int, str, str]] = []
    for i in range(count):
        item = items.nth(i)
        name = _item_channel_name(item)
        sub_label = _item_subscriber_label(item)
        score = _parse_subscriber_count(sub_label)
        options.append((i, score, name or f"#{i + 1}", sub_label or "—"))
    return options


def _pick_channel_index(options: list[tuple[int, int, str, str]]) -> int:
    """Максимум подписчиков; при равенстве — нижний (последний) в списке."""
    if not options:
        return 0
    known = [o for o in options if o[1] >= 0]
    if known:
        # (подписчики, индекс): при одинаковом числе побеждает больший индекс = ниже в списке
        best = max(known, key=lambda o: (o[1], o[0]))
        return best[0]
    return options[-1][0]


def _handle_channel_switcher(page) -> None:
    _log("YouTube: окно выбора канала — «Не спрашивать» и выбор канала…")
    root = _channel_switcher_root(page)
    root.first.wait_for(state="visible", timeout=30_000)
    page.wait_for_timeout(400)

    checkbox = (
        root.locator("tp-yt-paper-checkbox").filter(has_text=_DONT_ASK_RE)
        .or_(root.locator("yt-checkbox-renderer").filter(has_text=_DONT_ASK_RE))
        .or_(root.get_by_text(_DONT_ASK_RE))
    )
    try:
        if checkbox.count() > 0 and checkbox.first.is_visible(timeout=3_000):
            box = checkbox.first
            try:
                checked = (box.get_attribute("aria-checked") or "").lower() == "true"
            except Exception:
                checked = False
            if not checked:
                box.click(timeout=10_000)
                page.wait_for_timeout(300)
                _log("YouTube: отмечен «Не спрашивать на этом устройстве».")
    except Exception as e:
        _log(f"YouTube: галочка «Не спрашивать» не отмечена: {e!r}")

    options = _collect_channel_switcher_options(page, root)
    if not options:
        raise RuntimeError(
            "YouTube: в окне выбора канала нет пунктов (ytd-account-item-renderer)."
        )

    for idx, score, name, sub in options:
        subs = str(score) if score >= 0 else "неизвестно"
        _log(f"YouTube: канал [{idx + 1}] «{name}» — подписчики: {sub} ({subs})")

    pick_idx = _pick_channel_index(options)
    _, best_score, name, sub = options[pick_idx]
    pick = root.locator("ytd-account-item-renderer").nth(pick_idx)
    _log(
        f"YouTube: выбираем канал «{name}» "
        f"(подписчики≈{best_score if best_score >= 0 else sub or 'неизвестно'}, "
        f"позиция {pick_idx + 1}/{len(options)})."
    )

    clicked = False
    for sel in (
        pick.locator("tp-yt-paper-icon-item"),
        pick.locator("tp-yt-paper-item"),
        pick.locator('[role="option"]'),
        pick,
    ):
        try:
            target = sel.first
            target.wait_for(state="visible", timeout=5_000)
            target.click(timeout=30_000)
            clicked = True
            break
        except Exception:
            continue
    if not clicked:
        raise RuntimeError(f"YouTube: не удалось кликнуть канал «{name}».")

    page.wait_for_timeout(1_500)

    from zaliver.youtube_upload.studio import _studio_handle_channel_creation_after_account_pick

    _studio_handle_channel_creation_after_account_pick(page)

    try:
        root.first.wait_for(state="hidden", timeout=60_000)
    except Exception:
        deadline = time.monotonic() + 45.0
        while time.monotonic() < deadline:
            if not _channel_switcher_visible(page):
                break
            time.sleep(0.4)
        else:
            raise RuntimeError(
                "YouTube: окно выбора канала не закрылось после выбора."
            )
    _log("YouTube: окно выбора канала закрыто.")


def _wait_for_channel_switcher_after_auth(page, *, max_seconds: float = 35.0) -> None:
    """
    После пароля / 2FA / passkey диалог каналов часто появляется с задержкой.
    """
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        if _channel_switcher_visible(page):
            _handle_channel_switcher(page)
            return
        time.sleep(0.5)


def attempt_google_login_for_studio(
    page,
    credentials: GoogleLoginCredentials | None,
    *,
    max_seconds: float = _GOOGLE_LOGIN_MAX_S,
    handle_channel_switcher: bool = True,
) -> bool:
    """
    Пройти цепочку Google: email → личность → пароль → ключ доступа (пропуск) →
    видеоселфи (пропуск) → выбор 2FA (Authenticator) → 2FA → (опционально) канал.
    Возвращает True, если интерактивный вход больше не нужен.

    ``handle_channel_switcher=False`` — для Gmail/Instagram: без выбора канала YouTube.
    """
    if credentials is None:
        return False

    studio_login_required = None
    if handle_channel_switcher:
        from zaliver.youtube_upload.studio import _studio_login_required as studio_login_required

    def _still_needs_login() -> bool:
        if google_auth_interaction_visible(page):
            return True
        if "accounts.google.com" in _page_url_lower(page):
            return True
        if handle_channel_switcher and studio_login_required is not None:
            try:
                return bool(studio_login_required(page))
            except Exception:
                return False
        return False

    _log("Google: обнаружен вход — пробуем автоматический сценарий…")
    _log(f"Google: URL при старте входа: {page.url!r}")
    deadline = time.monotonic() + max_seconds
    steps = 0
    idle_rounds = 0

    while time.monotonic() < deadline:
        if handle_channel_switcher and _channel_switcher_visible(page):
            steps += 1
            _log(f"YouTube: выбор канала (шаг {steps})…")
            _handle_channel_switcher(page)
            continue

        if (
            not _identifier_step_visible(page)
            and not _identity_confirm_visible(page)
            and not _password_step_visible(page)
            and not _passkey_enrollment_visible(page)
            and not _selfie_enrollment_visible(page)
            and not _recovery_info_step_visible(page)
            and not _home_address_step_visible(page)
            and not _birthday_confirm_step_visible(page)
            and not _birthday_success_step_visible(page)
            and not _birthday_step_visible(page)
            and not _2fa_challenge_picker_visible(page)
            and not _totp_step_visible(page)
            and not _account_chooser_step_visible(page)
            and not google_auth_interaction_visible(page)
            and not (
                handle_channel_switcher
                and studio_login_required is not None
                and studio_login_required(page)
            )
        ):
            if handle_channel_switcher:
                _wait_for_channel_switcher_after_auth(page)
                if not _channel_switcher_visible(page) and not (
                    studio_login_required is not None and studio_login_required(page)
                ):
                    _log("Google/YouTube: вход завершён.")
                    return True
                continue
            _log("Google: вход завершён (без выбора канала YouTube).")
            return True

        if _account_chooser_step_visible(page):
            steps += 1
            idle_rounds = 0
            _log(f"Google: выбор аккаунта — «Использовать другой аккаунт» (шаг {steps})…")
            _click_use_another_account(page)
            continue

        if _2fa_challenge_picker_visible(page):
            steps += 1
            idle_rounds = 0
            _log(f"Google: выбор способа 2FA — Google Authenticator (шаг {steps})…")
            _click_google_authenticator_challenge(page)
            continue

        if _totp_step_visible(page):
            token = (credentials.twofa_token or "").strip()
            if not token:
                raise GoogleLoginCredentialsMissingError(
                    "YouTube/Google: требуется 2FA, но yt_2fa не задан в данных учётки профиля."
                )
            steps += 1
            otp = _generate_totp_code(token)
            _log(f"Google: ввод кода 2FA и «Далее» (шаг {steps})…")
            totp_field = None
            for scope in _google_auth_scopes(page):
                loc = _totp_input_locator(scope)
                try:
                    if loc.count() > 0 and loc.first.is_visible(timeout=400):
                        totp_field = loc
                        break
                except Exception:
                    pass
            if totp_field is None:
                totp_field = page.locator('input[name="totpPin"], #totpPin')
            _fill_input_and_click_next(
                page,
                totp_field,
                otp,
                use_totp_next=True,
            )
            continue

        if _identity_confirm_visible(page):
            steps += 1
            _log(f"Google: «Подтвердите личность» — «Далее» (шаг {steps})…")
            _click_identity_confirm_next(page)
            continue

        if _password_step_visible(page):
            pwd = (credentials.password or "").strip()
            if not pwd:
                raise GoogleLoginPasswordMissingError(
                    "YouTube Studio: для входа нужен пароль (yt_password) в данных учётки "
                    "локального профиля — пароль не задан."
                )
            steps += 1
            _log(f"Google: ввод пароля и «Далее» (шаг {steps})…")
            _fill_input_and_click_next(page, page.locator('input[name="Passwd"]'), pwd)
            continue

        if _identifier_step_visible(page):
            email = (credentials.email or "").strip()
            if not email:
                raise GoogleLoginCredentialsMissingError(
                    "YouTube Studio: для входа нужен логин (yt_login) в данных учётки "
                    "локального профиля — email не задан."
                )
            steps += 1
            _log(f"Google: ввод email ({email}) и «Далее» (шаг {steps})…")
            _fill_identifier_and_continue(page, email)
            continue

        if _passkey_enrollment_visible(page):
            steps += 1
            _click_passkey_enrollment_not_now(page)
            continue

        if _selfie_enrollment_visible(page):
            steps += 1
            _click_selfie_enrollment_not_now(page)
            continue

        if _recovery_info_step_visible(page):
            steps += 1
            idle_rounds = 0
            _log(f"Google: восстановление доступа — «Сохранить» (шаг {steps})…")
            _click_recovery_info_save(page)
            continue

        if _home_address_step_visible(page):
            steps += 1
            idle_rounds = 0
            _log(f"Google: домашний адрес — «Пропустить» (шаг {steps})…")
            _click_home_address_skip(page)
            continue

        if _birthday_confirm_step_visible(page):
            steps += 1
            idle_rounds = 0
            _log(f"Google: подтверждение даты рождения (шаг {steps})…")
            _click_birthday_confirm(page)
            continue

        if _birthday_success_step_visible(page):
            steps += 1
            idle_rounds = 0
            _log(f"Google: дата рождения сохранена — «Done» (шаг {steps})…")
            _click_birthday_success_done(page)
            continue

        if _birthday_step_visible(page):
            steps += 1
            idle_rounds = 0
            _log(f"Google: дата рождения (шаг {steps})…")
            _fill_birthday_and_save(page)
            continue

        if _still_needs_login():
            if _try_use_another_account_if_present(page):
                steps += 1
                idle_rounds = 0
                continue

        idle_rounds += 1
        if idle_rounds == 1 or idle_rounds % 10 == 0:
            _log(
                f"Google: ожидание шага входа (URL={page.url!r}, "
                f"chooser={_account_chooser_step_visible(page)}, "
                f"email={_identifier_step_visible(page)}, "
                f"recovery={_recovery_info_step_visible(page)}, "
                f"home_address={_home_address_step_visible(page)}, "
                f"selfie={_selfie_enrollment_visible(page)}, "
                f"birthday_confirm={_birthday_confirm_step_visible(page)}, "
                f"birthday_success={_birthday_success_step_visible(page)}, "
                f"birthday={_birthday_step_visible(page)}, "
                f"2fa_picker={_2fa_challenge_picker_visible(page)}, "
                f"totp={_totp_step_visible(page)})…"
            )
        page.wait_for_timeout(500)

    raise RuntimeError(
        f"YouTube/Google: не завершили вход за {max_seconds:.0f} с "
        f"(выполнено шагов: {steps})."
    )
