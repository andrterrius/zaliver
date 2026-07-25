"""Редактирование профиля Instagram: bio/фото + смена юзернейма."""

from __future__ import annotations

import random
import re
import time
from pathlib import Path

from zaliver.instagram_upload.instagram_availability import (
    verify_instagram_home_available,
)
from zaliver.instagram_upload.logutil import emit_instagram_log, instagram_entrypoint
from zaliver.instagram_upload.register import (
    accept_instagram_cookie_consent_if_present,
    _navigate_page_to,
)

EDIT_PROFILE_URL = "https://www.instagram.com/accounts/edit/"
LANGUAGE_PREFERENCES_URL = "https://www.instagram.com/language/preferences/"
ACCOUNTS_CENTER_URL = (
    "https://accountscenter.instagram.com/?entry_point=app_settings"
)
_BIO_MAX_LEN = 150
_USERNAME_MAX_LEN = 30
_SUBMIT_DISABLE_WAIT_S = 5.0
_PAGE_READY_TIMEOUT_MS = 60_000
_USERNAME_RETRY_MAX = 8
_RUSSIAN_LANG_RE = re.compile(r"^\s*Русский\s*$")
_YAZYK_RE = re.compile(r"язык", re.IGNORECASE)

_SUBMIT_RE = re.compile(
    r"^\s*(Submit|Отправить|Сохранить|Save)\s*$",
    re.IGNORECASE,
)
_SUBMIT_CONTAINS_RE = re.compile(
    r"(Submit|Отправить|Сохранить|Save)",
    re.IGNORECASE,
)
_CHANGE_PHOTO_RE = re.compile(
    r"Change\s+photo|Change\s+profile\s+photo|"
    r"Add\s+a\s+profile\s+photo|"
    r"Изменить\s+фото|Добавить\s+фото",
    re.IGNORECASE,
)


class InstagramEditProfileError(RuntimeError):
    """Ошибка редактирования профиля Instagram."""


def _log(message: str) -> None:
    emit_instagram_log(message, tag="[ig-edit]")


def _page_url(page) -> str:
    try:
        return (page.url or "").strip()
    except Exception:
        return ""


def _bio_textarea(page):
    return page.locator("textarea#pepBio, textarea[placeholder='Bio']").first


def _file_inputs(page):
    return page.locator('input[type="file"][accept*="image"]')


def _submit_buttons(page):
    """Кнопка Submit на Edit profile (div[role=button], не <button>)."""
    exact = page.locator('[role="button"]').filter(has_text=_SUBMIT_RE)
    try:
        if exact.count() > 0:
            return exact
    except Exception:
        pass
    return page.locator('[role="button"]').filter(has_text=_SUBMIT_CONTAINS_RE)


def _scroll_edit_form_to_submit(page) -> None:
    """Submit внизу длинной формы — без этого локатор часто вне viewport."""
    try:
        page.evaluate(
            """() => {
                const bio = document.querySelector('textarea#pepBio');
                if (bio) bio.scrollIntoView({block: 'center'});
                const scrollables = [
                    document.scrollingElement,
                    document.querySelector('main'),
                    document.querySelector('[role="main"]'),
                ].filter(Boolean);
                for (const el of scrollables) {
                    try { el.scrollTop = el.scrollHeight; } catch (e) {}
                }
                window.scrollTo(0, document.body.scrollHeight || 99999);
            }"""
        )
    except Exception as exc:
        _log(f"Edit profile: scroll к Submit: {exc!r}")
    try:
        page.wait_for_timeout(400)
    except Exception:
        time.sleep(0.4)


def _find_submit_button(page):
    """Найти видимую кнопку Submit без долгого auto-wait scroll_into_view."""
    _scroll_edit_form_to_submit(page)

    candidates = []
    try:
        candidates.append(page.get_by_role("button", name=_SUBMIT_CONTAINS_RE))
    except Exception:
        pass
    try:
        candidates.append(
            page.locator("form").locator('[role="button"]').filter(
                has_text=_SUBMIT_CONTAINS_RE
            )
        )
    except Exception:
        pass
    candidates.append(_submit_buttons(page))
    try:
        candidates.append(page.get_by_text(_SUBMIT_RE))
    except Exception:
        pass

    for loc in candidates:
        try:
            n = loc.count()
        except Exception:
            continue
        for i in range(n - 1, -1, -1):
            btn = loc.nth(i)
            try:
                if btn.is_visible():
                    return btn
            except Exception:
                continue
    return None


