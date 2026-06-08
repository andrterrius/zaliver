from __future__ import annotations

import re
import time
from dataclasses import dataclass
from urllib.parse import quote

import requests

YT_LOGIN_KEY = "yt_login"
YT_PASSWORD_KEY = "yt_password"
YT_2FA_KEY = "yt_2fa"

_IDENTITY_HEADING_RE = re.compile(
    r"подтвердите\s+свою\s+личность|confirm\s+your\s+identity",
    re.I,
)
_2FA_HEADING_RE = re.compile(
    r"двухэтапн|two[- ]step|2[- ]step",
    re.I,
)
_PASSKEY_ENROLLMENT_HEADING_RE = re.compile(
    r"входите\s+в\s+аккаунт\s+быстрее|sign\s+in\s+faster|faster\s+sign[- ]in",
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

_GOOGLE_LOGIN_MAX_S = 180.0
_OTP_API_BASE = "https://2fa.fb.tools/api/otp/"


class GoogleLoginPasswordMissingError(RuntimeError):
    """В custom_data профиля нет yt_password — профиль пропускаем, остальные продолжают."""


@dataclass(frozen=True, slots=True)
class GoogleLoginCredentials:
    email: str = ""
    password: str = ""
    twofa_token: str = ""


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


def _log(message: str) -> None:
    from zaliver.youtube_upload import studio as _studio

    _studio._log(message)


def _page_url_lower(page) -> str:
    try:
        return (page.url or "").lower()
    except Exception:
        return ""


def google_auth_interaction_visible(page) -> bool:
    """Один из шагов входа Google / выбора канала YouTube."""
    if "accounts.google.com" in _page_url_lower(page):
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
            if _PASSKEY_ENROLLMENT_HEADING_RE.search(txt):
                return True
    except Exception:
        pass
    if _passkey_enrollment_visible(page):
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


def _identity_confirm_visible(page) -> bool:
    try:
        h = page.locator("#headingText").first
        if h.is_visible(timeout=400):
            if _IDENTITY_HEADING_RE.search((h.inner_text(timeout=500) or "").strip()):
                return True
    except Exception:
        pass
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
    try:
        return page.locator('input[name="totpPin"], #totpPin').first.is_visible(timeout=400)
    except Exception:
        return False


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


def _google_next_button_locator(page):
    return (
        page.locator("#identifierNext button")
        .or_(page.locator("#passwordNext button"))
        .or_(page.locator("#totpNext button"))
        .or_(
            page.locator('div[jsname="Njthtb"] button[jsname="LgbsSe"]')
        )
        .or_(page.get_by_role("button", name=_NEXT_BTN_RE))
    )


def _click_google_next(page) -> None:
    btn = _google_next_button_locator(page)
    btn.first.wait_for(state="visible", timeout=60_000)
    try:
        btn.first.click(timeout=15_000)
    except Exception:
        btn.first.click(timeout=15_000, force=True)
    page.wait_for_timeout(1000)


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


def _fill_input_and_click_next(page, locator, value: str) -> None:
    field = locator.first
    field.wait_for(state="visible", timeout=20_000)
    field.fill(value, timeout=15_000)
    page.wait_for_timeout(200)
    try:
        field.press("Enter")
        page.wait_for_timeout(800)
    except Exception:
        _click_google_next(page)
        return
    try:
        if field.is_visible(timeout=400):
            _click_google_next(page)
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


def _fetch_otp_from_api(twofa_token: str) -> str:
    token = (twofa_token or "").strip()
    if not token:
        raise RuntimeError("YouTube/Google 2FA: не задан yt_2fa в данных учётки профиля.")

    url = _OTP_API_BASE + quote(token, safe="")
    last_err: str = ""
    for attempt in range(12):
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            last_err = repr(e)
            time.sleep(2.0)
            continue

        if not payload.get("ok"):
            last_err = f"API ok=false: {payload!r}"
            time.sleep(2.0)
            continue

        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        otp = str(data.get("otp") or "").strip()
        try:
            remaining = int(data.get("timeRemaining") or 0)
        except (TypeError, ValueError):
            remaining = 0

        if remaining < 5:
            _log(
                f"Google 2FA: OTP expires in {remaining}s — ждём 6 с и повторяем запрос "
                f"(попытка {attempt + 1})…"
            )
            time.sleep(6.0)
            continue

        if otp:
            _log(f"Google 2FA: получен OTP (осталось {remaining} с).")
            return otp

        last_err = f"empty otp in {payload!r}"
        time.sleep(2.0)

    raise RuntimeError(
        f"YouTube/Google 2FA: не удалось получить OTP с 2fa.fb.tools. {last_err}"
    )


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
) -> bool:
    """
    Пройти цепочку Google: email → личность → пароль → ключ доступа (пропуск) → 2FA → канал.
    Возвращает True, если интерактивный вход больше не нужен.
    """
    if credentials is None:
        return False

    from zaliver.youtube_upload.studio import _studio_login_required

    _log("Google/YouTube: обнаружен вход — пробуем автоматический сценарий…")
    deadline = time.monotonic() + max_seconds
    steps = 0

    while time.monotonic() < deadline:
        if _channel_switcher_visible(page):
            steps += 1
            _log(f"YouTube: выбор канала (шаг {steps})…")
            _handle_channel_switcher(page)
            continue

        if (
            not _identifier_step_visible(page)
            and not _identity_confirm_visible(page)
            and not _password_step_visible(page)
            and not _passkey_enrollment_visible(page)
            and not _totp_step_visible(page)
            and not google_auth_interaction_visible(page)
            and not _studio_login_required(page)
        ):
            _wait_for_channel_switcher_after_auth(page)
            if not _channel_switcher_visible(page) and not _studio_login_required(page):
                _log("Google/YouTube: вход завершён.")
                return True
            continue

        if _identity_confirm_visible(page):
            steps += 1
            _log(f"Google: «Подтвердите личность» — «Далее» (шаг {steps})…")
            _click_google_next(page)
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
                raise RuntimeError(
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

        if _totp_step_visible(page):
            token = (credentials.twofa_token or "").strip()
            if not token:
                raise RuntimeError(
                    "YouTube/Google: требуется 2FA, но yt_2fa не задан в данных учётки профиля."
                )
            steps += 1
            otp = _fetch_otp_from_api(token)
            _log(f"Google: ввод кода 2FA и «Далее» (шаг {steps})…")
            _fill_input_and_click_next(
                page,
                page.locator('input[name="totpPin"], #totpPin'),
                otp,
            )
            continue

        page.wait_for_timeout(500)

    raise RuntimeError(
        f"YouTube/Google: не завершили вход за {max_seconds:.0f} с "
        f"(выполнено шагов: {steps})."
    )