def _is_submit_disabled(btn) -> bool:
    """Submit недоступен для нажатия — успех сохранения формы."""
    if btn is None:
        return False
    try:
        aria = (btn.get_attribute("aria-disabled") or "").strip().lower()
        if aria in ("true", "1"):
            return True
    except Exception:
        pass
    try:
        if btn.get_attribute("disabled") is not None:
            return True
    except Exception:
        pass
    try:
        return bool(
            btn.evaluate(
                """(el) => {
                    if (!el) return false;
                    if (el.getAttribute('aria-disabled') === 'true') return true;
                    if (el.hasAttribute('disabled')) return true;
                    const s = getComputedStyle(el);
                    if (s.pointerEvents === 'none') return true;
                    if (s.opacity && parseFloat(s.opacity) < 0.55) return true;
                    if (el.getAttribute('tabindex') === '-1') return true;
                    return false;
                }"""
            )
        )
    except Exception:
        return False


def _wait_edit_profile_ready(page, *, timeout_ms: int = _PAGE_READY_TIMEOUT_MS) -> None:
    deadline = time.monotonic() + max(5.0, timeout_ms / 1000.0)
    last_url = ""
    while time.monotonic() < deadline:
        last_url = _page_url(page)
        try:
            bio = _bio_textarea(page)
            if bio.count() > 0 and bio.is_visible():
                _log(f"Edit profile: форма готова (URL={last_url!r}).")
                return
        except Exception:
            pass
        # Заголовок страницы.
        try:
            heading = page.get_by_role(
                "heading", name=re.compile(r"Edit\s+profile|Редактировать\s+профиль", re.I)
            )
            if heading.count() > 0 and heading.first.is_visible():
                # Ждём bio ещё чуть-чуть.
                try:
                    _bio_textarea(page).wait_for(state="visible", timeout=5_000)
                    return
                except Exception:
                    pass
        except Exception:
            pass
        try:
            page.wait_for_timeout(250)
        except Exception:
            time.sleep(0.25)
    raise InstagramEditProfileError(
        "Не дождались формы Edit profile / Bio "
        f"(URL={last_url!r})."
    )


def _read_bio_value(page) -> str:
    bio = _bio_textarea(page)
    try:
        val = (bio.input_value(timeout=3_000) or "").strip()
        if val:
            return val
    except Exception:
        pass
    try:
        return (bio.evaluate("el => (el && el.value) || ''") or "").strip()
    except Exception:
        return ""


def _fill_bio(page, description: str) -> None:
    text = (description or "").strip()
    if not text:
        return
    if len(text) > _BIO_MAX_LEN:
        _log(
            f"Edit profile: bio обрезано с {len(text)} до {_BIO_MAX_LEN} символов."
        )
        text = text[:_BIO_MAX_LEN]
    bio = _bio_textarea(page)
    bio.wait_for(state="visible", timeout=30_000)
    bio.scroll_into_view_if_needed(timeout=10_000)
    try:
        bio.click(timeout=5_000)
    except Exception:
        pass

    filled = False
    # 1) Обычный fill.
    try:
        bio.fill(text, timeout=15_000)
        filled = True
    except Exception as exc:
        _log(f"Edit profile: bio.fill не удался: {exc!r}")

    # 2) Если React не принял — принудительно через native setter + input/change.
    if not filled or _read_bio_value(page) != text:
        try:
            bio.evaluate(
                """(el, value) => {
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value'
                    )?.set;
                    if (setter) setter.call(el, value);
                    else el.value = value;
                    el.dispatchEvent(new InputEvent('input', {
                        bubbles: true, cancelable: true, data: value, inputType: 'insertText'
                    }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                text,
            )
            filled = True
        except Exception as exc:
            _log(f"Edit profile: bio evaluate-fill: {exc!r}")

    # 3) Последний шанс — посимвольный ввод.
    if _read_bio_value(page) != text:
        try:
            bio.click(timeout=5_000)
            try:
                bio.press("Control+A")
            except Exception:
                pass
            try:
                bio.fill("")
            except Exception:
                pass
            bio.press_sequentially(text, delay=15, timeout=60_000)
            filled = True
        except Exception as exc:
            _log(f"Edit profile: bio press_sequentially: {exc!r}")

    # Ждём, пока значение реально окажется в поле.
    deadline = time.monotonic() + 8.0
    actual = ""
    while time.monotonic() < deadline:
        actual = _read_bio_value(page)
        if actual == text:
            break
        try:
            page.wait_for_timeout(200)
        except Exception:
            time.sleep(0.2)

    if actual != text:
        raise InstagramEditProfileError(
            "Bio не попало в поле перед Submit "
            f"(ожидали {len(text)} симв., в поле {len(actual)})."
        )

    # Даём React/Meta обновить состояние формы (Submit станет активным).
    try:
        bio.evaluate("el => el.blur && el.blur()")
    except Exception:
        pass
    try:
        page.wait_for_timeout(700)
    except Exception:
        time.sleep(0.7)
    _log(f"Edit profile: bio заполнено и проверено ({len(text)} симв.).")


def _wait_submit_enabled_after_edits(page, *, timeout_s: float = 10.0) -> None:
    """После правок Submit должен стать кликабельным; иначе сохранять нечего."""
    deadline = time.monotonic() + max(2.0, float(timeout_s))
    while time.monotonic() < deadline:
        btn = _find_submit_button(page)
        if btn is not None and not _is_submit_disabled(btn):
            _log("Edit profile: Submit активен после правок.")
            return
        try:
            page.wait_for_timeout(250)
        except Exception:
            time.sleep(0.25)
    _log("Edit profile: Submit так и не стал активным — всё равно попробуем клик.")


def _upload_avatar(page, avatar_path: Path) -> None:
    path = avatar_path.resolve()
    if not path.is_file():
        raise InstagramEditProfileError(f"Файл аватарки не найден: {path}")

    inputs = _file_inputs(page)
    transferred = False
    try:
        count = inputs.count()
    except Exception:
        count = 0
    for i in range(max(0, count)):
        inp = inputs.nth(i)
        try:
            inp.set_input_files(str(path), timeout=30_000)
            transferred = True
            _log(f"Edit profile: файл аватарки передан в input[{i}].")
            break
        except Exception as exc:
            _log(f"Edit profile: set_input_files input[{i}] не удался: {exc!r}")

    if not transferred:
        change_btn = page.locator('[role="button"]').filter(
            has_text=_CHANGE_PHOTO_RE
        ).first
        try:
            change_btn.wait_for(state="visible", timeout=10_000)
            change_btn.scroll_into_view_if_needed(timeout=8_000)
            with page.expect_file_chooser(timeout=30_000) as fc_info:
                change_btn.click(timeout=15_000)
            fc_info.value.set_files(str(path))
            transferred = True
            _log("Edit profile: файл аватарки выбран через Change photo.")
        except Exception as exc:
            # Кнопка «Add a profile photo» (img button).
            try:
                add_btn = page.locator('button[title*="profile photo" i]').first
                with page.expect_file_chooser(timeout=20_000) as fc_info:
                    add_btn.click(timeout=10_000)
                fc_info.value.set_files(str(path))
                transferred = True
                _log("Edit profile: файл аватарки выбран через Add a profile photo.")
            except Exception as exc2:
                raise InstagramEditProfileError(
                    "Не удалось передать файл аватарки в Edit profile: "
                    f"{exc!r} / {exc2!r}"
                ) from exc2

    if not transferred:
        raise InstagramEditProfileError(
            "Не удалось передать файл аватарки в Edit profile."
        )

    # Иногда после выбора фото появляется кроп — подтвердим, если есть.
    _dismiss_photo_crop_if_present(page)
    try:
        page.wait_for_timeout(800)
    except Exception:
        time.sleep(0.8)


def _dismiss_photo_crop_if_present(page) -> None:
    """Если Instagram показал редактор фото — Save/Done/Apply."""
    patterns = (
        r"^(Done|Save|Apply|Готово|Сохранить|Применить)$",
    )
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        for pat in patterns:
            try:
                btn = page.get_by_role("button", name=re.compile(pat, re.I)).first
                if btn.count() > 0 and btn.is_visible():
                    # Не путать с основным Submit формы профиля, если кропа нет.
                    # Кроп обычно в диалоге.
                    dialog = page.locator('[role="dialog"]')
                    if dialog.count() > 0:
                        inner = dialog.first.get_by_role(
                            "button", name=re.compile(pat, re.I)
                        ).first
                        if inner.count() > 0 and inner.is_visible():
                            inner.click(timeout=8_000)
                            _log(f"Edit profile: подтвердили кроп фото ({pat!r}).")
                            page.wait_for_timeout(600)
                            return
            except Exception:
                continue
        try:
            page.wait_for_timeout(250)
        except Exception:
            time.sleep(0.25)


def _click_submit(page) -> None:
    btn = _find_submit_button(page)
    if btn is None:
        raise InstagramEditProfileError(
            "Кнопка Submit не найдена на Edit profile "
            f"(URL={_page_url(page)!r})."
        )

    # JS scroll — Playwright scroll_into_view_if_needed часто таймаутится
    # на div[role=button] во вложенном scroll-контейнере.
    try:
        btn.evaluate(
            "el => el.scrollIntoView({block: 'center', inline: 'nearest'})"
        )
    except Exception as exc:
        _log(f"Edit profile: JS scrollIntoView Submit: {exc!r}")
    try:
        page.wait_for_timeout(300)
    except Exception:
        time.sleep(0.3)

    if _is_submit_disabled(btn):
        _log("Edit profile: Submit уже недоступен до клика.")
        return

    _log("Edit profile: нажимаем Submit…")
    try:
        btn.click(timeout=15_000)
        return
    except Exception as exc:
        _log(f"Edit profile: обычный click Submit: {exc!r}")
    try:
        btn.click(timeout=10_000, force=True)
        return
    except Exception as exc2:
        _log(f"Edit profile: force click Submit: {exc2!r}")
    try:
        btn.evaluate("el => el.click()")
    except Exception as exc3:
        raise InstagramEditProfileError(
            f"Не удалось нажать Submit: {exc3!r}"
        ) from exc3


def _wait_submit_disabled_or_fail(page, *, wait_s: float = _SUBMIT_DISABLE_WAIT_S) -> None:
    """После Submit ждём, пока кнопка станет недоступной; иначе ошибка."""
    deadline = time.monotonic() + max(1.0, float(wait_s))
    while time.monotonic() < deadline:
        try:
            btn = _find_submit_button(page)
            if btn is None:
                _log("Edit profile: Submit исчез с страницы — считаем успехом.")
                return
            if _is_submit_disabled(btn):
                _log("Edit profile: Submit недоступен — сохранение успешно.")
                return
        except Exception as exc:
            _log(f"Edit profile: проверка Submit: {exc!r}")
        try:
            page.wait_for_timeout(200)
        except Exception:
            time.sleep(0.2)

    raise InstagramEditProfileError(
        f"После Submit кнопка осталась активной более {wait_s:.0f} с — ошибка сохранения."
    )


def _sanitize_username(raw: str) -> str:
    s = (raw or "").strip().lstrip("@").lower()
    s = re.sub(r"[^a-z0-9._]", "", s)
    s = re.sub(r"\.{2,}", ".", s).strip(".")
    if len(s) > _USERNAME_MAX_LEN:
        s = s[:_USERNAME_MAX_LEN].rstrip(".")
    return s


def _username_candidate(base: str, *, suffix: int | None = None) -> str:
    base = _sanitize_username(base)
    if not base:
        raise InstagramEditProfileError("Пустой юзернейм после нормализации.")
    if suffix is None:
        return base[:_USERNAME_MAX_LEN]
    suf = str(int(suffix))
    room = _USERNAME_MAX_LEN - len(suf)
    if room < 1:
        return suf[:_USERNAME_MAX_LEN]
    stem = base[:room].rstrip("._")
    if not stem:
        stem = "u"
        room = _USERNAME_MAX_LEN - len(suf)
        stem = stem[: max(1, room)]
    return f"{stem}{suf}"[:_USERNAME_MAX_LEN]


def _wait_accounts_center_profiles(page, *, timeout_ms: int = _PAGE_READY_TIMEOUT_MS) -> None:
    deadline = time.monotonic() + max(5.0, timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        try:
            link = page.locator(
                'a[href*="/profiles/"][aria-label*="Instagram"]'
            ).first
            if link.count() > 0 and link.is_visible():
                _log("Accounts Center: список Profiles готов.")
                return
        except Exception:
            pass
        try:
            heading = page.get_by_role(
                "heading",
                name=re.compile(r"Profiles|Профили", re.I),
            )
            if heading.count() > 0 and heading.first.is_visible():
                page.wait_for_timeout(400)
                return
        except Exception:
            pass
        try:
            page.wait_for_timeout(250)
        except Exception:
            time.sleep(0.25)
    raise InstagramEditProfileError(
        "Не дождались списка Profiles в Accounts Center "
        f"(URL={_page_url(page)!r})."
    )


def _click_own_instagram_profile(page) -> None:
    link = page.locator('a[href*="/profiles/"][aria-label*="Instagram"]').first
    # Не кликать ссылки username/manage / photo/manage — только карточка профиля.
    n = 0
    try:
        n = page.locator('a[href*="/profiles/"][aria-label*="Instagram"]').count()
    except Exception:
        n = 0
    chosen = None
    for i in range(max(1, n)):
        cand = page.locator(
            'a[href*="/profiles/"][aria-label*="Instagram"]'
        ).nth(i)
        try:
            href = (cand.get_attribute("href") or "").strip()
        except Exception:
            href = ""
        if "/username/" in href or "/photo/" in href or "/name/" in href:
            continue
        if re.search(r"/profiles/\d+/?\s*$", href) or re.search(
            r"/profiles/\d+/?$", href
        ):
            chosen = cand
            break
        if "/profiles/" in href and chosen is None:
            chosen = cand
    if chosen is None:
        chosen = link
    chosen.wait_for(state="visible", timeout=30_000)
    _log("Accounts Center: открываем свой Instagram-профиль…")
    chosen.click(timeout=15_000)
    try:
        page.wait_for_timeout(800)
    except Exception:
        time.sleep(0.8)


def _wait_profile_details_dialog(page, *, timeout_ms: int = 45_000) -> None:
    deadline = time.monotonic() + max(5.0, timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        try:
            username_link = page.locator(
                'a[href*="/username/manage/"], a[aria-label="Username"]'
            ).first
            if username_link.count() > 0 and username_link.is_visible():
                _log("Accounts Center: диалог профиля открыт.")
                return
        except Exception:
            pass
        try:
            page.wait_for_timeout(250)
        except Exception:
            time.sleep(0.25)
    raise InstagramEditProfileError(
        "Не дождались диалога профиля (Username) в Accounts Center."
    )


def _click_username_menu(page) -> None:
    link = page.locator(
        'a[href*="/username/manage/"], a[aria-label="Username"]'
    ).first
    link.wait_for(state="visible", timeout=20_000)
    _log("Accounts Center: открываем Username…")
    link.click(timeout=15_000)
    try:
        page.wait_for_timeout(700)
    except Exception:
        time.sleep(0.7)


def _wait_username_edit_dialog(page, *, timeout_ms: int = 45_000):
    deadline = time.monotonic() + max(5.0, timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        try:
            dlg = page.get_by_role("dialog", name=re.compile(r"Username", re.I))
            if dlg.count() > 0:
                d = dlg.last
                inp = d.locator('input[type="text"]').first
                if inp.count() > 0 and inp.is_visible():
                    _log("Accounts Center: форма Username готова.")
                    return d
        except Exception:
            pass
        try:
            dlg = page.locator('[role="dialog"]').filter(
                has=page.locator('input[type="text"]')
            ).last
            inp = dlg.locator('input[type="text"]').first
            if inp.count() > 0 and inp.is_visible():
                # Заголовок Username рядом.
                txt = (dlg.inner_text(timeout=2_000) or "")[:400]
                if re.search(r"Username|Имя\s+пользователя", txt, re.I):
                    _log("Accounts Center: форма Username готова (fallback).")
                    return dlg
        except Exception:
            pass
        try:
            page.wait_for_timeout(250)
        except Exception:
            time.sleep(0.25)
    raise InstagramEditProfileError(
        "Не дождались поля ввода Username в Accounts Center."
    )


def _fill_username_input(dialog, username: str) -> None:
    inp = dialog.locator('input[type="text"]').first
    inp.wait_for(state="visible", timeout=15_000)
    try:
        inp.click(timeout=5_000)
    except Exception:
        pass
    try:
        inp.fill("")
    except Exception:
        pass
    try:
        inp.fill(username, timeout=10_000)
    except Exception:
        inp.evaluate(
            """(el, value) => {
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                )?.set;
                if (setter) setter.call(el, value);
                else el.value = value;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            username,
        )
    _log(f"Accounts Center: введён юзернейм {username!r}.")


def _username_input_invalid(dialog) -> bool | None:
    """True=занят/ошибка, False=ок (галочка), None=ещё проверяется."""
    try:
        err = dialog.get_by_text(
            re.compile(
                r"Username\s+is\s+not\s+available|"
                r"имя\s+пользователя\s+недоступно|"
                r"уже\s+занято",
                re.I,
            )
        )
        if err.count() > 0 and err.first.is_visible():
            return True
    except Exception:
        pass
    inp = dialog.locator('input[type="text"]').first
    try:
        aria = (inp.get_attribute("aria-invalid") or "").strip().lower()
        if aria == "true":
            return True
    except Exception:
        pass
    try:
        ok = dialog.locator('[title*="Username is valid" i], [title*="valid" i]')
        if ok.count() > 0:
            return False
    except Exception:
        pass
    try:
        if dialog.locator("svg title").filter(
            has_text=re.compile(r"valid", re.I)
        ).count() > 0:
            return False
    except Exception:
        pass
    return None


def _done_button(dialog):
    try:
        return dialog.get_by_role(
            "button", name=re.compile(r"^(Done|Готово)$", re.I)
        ).first
    except Exception:
        return dialog.locator('[role="button"]').filter(
            has_text=re.compile(r"^(Done|Готово)$", re.I)
        ).first


def _is_done_enabled(dialog) -> bool:
    btn = _done_button(dialog)
    try:
        if btn.count() <= 0:
            return False
    except Exception:
        return False
    try:
        aria = (btn.get_attribute("aria-disabled") or "").strip().lower()
        if aria in ("true", "1"):
            return False
    except Exception:
        pass
    try:
        tab = (btn.get_attribute("tabindex") or "").strip()
        if tab == "-1":
            return False
    except Exception:
        pass
    try:
        return btn.is_visible()
    except Exception:
        return False


def _wait_username_validation(
    dialog, *, timeout_s: float = 12.0
) -> bool:
    """True если юзернейм доступен (Done активна), False если занят/ошибка."""
    deadline = time.monotonic() + max(2.0, float(timeout_s))
    while time.monotonic() < deadline:
        state = _username_input_invalid(dialog)
        if state is True:
            return False
        if _is_done_enabled(dialog):
            return True
        try:
            dialog.page.wait_for_timeout(250)
        except Exception:
            time.sleep(0.25)
    return _is_done_enabled(dialog)


def _click_done(dialog) -> None:
    btn = _done_button(dialog)
    btn.wait_for(state="visible", timeout=10_000)
    if not _is_done_enabled(dialog):
        raise InstagramEditProfileError(
            "Кнопка Done недоступна — юзернейм не принят."
        )
    _log("Accounts Center: нажимаем Done…")
    try:
        btn.click(timeout=15_000)
    except Exception:
        btn.click(timeout=10_000, force=True)
    try:
        dialog.page.wait_for_timeout(900)
    except Exception:
        time.sleep(0.9)


def _verify_username_applied(page, expected: str, *, timeout_s: float = 20.0) -> None:
    want = (expected or "").strip().lstrip("@").lower()
    deadline = time.monotonic() + max(3.0, float(timeout_s))
    while time.monotonic() < deadline:
        try:
            # Заголовок в диалоге профиля после смены.
            headings = page.locator('[role="dialog"] h3, [role="dialog"] h2')
            n = headings.count()
            for i in range(n):
                text = (headings.nth(i).inner_text(timeout=1_500) or "").strip()
                if text.lstrip("@").lower() == want:
                    _log(f"Accounts Center: юзернейм подтверждён ({want!r}).")
                    return
        except Exception:
            pass
        try:
            # Список Profiles на фоне / aria-label.
            link = page.locator(
                f'a[aria-label*="{want}" i][aria-label*="Instagram" i]'
            ).first
            if link.count() > 0 and link.is_visible():
                _log(f"Accounts Center: юзернейм виден в списке ({want!r}).")
                return
        except Exception:
            pass
        try:
            page.wait_for_timeout(300)
        except Exception:
            time.sleep(0.3)
    raise InstagramEditProfileError(
        f"После Done юзернейм {want!r} не появился в Accounts Center."
    )


def _change_instagram_username(page, username: str) -> str:
    """Accounts Center → свой профиль → Username → Done. Возвращает итоговый username."""
    base = _sanitize_username(username)
    if not base:
        raise InstagramEditProfileError("Не задан юзернейм для смены.")

    _log(f"Accounts Center: открываем {ACCOUNTS_CENTER_URL}")
    _navigate_page_to(page, ACCOUNTS_CENTER_URL, label="IG accounts center")
    accept_instagram_cookie_consent_if_present(page, appear_seconds=3.0)
    _wait_accounts_center_profiles(page)
    _click_own_instagram_profile(page)
    _wait_profile_details_dialog(page)
    _click_username_menu(page)
    dialog = _wait_username_edit_dialog(page)

    applied = ""
    for attempt in range(1, _USERNAME_RETRY_MAX + 1):
        if attempt == 1:
            candidate = _username_candidate(base)
        else:
            suffix = random.randint(1, 1000)
            candidate = _username_candidate(base, suffix=suffix)
            _log(
                f"Accounts Center: юзернейм занят — пробуем "
                f"{candidate!r} (попытка {attempt}/{_USERNAME_RETRY_MAX})."
            )
        _fill_username_input(dialog, candidate)
        ok = _wait_username_validation(dialog, timeout_s=12.0)
        if not ok:
            continue
        applied = candidate
        break

    if not applied:
        raise InstagramEditProfileError(
            f"Не удалось подобрать свободный юзернейм от базы {base!r} "
            f"(попыток: {_USERNAME_RETRY_MAX})."
        )

    _click_done(dialog)
    _verify_username_applied(page, applied)
    _log(f"Accounts Center: юзернейм сменён на {applied!r}.")
    return applied


def _change_instagram_language_to_russian(page) -> None:
    """
    /language/preferences/ → выбрать «Русский» → после обновления страницы
    должно быть слово «язык».
    """
    _log(f"Language: открываем {LANGUAGE_PREFERENCES_URL}")
    _navigate_page_to(page, LANGUAGE_PREFERENCES_URL, label="IG language preferences")
    accept_instagram_cookie_consent_if_present(page, appear_seconds=3.0)

    deadline = time.monotonic() + 45.0
    list_ready = False
    while time.monotonic() < deadline:
        try:
            search = page.locator('input[aria-label="Search input"], input[placeholder="Search"]').first
            if search.count() > 0 and search.is_visible():
                list_ready = True
                break
        except Exception:
            pass
        try:
            if page.locator('[role="button"]').filter(has_text=_RUSSIAN_LANG_RE).count() > 0:
                list_ready = True
                break
        except Exception:
            pass
        try:
            page.wait_for_timeout(250)
        except Exception:
            time.sleep(0.25)
    if not list_ready:
        raise InstagramEditProfileError(
            "Не дождались списка языков на Language preferences "
            f"(URL={_page_url(page)!r})."
        )

    # Уже выбран русский?
    try:
        ru_row = page.locator('[role="button"]').filter(has_text=_RUSSIAN_LANG_RE).first
        checked = ru_row.locator('input[type="checkbox"][aria-checked="true"]')
        if checked.count() > 0:
            _log("Language: «Русский» уже выбран.")
            if _page_has_yazyk(page):
                _log("Language: на странице есть «язык» — ок.")
                return
    except Exception:
        pass

    ru_btn = page.locator('[role="button"]').filter(has_text=_RUSSIAN_LANG_RE).first
    try:
        ru_btn.wait_for(state="visible", timeout=20_000)
    except Exception as exc:
        raise InstagramEditProfileError(
            f"Не найден пункт языка «Русский»: {exc!r}"
        ) from exc

    try:
        if ru_btn.locator('input[aria-checked="true"]').count() > 0:
            _log("Language: «Русский» уже отмечен.")
            if _page_has_yazyk(page):
                return
    except Exception:
        pass

    _log("Language: выбираем «Русский»…")
    try:
        ru_btn.scroll_into_view_if_needed(timeout=8_000)
    except Exception:
        pass
    try:
        ru_btn.click(timeout=15_000)
    except Exception:
        try:
            ru_btn.click(timeout=10_000, force=True)
        except Exception as exc:
            raise InstagramEditProfileError(
                f"Не удалось нажать «Русский»: {exc!r}"
            ) from exc

    # После клика страница обновляется — ждём «язык».
    if not _wait_page_has_yazyk(page, timeout_s=30.0):
        raise InstagramEditProfileError(
            "После выбора «Русский» на странице не появилось слово «язык» "
            f"(URL={_page_url(page)!r})."
        )
    _log("Language: язык сменён на русский (на странице есть «язык»).")


def _page_has_yazyk(page) -> bool:
    try:
        body = page.locator("body")
        text = (body.inner_text(timeout=5_000) or "")
        return bool(_YAZYK_RE.search(text))
    except Exception:
        pass
    try:
        return page.get_by_text(_YAZYK_RE).count() > 0
    except Exception:
        return False


def _wait_page_has_yazyk(page, *, timeout_s: float = 30.0) -> bool:
    deadline = time.monotonic() + max(3.0, float(timeout_s))
    while time.monotonic() < deadline:
        if _page_has_yazyk(page):
            return True
        try:
            page.wait_for_timeout(400)
        except Exception:
            time.sleep(0.4)
    return _page_has_yazyk(page)


def _run_edit_profile_bio_avatar(
    page,
    *,
    bio: str,
    avatar: Path | None,
) -> None:
    _log(f"Edit profile: открываем {EDIT_PROFILE_URL}")
    _navigate_page_to(page, EDIT_PROFILE_URL, label="IG edit profile")
    accept_instagram_cookie_consent_if_present(page, appear_seconds=4.0)
    _wait_edit_profile_ready(page)

    if avatar is not None:
        _upload_avatar(page, avatar)
    if bio:
        _fill_bio(page, bio)
        expected_bio = bio[:_BIO_MAX_LEN]
        if _read_bio_value(page) != expected_bio:
            raise InstagramEditProfileError(
                "Перед Submit bio в поле не совпадает с ожидаемым текстом."
            )

    _wait_submit_enabled_after_edits(page)
    _click_submit(page)
    _wait_submit_disabled_or_fail(page, wait_s=_SUBMIT_DISABLE_WAIT_S)
    _log("Edit profile: bio/фото сохранены.")


@instagram_entrypoint
def run_instagram_edit_profile(
    page,
    *,
    description: str | None = None,
    avatar_path: str | Path | None = None,
    username: str | None = None,
    change_language: bool = False,
    session_login: str = "",
    session_password: str = "",
    session_twofa: str = "",
    profile_id: str | None = None,
) -> str | None:
    """
    0) Опционально: Language preferences → Русский (до остальных шагов).
    1) Если есть аватарка и/или bio — /accounts/edit/ → фото → bio → Submit.
    2) Если задан username — Accounts Center → Username → Done.

    Возвращает итоговый юзернейм (если меняли), иначе None.
    """
    bio = (description or "").strip()
    avatar = Path(avatar_path) if avatar_path else None
    if avatar is not None and not avatar.is_file():
        raise InstagramEditProfileError(f"Файл аватарки не найден: {avatar}")
    want_username = _sanitize_username(username or "")
    do_lang = bool(change_language)
    if not bio and avatar is None and not want_username and not do_lang:
        raise InstagramEditProfileError(
            "Не заданы смена языка, bio, аватарка или юзернейм."
        )

    _log("Edit profile: проверка сессии / главной Instagram…")
    verify_instagram_home_available(
        page,
        session_login=session_login,
        session_password=session_password,
        session_twofa=session_twofa,
        profile_id=profile_id,
    )

    if do_lang:
        _change_instagram_language_to_russian(page)

    if bio or avatar is not None:
        _run_edit_profile_bio_avatar(page, bio=bio, avatar=avatar)

    applied: str | None = None
    if want_username:
        applied = _change_instagram_username(page, want_username)

    _log("Edit profile: готово.")
    return applied
