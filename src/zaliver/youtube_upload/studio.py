from __future__ import annotations

import os
import random
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

from patchright.sync_api import Error as PlaywrightError

from zaliver.antydetect.profile_tags import (  # noqa: F401 — re-export
    PREVIOUS_UPLOAD_RESULT_TAGS,
    STUDIO_AVAILABILITY_ERROR_TAG,
    UPLOAD_PREVIOUS_ERROR_TAG,
    UPLOAD_PREVIOUS_SUCCESS_TAG,
)


_STUDIO_UI_MS = 120_000
# После передачи файла ждём в Studio один из исходов: лимит или завершение проверок (часто >1 мин).
_POST_UPLOAD_STUDIO_OUTCOME_MAX_S = 3600.0
_POST_UPLOAD_QUOTA_POLL_S = 2.0
_STUDIO_WIZARD_NEXT_MAX = 30
_STUDIO_WIZARD_NEXT_AFTER_CLICK_MS = 250
_STUDIO_WIZARD_NEXT_POLL_MS = 40
_STUDIO_UPLOAD_DETAILS_POLL_MS = 30
_STUDIO_INTERRUPT_DIALOG_EVERY_N_POLLS = 15
_STUDIO_WARM_WELCOME_NEXT_MAX = 10
_STUDIO_HOME_URL = "https://studio.youtube.com/"
_WELCOME_TITLE_RE = re.compile(
    r"добро\s+пожаловать|welcome\s+to\s+(the\s+)?youtube\s+studio",
    re.I,
)
_AADC_HEADING_RE = re.compile(
    r"видео\s+в\s+открытом\s+доступе|videos?\s+(in\s+)?public|publicly\s+available",
    re.I,
)
_NOT_FOR_KIDS_RADIO_NAME = "VIDEO_MADE_FOR_KIDS_NOT_MFK"
_NOT_FOR_KIDS_LABEL_RE = re.compile(
    r"no,?\s*it[''\u2019]?s?\s*not\s*made\s*for\s*kids|"
    r"нет,?\s*это\s*видео\s*не\s*для\s*детей|"
    r"not\s*made\s*for\s*kids",
    re.I,
)
_CHANNEL_REMOVED_PAGE_TITLE_RE = re.compile(
    r"удален\s+с\s+youtube|removed\s+from\s+youtube",
    re.I,
)
_CHANNEL_REMOVED_LABEL_RE = re.compile(
    r"канал\s+удален|channel\s+removed|removed\s+from\s+youtube",
    re.I,
)
_CHANNEL_PERMISSION_DENIED_RE = re.compile(
    r"don[''\u2019]?t\s+have\s+permission|"
    r"no\s+permission\s+to\s+view|"
    r"oops,?\s+you\s+don[''\u2019]?t\s+have\s+permission|"
    r"signed\s+into\s+an\s+account\s+that\s+has\s+access|"
    r"нет\s+прав|"
    r"не\s+имеете\s+права|"
    r"нет\s+доступа\s+к\s+этой\s+странице",
    re.I,
)
_SWITCH_ACCOUNT_LABEL_RE = re.compile(
    r"сменить\s+аккаунт|switch\s+account",
    re.I,
)
_YOUR_CHANNEL_LABEL_RE = re.compile(
    r"your\s+channel|ваш\s+канал",
    re.I,
)
_VIEW_CHANNEL_LABEL_RE = re.compile(
    r"посмотреть\s+канал|view\s+channel|your\s+channel|ваш\s+канал",
    re.I,
)
_JOINED_YEAR_CONTEXT_RE = re.compile(
    r"(?:joined|registered|registration|since|"
    r"дата\s+регистрации|на\s+youtube\s+с|"
    r"registr|создан|created|fecha|"
    r"присоединился|зарегистрирован|"
    r"date\s+joined|channel\s+created)[^\d]{0,64}(\d{4})",
    re.I,
)
_JOINED_LINE_HINT_RE = re.compile(
    r"joined|registration|registered|since|"
    r"дата\s+регистрации|на\s+youtube\s+с|"
    r"присоединился|зарегистрирован|"
    r"date\s+joined|channel\s+created|"
    r"registr|создан|created",
    re.I,
)
_YEAR_IN_TEXT_RE = re.compile(r"\b(19\d{2}|20[0-3]\d)\b")

# Playwright при connect_over_cdp шлёт тело файла по CDP и режет ~50 MiB.
# DOM.setFileInputFiles с путями на хосте браузера обходит это (Chromium читает файл сам);
# для загрузки в Studio всегда пробуем CDP первым, set_files — только fallback.
_PLAYWRIGHT_REMOTE_UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024  # лимит Playwright для fallback-пути
# set_files / set_input_files по CDP для крупных файлов; дефолт Playwright 30 с часто мало.
_STUDIO_FILE_PICKER_TRANSFER_MS = 600_000


class YoutubeStudioError(RuntimeError):
    pass


class YoutubeAllChannelsRemovedError(YoutubeStudioError):
    """Все каналы в списке помечены как удалённые — закрываем профиль."""


_LOG_SINK = None
_STUDIO_PROFILE_ID: ContextVar[str | None] = ContextVar("studio_profile_id", default=None)


def set_log_sink(sink) -> None:
    """
    Optional log sink callback.
    If set, each `_log()` line will be forwarded to `sink(str)`.
    """
    global _LOG_SINK
    _LOG_SINK = sink


def set_studio_profile_id(profile_id: str | None) -> Token:
    pid = (profile_id or "").strip() or None
    return _STUDIO_PROFILE_ID.set(pid)


def reset_studio_profile_id(token: Token) -> None:
    _STUDIO_PROFILE_ID.reset(token)


def get_studio_profile_id() -> str | None:
    return _STUDIO_PROFILE_ID.get()


@contextmanager
def studio_profile_context(profile_id: str | None):
    pid = (profile_id or "").strip() or None
    if not pid:
        yield
        return
    token = set_studio_profile_id(pid)
    try:
        yield
    finally:
        reset_studio_profile_id(token)


def _studio_entrypoint(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with studio_profile_context(kwargs.get("profile_id")):
            return fn(*args, **kwargs)

    return wrapped


def _studio_record_channel_name(rec: _ChannelJoinRecord) -> str:
    return (rec.about_name or rec.switch_name or "").strip()


def _studio_finalize_oldest_channel_name(
    name: str,
    *,
    on_oldest_channel_name=None,
) -> str:
    n = (name or "").strip()
    if not n:
        return ""
    if on_oldest_channel_name is not None:
        try:
            on_oldest_channel_name(n)
        except Exception as e:
            _log(f"Studio: не удалось сохранить yt_oldest_name: {e!r}")
    return n


def _log(message: str) -> None:
    from zaliver.log_format import format_log_line

    line = format_log_line(
        f"[youtube_studio] {message}",
        profile_id=get_studio_profile_id(),
    )
    sink = _LOG_SINK
    if sink is not None:
        try:
            sink(line)
        except Exception:
            # Logging must not break automation flow.
            pass
    else:
        print(line)


def _studio_raise_if_auth_without_credentials(page, login_credentials) -> None:
    """Экран входа Google без yt_login/yt_password/yt_2fa — пропускаем профиль."""
    from zaliver.youtube_upload.google_login import (
        GoogleLoginCredentialsMissingError,
        google_auth_interaction_visible,
        has_login_credentials,
    )

    if has_login_credentials(login_credentials):
        return
    if not (_studio_login_required(page) or google_auth_interaction_visible(page)):
        return
    _log(
        "Studio: требуется вход в Google, но в данных учётки нет "
        "yt_login / yt_password / yt_2fa — профиль пропущен."
    )
    raise GoogleLoginCredentialsMissingError(
        "YouTube Studio: требуется вход в Google, но в данных учётки профиля "
        "нет yt_login, yt_password и yt_2fa — профиль пропущен."
    )


def _studio_try_google_login_if_needed(page, login_credentials) -> bool:
    """
    При необходимости проходит Google-вход (личность → пароль → 2FA → канал).
    Возвращает True, если попытка была и экран входа снят.
    """
    from zaliver.youtube_upload.google_login import (
        GoogleLoginCredentialsMissingError,
        attempt_google_login_for_studio,
        google_auth_interaction_visible,
        handle_channel_switcher_if_present,
    )

    if handle_channel_switcher_if_present(page):
        return True

    if not (_studio_login_required(page) or google_auth_interaction_visible(page)):
        return False

    _studio_raise_if_auth_without_credentials(page, login_credentials)

    try:
        attempt_google_login_for_studio(page, login_credentials)
    except GoogleLoginCredentialsMissingError:
        raise
    except RuntimeError as e:
        raise YoutubeStudioError(str(e)) from e
    return True


def _studio_page_url_lower(page) -> str:
    try:
        return (page.url or "").lower()
    except Exception:
        return ""


def _studio_on_youtube_property(page) -> bool:
    url = _studio_page_url_lower(page)
    return "www.youtube.com" in url or "studio.youtube.com" in url


def _studio_login_required(page, *, fast: bool = False) -> bool:
    """
    Иногда вместо Studio открывается окно логина Google/YouTube.
    В этом случае на профиле нет активной сессии → залив нужно завершать.
    """
    probe_ms = 250 if fast else 800
    try:
        url = _studio_page_url_lower(page)
        if "accounts.google.com" in url or "servicelogin" in url:
            return True
    except Exception:
        pass
    try:
        # Пример из репорта пользователя:
        # <h1 id="headingText"><span>Вход</span></h1>
        # "Для перехода к YouTube войдите в свой аккаунт Google."
        login_block = page.locator("div.ObDc3.ZYOIke").first
        if login_block.count() > 0 and login_block.is_visible(timeout=probe_ms):
            return True
    except Exception:
        pass
    try:
        if page.locator(
            "#headingText",
            has_text=re.compile(r"вход|sign\s*in|выберите\s+аккаунт|choose\s+an?\s+account", re.I),
        ).first.is_visible(timeout=probe_ms):
            return True
    except Exception:
        pass
    try:
        if page.locator(
            "[role='heading'][aria-level='1'], .RY3zi, h1.qQnGVb",
            has_text=re.compile(
                r"убедитесь,\s*что\s+вы\s+всегда\s+сможете\s+войти|"
                r"make\s+sure\s+you\s+(can\s+)?always\s+sign\s+in|"
                r"add\s+your\s+birthday|добавьте\s+дату\s+рождения|"
                r"укажите\s+дату\s+рождения",
                re.I,
            ),
        ).first.is_visible(timeout=probe_ms):
            return True
    except Exception:
        pass
    try:
        if page.get_by_text(
            re.compile(r"для\s+перехода\s+к\s+youtube\s+войдите", re.I)
        ).first.is_visible(timeout=probe_ms):
            return True
    except Exception:
        pass
    return False


def _studio_on_google_auth_page(page, *, fast: bool = False) -> bool:
    url = _studio_page_url_lower(page)
    if "accounts.google.com" in url or "servicelogin" in url:
        return True
    if _studio_on_youtube_property(page):
        if _studio_login_required(page, fast=True):
            return True
        try:
            if page.locator("ytd-channel-switcher-renderer").first.is_visible(
                timeout=200
            ):
                return True
        except Exception:
            pass
        return False
    if fast:
        return _studio_login_required(page, fast=True)
    from zaliver.youtube_upload.google_login import google_auth_interaction_visible

    return _studio_login_required(page, fast=True) or google_auth_interaction_visible(page)


def _studio_wait_for_google_session(
    page, *, login_credentials=None, timeout_s: float | None = None, fast: bool = False
) -> None:
    """Не уходим со страницы входа Google, пока сессия не восстановлена."""
    if not _studio_on_google_auth_page(page, fast=fast):
        return

    from zaliver.youtube_upload.google_login import _GOOGLE_LOGIN_MAX_S

    max_s = _GOOGLE_LOGIN_MAX_S if timeout_s is None else timeout_s
    _studio_raise_if_auth_without_credentials(page, login_credentials)
    _log(
        "Studio: требуется вход в Google — ждём авторизацию "
        "перед проверкой каналов…"
    )
    deadline = time.monotonic() + max_s
    last_status_log = 0.0
    while time.monotonic() < deadline:
        _studio_raise_if_auth_without_credentials(page, login_credentials)
        if _studio_try_google_login_if_needed(page, login_credentials):
            if not _studio_on_google_auth_page(page):
                _log("Studio: вход в Google завершён.")
                return
            continue
        if not _studio_on_google_auth_page(page):
            _log("Studio: вход в Google завершён.")
            return
        now = time.monotonic()
        if now - last_status_log >= 15.0:
            last_status_log = now
            try:
                url = page.url or ""
            except Exception:
                url = ""
            _log(f"Studio: ожидание входа в Google… URL={url!r}")
        page.wait_for_timeout(500)

    raise YoutubeStudioError(
        "YouTube Studio: не дождались входа в Google. "
        "Завершите авторизацию в окне браузера или проверьте "
        "yt_login/yt_password в данных профиля."
    )


def _studio_channel_creation_dialog_locator(page):
    return page.locator("ytd-channel-creation-dialog-renderer")


def _studio_channel_creation_dialog_visible(page) -> bool:
    try:
        dlg = _studio_channel_creation_dialog_locator(page)
        return dlg.count() > 0 and dlg.first.is_visible()
    except Exception:
        return False


def _studio_channel_removed_page_visible(page) -> bool:
    """Страница апелляции: канал удалён/заблокирован при входе в Studio."""
    try:
        url = (page.url or "").lower()
        if "channel-appeal" in url:
            return True
    except Exception:
        pass
    try:
        appeal = page.locator("yttou-channel-appeal-app")
        if appeal.count() > 0 and appeal.first.is_visible(timeout=500):
            return True
    except Exception:
        pass
    try:
        title = page.locator(
            "yttou-channel-appeal-status h2#title, "
            "yttou-shared-display-page h2#title"
        )
        if title.count() > 0 and title.first.is_visible(timeout=500):
            txt = (title.first.inner_text(timeout=1_500) or "").strip()
            if _CHANNEL_REMOVED_PAGE_TITLE_RE.search(txt):
                return True
    except Exception:
        pass
    return False


def _studio_channel_permission_denied_page_visible(page) -> bool:
    """Oops: нет прав на просмотр страницы канала (#error-text / #selectaccount-link)."""
    try:
        err = page.locator("#error-text")
        if err.count() > 0 and err.first.is_visible(timeout=500):
            txt = (err.first.inner_text(timeout=1_500) or "").strip()
            if _CHANNEL_PERMISSION_DENIED_RE.search(txt):
                return True
    except Exception:
        pass
    try:
        switch_link = page.locator("#selectaccount-link")
        if switch_link.count() > 0 and switch_link.first.is_visible(timeout=500):
            return True
    except Exception:
        pass
    try:
        if page.get_by_text(_CHANNEL_PERMISSION_DENIED_RE).first.is_visible(timeout=500):
            return True
    except Exception:
        pass
    return False


def _studio_channel_access_blocked(page) -> bool:
    """Канал/страница недоступны: oops URL, удалён или нет прав."""
    return (
        _studio_is_oops_url(page)
        or _studio_channel_removed_page_visible(page)
        or _studio_channel_permission_denied_page_visible(page)
    )


def _studio_account_item_removed_label(item) -> str:
    """Текст статуса «Канал удалён» (может быть не первым secondary — после @handle)."""
    for sel in (
        "yt-formatted-string[secondary]",
        "tp-yt-paper-item-body yt-formatted-string[secondary]",
        "tp-yt-paper-item-body yt-formatted-string",
    ):
        loc = item.locator(sel)
        try:
            for i in range(loc.count()):
                label = (loc.nth(i).inner_text(timeout=1_500) or "").strip()
                if label and _CHANNEL_REMOVED_LABEL_RE.search(label):
                    return label
        except Exception:
            continue
    try:
        text = (item.inner_text(timeout=1_500) or "").strip()
        if text and _CHANNEL_REMOVED_LABEL_RE.search(text):
            return text
    except Exception:
        pass
    return ""


def _studio_account_item_channel_name(item) -> str:
    try:
        title = item.locator("yt-formatted-string#channel-title")
        if title.count() > 0:
            name = (title.first.inner_text(timeout=1_500) or "").strip()
            if name:
                return name
    except Exception:
        pass
    try:
        lines = [
            ln.strip()
            for ln in (item.inner_text(timeout=1_500) or "").splitlines()
            if ln.strip()
        ]
        if lines:
            return lines[0]
    except Exception:
        pass
    return ""


def _studio_account_item_secondary_texts(item) -> list[str]:
    texts: list[str] = []
    for sel in (
        "yt-formatted-string[secondary]",
        "tp-yt-paper-item-body yt-formatted-string[secondary]",
        "tp-yt-paper-item-body yt-formatted-string",
    ):
        loc = item.locator(sel)
        try:
            for i in range(loc.count()):
                t = (loc.nth(i).inner_text(timeout=1_500) or "").strip()
                if t and t not in texts:
                    texts.append(t)
        except Exception:
            continue
    return texts


def _studio_account_item_matches_name(item, expected_name: str) -> bool:
    """Совпадение с channel-title или @handle в secondary."""
    expected = (expected_name or "").strip()
    if not expected:
        return False
    title = _studio_account_item_channel_name(item)
    if title and _studio_channel_names_match(title, expected):
        return True
    for text in _studio_account_item_secondary_texts(item):
        if _CHANNEL_REMOVED_LABEL_RE.search(text):
            continue
        if _studio_channel_names_match(text, expected):
            return True
    return False


def _studio_account_item_is_removed(item) -> bool:
    return bool(_CHANNEL_REMOVED_LABEL_RE.search(_studio_account_item_removed_label(item)))


def _studio_account_switcher_all_channels_removed(page) -> bool:
    """True, если в switcher есть каналы и у всех статус «Канал удалён»."""
    if not _studio_account_switcher_visible(page):
        return False
    try:
        switcher = _studio_account_switcher_locator(page)
        items = switcher.first.locator("ytd-account-item-renderer")
        count = items.count()
    except Exception:
        return False
    if count <= 0:
        return False
    for i in range(count):
        try:
            if not _studio_account_item_is_removed(items.nth(i)):
                return False
        except Exception:
            return False
    return True


def _studio_abort_all_channels_removed(page, *, browser=None) -> None:
    _log(
        "Studio: все каналы в списке помечены как «Канал удалён» — "
        "закрываем профиль."
    )
    if browser is not None:
        _log("Playwright: закрытие браузера — все каналы удалены.")
        try:
            browser.close()
        except Exception:
            pass
    _studio_dismiss_open_menus(page)
    raise YoutubeAllChannelsRemovedError(
        "YouTube Studio: все каналы в аккаунте удалены — профиль закрыт."
    )


def _studio_account_item_is_selected(item) -> bool:
    try:
        selected = item.locator("yt-icon#selected")
        if selected.count() == 0:
            return False
        return selected.first.is_visible(timeout=300)
    except Exception:
        return False


def _studio_collect_available_account_switcher_channels(page) -> list[tuple[int, str]]:
    """
    Каналы в меню «Аккаунты», не помеченные как удалённые.
    Возвращает (индекс, имя).
    """
    switcher = _studio_account_switcher_locator(page)
    try:
        switcher.first.wait_for(state="visible", timeout=15_000)
    except Exception:
        return []

    items = switcher.first.locator("ytd-account-item-renderer")
    count = items.count()
    available: list[tuple[int, str]] = []
    for i in range(count):
        item = items.nth(i)
        name = _studio_account_item_channel_name(item) or f"#{i + 1}"
        if _studio_account_item_is_removed(item):
            _log(f"Studio: канал «{name}» пропущен — Channel removed / Канал удален.")
            continue
        available.append((i, name))
    if not available and count > 0 and _studio_account_switcher_all_channels_removed(page):
        _studio_abort_all_channels_removed(page)
    return available


def _studio_switcher_oldest_list_entry(
    page, *, menu_open: bool = False
) -> tuple[int, str, bool] | None:
    """
    Последний пункт в «Сменить аккаунт» — самый старый канал в списке.
    Возвращает (индекс, имя, удалён_ли) или None.
    """
    opened = False
    if not menu_open:
        try:
            _studio_open_account_switcher_menu(page)
            opened = True
        except Exception:
            return None
    try:
        switcher = _studio_account_switcher_locator(page)
        switcher.first.wait_for(state="visible", timeout=15_000)
        items = switcher.first.locator("ytd-account-item-renderer")
        count = items.count()
        if count <= 0:
            return None
        idx = count - 1
        item = items.nth(idx)
        name = _studio_account_item_channel_name(item) or f"#{idx + 1}"
        return idx, name, _studio_account_item_is_removed(item)
    except Exception:
        return None
    finally:
        if opened:
            _studio_dismiss_open_menus(page)


def _studio_try_select_oldest_in_switcher_list(
    page,
    *,
    current_idx: int | None = None,
    on_oldest_channel_name=None,
) -> str | None:
    """
    Если последний канал в списке не удалён — выбираем его без обхода всех каналов.
    Возвращает имя канала или None (нужен полный скан).
    """
    entry = _studio_switcher_oldest_list_entry(page)
    if entry is None:
        return None
    idx, name, removed = entry
    if removed:
        _log(
            f"Studio: самый старый в списке «{name}» (позиция {idx + 1}) удалён — "
            "полный скан по дате регистрации…"
        )
        return None

    _log(
        f"Studio: самый старый в списке «{name}» (позиция {idx + 1}) не удалён — "
        "выбираем без полного скана."
    )
    if current_idx == idx:
        _log(f"Studio: уже на канале «{name}».")
        return _studio_finalize_oldest_channel_name(
            name, on_oldest_channel_name=on_oldest_channel_name
        )

    if _studio_try_switch_to_account_by_index(page, idx, name):
        return _studio_finalize_oldest_channel_name(
            name, on_oldest_channel_name=on_oldest_channel_name
        )

    _log(f"Studio: не удалось переключиться на «{name}» — полный скан каналов…")
    return None


def _studio_try_select_only_available_channel(
    page,
    accounts: list[tuple[int, str]],
    *,
    current_idx: int | None = None,
    on_oldest_channel_name=None,
) -> str | None:
    """
    Все каналы в списке удалены, кроме одного — переключаемся на него,
    сохраняем имя в данные учётки (on_oldest_channel_name).
    """
    if len(accounts) != 1:
        return None
    idx, name = accounts[0]
    _log(
        f"Studio: в списке «Сменить аккаунт» один доступный канал «{name}» "
        f"(позиция {idx + 1}), остальные удалены — выбираем его."
    )
    if current_idx == idx:
        _log(f"Studio: уже на канале «{name}».")
        return _studio_finalize_oldest_channel_name(
            name, on_oldest_channel_name=on_oldest_channel_name
        )
    if _studio_try_switch_to_account_by_index(page, idx, name):
        return _studio_finalize_oldest_channel_name(
            name, on_oldest_channel_name=on_oldest_channel_name
        )
    _log(f"Studio: не удалось переключиться на «{name}».")
    return None


def _studio_try_fast_channel_pick(
    page,
    accounts: list[tuple[int, str]],
    *,
    current_idx: int | None = None,
    on_oldest_channel_name=None,
) -> str | None:
    """Один доступный канал в switcher — без полного скана по датам."""
    return _studio_try_select_only_available_channel(
        page,
        accounts,
        current_idx=current_idx,
        on_oldest_channel_name=on_oldest_channel_name,
    )


def _studio_page_url_lower(page) -> str:
    try:
        return (page.url or "").lower()
    except Exception:
        return ""


def _studio_avatar_locator(page):
    return (
        page.locator("yttou-channel-appeal-app #avatar-btn")
        .or_(page.locator("ytd-topbar-menu-button-renderer #avatar-btn"))
        .or_(page.locator("button#avatar-btn"))
    )


def _studio_is_oops_url(page) -> bool:
    return "oops" in _studio_page_url_lower(page)


def _studio_channel_names_match(a: str, b: str) -> bool:
    na = _studio_normalize_channel_name(a)
    nb = _studio_normalize_channel_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # handle vs display name: «siobahnwallace» / «Siobahn Wallace»
    if na in nb or nb in na:
        return True
    return False


def _studio_normalize_channel_name(name: str) -> str:
    n = (name or "").strip()
    if n.startswith("@"):
        n = n[1:].strip()
    return n.casefold()


@dataclass(frozen=True, slots=True)
class _ChannelJoinRecord:
    switch_index: int
    switch_name: str
    about_name: str
    join_year: int


def _studio_dismiss_open_menus(page) -> None:
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
    except Exception:
        pass


def _studio_open_avatar_menu(page) -> None:
    """Открыть меню профиля с клавиатуры — клик мышью попадает в «Аккаунт Google»."""
    _studio_dismiss_open_menus(page)
    page.wait_for_timeout(200)
    avatar = _studio_avatar_locator(page)
    avatar.first.wait_for(state="visible", timeout=15_000)
    avatar.first.focus()
    page.keyboard.press("Enter")
    page.wait_for_timeout(600)


def _studio_account_switcher_locator(page):
    return page.locator(
        'ytd-multi-page-menu-renderer[menu-style="multi-page-menu-style-type-switcher"]'
    ).or_(
        page.locator("ytd-multi-page-menu-renderer").filter(
            has=page.locator(
                "ytd-simple-menu-header-renderer yt-formatted-string",
                has_text=re.compile(r"аккаунты|accounts", re.I),
            )
        )
    )


def _studio_first_visible_locator(page, locators, *, probe_timeout_ms: int = 1_500):
    for loc in locators:
        try:
            if loc.count() > 0 and loc.first.is_visible(timeout=probe_timeout_ms):
                return loc.first
        except Exception:
            continue
    return None


def _studio_switch_account_menu_item_locators(page):
    """«Сменить аккаунт»: сначала разметка (стрелка #right-icon), потом текст."""
    return (
        page.locator(
            "ytd-compact-link-renderer:has(yt-icon#right-icon:not([hidden])) "
            "tp-yt-paper-item"
        ),
        page.locator(
            "ytd-compact-link-renderer:has(yt-icon#right-icon) a#endpoint"
        ),
        page.locator("ytd-compact-link-renderer")
        .filter(
            has=page.locator(
                "yt-formatted-string#label", has_text=_SWITCH_ACCOUNT_LABEL_RE
            )
        )
        .locator("tp-yt-paper-item"),
        page.locator("ytd-compact-link-renderer yt-formatted-string#label").filter(
            has_text=_SWITCH_ACCOUNT_LABEL_RE
        ),
        page.get_by_text(_SWITCH_ACCOUNT_LABEL_RE),
    )


def _studio_is_valid_view_channel_href(href: str) -> bool:
    url = _studio_normalize_youtube_channel_href(href)
    if not url or not _studio_is_youtube_channel_url(url):
        return False
    lower = url.lower()
    if "myaccount.google.com" in lower or "accounts.google.com" in lower:
        return False
    if "studio.youtube.com" in lower:
        return False
    if lower.rstrip("/").endswith("/account") or "/logout" in lower:
        return False
    return True


def _studio_is_google_account_settings_page(page) -> bool:
    lower = (page.url or "").lower()
    return "myaccount.google.com" in lower or "accounts.google.com" in lower


def _studio_channel_url_from_handle_text(handle_text: str) -> str:
    handle = (handle_text or "").strip().lstrip("@")
    if not handle:
        return ""
    encoded = quote(handle, safe="")
    return f"https://www.youtube.com/@{encoded}"


def _studio_channel_about_more_locators(page):
    """Кнопка «…more» / «…ещё» на странице канала — сначала по классу."""
    return (
        page.locator(
            "yt-description-preview-view-model button.ytTruncatedTextAbsoluteButton"
        ),
        page.locator("#description button.ytTruncatedTextAbsoluteButton"),
        page.locator(
            "ytd-about-channel-renderer button.ytTruncatedTextAbsoluteButton"
        ),
        page.locator("button.ytTruncatedTextAbsoluteButton"),
        page.locator('button[aria-label*="More about this channel"]'),
        page.locator('button[aria-label*="more about this channel"]'),
        page.locator('button[aria-label*="Подробнее о канале"]'),
        page.locator('button[aria-label*="подробнее о канале"]'),
        page.locator("button.ytTruncatedTextAbsoluteButton").filter(
            has_text=re.compile(r"more|ещё|еще|…|\.\.\.|подробнее", re.I)
        ),
    )


def _studio_channel_name_locators(page):
    return (
        page.locator(
            "ytd-engagement-panel-title-header-renderer yt-formatted-string#title-text"
        ),
        page.locator("yt-dynamic-text-view-model.ytd-channel-name"),
        page.locator("ytd-channel-name yt-formatted-string"),
        page.locator("#channel-name yt-formatted-string"),
        page.locator("ytd-active-account-header-renderer yt-formatted-string#account-name"),
    )


def _studio_read_locator_text(loc) -> str:
    try:
        text = (loc.inner_text(timeout=3_000) or "").strip()
        if text:
            return text
    except Exception:
        pass
    try:
        return (loc.get_attribute("title") or "").strip()
    except Exception:
        return ""


def _studio_account_switcher_visible(page) -> bool:
    try:
        switcher = _studio_account_switcher_locator(page)
        return switcher.count() > 0 and switcher.first.is_visible(timeout=400)
    except Exception:
        return False


def _studio_open_account_switcher_menu(page) -> None:
    """Профиль → «Сменить аккаунт» → меню выбора канала."""
    _studio_open_avatar_menu(page)
    try:
        page.mouse.move(8, 8)
    except Exception:
        pass

    switch_item = _studio_first_visible_locator(
        page, _studio_switch_account_menu_item_locators(page), probe_timeout_ms=3_000
    )
    if switch_item is None:
        raise YoutubeStudioError(
            "YouTube: не найден пункт «Сменить аккаунт» в меню профиля."
        )
    switch_item.click(timeout=30_000)
    page.wait_for_timeout(800)


def _studio_collect_account_switcher_channels_with_retry(
    page, *, attempts: int = 3
) -> tuple[list[tuple[int, str]], int | None]:
    """Список каналов в switcher; повтор при сбое открытия меню."""
    for attempt in range(1, attempts + 1):
        try:
            _studio_open_account_switcher_menu(page)
            accounts = _studio_collect_available_account_switcher_channels(page)
            current_idx = _studio_get_selected_account_index(page, menu_open=True)
            if accounts:
                return accounts, current_idx
            if _studio_account_switcher_all_channels_removed(page):
                _studio_abort_all_channels_removed(page)
            _log(
                f"Studio: switcher пуст (попытка {attempt}/{attempts}) — "
                "повторяем открытие меню…"
            )
        except YoutubeAllChannelsRemovedError:
            raise
        except Exception as e:
            _log(
                f"Studio: не удалось открыть «Сменить аккаунт» "
                f"(попытка {attempt}/{attempts}): {e!r}"
            )
        finally:
            _studio_dismiss_open_menus(page)
        page.wait_for_timeout(450)
    try:
        _studio_open_account_switcher_menu(page)
        if _studio_account_switcher_all_channels_removed(page):
            _studio_abort_all_channels_removed(page)
    except YoutubeAllChannelsRemovedError:
        raise
    except Exception:
        pass
    finally:
        _studio_dismiss_open_menus(page)
    return [], None


def _studio_youtube_active_channel_matches(page, expected_name: str) -> bool:
    """Текущий канал на youtube.com совпадает с ожидаемым именем."""
    expected = (expected_name or "").strip()
    if not expected:
        return False
    avatar_name = _studio_read_avatar_menu_account_name(page)
    if avatar_name and _studio_channel_names_match(expected, avatar_name):
        return True
    try:
        _studio_open_account_switcher_menu(page)
        switcher = _studio_account_switcher_locator(page)
        switcher.first.wait_for(state="visible", timeout=10_000)
        items = switcher.first.locator("ytd-account-item-renderer")
        for i in range(items.count()):
            item = items.nth(i)
            if not _studio_account_item_is_selected(item):
                continue
            if _studio_account_item_is_removed(item):
                _log("Studio: активный канал в switcher помечен как удалённый.")
                return False
            return _studio_account_item_matches_name(item, expected)
    except Exception:
        return False
    finally:
        _studio_dismiss_open_menus(page)
    return False


def _studio_click_account_switcher_channel(page, item_index: int, channel_name: str) -> None:
    switcher = _studio_account_switcher_locator(page)
    item = switcher.first.locator("ytd-account-item-renderer").nth(item_index)
    clicked = False
    for sel in (
        item.locator("tp-yt-paper-icon-item"),
        item.locator("tp-yt-paper-item"),
        item.locator('[role="option"]'),
        item,
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
        raise YoutubeStudioError(
            f"YouTube Studio: не удалось выбрать канал «{channel_name}» в меню аккаунтов."
        )


def _studio_wait_after_account_switch(page, *, timeout_s: float = 12.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _studio_channel_removed_page_visible(page):
            return
        try:
            url = (page.url or "").lower()
            if "studio.youtube.com" in url and "channel-appeal" not in url:
                return
        except Exception:
            pass
        page.wait_for_timeout(250)


def _studio_handle_channel_removed_if_present(page) -> bool:
    """
    Канал удалён при входе в Studio: профиль → сменить аккаунт → другой канал.
    После переключения возвращаемся в Studio и продолжаем сценарий.
    """
    if not _studio_channel_removed_page_visible(page):
        return False

    _log("Studio: канал удалён/заблокирован — пробуем сменить аккаунт…")
    _studio_open_account_switcher_menu(page)
    if _studio_account_switcher_all_channels_removed(page):
        _studio_abort_all_channels_removed(page)
    available = _studio_collect_available_account_switcher_channels(page)
    if not available:
        raise YoutubeStudioError(
            "YouTube Studio: все каналы в аккаунте удалены или заблокированы — "
            "сменить аккаунт на доступный канал не удалось."
        )

    switcher = _studio_account_switcher_locator(page)
    items = switcher.first.locator("ytd-account-item-renderer")

    pick: tuple[int, str] | None = None
    for idx, name in available:
        try:
            if not _studio_account_item_is_selected(items.nth(idx)):
                pick = (idx, name)
                break
        except Exception:
            pick = (idx, name)
            break
    if pick is None:
        pick = available[0]

    pick_idx, pick_name = pick
    _log(f"Studio: выбираем канал «{pick_name}» (позиция {pick_idx + 1})…")
    _studio_click_account_switcher_channel(page, pick_idx, pick_name)
    _studio_handle_channel_creation_after_account_pick(page)
    _studio_wait_after_account_switch(page)

    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    if "studio.youtube.com" not in url or "channel-appeal" in url:
        _log("Studio: после смены аккаунта — переход на https://studio.youtube.com/ …")
        page.goto(
            "https://studio.youtube.com/",
            wait_until="domcontentloaded",
            timeout=120_000,
        )
        page.wait_for_timeout(800)

    if _studio_channel_removed_page_visible(page):
        raise YoutubeStudioError(
            "YouTube Studio: после смены аккаунта всё ещё открыта страница удалённого канала."
        )

    _log("Studio: смена аккаунта выполнена — продолжаем сценарий Studio.")
    return True


def _studio_is_youtube_home_url(page) -> bool:
    url = _studio_page_url_lower(page)
    if "www.youtube.com" not in url:
        return False
    path = url.split("www.youtube.com", 1)[-1].split("?")[0].split("#")[0].strip("/")
    return path == ""


def _studio_youtube_home_page_ready(page, *, probe_timeout_ms: int = 800) -> bool:
    """True, если главная youtube.com хотя бы частично отрисовалась."""
    if not _studio_is_youtube_home_url(page):
        return False
    if _studio_on_google_auth_page(page, fast=True):
        return False
    for loc in (
        page.locator("ytd-app"),
        page.locator("ytd-browse[role='main']"),
        page.locator("ytd-masthead"),
        page.locator("#content ytd-rich-item-renderer"),
        page.locator("ytd-rich-grid-renderer"),
        page.locator("#search-input input"),
    ):
        try:
            if loc.count() > 0 and loc.first.is_visible(timeout=probe_timeout_ms):
                return True
        except Exception:
            continue
    return False


def _studio_wait_youtube_home_page(page, *, timeout_s: float = 45.0) -> bool:
    """Ждём хотя бы частичной загрузки главной youtube.com перед Studio."""
    _log("Studio: ждём загрузку главной youtube.com…")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _studio_youtube_home_page_ready(page):
            page.wait_for_timeout(500)
            _log("Studio: главная youtube.com загружена.")
            return True
        if _studio_on_google_auth_page(page):
            _log(
                "Studio: редирект на вход Google — "
                "ожидание главной youtube.com пропущено."
            )
            return False
        page.wait_for_timeout(250)
    _log(
        "Studio: таймаут ожидания главной youtube.com — "
        "переходим в Studio без полной загрузки."
    )
    return False


def _studio_goto_youtube_home(
    page, *, login_credentials=None, for_channel_scan: bool = True
) -> None:
    fast = not for_channel_scan
    _studio_wait_for_google_session(
        page, login_credentials=login_credentials, fast=fast
    )
    on_youtube = "www.youtube.com" in _studio_page_url_lower(page)
    if for_channel_scan:
        goto_reason = "для проверки каналов"
        already_reason = "проверка каналов"
    else:
        goto_reason = "перед переходом в Studio"
        already_reason = "перед переходом в Studio"
    need_goto = not on_youtube or (
        not for_channel_scan and not _studio_is_youtube_home_url(page)
    )
    if need_goto:
        _log(f"Studio: переход на https://www.youtube.com/ {goto_reason}…")
        page.goto(
            "https://www.youtube.com/",
            wait_until="domcontentloaded",
            timeout=120_000 if for_channel_scan else 60_000,
        )
        if not for_channel_scan:
            _studio_wait_youtube_home_page(page)
            return
    else:
        _log(f"Studio: уже на youtube.com — {already_reason}…")
        if not for_channel_scan:
            _studio_wait_youtube_home_page(page)
            return
    _studio_try_google_login_if_needed(page, login_credentials)
    if _studio_on_google_auth_page(page):
        _studio_wait_for_google_session(page, login_credentials=login_credentials)
    if for_channel_scan:
        avatar = _studio_avatar_locator(page)
        avatar.first.wait_for(state="visible", timeout=60_000)


def _studio_normalize_youtube_channel_href(href: str) -> str:
    raw = (href or "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        return f"https:{raw}"
    if raw.startswith("/"):
        return urljoin("https://www.youtube.com", raw)
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return urljoin("https://www.youtube.com/", raw)


def _studio_is_youtube_channel_url(url: str) -> bool:
    lower = (url or "").lower()
    return any(part in lower for part in ("/@", "/channel/", "/c/", "/user/"))


def _studio_youtube_channel_page_ready(page) -> bool:
    if not _studio_is_youtube_channel_url(page.url or ""):
        return False
    for loc in (
        page.locator("ytd-browse[role='main']"),
        page.locator("ytd-channel-name"),
        page.locator("yt-page-header-view-model"),
        page.locator("ytd-c4-tabbed-header-renderer"),
        page.locator("#channel-header"),
        page.locator("ytd-about-channel-renderer"),
    ):
        try:
            if loc.count() > 0 and loc.first.is_visible(timeout=800):
                return True
        except Exception:
            continue
    return False


def _studio_build_channel_about_url(page_url: str) -> str:
    url = (page_url or "").strip()
    if not url or not _studio_is_youtube_channel_url(url):
        return ""
    lower = url.lower()
    if lower.rstrip("/").endswith("/about"):
        return url.split("?")[0].rstrip("/")
    base = url.split("?")[0].rstrip("/")
    for sep in (
        "/videos",
        "/streams",
        "/playlists",
        "/community",
        "/channels",
        "/featured",
        "/shorts",
        "/about",
    ):
        idx = lower.find(sep)
        if idx > 0:
            base = base[:idx]
            break
    return f"{base.rstrip('/')}/about"


def _studio_goto_channel_about_page(page) -> bool:
    about_url = _studio_build_channel_about_url(page.url or "")
    if not about_url:
        return False
    _log(f"Studio: переход на страницу «О канале»: {about_url!r}")
    try:
        page.goto(about_url, wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(1_000)
        if _studio_channel_access_blocked(page):
            return False
        return _studio_youtube_channel_page_ready(page) or _studio_is_youtube_channel_url(
            page.url or ""
        )
    except Exception as e:
        _log(f"Studio: не удалось открыть /about: {e!r}")
        return False


def _studio_wait_youtube_channel_page(page, *, timeout_s: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _studio_channel_access_blocked(page):
            return False
        if _studio_youtube_channel_page_ready(page):
            page.wait_for_timeout(500)
            if _studio_channel_access_blocked(page):
                return False
            return True
        page.wait_for_timeout(350)
    return False


def _studio_wait_account_switch_or_oops(page, *, timeout_s: float = 60.0) -> bool:
    """True — аватар виден; False — oops или удалённый канал."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _studio_channel_access_blocked(page):
            return False
        try:
            avatar = _studio_avatar_locator(page)
            if avatar.count() > 0 and avatar.first.is_visible(timeout=400):
                return True
        except Exception:
            pass
        page.wait_for_timeout(450)
    return False


def _studio_profile_system_menu_locator(page):
    return page.locator(
        'ytd-multi-page-menu-renderer[menu-style="multi-page-menu-style-type-system"]'
    )


def _studio_open_profile_system_menu(page) -> None:
    """Меню профиля (не switcher «Сменить аккаунт») — только клавиатура, без клика по аватару."""
    _studio_dismiss_open_menus(page)
    page.wait_for_timeout(200)
    avatar = _studio_avatar_locator(page)
    avatar.first.wait_for(state="visible", timeout=15_000)
    avatar.first.focus()
    page.keyboard.press("Enter")
    page.wait_for_timeout(600)
    if _studio_account_switcher_visible(page):
        _studio_dismiss_open_menus(page)
        page.wait_for_timeout(300)
        avatar.first.focus()
        page.keyboard.press("Enter")
        page.wait_for_timeout(600)
    _studio_profile_system_menu_locator(page).first.wait_for(
        state="visible", timeout=10_000
    )
    try:
        page.mouse.move(8, 8)
    except Exception:
        pass


def _studio_wait_avatar_menu_header_ready(page) -> None:
    """Ждём шапку меню профиля (#manage-account / #channel-handle)."""
    _studio_profile_system_menu_locator(page).first.wait_for(
        state="visible", timeout=15_000
    )
    page.locator(
        "ytd-multi-page-menu-renderer[menu-style='multi-page-menu-style-type-system'] "
        "ytd-active-account-header-renderer"
    ).first.wait_for(state="visible", timeout=10_000)
    try:
        page.locator(
            "ytd-multi-page-menu-renderer[menu-style='multi-page-menu-style-type-system'] "
            "ytd-active-account-header-renderer #manage-account a, "
            "ytd-multi-page-menu-renderer[menu-style='multi-page-menu-style-type-system'] "
            "ytd-active-account-header-renderer yt-formatted-string#channel-handle"
        ).first.wait_for(state="attached", timeout=8_000)
    except Exception:
        pass
    page.wait_for_timeout(400)


def _studio_normalize_channel_url_candidate(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.startswith("@"):
        url = _studio_channel_url_from_handle_text(raw)
        return url if _studio_is_valid_view_channel_href(url) else ""
    if _studio_is_valid_view_channel_href(raw):
        return _studio_normalize_youtube_channel_href(raw)
    return ""


def _studio_read_view_channel_url_from_profile_menu_dom(page) -> str:
    """URL «Посмотреть канал» из #manage-account (меню может быть скрыто в DOM)."""
    try:
        raw = page.evaluate(
            """
            () => {
                const menu = document.querySelector(
                    'ytd-multi-page-menu-renderer[menu-style="multi-page-menu-style-type-system"]'
                );
                const header = menu?.querySelector('ytd-active-account-header-renderer');
                if (!header) return '';
                const link = header.querySelector('#manage-account a[href]');
                const href = (link?.href || link?.getAttribute('href') || '').trim();
                if (href && !href.includes('myaccount.google.com')) return href;
                const handle = (header.querySelector('#channel-handle')?.textContent || '').trim();
                if (handle.startsWith('@')) return handle;
                return '';
            }
            """
        )
    except Exception:
        raw = ""
    return _studio_normalize_channel_url_candidate(raw)


def _studio_read_view_channel_url_from_profile_menu(page) -> str:
    """URL «Посмотреть канал» только из #manage-account в шапке (не из пунктов меню)."""
    _studio_wait_avatar_menu_header_ready(page)
    return _studio_read_view_channel_url_from_profile_menu_dom(page)


def _studio_click_view_channel_link_in_profile_menu_js(page) -> bool:
    """JS-клик только по #manage-account a (не по «Аккаунт Google»)."""
    try:
        return bool(
            page.evaluate(
                """
                () => {
                    const menu = document.querySelector(
                        'ytd-multi-page-menu-renderer[menu-style="multi-page-menu-style-type-system"]'
                    );
                    const link = menu?.querySelector(
                        'ytd-active-account-header-renderer #manage-account a[href]'
                    );
                    if (!link) return false;
                    link.click();
                    return true;
                }
                """
            )
        )
    except Exception:
        return False


def _studio_recover_from_google_account_page(page) -> None:
    if not _studio_is_google_account_settings_page(page):
        return
    _log("Studio: на странице «Аккаунт Google» — возвращаемся на YouTube…")
    page.goto(
        "https://www.youtube.com/",
        wait_until="domcontentloaded",
        timeout=120_000,
    )
    page.wait_for_timeout(800)


def _studio_goto_channel_url(page, channel_url: str, *, log_prefix: str) -> bool:
    _log(f"Studio: {log_prefix}: {channel_url!r}")
    try:
        page.goto(channel_url, wait_until="domcontentloaded", timeout=120_000)
    except Exception as e:
        _log(f"Studio: переход на канал не удался: {e!r}")
        return False
    page.wait_for_timeout(900)
    if _studio_is_google_account_settings_page(page):
        _log("Studio: вместо канала открылась страница «Аккаунт Google».")
        _studio_recover_from_google_account_page(page)
        return False
    return True


def _studio_goto_current_channel_page(page) -> bool:
    """Профиль → «Посмотреть канал»: href из DOM / меню, переход без кликов по пунктам."""
    _studio_recover_from_google_account_page(page)
    _studio_dismiss_open_menus(page)
    page.wait_for_timeout(250)

    channel_url = _studio_read_view_channel_url_from_profile_menu_dom(page)
    if channel_url:
        if _studio_goto_channel_url(
            page, channel_url, log_prefix="переход на страницу канала (из DOM)"
        ):
            return True

    try:
        _studio_open_profile_system_menu(page)
        channel_url = _studio_read_view_channel_url_from_profile_menu(page)
    except Exception as e:
        _log(f"Studio: меню профиля / ссылка на канал недоступны: {e!r}")
        _studio_dismiss_open_menus(page)
        return False

    _studio_dismiss_open_menus(page)
    page.wait_for_timeout(450)

    if channel_url:
        if _studio_goto_channel_url(
            page, channel_url, log_prefix="переход на страницу канала"
        ):
            return True

    _log(
        "Studio: page.goto не сработал — пробуем JS-клик по #manage-account «Посмотреть канал»…"
    )
    try:
        _studio_open_profile_system_menu(page)
        if _studio_click_view_channel_link_in_profile_menu_js(page):
            page.wait_for_load_state("domcontentloaded", timeout=120_000)
            page.wait_for_timeout(900)
            _studio_dismiss_open_menus(page)
            if not _studio_is_google_account_settings_page(page):
                return True
            _studio_recover_from_google_account_page(page)
    except Exception as e:
        _log(f"Studio: JS-клик по «Посмотреть канал» не удался: {e!r}")
    finally:
        _studio_dismiss_open_menus(page)

    _log(
        "Studio: ссылка «Посмотреть канал» (#manage-account) не найдена или недоступна."
    )
    return False


def _studio_open_current_channel_from_avatar_menu(page) -> None:
    """Обратная совместимость: переход на канал без клика по пунктам меню."""
    if not _studio_goto_current_channel_page(page):
        raise YoutubeStudioError(
            "YouTube: не удалось перейти на страницу канала "
            "(#manage-account / #channel-handle)."
        )


def _studio_click_channel_about_more_button(page) -> bool:
    more = _studio_first_visible_locator(
        page, _studio_channel_about_more_locators(page), probe_timeout_ms=4_000
    )
    if more is None:
        return False
    try:
        more.scroll_into_view_if_needed(timeout=10_000)
        more.click(timeout=30_000)
        page.wait_for_timeout(700)
        return True
    except Exception as e:
        _log(f"Studio: не удалось нажать «…more» на странице канала: {e!r}")
        return False


def _studio_read_channel_page_name(page) -> str:
    title = _studio_first_visible_locator(
        page, _studio_channel_name_locators(page), probe_timeout_ms=2_000
    )
    if title is not None:
        name = _studio_read_locator_text(title)
        if name:
            return name
    return ""


def _studio_parse_join_year_from_text(text: str) -> int | None:
    body = (text or "").strip()
    if not body:
        return None
    for line in body.splitlines():
        line = line.strip()
        if not line or not _JOINED_LINE_HINT_RE.search(line):
            continue
        match = _JOINED_YEAR_CONTEXT_RE.search(line)
        if match:
            return int(match.group(1))
        years = [int(y) for y in _YEAR_IN_TEXT_RE.findall(line)]
        plausible = [y for y in years if 2005 <= y <= 2035]
        if plausible:
            return min(plausible)
    match = _JOINED_YEAR_CONTEXT_RE.search(body)
    if match:
        return int(match.group(1))
    return None


def _studio_add_channel_join_record(
    records: list[_ChannelJoinRecord], record: _ChannelJoinRecord
) -> None:
    for i, existing in enumerate(records):
        if existing.switch_index != record.switch_index:
            continue
        if record.join_year < existing.join_year:
            records[i] = record
        return
    records.append(record)


def _studio_log_channel_join_records(records: list[_ChannelJoinRecord]) -> None:
    if not records:
        return
    _log("Studio: сводка каналов по году регистрации (от старых к новым):")
    for rec in sorted(records, key=lambda r: (r.join_year, r.switch_index)):
        _log(
            f"  — «{rec.about_name or rec.switch_name}» ({rec.join_year}), "
            f"switcher #{rec.switch_index + 1}"
        )


def _studio_read_avatar_menu_account_name(page) -> str:
    """Имя канала в шапке меню профиля (#account-name)."""
    opened = False
    try:
        _studio_open_avatar_menu(page)
        opened = True
        loc = page.locator(
            "ytd-active-account-header-renderer yt-formatted-string#account-name"
        )
        if loc.count() > 0:
            return _studio_read_locator_text(loc.first)
    except Exception:
        pass
    finally:
        if opened:
            _studio_dismiss_open_menus(page)
    return ""


def _studio_channel_record_names(record: _ChannelJoinRecord) -> tuple[str, ...]:
    names: list[str] = []
    for raw in (record.switch_name, record.about_name, _studio_record_channel_name(record)):
        n = (raw or "").strip()
        if n and n not in names:
            names.append(n)
    return tuple(names)


def _studio_verify_selected_account_index(
    page, target_index: int, *, menu_open: bool = False
) -> bool:
    return (
        _studio_get_selected_account_index(page, menu_open=menu_open) == target_index
    )


def _studio_try_switch_to_account_by_name(
    page,
    channel_name: str,
    *,
    wait_timeout_s: float = 20.0,
) -> bool:
    """Переключение по имени в switcher; False при ошибке (без исключения)."""
    name = (channel_name or "").strip()
    if not name:
        return False
    try:
        _studio_open_account_switcher_menu(page)
        switcher = _studio_account_switcher_locator(page)
        switcher.first.wait_for(state="visible", timeout=15_000)
        items = switcher.first.locator("ytd-account-item-renderer")
        count = items.count()
        for i in range(count):
            item = items.nth(i)
            item_name = _studio_account_item_channel_name(item) or f"#{i + 1}"
            if not _studio_account_item_matches_name(item, name):
                continue
            if _studio_account_item_is_removed(item):
                _log(
                    f"Studio: канал «{item_name}» помечен как удалённый "
                    "(Channel removed / Канал удален) — пропускаем."
                )
                _studio_dismiss_open_menus(page)
                return False
            return _studio_try_switch_to_account_by_index(
                page,
                i,
                item_name,
                wait_timeout_s=wait_timeout_s,
                switcher_already_open=True,
            )
        _log(f"Studio: канал «{name}» не найден в меню «Сменить аккаунт».")
    except Exception as e:
        _log(f"Studio: переключение по имени «{name}» не удалось: {e!r}")
    _studio_dismiss_open_menus(page)
    return False


def _studio_switch_to_oldest_channel_record(
    page,
    best: _ChannelJoinRecord,
    *,
    login_credentials=None,
) -> bool:
    _studio_goto_youtube_home(page, login_credentials=login_credentials)
    _studio_dismiss_open_menus(page)

    target_idx = best.switch_index
    target_name = best.switch_name
    resolved: tuple[int, str] | None = None
    already_on_oldest = False
    try:
        _studio_open_account_switcher_menu(page)
        resolved = _studio_resolve_switcher_index_for_record(page, best, menu_open=True)
        if resolved is not None:
            target_idx, target_name = resolved
            if target_idx != best.switch_index or target_name != best.switch_name:
                _log(
                    f"Studio: уточнён индекс канала для переключения: "
                    f"«{target_name}» (позиция {target_idx + 1})."
                )
            if _studio_verify_selected_account_index(
                page, target_idx, menu_open=True
            ):
                already_on_oldest = True
    except Exception:
        resolved = None
    finally:
        _studio_dismiss_open_menus(page)

    if resolved is None:
        _log(
            f"Studio: канал «{best.switch_name}» (позиция {best.switch_index + 1}) "
            "не найден в switcher — пробуем переключение по имени."
        )
    elif already_on_oldest:
        avatar_name = _studio_read_avatar_menu_account_name(page)
        names = _studio_channel_record_names(best)
        if avatar_name and any(
            _studio_channel_names_match(avatar_name, cand) for cand in names
        ):
            _log("Studio: уже на самом старом канале (switcher + имя в профиле).")
            return True
        if avatar_name:
            _log(
                f"Studio: switcher #{target_idx + 1}, но в профиле «{avatar_name}» "
                f"≠ ожидаемый «{_studio_record_channel_name(best)}» — переключаемся."
            )
        else:
            _log(
                f"Studio: switcher #{target_idx + 1} выбран, имя в меню не прочитано — "
                "переключаемся для надёжности."
            )

    for attempt in range(1, 4):
        _log(f"Studio: финальное переключение на самый старый канал, попытка {attempt}/3…")
        _studio_goto_youtube_home(page, login_credentials=login_credentials)
        _studio_dismiss_open_menus(page)

        switched = False
        if resolved is not None:
            switched = _studio_try_switch_to_account_by_index(
                page, target_idx, target_name, wait_timeout_s=50.0
            )
        if not switched:
            for name in _studio_channel_record_names(best):
                _log(f"Studio: резервное переключение по имени «{name}»…")
                if _studio_try_switch_to_account_by_name(
                    page, name, wait_timeout_s=50.0
                ):
                    switched = True
                    break
        if not switched:
            _studio_recover_on_youtube_after_switch_failure(
                page, login_credentials=login_credentials
            )
            continue

        _studio_goto_youtube_home(page, login_credentials=login_credentials)
        if resolved is not None and _studio_verify_selected_account_index(
            page, target_idx
        ):
            _log(
                f"Studio: переключение на самый старый канал «{target_name}» "
                "подтверждено в switcher."
            )
            return True

        avatar_name = _studio_read_avatar_menu_account_name(page)
        names = _studio_channel_record_names(best)
        if avatar_name and any(
            _studio_channel_names_match(avatar_name, cand) for cand in names
        ):
            _log(
                f"Studio: переключение на самый старый канал «{avatar_name}» "
                "подтверждено в меню профиля."
            )
            return True

        selected = _studio_get_selected_account_index(page)
        _log(
            f"Studio: после переключения выбран канал "
            f"#{selected + 1 if selected is not None else '?'}, "
            f"ожидался #{target_idx + 1}."
        )

    _log(
        f"Studio: не удалось переключиться на самый старый канал "
        f"«{_studio_record_channel_name(best)}» — остаёмся на текущем."
    )
    _studio_recover_on_youtube_after_switch_failure(
        page, login_credentials=login_credentials
    )
    return False


def _studio_close_engagement_panel_if_open(page) -> None:
    close_btn = (
        page.locator(
            "ytd-engagement-panel-title-header-renderer #visibility-button button"
        )
        .or_(page.locator("ytd-engagement-panel-title-header-renderer button"))
        .or_(page.get_by_role("button", name=re.compile(r"close|закрыть", re.I)))
    )
    try:
        if close_btn.count() > 0 and close_btn.first.is_visible(timeout=800):
            close_btn.first.click(timeout=10_000)
            page.wait_for_timeout(400)
            return
    except Exception:
        pass
    _studio_dismiss_open_menus(page)


def _studio_collect_channel_about_text(page) -> tuple[str, str]:
    """(panel_text, joined_row_text) со страницы канала или /about."""
    panel_text = ""
    joined_row_text = ""
    for sel in (
        "ytd-about-channel-renderer #right-column yt-formatted-string",
        "ytd-about-channel-renderer .description",
        "ytd-about-channel-renderer",
        "ytd-channel-about-metadata-renderer",
        "#about-container",
    ):
        loc = page.locator(sel)
        try:
            if loc.count() > 0 and loc.first.is_visible(timeout=1_500):
                chunk = (loc.first.inner_text(timeout=5_000) or "").strip()
                if chunk:
                    if not joined_row_text and _JOINED_LINE_HINT_RE.search(chunk):
                        joined_row_text = chunk
                    if not panel_text:
                        panel_text = chunk
        except Exception:
            continue
    if not panel_text:
        try:
            panel_text = (page.locator("body").inner_text(timeout=5_000) or "").strip()
        except Exception:
            panel_text = ""
    return panel_text, joined_row_text


def _studio_read_join_info_from_about_tab(page) -> tuple[str, int | None] | None:
    channel_name = _studio_read_channel_page_name(page)
    panel_text, joined_row_text = _studio_collect_channel_about_text(page)
    join_year = _studio_parse_join_year_from_text(joined_row_text or panel_text)
    if not channel_name and panel_text:
        for line in panel_text.splitlines():
            line = line.strip()
            if line and not _JOINED_LINE_HINT_RE.search(line):
                channel_name = line
                break
    if not channel_name:
        return None
    return channel_name, join_year


def _studio_read_channel_join_info_from_about_panel(page) -> tuple[str, int | None] | None:
    channel_name = _studio_read_channel_page_name(page)

    if _studio_click_channel_about_more_button(page):
        panel_title = _studio_first_visible_locator(
            page,
            (
                page.locator(
                    "ytd-engagement-panel-title-header-renderer "
                    "yt-formatted-string#title-text"
                ),
            ),
            probe_timeout_ms=5_000,
        )
        if panel_title is not None:
            panel_name = _studio_read_locator_text(panel_title)
            if panel_name:
                channel_name = panel_name

        panel_text, joined_row_text = _studio_collect_channel_about_text(page)
        for sel in (
            "ytd-engagement-panel-section-list-renderer",
            "#engagement-panel",
            "ytd-engagement-panel-section-list-target-renderer",
        ):
            loc = page.locator(sel)
            try:
                if loc.count() > 0 and loc.first.is_visible(timeout=1_500):
                    panel_text = (loc.first.inner_text(timeout=5_000) or "").strip()
                    if panel_text:
                        break
            except Exception:
                continue

        join_year = _studio_parse_join_year_from_text(joined_row_text or panel_text)
        _studio_close_engagement_panel_if_open(page)
        if not channel_name:
            return None
        return channel_name, join_year

    _log(
        f"Studio: кнопка «…more» на странице канала не найдена (URL: {page.url!r}) — "
        "пробуем вкладку /about…"
    )
    if not _studio_goto_channel_about_page(page):
        return None
    return _studio_read_join_info_from_about_tab(page)


def _studio_read_current_channel_join_info(page) -> tuple[str, int | None] | None:
    """Аватар → канал (#manage-account / Your channel) → «…more» → имя и год."""
    if not _studio_goto_current_channel_page(page):
        return None
    if not _studio_wait_youtube_channel_page(page):
        if _studio_channel_permission_denied_page_visible(page):
            _log("Studio: нет прав на просмотр страницы канала — пропускаем.")
        elif _studio_is_google_account_settings_page(page):
            _log("Studio: открылась страница «Аккаунт Google» вместо канала — пропускаем.")
        else:
            _log(f"Studio: страница канала недоступна (URL: {page.url!r}).")
        return None
    _log(f"Studio: открыта страница канала: {page.url!r}")
    return _studio_read_channel_join_info_from_about_panel(page)


def _studio_get_selected_account_index(page, *, menu_open: bool = False) -> int | None:
    """Индекс ytd-account-item-renderer с yt-icon#selected в меню «Аккаунты»."""
    if _studio_channel_access_blocked(page):
        return None
    opened_here = False
    if not menu_open:
        try:
            _studio_open_account_switcher_menu(page)
            opened_here = True
        except Exception:
            return None
    try:
        switcher = _studio_account_switcher_locator(page)
        switcher.first.wait_for(state="visible", timeout=10_000)
        items = switcher.first.locator("ytd-account-item-renderer")
        for i in range(items.count()):
            if _studio_account_item_is_selected(items.nth(i)):
                return i
    except Exception:
        return None
    finally:
        if opened_here:
            _studio_dismiss_open_menus(page)
    return None


def _studio_wait_for_account_selection(
    page,
    *,
    target_index: int,
    timeout_s: float = 60.0,
    switcher_open: bool = False,
) -> bool:
    """Ждём, пока в switcher выбран канал с индексом target_index."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=45_000)
    except Exception:
        pass
    page.wait_for_timeout(500)

    opened_here = False
    if not switcher_open and not _studio_account_switcher_visible(page):
        try:
            _studio_open_account_switcher_menu(page)
            opened_here = True
        except Exception:
            return False

    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            if _studio_account_switcher_visible(page):
                if _studio_account_switcher_all_channels_removed(page):
                    _studio_abort_all_channels_removed(page)
            if _studio_handle_channel_creation_after_account_pick(page):
                return True
            if _studio_channel_access_blocked(page):
                return False

            if not _studio_account_switcher_visible(page):
                try:
                    _studio_open_account_switcher_menu(page)
                    opened_here = True
                except Exception:
                    page.wait_for_timeout(450)
                    continue

            selected = _studio_get_selected_account_index(page, menu_open=True)
            if selected == target_index:
                page.wait_for_timeout(400)
                return True
            page.wait_for_timeout(450)
        return False
    finally:
        if opened_here or _studio_account_switcher_visible(page):
            _studio_dismiss_open_menus(page)


def _studio_resolve_switcher_index_for_record(
    page, record: _ChannelJoinRecord, *, menu_open: bool = False
) -> tuple[int, str] | None:
    """Актуальный индекс канала в switcher (индекс из сканирования приоритетнее имени)."""
    opened_here = False
    if not menu_open:
        try:
            _studio_open_account_switcher_menu(page)
            opened_here = True
        except Exception:
            return None
    try:
        switcher = _studio_account_switcher_locator(page)
        switcher.first.wait_for(state="visible", timeout=10_000)
        items = switcher.first.locator("ytd-account-item-renderer")
        count = items.count()
        if 0 <= record.switch_index < count:
            item = items.nth(record.switch_index)
            if not _studio_account_item_is_removed(item):
                name = (
                    _studio_account_item_channel_name(item)
                    or f"#{record.switch_index + 1}"
                )
                return record.switch_index, name

        candidates = _studio_channel_record_names(record)
        for i in range(count):
            item = items.nth(i)
            if _studio_account_item_is_removed(item):
                continue
            name = _studio_account_item_channel_name(item) or f"#{i + 1}"
            if any(_studio_account_item_matches_name(item, cand) for cand in candidates):
                return i, name
    except Exception:
        return None
    finally:
        if opened_here:
            _studio_dismiss_open_menus(page)
    return None


def _studio_recover_on_youtube_after_switch_failure(
    page, *, login_credentials=None
) -> None:
    _log("Studio: oops/ошибка переключения — возвращаемся на https://www.youtube.com/ …")
    _studio_goto_youtube_home(page, login_credentials=login_credentials)


def _studio_confirm_account_switch(
    page,
    item_index: int,
    channel_name: str,
    *,
    timeout_s: float = 18.0,
) -> bool:
    """
    Подтверждение смены канала после клика в switcher.
    Без wait_for('load') и без минутного опроса меню.
    """
    deadline = time.monotonic() + timeout_s
    creation_until = time.monotonic() + 8.0
    last_avatar_check = 0.0
    while time.monotonic() < deadline:
        if _studio_channel_access_blocked(page):
            return False
        if time.monotonic() < creation_until:
            if _studio_handle_channel_creation_dialog_if_present(page):
                page.wait_for_timeout(500)
                return True
            if _studio_channel_creation_dialog_visible(page):
                page.wait_for_timeout(200)
                continue

        if not _studio_account_switcher_visible(page):
            now = time.monotonic()
            if now - last_avatar_check >= 0.5:
                last_avatar_check = now
                avatar_name = _studio_read_avatar_menu_account_name(page)
                if avatar_name and _studio_channel_names_match(avatar_name, channel_name):
                    return True
            page.wait_for_timeout(150)
            continue

        if _studio_get_selected_account_index(page, menu_open=True) == item_index:
            _studio_dismiss_open_menus(page)
            return True
        page.wait_for_timeout(200)
    return False


def _studio_try_switch_to_account_by_index(
    page,
    item_index: int,
    channel_name: str,
    *,
    wait_timeout_s: float = 20.0,
    switcher_already_open: bool = False,
) -> bool:
    """Переключение канала; False — oops, удалённый канал или таймаут (без исключения)."""
    _log(f"Studio: переключаемся на канал «{channel_name}» (позиция {item_index + 1})…")
    try:
        if not switcher_already_open:
            _studio_open_account_switcher_menu(page)
        elif not _studio_account_switcher_visible(page):
            _studio_open_account_switcher_menu(page)
        switcher = _studio_account_switcher_locator(page)
        item = switcher.first.locator("ytd-account-item-renderer").nth(item_index)
        if _studio_account_item_is_removed(item):
            _log(
                f"Studio: канал «{channel_name}» (позиция {item_index + 1}) "
                "помечен как удалённый — пропускаем."
            )
            _studio_dismiss_open_menus(page)
            return False
        _studio_click_account_switcher_channel(page, item_index, channel_name)
    except Exception as e:
        _log(f"Studio: клик по каналу «{channel_name}» не удался: {e!r}")
        _studio_dismiss_open_menus(page)
        return False

    try:
        page.wait_for_load_state("domcontentloaded", timeout=10_000)
    except Exception:
        pass
    page.wait_for_timeout(400)

    if _studio_channel_access_blocked(page):
        _studio_dismiss_open_menus(page)
        return False

    confirm_s = min(float(wait_timeout_s), 18.0)
    if not _studio_confirm_account_switch(
        page, item_index, channel_name, timeout_s=confirm_s
    ):
        _log(
            f"Studio: не подтвердили переключение на «{channel_name}» "
            f"за {confirm_s:.0f} с."
        )
        _studio_dismiss_open_menus(page)
        return False

    _log(f"Studio: канал «{channel_name}» выбран.")
    _studio_wait_after_account_switch(page)
    return True


def _studio_switch_to_account_by_index(page, item_index: int, channel_name: str) -> None:
    if not _studio_try_switch_to_account_by_index(page, item_index, channel_name):
        raise YoutubeStudioError(
            f"YouTube Studio: не дождались переключения на канал «{channel_name}»."
        )


def _studio_switch_to_account_by_name(page, channel_name: str) -> None:
    _studio_open_account_switcher_menu(page)
    switcher = _studio_account_switcher_locator(page)
    switcher.first.wait_for(state="visible", timeout=15_000)
    items = switcher.first.locator("ytd-account-item-renderer")
    count = items.count()
    for i in range(count):
        item = items.nth(i)
        name = _studio_account_item_channel_name(item) or f"#{i + 1}"
        if not _studio_account_item_matches_name(item, channel_name):
            continue
        if _studio_account_item_is_removed(item):
            _studio_dismiss_open_menus(page)
            raise YoutubeStudioError(
                f"YouTube Studio: канал «{channel_name}» помечен как удалённый."
            )
        if not _studio_try_switch_to_account_by_index(
            page, i, name, switcher_already_open=True
        ):
            raise YoutubeStudioError(
                f"YouTube Studio: не дождались переключения на канал «{channel_name}»."
            )
        return
    _studio_dismiss_open_menus(page)
    raise YoutubeStudioError(
        f"YouTube Studio: канал «{channel_name}» не найден в меню «Сменить аккаунт»."
    )


def _studio_is_current_account_index(page, item_index: int) -> bool:
    return _studio_get_selected_account_index(page) == item_index


def _studio_resolve_open_channel_switch_index(
    accounts: list[tuple[int, str]],
    *,
    current_idx: int | None,
    about_name: str,
) -> tuple[int | None, str]:
    switch_name = about_name
    if current_idx is not None:
        for idx, name in accounts:
            if idx == current_idx:
                switch_name = name
                break
        return current_idx, switch_name
    for idx, name in accounts:
        if _studio_channel_names_match(name, about_name):
            return idx, name
    return None, about_name


def _studio_probe_current_open_channel_join_info(
    page,
    accounts: list[tuple[int, str]],
    current_idx: int | None,
    records: list[_ChannelJoinRecord],
    checked_indices: set[int],
    *,
    login_credentials=None,
) -> int | None:
    """
    Главная YouTube → страница текущего открытого канала → год регистрации.
    Без переключения аккаунтов; результат попадает в records / checked_indices.
    """
    if current_idx is not None:
        _log(f"Studio: текущий канал в switcher — позиция {current_idx + 1}.")
        try:
            _studio_open_account_switcher_menu(page)
            switcher = _studio_account_switcher_locator(page)
            item = switcher.first.locator("ytd-account-item-renderer").nth(current_idx)
            if _studio_account_item_is_removed(item):
                rem_name = _studio_account_item_channel_name(item) or f"#{current_idx + 1}"
                _log(
                    f"Studio: в switcher канал «{rem_name}» помечен как удалённый — "
                    "всё равно проверяем открытый канал по странице «О канале»."
                )
        except Exception:
            pass
        finally:
            _studio_dismiss_open_menus(page)

    _log("Studio: проверяем текущий открытый канал (дата регистрации)…")
    info = _studio_read_current_channel_join_info(page)
    if _studio_channel_access_blocked(page):
        _log(
            "Studio: текущий канал недоступен (oops/нет прав/удалён) — "
            "пропускаем и проверяем другие."
        )
        if current_idx is not None:
            checked_indices.add(current_idx)
        _studio_recover_on_youtube_after_switch_failure(
            page, login_credentials=login_credentials
        )
        return current_idx

    if not info:
        return current_idx

    about_name, year = info
    switch_index, switch_name = _studio_resolve_open_channel_switch_index(
        accounts, current_idx=current_idx, about_name=about_name
    )
    if switch_index is not None:
        checked_indices.add(switch_index)
    if year is not None and switch_index is not None:
        _studio_add_channel_join_record(
            records,
            _ChannelJoinRecord(
                switch_index=switch_index,
                switch_name=switch_name,
                about_name=about_name,
                join_year=year,
            ),
        )
        _log(f"Studio: канал «{about_name}» — год регистрации {year}.")
    elif year is not None:
        _log(
            f"Studio: канал «{about_name}» — год регистрации {year}, "
            "но индекс в switcher не определён."
        )
    else:
        _log(f"Studio: канал «{about_name}» — год регистрации не определён.")
    return current_idx


def _studio_select_oldest_channel_for_upload(
    page, *, login_credentials=None, on_oldest_channel_name=None
) -> str:
    """
    Перед заливом/проверкой: сначала дата регистрации текущего открытого канала,
    затем полный обход остальных через «Сменить аккаунт» (или один канал без скана).
    """
    _log("Studio: выбор самого старого канала…")
    _studio_goto_youtube_home(page, login_credentials=login_credentials)

    records: list[_ChannelJoinRecord] = []
    checked_indices: set[int] = set()
    accounts: list[tuple[int, str]] = []
    current_idx: int | None = None

    accounts, current_idx = _studio_collect_account_switcher_channels_with_retry(page)

    current_idx = _studio_probe_current_open_channel_join_info(
        page,
        accounts,
        current_idx,
        records,
        checked_indices,
        login_credentials=login_credentials,
    )

    fast_pick = _studio_try_fast_channel_pick(
        page,
        accounts,
        current_idx=current_idx,
        on_oldest_channel_name=on_oldest_channel_name,
    )
    if fast_pick:
        return fast_pick

    if not accounts:
        if records:
            best = min(records, key=lambda rec: rec.join_year)
            _log(
                f"Studio: один канал — используем «{best.about_name}» ({best.join_year})."
            )
            _studio_switch_to_oldest_channel_record(
                page, best, login_credentials=login_credentials
            )
            return _studio_finalize_oldest_channel_name(
                _studio_record_channel_name(best),
                on_oldest_channel_name=on_oldest_channel_name,
            )
        try:
            _studio_open_account_switcher_menu(page)
            if _studio_account_switcher_all_channels_removed(page):
                _studio_abort_all_channels_removed(page)
        except YoutubeAllChannelsRemovedError:
            raise
        except Exception:
            pass
        finally:
            _studio_dismiss_open_menus(page)
        _log("Studio: «Сменить аккаунт» недоступно — продолжаем с текущим каналом.")
        return ""

    if len(accounts) == 1:
        if records:
            best = min(records, key=lambda rec: rec.join_year)
            _log(
                f"Studio: один канал — используем «{best.about_name}» ({best.join_year})."
            )
            _studio_switch_to_oldest_channel_record(
                page, best, login_credentials=login_credentials
            )
            return _studio_finalize_oldest_channel_name(
                _studio_record_channel_name(best),
                on_oldest_channel_name=on_oldest_channel_name,
            )
        return ""

    _log("Studio: полный скан — ищем самый старый по дате регистрации…")

    for idx, switch_name in accounts:
        if idx in checked_indices:
            continue

        _log(f"Studio: проверяем канал «{switch_name}» через «Сменить аккаунт»…")
        if not _studio_try_switch_to_account_by_index(page, idx, switch_name):
            _log(
                f"Studio: канал «{switch_name}» недоступен (oops/нет прав/не переключился) — "
                "возвращаемся на YouTube и пробуем следующий."
            )
            _studio_recover_on_youtube_after_switch_failure(
                page, login_credentials=login_credentials
            )
            continue

        checked_indices.add(idx)
        if _studio_channel_access_blocked(page):
            _log(
                f"Studio: канал «{switch_name}» недоступен после переключения — "
                "возвращаемся на YouTube и пробуем следующий."
            )
            _studio_recover_on_youtube_after_switch_failure(
                page, login_credentials=login_credentials
            )
            continue

        account_info = _studio_read_current_channel_join_info(page)
        if _studio_channel_access_blocked(page):
            _log(
                f"Studio: страница канала «{switch_name}» недоступна "
                "(oops/нет прав) — возвращаемся на YouTube и пробуем следующий."
            )
            _studio_recover_on_youtube_after_switch_failure(
                page, login_credentials=login_credentials
            )
            continue
        if account_info:
            about_name, year = account_info
            if year is not None:
                _studio_add_channel_join_record(
                    records,
                    _ChannelJoinRecord(
                        switch_index=idx,
                        switch_name=switch_name,
                        about_name=about_name,
                        join_year=year,
                    ),
                )
                _log(f"Studio: канал «{about_name}» — год регистрации {year}.")
            else:
                _log(f"Studio: канал «{about_name}» — год регистрации не определён.")

    if not records:
        _log(
            "Studio: год регистрации ни для одного канала не определён — "
            "продолжаем с текущим каналом."
        )
        return ""

    _studio_log_channel_join_records(records)
    best = min(records, key=lambda rec: (rec.join_year, rec.switch_index))
    _log(
        f"Studio: самый старый канал — «{_studio_record_channel_name(best)}» "
        f"({best.join_year}), переключаемся на «{best.switch_name}» "
        f"(позиция {best.switch_index + 1})…"
    )
    switched = _studio_switch_to_oldest_channel_record(
        page, best, login_credentials=login_credentials
    )
    if not switched:
        _log(
            "Studio: предупреждение — финальное переключение на самый старый канал "
            "не подтверждено; Studio может открыться на другом канале."
        )

    return _studio_finalize_oldest_channel_name(
        _studio_record_channel_name(best),
        on_oldest_channel_name=on_oldest_channel_name,
    )


def _studio_navigation_drawer_channel_name_locators(page):
    return (
        page.locator("ytcp-navigation-drawer #entity-name"),
        page.locator("#entity-label-container #entity-name"),
        page.locator("ytcp-navigation-drawer .entity-name"),
    )


def _studio_read_navigation_drawer_channel_name(
    page, *, probe_timeout_ms: int = 3_000
) -> str:
    loc = _studio_first_visible_locator(
        page,
        _studio_navigation_drawer_channel_name_locators(page),
        probe_timeout_ms=probe_timeout_ms,
    )
    if loc is None:
        return ""
    return _studio_read_locator_text(loc)


def _studio_wait_navigation_drawer_channel_name(
    page, *, total_timeout_s: float = 12.0, probe_timeout_ms: int = 3_000
) -> str:
    deadline = time.monotonic() + total_timeout_s
    while time.monotonic() < deadline:
        name = _studio_read_navigation_drawer_channel_name(
            page, probe_timeout_ms=probe_timeout_ms
        )
        if name:
            return name
        page.wait_for_timeout(350)
    return ""


def _studio_page_on_studio_home(page) -> bool:
    try:
        return "studio.youtube.com" in (page.url or "").lower()
    except Exception:
        return False


_STUDIO_CHANNEL_ID_URL_RE = re.compile(
    r"studio\.youtube\.com/channel/([A-Za-z0-9_-]+)",
    re.I,
)


def _studio_availability_url_state(url: str) -> str | None:
    """
    Состояние URL для проверки доступности Studio.
    None — ещё не готово; 'success' — /channel/{id}; 'appeal' — /channel-appeal.
    """
    raw = (url or "").strip()
    if not raw:
        return None
    lower = raw.lower().split("?")[0].split("#")[0].rstrip("/")
    if lower.endswith("/channel-appeal") or "/channel-appeal" in lower:
        return "appeal"
    match = _STUDIO_CHANNEL_ID_URL_RE.search(lower)
    if match:
        channel_id = (match.group(1) or "").strip()
        if channel_id and channel_id.lower() != "appeal":
            return "success"
    return None


def _studio_wait_for_availability_url(
    page, *, timeout_s: float = 120.0, login_credentials=None
) -> str:
    """Ждёт studio.youtube.com/channel/{id} или studio.youtube.com/channel-appeal."""
    _log(
        "Studio: ждём URL studio.youtube.com/channel/{channel_id} "
        "или studio.youtube.com/channel-appeal…"
    )
    deadline = time.monotonic() + timeout_s
    last_goto = 0.0
    while time.monotonic() < deadline:
        try:
            state = _studio_availability_url_state(page.url or "")
            if state:
                return state
        except Exception:
            pass
        if _studio_channel_removed_page_visible(page):
            return "appeal"
        if _studio_on_google_auth_page(page) or _studio_login_required(page, fast=True):
            _studio_raise_if_auth_without_credentials(page, login_credentials)
            if _studio_try_google_login_if_needed(page, login_credentials):
                continue
        now = time.monotonic()
        if now - last_goto >= 8.0 and not _studio_page_on_studio_home(page):
            last_goto = now
            try:
                page.goto(
                    "https://studio.youtube.com/",
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
            except Exception:
                pass
        page.wait_for_timeout(100)
    raise YoutubeStudioError(
        "YouTube Studio: не дождались URL канала "
        "(studio.youtube.com/channel/{channel_id}) "
        "или страницы апелляции (channel-appeal)."
    )


def _studio_try_match_expected_channel_in_studio(
    page,
    expected_name: str,
    *,
    login_credentials=None,
) -> bool:
    """На studio.youtube.com имя канала уже совпадает с yt_oldest_name."""
    expected = (expected_name or "").strip()
    if not expected:
        return False

    _studio_wait_for_google_session(
        page, login_credentials=login_credentials, fast=True
    )

    if _studio_page_on_studio_home(page) and _studio_dashboard_ready(
        page, timeout_ms=800
    ):
        _log(
            f"Studio: уже на studio.youtube.com — проверяем yt_oldest_name «{expected}»…"
        )
    else:
        if _studio_page_on_studio_home(page):
            _log(
                "Studio: studio.youtube.com в URL, но интерфейс не загружен — "
                "повторная загрузка Studio…"
            )
        else:
            _log(
                f"Studio: проверка yt_oldest_name «{expected}» на studio.youtube.com…"
            )
        _studio_warmup_youtube_then_studio(
            page, login_credentials=login_credentials
        )

    _studio_try_google_login_if_needed(page, login_credentials)
    _studio_handle_channel_removed_if_present(page)
    _studio_handle_onboarding_dialogs_if_present(page)

    current = _studio_wait_navigation_drawer_channel_name(page)
    if current and _studio_channel_names_match(expected, current):
        _log(
            f"Studio: в Studio активен yt_oldest_name «{expected}» — "
            "смена аккаунта не нужна."
        )
        return True

    if current:
        _log(
            f"Studio: в Studio канал «{current}» ≠ yt_oldest_name «{expected}»."
        )
    else:
        _log("Studio: не удалось прочитать имя канала в Studio.")
    return False


def _studio_ensure_current_channel_in_studio(
    page, *, login_credentials=None
) -> str:
    """Открыть Studio без переключения канала."""
    _log(
        "Studio: поиск старого канала отключён — "
        "текущий канал без переключения (открываем Studio)…"
    )
    if _studio_page_on_studio_home(page) and _studio_dashboard_ready(
        page, timeout_ms=800
    ):
        _log("Studio: Studio уже загружен — переключение канала не нужно.")
        return ""

    _studio_goto_studio_if_needed(
        page, login_credentials=login_credentials, quick=True
    )
    _log("Studio: Studio открыт — текущий канал без переключения.")
    return ""


def _studio_ensure_correct_studio_channel(
    page,
    *,
    yt_oldest_name: str | None = None,
    login_credentials=None,
    on_oldest_channel_name=None,
    search_oldest_channel: bool = True,
) -> str:
    """
    YouTube → выбор самого старого канала → Studio.
    Если yt_oldest_name задан и в Studio уже тот же канал — переключение не делаем.
    Иначе переключаем канал на youtube.com, затем открываем Studio и сверяем #entity-name.
    При search_oldest_channel=False — только Studio на текущем канале.
    """
    if not search_oldest_channel:
        return _studio_ensure_current_channel_in_studio(
            page, login_credentials=login_credentials
        )

    expected = (yt_oldest_name or "").strip()

    if expected and _studio_try_match_expected_channel_in_studio(
        page, expected, login_credentials=login_credentials
    ):
        return expected

    _studio_goto_youtube_home(page, login_credentials=login_credentials)

    oldest = ""
    confirmed_on_youtube = False
    if expected and _studio_youtube_active_channel_matches(page, expected):
        _log(
            f"Studio: на YouTube уже активен yt_oldest_name «{expected}» — "
            "полный скан каналов не нужен."
        )
        oldest = expected
        confirmed_on_youtube = True
    elif expected:
        _log(
            f"Studio: на YouTube канал ≠ yt_oldest_name «{expected}» — "
            f"пробуем переключиться на «{expected}»…"
        )
        if _studio_try_switch_to_account_by_name(page, expected):
            _log(
                f"Studio: переключились на yt_oldest_name «{expected}» — "
                "полный скан каналов не нужен."
            )
            oldest = expected
            confirmed_on_youtube = True
        else:
            _log(
                f"Studio: yt_oldest_name «{expected}» не найден или удалён — "
                "ищем доступный канал…"
            )
            accounts, current_idx = _studio_collect_account_switcher_channels_with_retry(
                page
            )
            only = _studio_try_select_only_available_channel(
                page,
                accounts,
                current_idx=current_idx,
                on_oldest_channel_name=on_oldest_channel_name,
            )
            if only:
                oldest = only
                confirmed_on_youtube = True
            else:
                oldest = _studio_select_oldest_channel_for_upload(
                    page,
                    login_credentials=login_credentials,
                    on_oldest_channel_name=on_oldest_channel_name,
                )
    else:
        _log("Studio: yt_oldest_name не задан — ищем самый старый канал…")
        oldest = _studio_select_oldest_channel_for_upload(
            page,
            login_credentials=login_credentials,
            on_oldest_channel_name=on_oldest_channel_name,
        )

    if not oldest and expected:
        oldest = expected

    if confirmed_on_youtube and oldest:
        return oldest

    _studio_goto_studio_if_needed(page, login_credentials=login_credentials)
    current = _studio_wait_navigation_drawer_channel_name(page)
    if oldest and current and _studio_channel_names_match(oldest, current):
        _log(
            f"Studio: канал в Studio «{current}» совпадает с выбранным «{oldest}»."
        )
        return oldest

    if expected and current and _studio_channel_names_match(expected, current):
        _log(
            f"Studio: в Studio активен yt_oldest_name «{expected}» — "
            "повторное переключение не нужно."
        )
        return expected

    if oldest and current:
        _log(
            f"Studio: канал в Studio «{current}» ≠ выбранный «{oldest}» — "
            "повторяем переключение на YouTube…"
        )
    elif oldest:
        _log(
            f"Studio: не удалось прочитать канал в боковой панели "
            f"(ожидался «{oldest}») — повторяем переключение…"
        )

    if oldest or not expected:
        oldest = _studio_select_oldest_channel_for_upload(
            page,
            login_credentials=login_credentials,
            on_oldest_channel_name=on_oldest_channel_name,
        ) or oldest
        _studio_goto_studio_if_needed(page, login_credentials=login_credentials)
        current = _studio_wait_navigation_drawer_channel_name(page)
        if oldest and current and _studio_channel_names_match(oldest, current):
            _log(
                f"Studio: после повторного переключения канал «{current}» подтверждён."
            )
        elif oldest and current:
            _log(
                f"Studio: предупреждение — в Studio «{current}», "
                f"ожидался «{oldest}»."
            )

    return oldest


def _studio_handle_channel_creation_dialog_if_present(page) -> bool:
    """
    Google-аккаунт без канала: диалог ytd-channel-creation-dialog-renderer
    («How you'll appear» / «Как вас будут видеть») после входа в Studio
    или после выбора аккаунта в меню «Сменить аккаунт» на youtube.com.
    Нажимаем «Создать канал» / «Create channel» (имя обычно предзаполнено).
    """
    if not _studio_channel_creation_dialog_visible(page):
        return False

    _log("Studio: диалог создания канала — нажимаем «Создать канал»…")
    create_btn = (
        page.locator(
            "ytd-channel-creation-dialog-renderer #create-channel-button button"
        )
        .or_(
            page.locator(
                "ytd-channel-creation-dialog-renderer "
                "ytd-button-renderer#create-channel-button button"
            )
        )
        .or_(
            page.locator(
                'ytd-channel-creation-dialog-renderer button[aria-label="Создать канал"]'
            )
        )
        .or_(
            page.locator(
                'ytd-channel-creation-dialog-renderer button[aria-label="Create channel"]'
            )
        )
        .or_(
            page.get_by_role(
                "button",
                name=re.compile(r"^создать\s+канал$|^create\s+channel$", re.I),
            )
        )
    )
    try:
        create_btn.first.wait_for(state="visible", timeout=15_000)
        create_btn.first.click(timeout=30_000)
    except Exception as e:
        raise YoutubeStudioError(
            "YouTube Studio: не удалось нажать «Создать канал» в диалоге создания канала."
        ) from e

    dlg = _studio_channel_creation_dialog_locator(page)
    try:
        dlg.first.wait_for(state="hidden", timeout=_STUDIO_UI_MS)
    except Exception:
        deadline = time.monotonic() + (_STUDIO_UI_MS / 1000.0)
        while time.monotonic() < deadline:
            if not _studio_channel_creation_dialog_visible(page):
                break
            page.wait_for_timeout(500)
        else:
            raise YoutubeStudioError(
                "YouTube Studio: диалог создания канала не закрылся после «Создать канал»."
            )

    page.wait_for_timeout(800)
    _log("Studio: диалог создания канала закрыт.")
    return True


def _studio_handle_channel_creation_after_account_pick(page) -> bool:
    """
    После клика по аккаунту в «Сменить аккаунт» канал может ещё не существовать —
    ждём диалог создания и нажимаем «Создать канал».
    """
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if _studio_handle_channel_creation_dialog_if_present(page):
            _log(
                "Studio: канал создан после выбора в меню «Сменить аккаунт» — "
                "продолжаем сценарий."
            )
            page.wait_for_timeout(500)
            return True
        if _studio_channel_creation_dialog_visible(page):
            page.wait_for_timeout(250)
            continue
        page.wait_for_timeout(200)
    return False


def _studio_warm_welcome_dialog_locator(page):
    """
    Приветствие Studio: ytcp-dialog с кнопкой #dismiss-button («Далее»)
    или legacy-элемент ytcp-warm-welcome-dialog.
    """
    by_dismiss = page.locator("ytcp-dialog").filter(
        has=page.locator(
            '#dismiss-button[label="Далее"], #dismiss-button[label="Next"]'
        )
    )
    by_title = page.locator("ytcp-dialog").filter(
        has=page.locator("p#title").filter(has_text=_WELCOME_TITLE_RE)
    )
    legacy = page.locator("ytcp-warm-welcome-dialog")
    return by_dismiss.or_(by_title).or_(legacy)


def _studio_warm_welcome_next_button_locator(page):
    dialog = _studio_warm_welcome_dialog_locator(page)
    return (
        dialog.locator("#dismiss-button button[aria-label='Далее']")
        .or_(dialog.locator("#dismiss-button button[aria-label='Next']"))
        .or_(dialog.locator("ytcp-button#dismiss-button button"))
        .or_(dialog.locator("#dismiss-button button"))
    )


def _studio_warm_welcome_dialog_visible(page) -> bool:
    try:
        btn = _studio_warm_welcome_next_button_locator(page)
        return btn.count() > 0 and btn.first.is_visible()
    except Exception:
        return False


def _studio_handle_warm_welcome_dialog_if_present(page) -> bool:
    """
    Приветствие «Добро пожаловать в Творческую студию YouTube!» — кнопка «Далее».
    Может появиться после создания канала или при первом заходе в Studio.
    Несколько шагов — нажимаем «Далее», пока окно не исчезнет.
    """
    if not _studio_warm_welcome_dialog_visible(page):
        return False

    handled = False
    for step in range(_STUDIO_WARM_WELCOME_NEXT_MAX):
        if not _studio_warm_welcome_dialog_visible(page):
            break
        handled = True
        _log(
            f"Studio: приветственное окно — нажимаем «Далее» "
            f"(шаг {step + 1}/{_STUDIO_WARM_WELCOME_NEXT_MAX})…"
        )
        next_btn = _studio_warm_welcome_next_button_locator(page)
        try:
            next_btn.first.wait_for(state="visible", timeout=15_000)
            next_btn.first.click(timeout=30_000)
        except Exception as e:
            raise YoutubeStudioError(
                "YouTube Studio: не удалось нажать «Далее» в приветственном окне."
            ) from e
        page.wait_for_timeout(600)

    if _studio_warm_welcome_dialog_visible(page):
        raise YoutubeStudioError(
            "YouTube Studio: приветственное окно не закрылось после «Далее»."
        )

    if handled:
        _log("Studio: приветственное окно закрыто.")
    return handled


def _studio_aadc_notice_dialog_locator(page):
    """
    AADC: ytcp-aadc-notice-dialog — «Видео в открытом доступе могут смотреть все», кнопка «ОК».
    """
    by_component = page.locator("ytcp-aadc-notice-dialog")
    by_got_it = page.locator("ytcp-dialog").filter(
        has=page.locator("#got-it-button")
    )
    by_heading = page.locator("ytcp-dialog").filter(
        has=page.locator("#text-heading").filter(has_text=_AADC_HEADING_RE)
    )
    return by_component.or_(by_got_it).or_(by_heading)


def _studio_aadc_notice_ok_button_locator(page):
    dialog = _studio_aadc_notice_dialog_locator(page)
    return (
        dialog.locator("#got-it-button button[aria-label='ОК']")
        .or_(dialog.locator("#got-it-button button[aria-label='OK']"))
        .or_(dialog.locator("#got-it-button button[aria-label='Got it']"))
        .or_(dialog.locator("ytcp-button#got-it-button button"))
        .or_(dialog.locator("#got-it-button button"))
    )


def _studio_aadc_notice_dialog_visible(page) -> bool:
    try:
        btn = _studio_aadc_notice_ok_button_locator(page)
        return btn.count() > 0 and btn.first.is_visible()
    except Exception:
        return False


def _studio_handle_aadc_notice_dialog_if_present(page) -> bool:
    """
    AADC: «Видео в открытом доступе могут смотреть все» — кнопка «ОК» (#got-it-button).
    Может появиться на любом этапе залива в Studio.
    """
    if not _studio_aadc_notice_dialog_visible(page):
        return False

    _log("Studio: окно AADC — нажимаем «ОК»…")
    ok_btn = _studio_aadc_notice_ok_button_locator(page)
    try:
        ok_btn.first.wait_for(state="visible", timeout=15_000)
        ok_btn.first.click(timeout=30_000)
    except Exception as e:
        raise YoutubeStudioError(
            "YouTube Studio: не удалось нажать «ОК» в окне AADC."
        ) from e

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if not _studio_aadc_notice_dialog_visible(page):
            break
        page.wait_for_timeout(300)
    else:
        raise YoutubeStudioError(
            "YouTube Studio: окно AADC не закрылось после «ОК»."
        )

    page.wait_for_timeout(400)
    _log("Studio: окно AADC закрыто.")
    return True


def _studio_handle_interrupt_dialogs_if_present(page) -> bool:
    """
    Прерывающие диалоги Studio: создание канала, приветствие «Далее», AADC «ОК».
    Могут появиться на разных этапах залива — опрашиваем и закрываем.
    """
    from zaliver.youtube_upload.google_login import handle_channel_switcher_if_present

    handled = False
    for _ in range(5):
        step_handled = False
        if _studio_handle_channel_removed_if_present(page):
            step_handled = True
        if handle_channel_switcher_if_present(page):
            step_handled = True
        if _studio_handle_channel_creation_dialog_if_present(page):
            step_handled = True
        if _studio_handle_warm_welcome_dialog_if_present(page):
            step_handled = True
        if _studio_handle_aadc_notice_dialog_if_present(page):
            step_handled = True
        if not step_handled:
            break
        handled = True
    return handled


def _studio_handle_onboarding_dialogs_if_present(page) -> bool:
    """См. ``_studio_handle_interrupt_dialogs_if_present`` (обратная совместимость)."""
    return _studio_handle_interrupt_dialogs_if_present(page)


def _studio_wait_create_or_login(
    page, create_locator, *, login_credentials=None, timeout_s: float | None = None
) -> None:
    """
    Ждём появления кнопки «Создать», но параллельно проверяем, что нас не выкинуло на логин.
    """
    try:
        if create_locator.count() > 0 and create_locator.first.is_visible(timeout=800):
            return
    except Exception:
        pass

    max_s = (_STUDIO_UI_MS / 1000.0) if timeout_s is None else float(timeout_s)
    deadline = time.monotonic() + max_s
    last_dialog_check = 0.0
    while True:
        if _studio_on_google_auth_page(page) or _studio_login_required(page):
            if _studio_try_google_login_if_needed(page, login_credentials):
                continue
            if _studio_login_required(page):
                _studio_raise_if_auth_without_credentials(page, login_credentials)
                raise YoutubeStudioError(
                    "YouTube Studio: требуется вход в Google (профиль без активной сессии). "
                    "Останавливаем залив для этого профиля."
                )
        now = time.monotonic()
        if now - last_dialog_check >= 1.0:
            last_dialog_check = now
            if _studio_handle_onboarding_dialogs_if_present(page):
                continue
        try:
            if create_locator.count() > 0 and create_locator.first.is_visible(timeout=300):
                return
        except Exception:
            pass

        if time.monotonic() >= deadline:
            break
        page.wait_for_timeout(150)

    raise YoutubeStudioError("YouTube Studio: не дождались кнопки «Создать» (таймаут).")


def resolve_latest_zaliver_video_on_disk(*, db_path: Path | None = None) -> str:
    """Путь к последнему по БД видео, для которого файл ещё есть на диске."""
    from zaliver.db.video_store import VideoStore

    store = VideoStore(db_path=db_path)
    for row in store.list_videos(500):
        try:
            p = Path(str(row.path)).expanduser()
            if p.is_file():
                _log(
                    f"Каталог Zaliver: последний файл на диске id={row.id}, "
                    f"длина пути {len(str(p))} симв."
                )
                return str(p.resolve())
        except OSError:
            continue
    raise YoutubeStudioError(
        "В каталоге обработанных видео Zaliver нет записи с существующим файлом на диске. "
        "Добавьте результат в «Готовые видео» или проверьте, что файлы не удалены."
    )


_CHANNEL_SETTINGS_NAV_RE = re.compile(
    r"настройка\s+канала|customization|channel\s+customization|customize\s+channel",
    re.I,
)
_CHANNEL_PROFILE_TAB_RE = re.compile(r"^profile$|^профиль$", re.I)


def _studio_ensure_channel_profile_tab(page) -> None:
    """Ссылки канала только на вкладке Profile / Профиль в «Настройка канала»."""
    links = page.locator(
        "ytcp-channel-editing-profile-tab ytcp-channel-links, ytcp-channel-links"
    )
    try:
        if links.count() > 0:
            links.first.wait_for(state="attached", timeout=5_000)
            _log("Studio: блок «Links» / ссылки уже на экране (вкладка Profile).")
            return
    except Exception:
        pass

    _log("Studio: переключение на вкладку Profile / Профиль…")
    tab = page.locator("tp-yt-paper-tab").filter(has_text=_CHANNEL_PROFILE_TAB_RE)
    if tab.count() == 0:
        tab = page.get_by_role("tab", name=_CHANNEL_PROFILE_TAB_RE)
    if tab.count() > 0:
        tab.first.scroll_into_view_if_needed(timeout=10_000)
        tab.first.click(timeout=15_000)
        page.wait_for_timeout(600)
    else:
        try:
            page.evaluate(
                """() => {
                    const tabs = document.querySelectorAll('tp-yt-paper-tab, ytcp-tab');
                    for (const t of tabs) {
                        const text = (t.textContent || '').trim();
                        if (/^profile$/i.test(text) || /^профиль$/i.test(text)) {
                            t.click();
                            return true;
                        }
                    }
                    return false;
                }"""
            )
            page.wait_for_timeout(600)
        except Exception:
            pass

    links.first.wait_for(state="attached", timeout=30_000)
    _log("Studio: вкладка Profile — ytcp-channel-links найден.")


def _studio_create_button_locator(page):
    return (
        page.locator('ytcp-button-shape button[aria-label="Создать"]')
        .or_(page.locator('ytcp-button-shape button[aria-label="Create"]'))
        .or_(page.get_by_role("button", name=re.compile(r"^создать$|^create$", re.I)))
    )


def _studio_create_button_visible(page, *, timeout_ms: int = 1_000) -> bool:
    if not _studio_page_on_studio_home(page):
        return False
    try:
        create = _studio_create_button_locator(page)
        return create.count() > 0 and create.first.is_visible(timeout=timeout_ms)
    except Exception:
        return False


def _studio_dashboard_ready(page, *, timeout_ms: int = 2_000) -> bool:
    """True, если Studio загрузилась (не только URL studio.youtube.com в адресной строке)."""
    if not _studio_page_on_studio_home(page):
        return False
    if _studio_on_google_auth_page(page, fast=True):
        return False
    if _studio_create_button_visible(page, timeout_ms=timeout_ms):
        return True
    probe_ms = min(timeout_ms, 600)
    return bool(
        _studio_read_navigation_drawer_channel_name(
            page, probe_timeout_ms=probe_ms
        )
    )


def _studio_warmup_youtube_then_studio(
    page, *, login_credentials=None, quick: bool = False
) -> None:
    """Открыть YouTube Studio напрямую (без предварительного захода на youtube.com)."""
    _studio_goto_studio_if_needed(
        page, login_credentials=login_credentials, quick=quick
    )


def _studio_prepare_studio_dashboard(page, *, login_credentials=None) -> None:
    """
    Studio открыт: сессия Google, диалоги онбординга/удалённого канала.
    Без ожидания кнопки «Создать» — для настройки канала и пр.
    """
    _studio_goto_studio_if_needed(page, login_credentials=login_credentials)
    _studio_handle_onboarding_dialogs_if_present(page)


def _studio_goto_studio_if_needed(
    page, *, login_credentials=None, quick: bool = False
) -> None:
    """Открыть studio.youtube.com без ожидания кнопки «Создать»."""
    auth_fast = quick
    on_studio = _studio_page_on_studio_home(page) and not _studio_on_google_auth_page(
        page, fast=auth_fast
    )
    if on_studio and _studio_dashboard_ready(
        page, timeout_ms=600 if quick else 2_000
    ):
        return
    if on_studio:
        _log(
            "Studio: studio.youtube.com в URL, но дашборд не готов — "
            "повторная загрузка Studio…"
        )
        page.goto(
            _STUDIO_HOME_URL,
            wait_until="commit" if quick else "domcontentloaded",
            timeout=30_000 if quick else 90_000,
        )
    elif not quick:
        _studio_wait_for_google_session(
            page, login_credentials=login_credentials, fast=False
        )
    if not _studio_page_on_studio_home(page):
        _log(f"Studio: переход на {_STUDIO_HOME_URL} …")
        page.goto(
            _STUDIO_HOME_URL,
            wait_until="commit" if quick else "domcontentloaded",
            timeout=30_000 if quick else 90_000,
        )
        if quick:
            _log("Studio: studio.youtube.com открыт.")
    if quick:
        if _studio_on_google_auth_page(page, fast=True):
            _studio_try_google_login_if_needed(page, login_credentials)
    elif _studio_on_google_auth_page(page) or _studio_login_required(page, fast=True):
        _studio_try_google_login_if_needed(page, login_credentials)
    _studio_handle_channel_removed_if_present(page)


def _studio_goto_studio_home_ready(page, *, login_credentials=None):
    """
    studio.youtube.com → логин / онбординг → кнопка «Создать» видна.
    Возвращает локатор кнопки «Создать».
    """
    return _studio_resolve_create_button(page, login_credentials=login_credentials)


def _studio_resolve_create_button(page, *, login_credentials=None):
    """
    Кнопка «Создать» на главной Studio. Один переход в Studio и короткий опрос.
    """
    _studio_goto_studio_if_needed(page, login_credentials=login_credentials)
    create = _studio_create_button_locator(page)
    _studio_handle_onboarding_dialogs_if_present(page)
    if _studio_create_button_visible(page, timeout_ms=4_000):
        return create

    _log("Studio: ждём кнопку «Создать»…")
    deadline = time.monotonic() + 18.0
    last_dialog_check = 0.0
    while time.monotonic() < deadline:
        if _studio_on_google_auth_page(page) or _studio_login_required(page):
            _studio_raise_if_auth_without_credentials(page, login_credentials)
            if _studio_try_google_login_if_needed(page, login_credentials):
                continue
            if _studio_login_required(page):
                raise YoutubeStudioError(
                    "YouTube Studio: требуется вход в Google (профиль без активной сессии)."
                )
        now = time.monotonic()
        if now - last_dialog_check >= 1.0:
            last_dialog_check = now
            if _studio_handle_onboarding_dialogs_if_present(page):
                if _studio_create_button_visible(page, timeout_ms=400):
                    return create
                continue
        if _studio_create_button_visible(page, timeout_ms=250):
            return create
        page.wait_for_timeout(120)

    _studio_wait_create_or_login(
        page, create, login_credentials=login_credentials, timeout_s=45.0
    )
    return create


def _studio_click_create_then_add_video(
    page,
    *,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    on_oldest_channel_name=None,
    search_oldest_channel: bool = True,
    wait_for_upload_picker: bool = True,
) -> str:
    """
    studio.youtube.com → кнопка «Создать» (ytcp-button-shape) → меню ytcp-text-menu
    → пункт «Добавить видео» (test-id=upload).
    Сессия Google должна уже быть в профиле антидетекта (без логина из Zaliver).
    """
    oldest = _studio_ensure_correct_studio_channel(
        page,
        yt_oldest_name=yt_oldest_name,
        login_credentials=login_credentials,
        on_oldest_channel_name=on_oldest_channel_name,
        search_oldest_channel=search_oldest_channel,
    )
    create = _studio_resolve_create_button(page, login_credentials=login_credentials)
    if not _studio_page_on_studio_home(page):
        raise YoutubeStudioError(
            "YouTube Studio: перед кликом «Создать» страница не studio.youtube.com."
        )
    create.first.scroll_into_view_if_needed(timeout=10_000)
    _log("Studio: клик по «Создать»…")
    create.first.click(timeout=30_000)

    _log("Studio: ожидание меню (ytcp-text-menu / paper-listbox)…")
    menu = page.locator("ytcp-text-menu tp-yt-paper-listbox[role='menu']").or_(
        page.locator('tp-yt-paper-listbox[role="menu"]')
    )
    menu.first.wait_for(state="visible", timeout=30_000)

    upload_item = (
        page.locator('ytcp-text-menu tp-yt-paper-item[test-id="upload"]')
        .or_(page.locator('tp-yt-paper-item[test-id="upload"]'))
        .or_(
            menu.first.get_by_role(
                "menuitem", name=re.compile(r"добавить видео|upload\s*video", re.I)
            )
        )
    )
    _log("Studio: клик по пункту «Добавить видео»…")
    upload_item.first.wait_for(state="visible", timeout=20_000)
    upload_item.first.click(timeout=30_000)
    page.wait_for_timeout(500)
    _log(f"Studio: после «Добавить видео» URL: {page.url!r}")
    if wait_for_upload_picker:
        _studio_wait_upload_file_picker_visible(
            page, timeout_ms=_STUDIO_UI_MS, login_credentials=login_credentials
        )
    return oldest


def _studio_upload_file_picker_locator(page):
    return page.locator(
        "ytcp-uploads-file-picker#ytcp-uploads-dialog-file-picker"
    ).or_(page.locator("ytcp-uploads-file-picker"))


def _studio_wait_upload_file_picker_visible(
    page, *, timeout_ms: int = 120_000, login_credentials=None
) -> None:
    """
    Ждём ytcp-uploads-file-picker, параллельно закрывая диалоги создания канала
    и приветствия «Далее» — те же шаги, что при заливе.
    """
    picker = _studio_upload_file_picker_locator(page)
    _log("Studio: ожидание окна загрузки видео (ytcp-uploads-file-picker)…")
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        if _studio_try_google_login_if_needed(page, login_credentials):
            continue
        if _studio_login_required(page):
            _studio_raise_if_auth_without_credentials(page, login_credentials)
            raise YoutubeStudioError(
                "YouTube Studio: требуется вход в Google (профиль без активной сессии)."
            )
        if _studio_handle_onboarding_dialogs_if_present(page):
            continue
        try:
            if picker.count() > 0 and picker.first.is_visible():
                _log("Studio: окно загрузки видео доступно.")
                return
        except Exception:
            pass
        page.wait_for_timeout(500)

    raise YoutubeStudioError(
        "YouTube Studio: не дождались окна загрузки (ytcp-uploads-file-picker). "
        "Возможны диалог создания канала, приветствие «Далее» или блокировка аккаунта."
    )


_STUDIO_CONTENTEDITABLE_TEXT_JS = (
    "(el) => (el && (el.innerText ?? el.textContent) "
    "? String(el.innerText ?? el.textContent) : '')"
)


def _studio_read_contenteditable_text(contenteditable) -> str:
    try:
        return contenteditable.first.evaluate(_STUDIO_CONTENTEDITABLE_TEXT_JS) or ""
    except Exception:
        return ""


def _studio_contenteditable_has_old_title_remnants(old_title: str, current: str) -> bool:
    cur = (current or "").strip()
    old = (old_title or "").strip()
    if not cur or not old:
        return False
    if cur == old or cur in old or old in cur:
        return True
    for i in range(len(old)):
        for j in range(i + 1, len(old) + 1):
            if old[i:j] in cur:
                return True
    return False


def _studio_clear_contenteditable_like_user(
    page,
    contenteditable,
    *,
    right_slack: int = 8,
    backspace_extra: int = 0,
) -> None:
    """
    Очистка contenteditable: читаем текст, End + запас вправо, Backspace по числу символов.
    """
    current = _studio_read_contenteditable_text(contenteditable)
    n = len(current or "")
    try:
        page.keyboard.press("End")
        for _ in range(right_slack):
            page.keyboard.press("ArrowRight")
    except Exception:
        for _ in range(n + right_slack):
            page.keyboard.press("ArrowRight")
    for _ in range(n + backspace_extra):
        page.keyboard.press("Backspace")


def _studio_clear_contenteditable_until_old_title_gone(
    page,
    contenteditable,
    *,
    old_title: str | None = None,
    right_slack: int = 8,
    backspace_extra: int = 0,
    max_attempts: int = 10,
) -> None:
    """Очищает поле, пока от старого названия не останется ни символа."""
    old = (old_title or "").strip()
    for attempt in range(1, max_attempts + 1):
        _studio_clear_contenteditable_like_user(
            page,
            contenteditable,
            right_slack=right_slack,
            backspace_extra=backspace_extra,
        )
        page.wait_for_timeout(80)
        current = _studio_read_contenteditable_text(contenteditable)
        cur = (current or "").strip()
        if not cur:
            break
        if _studio_contenteditable_has_old_title_remnants(old, current):
            _log(
                f"Studio: от старого названия {old!r} осталось {cur!r} "
                f"— повтор очистки {attempt}/{max_attempts}…"
            )
        else:
            _log(
                f"Studio: в поле «Название» остался текст {cur!r} "
                f"— повтор очистки {attempt}/{max_attempts}…"
            )
    else:
        _log(
            f"Studio: предупреждение — лимит очистки ({max_attempts} попыток) исчерпан."
        )

    remaining = (_studio_read_contenteditable_text(contenteditable) or "").strip()
    if remaining:
        _log(f"Studio: после очистки в поле «Название» осталось: {remaining!r}")
    else:
        _log("Studio: после очистки поле «Название» пустое.")


def _studio_fill_contenteditable_field(
    page,
    contenteditable,
    text: str,
    *,
    clear_first: bool = False,
    right_slack: int = 8,
) -> None:
    contenteditable.first.wait_for(state="visible", timeout=60_000)
    contenteditable.first.click(timeout=30_000)
    if clear_first:
        _studio_clear_contenteditable_like_user(
            page, contenteditable, right_slack=right_slack
        )
        page.wait_for_timeout(80)
    page.keyboard.type(text, delay=0)
    page.wait_for_timeout(150)


def _studio_navigate_to_channel_customization(
    page,
    *,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    on_oldest_channel_name=None,
    search_oldest_channel: bool = True,
) -> None:
    """Studio → «Настройка канала» (тот же путь входа, что проверка доступности)."""
    _studio_ensure_correct_studio_channel(
        page,
        yt_oldest_name=yt_oldest_name,
        login_credentials=login_credentials,
        on_oldest_channel_name=on_oldest_channel_name,
        search_oldest_channel=search_oldest_channel,
    )
    _studio_prepare_studio_dashboard(page, login_credentials=login_credentials)
    _log("Studio: переход в «Настройка канала» / Customization…")
    profile_link = page.locator('a.menu-item-link[href*="/editing/profile"]')
    if profile_link.count() > 0:
        profile_link.first.click(timeout=30_000)
    else:
        nav_item = page.locator("tp-yt-paper-icon-item").filter(
            has=page.locator(".nav-item-text", has_text=_CHANNEL_SETTINGS_NAV_RE)
        )
        nav_item.first.wait_for(state="visible", timeout=30_000)
        nav_item.first.click(timeout=30_000)

    desc_box = (
        page.locator('ytcp-social-suggestions-textbox #textbox[contenteditable="true"]')
        .or_(page.locator('#textbox[aria-label*="канале"]'))
        .or_(page.locator('#textbox[aria-label*="channel"]'))
    )
    desc_box.first.wait_for(state="visible", timeout=120_000)
    _studio_ensure_channel_profile_tab(page)
    _log("Studio: раздел «Настройка канала» загружен.")


def _studio_read_input_value(inp) -> str:
    try:
        return (inp.first.input_value(timeout=2_000) or "").strip()
    except Exception:
        try:
            return (inp.first.evaluate("(n) => String(n.value || '')") or "").strip()
        except Exception:
            return ""


def _studio_channel_links_root(page):
    return page.locator(
        "ytcp-channel-editing-profile-tab ytcp-channel-links, ytcp-channel-links"
    ).first


_LINK_TITLE_PH_RE = re.compile(r"^enter a title$|^укажите название", re.I)
_LINK_URL_PH_RE = re.compile(r"^enter a url$|^укажите url", re.I)


def _studio_channel_link_input_locators(page):
    """Поля ссылки: item → FormInput, fallback placeholder."""
    title_inp = (
        page.locator("ytcp-channel-link-item input.ytcpChannelLinkItemTitleInput")
        .or_(page.get_by_placeholder(_LINK_TITLE_PH_RE))
    ).last
    url_inp = (
        page.locator(
            "ytcp-channel-link-item "
            "input.ytcpChannelLinkItemFormInput:not(.ytcpChannelLinkItemTitleInput)"
        )
        .or_(page.get_by_placeholder(_LINK_URL_PH_RE))
    ).last
    return title_inp, url_inp


def _studio_channel_link_row_locators(page):
    """ytcp-channel-link-item + поля (последняя строка)."""
    item = page.locator("ytcp-channel-link-item").last
    title_inp, url_inp = _studio_channel_link_input_locators(page)
    return item, title_inp, url_inp


_LINKS_ROW_JS = r"""
function deepQuery(root, sel) {
  if (!root) return null;
  try {
    const hit = root.querySelector(sel);
    if (hit) return hit;
  } catch (e) {}
  const nodes = root.querySelectorAll ? root.querySelectorAll('*') : [];
  for (const node of nodes) {
    if (node.shadowRoot) {
      const found = deepQuery(node.shadowRoot, sel);
      if (found) return found;
    }
  }
  return null;
}
function deepQueryAll(root, sel) {
  const out = [];
  if (!root) return out;
  try {
    root.querySelectorAll(sel).forEach((n) => out.push(n));
  } catch (e) {}
  const nodes = root.querySelectorAll ? root.querySelectorAll('*') : [];
  for (const node of nodes) {
    if (node.shadowRoot) {
      deepQueryAll(node.shadowRoot, sel).forEach((n) => out.push(n));
    }
  }
  return out;
}
function lastLinkItem(root) {
  const items = deepQueryAll(root, 'ytcp-channel-link-item');
  return items.length ? items[items.length - 1] : null;
}
function linkItemCount(root) {
  return deepQueryAll(root, 'ytcp-channel-link-item').length;
}
function findLinkFormInputs(scope) {
  let titleInp = null;
  let urlInp = null;
  const allInputs = deepQueryAll(scope, 'input');
  for (const inp of allInputs) {
    if (!inp.classList || !inp.classList.contains('ytcpChannelLinkItemFormInput')) {
      continue;
    }
    if (inp.classList.contains('ytcpChannelLinkItemTitleInput')) {
      titleInp = inp;
    } else {
      urlInp = inp;
    }
  }
  const inputs = deepQueryAll(scope, 'input.ytcpChannelLinkItemFormInput');
  for (const inp of inputs) {
    if (inp.classList.contains('ytcpChannelLinkItemTitleInput')) {
      titleInp = inp;
    } else {
      urlInp = inp;
    }
  }
  if (!titleInp) {
    titleInp =
      deepQuery(scope, 'input.ytcpChannelLinkItemFormInput.ytcpChannelLinkItemTitleInput') ||
      deepQuery(scope, 'input.ytcpChannelLinkItemTitleInput') ||
      deepQuery(scope, 'input[placeholder="Enter a title"]') ||
      deepQuery(scope, 'input[placeholder*="title"]') ||
      deepQuery(scope, 'input[placeholder*="Title"]') ||
      deepQuery(scope, 'input[placeholder*="название"]') ||
      deepQuery(scope, 'input[placeholder*="Название"]');
  }
  if (!urlInp) {
    urlInp =
      deepQuery(scope, 'input.ytcpChannelLinkItemFormInput[placeholder="Enter a URL"]') ||
      deepQuery(scope, 'input.ytcpChannelLinkItemFormInput[placeholder*="URL"]') ||
      deepQuery(scope, 'input.ytcpChannelLinkItemFormInput[placeholder*="url"]') ||
      deepQuery(scope, 'input.ytcpChannelLinkItemFormInput:not(.ytcpChannelLinkItemTitleInput)') ||
      deepQuery(scope, 'input[placeholder="Enter a URL"]') ||
      deepQuery(scope, 'input[placeholder*="URL"]') ||
      deepQuery(scope, 'input[placeholder*="url"]') ||
      deepQuery(scope, 'input[placeholder*="ссылк"]');
  }
  return { titleInp, urlInp };
}
function linkInputsFromDocument() {
  const items = document.querySelectorAll('ytcp-channel-link-item');
  const item = items.length ? items[items.length - 1] : null;
  let titleInp =
    document.querySelector('ytcp-channel-link-item input.ytcpChannelLinkItemTitleInput') ||
    document.querySelector('input.ytcpChannelLinkItemFormInput.ytcpChannelLinkItemTitleInput') ||
    document.querySelector('input.ytcpChannelLinkItemTitleInput') ||
    document.querySelector('input[placeholder="Enter a title"]');
  let urlInp =
    document.querySelector(
      'ytcp-channel-link-item input.ytcpChannelLinkItemFormInput:not(.ytcpChannelLinkItemTitleInput)'
    ) ||
    document.querySelector('input.ytcpChannelLinkItemFormInput[placeholder="Enter a URL"]') ||
    document.querySelector('input[placeholder="Enter a URL"]');
  if (item) {
    titleInp =
      titleInp ||
      item.querySelector('input.ytcpChannelLinkItemTitleInput') ||
      item.querySelector('input.ytcpChannelLinkItemFormInput.ytcpChannelLinkItemTitleInput') ||
      item.querySelector('input[placeholder="Enter a title"]');
    urlInp =
      urlInp ||
      item.querySelector('input.ytcpChannelLinkItemFormInput:not(.ytcpChannelLinkItemTitleInput)') ||
      item.querySelector('input[placeholder="Enter a URL"]');
    if (!titleInp || !urlInp) {
      const found = findLinkFormInputs(item);
      titleInp = titleInp || found.titleInp;
      urlInp = urlInp || found.urlInp;
    }
    if (!titleInp || !urlInp) {
      const shadowFound = findLinkFormInputs(item.shadowRoot || item);
      titleInp = titleInp || shadowFound.titleInp;
      urlInp = urlInp || shadowFound.urlInp;
    }
  }
  if (!titleInp || !urlInp) {
    const all = document.querySelectorAll('input.ytcpChannelLinkItemFormInput');
    for (const inp of all) {
      if (inp.classList.contains('ytcpChannelLinkItemTitleInput')) {
        titleInp = titleInp || inp;
      } else {
        urlInp = urlInp || inp;
      }
    }
  }
  if (!titleInp || !urlInp) return null;
  let urlBox = null;
  if (urlInp.closest) {
    urlBox = urlInp.closest('.ytcpChannelLinkItemUrlContainer');
  }
  return { item, titleInp, urlInp, urlBox };
}
function linkInputs(root) {
  let inp = null;
  if (root) {
    const item = lastLinkItem(root);
    if (item) {
      ({ titleInp, urlInp } = findLinkFormInputs(item));
      if (titleInp && urlInp) {
        let urlBox = deepQuery(item, '.ytcpChannelLinkItemUrlContainer');
        if (!urlBox && urlInp.closest) {
          urlBox = urlInp.closest('.ytcpChannelLinkItemUrlContainer');
        }
        inp = { item, titleInp, urlInp, urlBox };
      }
    }
    if (!inp) {
      const fromRoot = findLinkFormInputs(root);
      if (fromRoot.titleInp && fromRoot.urlInp) {
        inp = {
          item: lastLinkItem(root),
          titleInp: fromRoot.titleInp,
          urlInp: fromRoot.urlInp,
          urlBox: null,
        };
      }
    }
  }
  return inp || linkInputsFromDocument();
}
function setInput(node, value) {
  if (!node) return false;
  node.focus();
  node.click();
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  setter.call(node, value);
  node.dispatchEvent(new InputEvent('input', {
    bubbles: true,
    composed: true,
    inputType: 'insertFromPaste',
    data: value,
  }));
  node.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
  node.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, composed: true }));
  return true;
}
"""


def _studio_links_page_eval(body: str, *, params: str = "") -> str:
    """Один callback для page.evaluate — helpers внутри функции, не снаружи."""
    if params:
        return f"({params}) => {{\n{_LINKS_ROW_JS}\n{body}\n}}"
    return f"() => {{\n{_LINKS_ROW_JS}\n{body}\n}}"


def _studio_page_link_diag(page) -> dict:
    """Состояние строки ссылки через page.evaluate (не element.handle)."""
    try:
        state = page.evaluate(
            _studio_links_page_eval(
                """
  const root =
    document.querySelector('ytcp-channel-editing-profile-tab ytcp-channel-links') ||
    document.querySelector('ytcp-channel-links');
  const itemsAll = document.querySelectorAll('ytcp-channel-link-item').length;
  const inp = linkInputsFromDocument() || (root ? linkInputs(root) : null);
  if (!inp) {
    return {
      hasRoot: !!root,
      items: itemsAll,
      ready: false,
      title: '',
      url: '',
      urlError: false,
      href: location.href,
    };
  }
  return {
    hasRoot: !!root,
    items: itemsAll || linkItemCount(root || document.body),
    ready: true,
    title: String(inp.titleInp.value || ''),
    url: String(inp.urlInp.value || ''),
    urlError: !!(inp.urlBox && inp.urlBox.getAttribute('label-icon-style') === 'error'),
    href: location.href,
  };
"""
            )
        )
        return state if isinstance(state, dict) else {"error": "bad_result"}
    except Exception as exc:
        return {"error": str(exc)}


def _studio_link_row_state(page) -> dict:
    return _studio_page_link_diag(page)


def _studio_wait_channel_link_row(
    page,
    links_root,
    *,
    timeout_s: float = 30.0,
) -> tuple[object, object]:
    """Ждём ytcp-channel-link-item + FormInput (page JS и/или Playwright)."""
    title_loc, url_loc = _studio_channel_link_input_locators(page)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        st = _studio_link_row_state(page)
        if st.get("ready"):
            return title_loc, url_loc
        if st.get("items", 0) > 0 and not st.get("error"):
            return title_loc, url_loc
        try:
            tc = title_loc.count()
            uc = url_loc.count()
            if tc > 0 and uc > 0:
                return title_loc, url_loc
        except Exception:
            pass
        page.wait_for_timeout(200)
    st = _studio_link_row_state(page)
    try:
        tc = title_loc.count()
        uc = url_loc.count()
    except Exception:
        tc, uc = -1, -1
    raise YoutubeStudioError(
        "YouTube Studio: не найдены поля ссылки канала после «Add link». "
        f"Диагностика: js={st!r}, playwright_title={tc}, playwright_url={uc}"
    )


def _studio_scroll_channel_links_into_view(page, links_root) -> None:
    links_root.wait_for(state="attached", timeout=30_000)
    links_root.scroll_into_view_if_needed(timeout=15_000)
    try:
        links_root.locator(".YtcpChannelLinksSectionLabel").first.scroll_into_view_if_needed(
            timeout=10_000
        )
    except Exception:
        pass
    page.wait_for_timeout(200)


def _studio_commit_channel_link_form(page, links_root) -> None:
    """Снять фокус с полей ссылки — Studio включает Publish после blur."""
    try:
        page.evaluate(
            _studio_links_page_eval(
                """
  const inp = linkInputsFromDocument();
  if (inp) {
    inp.urlInp.blur();
    inp.titleInp.blur();
  }
  if (document.activeElement && document.activeElement.blur) {
    document.activeElement.blur();
  }
  const label = document.querySelector('.YtcpChannelLinksSectionLabel');
  if (label) label.click();
"""
            )
        )
    except Exception:
        pass
    try:
        links_root.locator(".YtcpChannelLinksSectionLabel").first.click(timeout=2_000)
    except Exception:
        pass
    page.wait_for_timeout(150)


def _studio_link_fields_filled(page) -> bool:
    st = _studio_link_row_state(page)
    return bool((st.get("title") or "").strip() and (st.get("url") or "").strip())


def _studio_normalize_channel_link_url(raw: str) -> str:
    u = (raw or "").strip()
    if not u:
        return u
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u.lstrip("/")
    parsed = urlparse(u)
    if not parsed.netloc:
        raise YoutubeStudioError(f"Некорректный URL ссылки: {raw!r}")
    return u


def _studio_remove_all_channel_links(page, links_root) -> None:
    """Удалить все строки ссылок (чтобы не оставаться в состоянии ошибки)."""
    for _ in range(8):
        try:
            removed = page.evaluate(
                _studio_links_page_eval(
                    """
  const items = document.querySelectorAll('ytcp-channel-link-item');
  if (!items.length) return false;
  const item = items[items.length - 1];
  const btn =
    item.querySelector('ytcp-icon-button.ytcpChannelLinkItemDeleteButton') ||
    item.querySelector('ytcp-icon-button[aria-label="Remove"]') ||
    item.querySelector('ytcp-icon-button[aria-label="Delete"]') ||
    item.querySelector('ytcp-icon-button[aria-label="Удалить"]') ||
    deepQuery(item, 'ytcp-icon-button.ytcpChannelLinkItemDeleteButton') ||
    deepQuery(item, 'ytcp-icon-button[aria-label="Remove"]');
  if (!btn) return false;
  btn.click();
  return true;
"""
                )
            )
            if removed:
                page.wait_for_timeout(450)
                continue
        except Exception:
            pass
        delete_btns = links_root.locator(
            "ytcp-channel-link-item ytcp-icon-button.ytcpChannelLinkItemDeleteButton"
        ).or_(
            links_root.locator(
                'ytcp-channel-link-item ytcp-icon-button[aria-label="Remove"], '
                'ytcp-channel-link-item ytcp-icon-button[aria-label="Delete"], '
                'ytcp-channel-link-item ytcp-icon-button[aria-label="Удалить"]'
            )
        )
        try:
            if delete_btns.count() == 0:
                break
            delete_btns.first.scroll_into_view_if_needed(timeout=5_000)
            delete_btns.first.click(timeout=5_000)
            page.wait_for_timeout(450)
        except Exception:
            break
        st = _studio_link_row_state(page)
        if st.get("items", 0) == 0:
            break


def _studio_click_add_channel_link(page, links_root) -> None:
    """Клик «Add link» только если строки ещё нет."""
    st = _studio_link_row_state(page)
    if st.get("items", 0) > 0:
        _log(
            "Studio: «Add link» не нужен — ytcp-channel-link-item уже на экране "
            f"(ready={st.get('ready')}, urlError={st.get('urlError')})."
        )
        return
    try:
        clicked = page.evaluate(
            _studio_links_page_eval(
                """
  const root =
    document.querySelector('ytcp-channel-editing-profile-tab ytcp-channel-links') ||
    document.querySelector('ytcp-channel-links');
  const btn =
    (root && deepQuery(root, 'button[aria-label="Add link"]:not([disabled])')) ||
    (root && deepQuery(root, 'button[aria-label="Добавить ссылку"]:not([disabled])')) ||
    (root && deepQuery(root, '.YtcpChannelLinksAddLinkButton button:not([disabled])')) ||
    document.querySelector('button[aria-label="Add link"]:not([disabled])') ||
    document.querySelector('.YtcpChannelLinksAddLinkButton button:not([disabled])');
  if (!btn) return false;
  btn.scrollIntoView({ block: 'center', inline: 'nearest' });
  btn.click();
  return true;
"""
            )
        )
        if clicked:
            page.wait_for_timeout(800)
            return
    except Exception:
        pass
    add_link = links_root.locator(
        'button[aria-label="Add link"]:not([disabled]), '
        'button[aria-label="Добавить ссылку"]:not([disabled])'
    )
    if add_link.count() == 0:
        add_link = links_root.locator(".YtcpChannelLinksAddLinkButton button:not([disabled])")
    if add_link.count() == 0:
        st2 = _studio_link_row_state(page)
        if st2.get("items", 0) > 0:
            _log("Studio: «Add link» disabled, но строка ссылки уже есть — продолжаем.")
            return
        raise YoutubeStudioError(
            "YouTube Studio: кнопка «Add link» недоступна и строка ссылки не открыта. "
            "Возможно, уже есть строка с ошибкой — удалите её вручную (Remove)."
        )
    add_link.first.wait_for(state="attached", timeout=30_000)
    add_link.first.scroll_into_view_if_needed(timeout=10_000)
    if not add_link.first.is_enabled():
        st2 = _studio_link_row_state(page)
        if st2.get("items", 0) > 0 and st2.get("ready"):
            return
        raise YoutubeStudioError(
            "YouTube Studio: кнопка «Add link» / «Добавить ссылку» недоступна."
        )
    add_link.first.click(timeout=30_000)
    page.wait_for_timeout(800)


def _studio_fill_channel_link_row_js(
    page,
    *,
    link_title: str,
    link_url: str,
) -> dict:
    lt = (link_title or "").strip()
    lu = _studio_normalize_channel_link_url(link_url)
    try:
        result = page.evaluate(
            _studio_links_page_eval(
                """
  const [titleText, urlText] = args;
  const root =
    document.querySelector('ytcp-channel-editing-profile-tab ytcp-channel-links') ||
    document.querySelector('ytcp-channel-links');
  const inp = linkInputsFromDocument() || (root ? linkInputs(root) : null);
  if (!inp) return { ok: false, reason: 'no_inputs' };
  setInput(inp.urlInp, urlText);
  inp.urlInp.blur();
  setInput(inp.titleInp, titleText);
  inp.titleInp.blur();
  const label = document.querySelector('.YtcpChannelLinksSectionLabel');
  if (label) label.click();
  const urlError = !!(inp.urlBox && inp.urlBox.getAttribute('label-icon-style') === 'error');
  return {
    ok: true,
    title: String(inp.titleInp.value || ''),
    url: String(inp.urlInp.value || ''),
    urlError,
  };
""",
                params="args",
            ),
            [lt, lu],
        )
        return result if isinstance(result, dict) else {"ok": False, "reason": "bad_result"}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


def _studio_focus_link_input_for_keyboard(page, field: str) -> bool:
    try:
        return bool(
            page.evaluate(
                _studio_links_page_eval(
                    """
  const inp = linkInputsFromDocument();
  if (!inp) return false;
  const node = field === 'url' ? inp.urlInp : inp.titleInp;
  node.focus();
  node.click();
  node.select();
  return true;
""",
                    params="field",
                ),
                field,
            )
        )
    except Exception:
        return False


def _studio_keyboard_type_link_field(
    page,
    field: str,
    text: str,
    *,
    field_label: str,
) -> None:
    t = (text or "").strip()
    if not t:
        return
    if not _studio_focus_link_input_for_keyboard(page, field):
        raise YoutubeStudioError(f"Studio: не удалось сфокусировать {field_label}.")
    page.wait_for_timeout(100)
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    page.keyboard.insert_text(t)
    page.wait_for_timeout(250)
    st = _studio_link_row_state(page)
    actual = (st.get("url") if field == "url" else st.get("title")) or ""
    if not actual.strip():
        raise YoutubeStudioError(
            f"Studio: поле {field_label} пустое после ввода с клавиатуры."
        )
    _log(f"Studio: {field_label} = {actual.strip()!r} (keyboard)")


def _studio_type_link_input_playwright(
    page,
    inp,
    text: str,
    *,
    field_label: str,
) -> str:
    """Ввод в input.ytcpChannelLinkItemFormInput через Playwright."""
    t = (text or "").strip()
    if not t:
        return ""
    el = inp.last if hasattr(inp, "last") else inp
    el.wait_for(state="attached", timeout=15_000)
    el.scroll_into_view_if_needed(timeout=10_000)
    el.click(timeout=10_000, force=True)
    page.wait_for_timeout(120)
    try:
        el.evaluate("(node) => { node.focus(); node.click(); node.select(); }")
    except Exception:
        pass
    page.wait_for_timeout(80)
    try:
        el.press("Control+A")
        el.press("Backspace")
    except Exception:
        pass
    page.wait_for_timeout(50)
    el.press_sequentially(t, delay=18)
    page.wait_for_timeout(200)
    actual = (el.input_value(timeout=3_000) or "").strip()
    if not actual:
        try:
            el.fill(t, force=True, timeout=10_000)
            page.wait_for_timeout(150)
            actual = (el.input_value(timeout=3_000) or "").strip()
        except Exception:
            pass
    if not actual:
        actual = _studio_read_input_value(inp)
    if not actual:
        raise YoutubeStudioError(
            f"Studio: поле {field_label} пустое после ввода (FormInput)."
        )
    _log(f"Studio: {field_label} = {actual!r}")
    return actual


def _studio_read_link_fields_playwright(title_loc, url_loc) -> tuple[str, str]:
    title_val = _studio_read_input_value(title_loc)
    url_val = _studio_read_input_value(url_loc)
    return title_val, url_val


def _studio_wait_channel_link_row_valid(
    page,
    links_root,
    title_loc,
    url_loc,
    *,
    timeout_s: float = 3.0,
) -> None:
    """Краткая проверка: оба поля заполнены."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        st = _studio_link_row_state(page)
        title_val = (st.get("title") or "").strip()
        url_val = (st.get("url") or "").strip()
        if title_val and url_val:
            return
        pt, pu = _studio_read_link_fields_playwright(title_loc, url_loc)
        if pt and pu:
            return
        page.wait_for_timeout(100)
    st = _studio_link_row_state(page)
    pt, pu = _studio_read_link_fields_playwright(title_loc, url_loc)
    raise YoutubeStudioError(
        "YouTube Studio: ссылка не прошла проверку "
        f"(js_title={st.get('title')!r}, js_url={st.get('url')!r}, "
        f"pw_title={pt!r}, pw_url={pu!r})."
    )


def _studio_fill_channel_link_fields(
    page,
    links_root,
    title_loc,
    url_loc,
    *,
    link_title: str,
    link_url: str,
) -> None:
    lt = (link_title or "").strip()
    lu = _studio_normalize_channel_link_url(link_url)

    js_result = _studio_fill_channel_link_row_js(
        page, link_title=lt, link_url=lu
    )
    filled = bool(
        js_result.get("ok")
        and (js_result.get("url") or "").strip()
        and (js_result.get("title") or "").strip()
    )
    if not filled:
        try:
            _studio_type_link_input_playwright(
                page, url_loc, lu, field_label="URL ссылки"
            )
        except YoutubeStudioError:
            _studio_keyboard_type_link_field(
                page, "url", lu, field_label="URL ссылки"
            )
        try:
            _studio_type_link_input_playwright(
                page, title_loc, lt, field_label="название ссылки"
            )
        except YoutubeStudioError:
            _studio_keyboard_type_link_field(
                page, "title", lt, field_label="название ссылки"
            )

    _studio_commit_channel_link_form(page, links_root)
    if not _studio_link_fields_filled(page):
        _studio_fill_channel_link_row_js(page, link_title=lt, link_url=lu)
        _studio_commit_channel_link_form(page, links_root)
    _studio_wait_channel_link_row_valid(
        page, links_root, title_loc, url_loc, timeout_s=3.0
    )


def _studio_open_channel_link_row(page, links_root) -> tuple[object, object]:
    """Открыть пустую строку ссылки или использовать уже открытую."""
    _studio_ensure_channel_profile_tab(page)
    _studio_scroll_channel_links_into_view(page, links_root)

    st = _studio_link_row_state(page)
    if st.get("items", 0) > 0:
        if st.get("ready"):
            title_v = (st.get("title") or "").strip()
            url_v = (st.get("url") or "").strip()
            if not title_v and not url_v:
                _log(
                    "Studio: используем открытую пустую строку ссылки "
                    f"(urlError={st.get('urlError')} — нормально до ввода URL)."
                )
                return _studio_channel_link_input_locators(page)
            _log(
                "Studio: удаляем заполненную строку ссылки "
                f"(title={title_v!r}, url={url_v!r})…"
            )
            _studio_remove_all_channel_links(page, links_root)
            page.wait_for_timeout(300)
        else:
            _log(
                "Studio: ytcp-channel-link-item уже есть "
                f"(items={st.get('items')}) — заполняем без «Add link»."
            )
            return _studio_wait_channel_link_row(page, links_root, timeout_s=15.0)

    _log("Studio: клик «Add link»…")
    _studio_click_add_channel_link(page, links_root)
    return _studio_wait_channel_link_row(page, links_root, timeout_s=30.0)


_CHANNEL_CUSTOMIZATION_PUBLISH_WAIT_MS = 10_000


def _studio_channel_customization_publish_state(page) -> dict:
    try:
        return page.evaluate(
            """
() => {
  const host = document.querySelector('#publish-button');
  if (!host) return { found: false, enabled: false };
  let btn = host.querySelector('button');
  if (!btn && host.shadowRoot) {
    btn = host.shadowRoot.querySelector('button');
  }
  if (!btn) return { found: false, enabled: false };
  return { found: true, enabled: !btn.disabled };
}
"""
        )
    except Exception as exc:
        return {"found": False, "enabled": False, "error": str(exc)}


def _studio_channel_customization_publish_still_enabled(page) -> bool:
    pub = _studio_channel_customization_publish_state(page)
    return bool(pub.get("found") and pub.get("enabled"))


def _studio_click_channel_customization_publish(page) -> bool:
    """Publish на странице «Настройка канала». Возвращает True, если кнопка стала неактивной."""
    _studio_handle_interrupt_dialogs_if_present(page)
    _log("Studio: публикация настроек канала…")

    def _click_publish() -> bool:
        try:
            return bool(
                page.evaluate(
                    """
() => {
  const host = document.querySelector('#publish-button');
  if (!host) return false;
  let btn = host.querySelector('button:not([disabled])');
  if (!btn && host.shadowRoot) {
    btn = host.shadowRoot.querySelector('button:not([disabled])');
  }
  if (!btn) return false;
  btn.scrollIntoView({ block: 'center', inline: 'nearest' });
  btn.click();
  return true;
}
"""
                )
            )
        except Exception:
            return False

    try:
        page.evaluate(
            "() => { document.activeElement && document.activeElement.blur && document.activeElement.blur(); }"
        )
    except Exception:
        pass

    pub = _studio_channel_customization_publish_state(page)
    if pub.get("found") and pub.get("enabled") and _click_publish():
        _log("Studio: ожидание 10 с после «Опубликовать»…")
        page.wait_for_timeout(_CHANNEL_CUSTOMIZATION_PUBLISH_WAIT_MS)
        still_enabled = _studio_channel_customization_publish_still_enabled(page)
        if still_enabled:
            _log("Studio: кнопка «Опубликовать» всё ещё активна.")
        else:
            _log("Studio: настройки канала опубликованы.")
        return not still_enabled

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        pub = _studio_channel_customization_publish_state(page)
        if pub.get("found") and pub.get("enabled") and _click_publish():
            _log("Studio: ожидание 10 с после «Опубликовать»…")
            page.wait_for_timeout(_CHANNEL_CUSTOMIZATION_PUBLISH_WAIT_MS)
            still_enabled = _studio_channel_customization_publish_still_enabled(page)
            if still_enabled:
                _log("Studio: кнопка «Опубликовать» всё ещё активна.")
            else:
                _log("Studio: настройки канала опубликованы.")
            return not still_enabled

        try:
            btn = page.locator("#publish-button button").first
            if btn.is_enabled():
                btn.scroll_into_view_if_needed(timeout=2_000)
                btn.click(timeout=10_000)
                _log("Studio: ожидание 10 с после «Опубликовать»…")
                page.wait_for_timeout(_CHANNEL_CUSTOMIZATION_PUBLISH_WAIT_MS)
                still_enabled = _studio_channel_customization_publish_still_enabled(page)
                if still_enabled:
                    _log("Studio: кнопка «Опубликовать» всё ещё активна.")
                else:
                    _log("Studio: настройки канала опубликованы.")
                return not still_enabled
        except Exception:
            pass

        page.wait_for_timeout(150)

    pub = _studio_channel_customization_publish_state(page)
    st = _studio_link_row_state(page)
    raise YoutubeStudioError(
        "YouTube Studio: кнопка Publish недоступна — проверьте ошибки на странице "
        f"(publish={pub!r}, link urlError={st.get('urlError')}, "
        f"title={st.get('title')!r}, url={st.get('url')!r})."
    )


def _studio_fill_channel_description_and_link(
    page,
    *,
    description: str | None,
    link_title: str | None,
    link_url: str | None,
) -> None:
    d = (description or "").strip()
    lt = (link_title or "").strip()
    lu = (link_url or "").strip()
    if not d and not (lt and lu):
        raise YoutubeStudioError(
            "Укажите описание канала и/или пару «название ссылки + URL»."
        )
    if (lt and not lu) or (lu and not lt):
        raise YoutubeStudioError("Название ссылки и URL нужно указать оба.")

    _studio_handle_interrupt_dialogs_if_present(page)

    desc_box = (
        page.locator('ytcp-social-suggestions-textbox #textbox[contenteditable="true"]')
        .or_(page.locator('#textbox[aria-label*="канале"]'))
        .or_(page.locator('#textbox[aria-label*="channel"]'))
    )

    if d:
        _log("Studio: заполнение «Описание канала»…")
        _studio_fill_contenteditable_field(page, desc_box, d, clear_first=True)
        try:
            links_root_preview = _studio_channel_links_root(page)
            _studio_scroll_channel_links_into_view(page, links_root_preview)
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
        page.wait_for_timeout(250)

    if lt and lu:
        _log("Studio: добавление ссылки на канал…")
        links_root = _studio_channel_links_root(page)
        title_loc, url_loc = _studio_open_channel_link_row(page, links_root)
        _studio_fill_channel_link_fields(
            page,
            links_root,
            title_loc,
            url_loc,
            link_title=lt,
            link_url=lu,
        )

    _studio_click_channel_customization_publish(page)


@_studio_entrypoint
def run_studio_channel_description_and_link(
    page,
    *,
    description: str | None = None,
    link_title: str | None = None,
    link_url: str | None = None,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    on_oldest_channel_name=None,
    search_oldest_channel: bool = True,
    profile_id: str | None = None,
) -> None:
    """Studio → «Настройка канала» → описание, ссылка → «Опубликовать»."""
    _studio_navigate_to_channel_customization(
        page,
        login_credentials=login_credentials,
        yt_oldest_name=yt_oldest_name,
        on_oldest_channel_name=on_oldest_channel_name,
        search_oldest_channel=search_oldest_channel,
    )
    _studio_fill_channel_description_and_link(
        page,
        description=description,
        link_title=link_title,
        link_url=link_url,
    )


def _studio_channel_name_input(page):
    return page.locator(
        "input.ytcpChannelEditingChannelNameBrandNameInput, "
        "ytcp-channel-editing-channel-name input.ytcpChannelEditingChannelNameFormInput"
    ).first


def _studio_channel_handle_input(page):
    return page.locator("input.YtcpChannelEditingChannelHandleHandleInput").first


def _studio_fill_plain_input(page, inp, text: str, *, label: str) -> None:
    value = (text or "").strip()
    if not value:
        return
    field = inp.first if hasattr(inp, "first") else inp
    field.wait_for(state="visible", timeout=60_000)
    field.scroll_into_view_if_needed(timeout=15_000)
    field.click(timeout=15_000)
    page.wait_for_timeout(120)
    try:
        field.fill("")
    except Exception:
        pass
    try:
        field.press("Control+A")
        field.press("Backspace")
    except Exception:
        pass
    page.wait_for_timeout(80)
    field.press_sequentially(value, delay=12)
    page.wait_for_timeout(350)
    _log(f"Studio: {label} = {value!r}")


def _studio_channel_handle_error_text(page) -> str:
    try:
        return str(
            page.evaluate(
                """
() => {
  const host = document.querySelector('ytcp-channel-editing-channel-handle');
  if (!host) return '';
  const info = host.querySelector('.YtcpChannelEditingChannelHandleSupplementaryInfo');
  if (info) {
    const msg = info.querySelector('ytcp-msg');
    const msgText = (msg?.textContent || info.textContent || '').trim();
    if (/isn't available|not available|недоступн/i.test(msgText)) {
      return msgText;
    }
    if (info.querySelector('.YtcpChannelEditingChannelHandleSuggestedHandleAnchor, ytcp-anchor.YtcpChannelEditingChannelHandleSuggestedHandleAnchor')) {
      return msgText || 'handle unavailable';
    }
  }
  const tips = host.querySelectorAll('ytcp-form-error-tip');
  for (const tip of tips) {
    if (tip.hidden) continue;
    const message = (tip.querySelector('#message')?.textContent || '').trim();
    if (message) return message;
  }
  const indicator = host.querySelector(
    '.YtcpChannelEditingChannelHandleValidityIndicatorContainer'
  );
  if (indicator) {
    const cls = indicator.className || '';
    if (/invalid|error/i.test(cls)) {
      return (indicator.textContent || '').trim() || 'handle invalid';
    }
  }
  return '';
}
"""
            )
            or ""
        ).strip()
    except Exception:
        return ""


def _studio_channel_handle_suggested_locator(page):
    return page.locator(
        "ytcp-channel-editing-channel-handle "
        "ytcp-anchor.YtcpChannelEditingChannelHandleSuggestedHandleAnchor"
    ).first


def _studio_click_suggested_channel_handle_js(page) -> str:
    return str(
        page.evaluate(
            """
() => {
  const host = document.querySelector('ytcp-channel-editing-channel-handle');
  if (!host) return '';
  const anchorHost = host.querySelector(
    'ytcp-anchor.YtcpChannelEditingChannelHandleSuggestedHandleAnchor, '
    + '.YtcpChannelEditingChannelHandleSuggestedHandleAnchor'
  );
  if (!anchorHost) return '';
  const roots = [anchorHost, anchorHost.shadowRoot].filter(Boolean);
  for (const root of roots) {
    const link = root.querySelector?.('a#anchor, a');
    const target = link || anchorHost;
    const text = (target.textContent || anchorHost.textContent || '').trim();
    target.scrollIntoView({ block: 'center', inline: 'nearest' });
    target.click();
    return text;
  }
  anchorHost.click();
  return (anchorHost.textContent || '').trim();
}
"""
        )
        or ""
    ).strip()


def _studio_click_suggested_channel_handle(page) -> bool:
    locator = _studio_channel_handle_suggested_locator(page)
    try:
        locator.wait_for(state="visible", timeout=5_000)
        link = locator.locator("a#anchor").first
        target = link if link.count() > 0 else locator
        target.scroll_into_view_if_needed(timeout=5_000)
        try:
            target.click(timeout=5_000)
        except Exception:
            locator.evaluate(
                """(node) => {
                  const roots = [node, node.shadowRoot].filter(Boolean);
                  for (const root of roots) {
                    const link = root.querySelector?.('a#anchor, a');
                    (link || node).click();
                    return;
                  }
                  node.click();
                }"""
            )
        text = ""
        try:
            text = (locator.inner_text(timeout=2_000) or "").strip()
        except Exception:
            pass
        if text:
            _log(f"Studio: выбран предложенный handle {text!r}.")
        else:
            _log("Studio: нажата подсказка handle (YtcpChannelEditingChannelHandleSuggestedHandleAnchor).")
        page.wait_for_timeout(500)
        return True
    except Exception as exc:
        _log(f"Studio: клик по подсказке handle (locator): {exc!r}")

    try:
        clicked = _studio_click_suggested_channel_handle_js(page)
    except Exception as exc:
        _log(f"Studio: клик по подсказке handle (js): {exc!r}")
        clicked = ""
    if clicked:
        _log(f"Studio: выбран предложенный handle {clicked!r}.")
        page.wait_for_timeout(500)
        return True
    return False


def _studio_wait_channel_handle_error_text(
    page,
    *,
    timeout_s: float = 5.0,
    poll_ms: int = 250,
) -> str:
    """Ждём появления ошибки handle после ввода (YouTube отвечает не сразу)."""
    deadline = time.monotonic() + max(0.5, timeout_s)
    while time.monotonic() < deadline:
        err = _studio_channel_handle_error_text(page)
        if err:
            return err
        page.wait_for_timeout(poll_ms)
    return ""


def _studio_read_channel_editing_name(page) -> str:
    """Текущее название на странице «Настройка канала» (до редактирования)."""
    try:
        name_input = _studio_channel_name_input(page)
        name_input.wait_for(state="visible", timeout=60_000)
        value = _studio_read_input_value(name_input)
        if value:
            return value
    except Exception:
        pass
    return _studio_read_navigation_drawer_channel_name(page)


def _studio_apply_channel_name_and_handle(page, channel_name: str) -> None:
    name = (channel_name or "").strip()
    if not name:
        return
    _log(f"Studio: смена названия канала на {name!r}…")
    name_input = _studio_channel_name_input(page)
    name_input.wait_for(state="visible", timeout=60_000)
    _studio_fill_plain_input(page, name_input, name, label="Название канала")

    handle_value = name.lstrip("@").replace(" ", "-")
    _log(
        f"Studio: пробуем handle {handle_value!r} "
        f"(из названия канала, пробелы → «-»)…"
    )
    handle_input = _studio_channel_handle_input(page)
    handle_input.wait_for(state="visible", timeout=60_000)
    _studio_fill_plain_input(page, handle_input, handle_value, label="Handle")
    try:
        handle_input.press("Tab")
    except Exception:
        pass
    page.wait_for_timeout(200)

    _log("Studio: ожидание ответа по handle (до 5 с)…")
    err = _studio_wait_channel_handle_error_text(page, timeout_s=5.0)
    if err:
        _log(f"Studio: handle недоступен ({err!r}), нажимаем подсказку…")
        if not _studio_click_suggested_channel_handle(page):
            raise YoutubeStudioError(
                f"Handle «{handle_value}» недоступен и подсказка не найдена: {err}"
            )
        page.wait_for_timeout(600)
        err2 = _studio_wait_channel_handle_error_text(page, timeout_s=3.0)
        if err2:
            raise YoutubeStudioError(
                f"Handle не принят после выбора подсказки: {err2}"
            )


def _studio_channel_profile_image_root(page):
    return page.locator(
        "ytcp-channel-editing-profile-tab ytcp-profile-image-upload, "
        "ytcp-profile-image-upload"
    ).first


def _studio_click_profile_image_done(page) -> bool:
    try:
        return bool(
            page.evaluate(
                """
() => {
  const hosts = [
    document.querySelector('ytcp-profile-image-editor #done-button'),
    document.querySelector('#done-button'),
  ].filter(Boolean);
  for (const host of hosts) {
    let btn = host.querySelector('button:not([disabled])');
    if (!btn && host.shadowRoot) {
      btn = host.shadowRoot.querySelector('button:not([disabled])');
    }
    if (btn) {
      btn.scrollIntoView({ block: 'center', inline: 'nearest' });
      btn.click();
      return true;
    }
  }
  return false;
}
"""
            )
        )
    except Exception:
        return False


def _studio_transfer_channel_profile_picture_file(page, avatar_path: Path) -> None:
    """Customization → Picture → Upload/Change → Done (без Publish)."""
    path = avatar_path.resolve()
    if not path.is_file():
        raise YoutubeStudioError(f"Файл аватарки не найден: {path}")

    _studio_handle_interrupt_dialogs_if_present(page)
    _log("Studio: загрузка аватарки канала…")

    root = _studio_channel_profile_image_root(page)
    root.wait_for(state="visible", timeout=60_000)
    root.scroll_into_view_if_needed(timeout=15_000)
    page.wait_for_timeout(300)

    file_input = (
        page.locator("ytcp-profile-image-upload input#file-selector")
        .or_(page.locator('ytcp-profile-image-upload input[type="file"]'))
    ).first
    upload_btn = page.locator(
        "ytcp-profile-image-upload #upload-button button, "
        "ytcp-profile-image-upload #replace-button button"
    ).first

    transferred = False
    try:
        file_input.set_input_files(str(path), timeout=30_000)
        transferred = True
        _log("Studio: файл аватарки передан в #file-selector.")
    except Exception as exc:
        _log(
            f"Studio: прямой set_input_files не удался ({exc!r}), "
            "пробуем кнопку Upload/Change…"
        )

    if not transferred:
        upload_btn.wait_for(state="visible", timeout=15_000)
        upload_btn.scroll_into_view_if_needed(timeout=10_000)
        try:
            with page.expect_file_chooser(timeout=30_000) as fc_info:
                upload_btn.click(timeout=15_000)
            fc_info.value.set_files(str(path))
            transferred = True
            _log("Studio: файл аватарки выбран через Upload/Change.")
        except Exception as exc:
            raise YoutubeStudioError(
                f"Не удалось передать файл аватарки в YouTube Studio: {exc}"
            ) from exc

    done_btn = page.locator(
        "ytcp-profile-image-editor #done-button button, "
        "ytcp-button#done-button button"
    )
    done_btn.first.wait_for(state="visible", timeout=60_000)
    page.wait_for_timeout(600)

    _log("Studio: подтверждение аватарки (Done)…")
    if not _studio_click_profile_image_done(page):
        done_btn.first.click(timeout=15_000)

    try:
        page.locator("ytcp-profile-image-editor").first.wait_for(
            state="hidden", timeout=30_000
        )
    except Exception:
        page.wait_for_timeout(800)


def _studio_upload_channel_profile_picture(page, avatar_path: Path) -> None:
    """Customization → Picture → Upload/Change → Done → Publish."""
    _studio_transfer_channel_profile_picture_file(page, avatar_path)
    _studio_click_channel_customization_publish(page)


@_studio_entrypoint
def run_studio_channel_profile_picture(
    page,
    *,
    avatar_path: str | Path,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    on_oldest_channel_name=None,
    search_oldest_channel: bool = True,
    profile_id: str | None = None,
) -> None:
    """Studio → «Настройка канала» → аватарка → «Опубликовать»."""
    run_studio_channel_profile_customization(
        page,
        avatar_path=avatar_path,
        login_credentials=login_credentials,
        yt_oldest_name=yt_oldest_name,
        on_oldest_channel_name=on_oldest_channel_name,
        search_oldest_channel=search_oldest_channel,
        profile_id=profile_id,
    )


@_studio_entrypoint
def run_studio_channel_profile_customization(
    page,
    *,
    avatar_path: str | Path | None = None,
    channel_name: str | None = None,
    skip_name_change: bool = False,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    on_oldest_channel_name=None,
    on_name_change_cooldown=None,
    search_oldest_channel: bool = True,
    profile_id: str | None = None,
) -> None:
    """
    Studio → «Настройка канала»: аватарка, название/handle, «Опубликовать».
    Порядок: аватарка → название → публикация.
    """
    has_avatar = bool(avatar_path)
    name = (channel_name or "").strip()
    change_name = bool(name) and not skip_name_change

    if not has_avatar and not change_name:
        raise YoutubeStudioError(
            "Не заданы ни аватарка, ни название канала для изменения."
        )

    _studio_navigate_to_channel_customization(
        page,
        login_credentials=login_credentials,
        yt_oldest_name=yt_oldest_name,
        on_oldest_channel_name=on_oldest_channel_name,
        search_oldest_channel=search_oldest_channel,
    )

    avatar_file = Path(avatar_path) if has_avatar else None

    if has_avatar and avatar_file is not None:
        _studio_transfer_channel_profile_picture_file(page, avatar_file)

    previous_channel_name = ""
    should_update_saved_oldest = False
    saved_oldest_name = (yt_oldest_name or "").strip()

    if change_name:
        previous_channel_name = _studio_read_channel_editing_name(page)
        if previous_channel_name:
            _log(f"Studio: текущее название канала: {previous_channel_name!r}")
        if saved_oldest_name and previous_channel_name:
            should_update_saved_oldest = _studio_channel_names_match(
                previous_channel_name, saved_oldest_name
            )
            if should_update_saved_oldest:
                _log(
                    f"Studio: канал совпадает с yt_oldest_name «{saved_oldest_name}» — "
                    "после успешной смены обновим custom_data."
                )
            else:
                _log(
                    f"Studio: канал «{previous_channel_name}» ≠ yt_oldest_name "
                    f"«{saved_oldest_name}» — yt_oldest_name в custom_data не меняем."
                )
        _studio_apply_channel_name_and_handle(page, name)

    published = _studio_click_channel_customization_publish(page)

    if change_name and published and should_update_saved_oldest:
        _studio_finalize_oldest_channel_name(
            name,
            on_oldest_channel_name=on_oldest_channel_name,
        )
        _log(f"Studio: yt_oldest_name в custom_data обновлён на {name!r}.")

    if change_name and not published:
        _log(
            "Studio: «Опубликовать» осталась активной — вероятен лимит смены названия "
            "(раз в 14 дней)."
        )
        if on_name_change_cooldown is not None:
            try:
                on_name_change_cooldown()
            except Exception as exc:
                _log(f"Studio: on_name_change_cooldown: {exc!r}")

        if has_avatar and avatar_file is not None:
            _log(
                "Studio: обновление страницы и повторная загрузка только аватарки "
                "(без смены названия)…"
            )
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            _studio_navigate_to_channel_customization(
                page,
                login_credentials=login_credentials,
                yt_oldest_name=yt_oldest_name,
                on_oldest_channel_name=on_oldest_channel_name,
                search_oldest_channel=search_oldest_channel,
            )
            _studio_transfer_channel_profile_picture_file(page, avatar_file)
            if not _studio_click_channel_customization_publish(page):
                raise YoutubeStudioError(
                    "Не удалось опубликовать аватарку после повторной попытки."
                )
        elif not has_avatar:
            raise YoutubeStudioError(
                "Не удалось изменить название канала (лимит 14 дней)."
            )


@_studio_entrypoint
def verify_studio_upload_dialog_available(
    page,
    *,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    on_oldest_channel_name=None,
    search_oldest_channel: bool = True,
    profile_id: str | None = None,
) -> str:
    """
    Проверка доступности YouTube Studio по URL.
    Успех — studio.youtube.com/channel/{channel_id} (непустой id).
    Ошибка — studio.youtube.com/channel-appeal или таймаут ожидания URL.
    При search_oldest_channel=True: обход каналов, выбор самого старого.
    При search_oldest_channel=False: текущий канал без переключения.
    """
    saved = (yt_oldest_name or "").strip()
    if search_oldest_channel and saved:
        _log(
            f"Studio: проверка доступности — сохранённый yt_oldest_name «{saved}» "
            "игнорируем, обходим все каналы…"
        )
    oldest = _studio_ensure_correct_studio_channel(
        page,
        yt_oldest_name=None if search_oldest_channel else yt_oldest_name,
        login_credentials=login_credentials,
        on_oldest_channel_name=on_oldest_channel_name,
        search_oldest_channel=search_oldest_channel,
    )
    state = _studio_wait_for_availability_url(
        page, login_credentials=login_credentials
    )
    if state == "appeal":
        raise YoutubeStudioError(
            "YouTube Studio: открыта страница апелляции (channel-appeal) — "
            "канал удалён или заблокирован."
        )
    _log(
        f"Studio: URL канала подтверждён ({page.url!r}) — проверка успешна."
    )
    return oldest


def _studio_dismiss_upload_dialog(page) -> None:
    """Закрыть диалог загрузки, если он открыт (перед остановкой профиля)."""
    try:
        cancel = (
            page.locator("ytcp-uploads-dialog #cancel-button button")
            .or_(page.locator("ytcp-uploads-dialog ytcp-button#cancel-button button"))
            .or_(
                page.get_by_role(
                    "button", name=re.compile(r"отмена|cancel|закрыть|close", re.I)
                )
            )
        )
        if cancel.count() > 0 and cancel.first.is_visible():
            cancel.first.click(timeout=10_000)
            page.wait_for_timeout(400)
            _log("Studio: диалог загрузки закрыт (кнопка отмены).")
            return
    except Exception as e:
        _log(f"Studio: не удалось закрыть диалог кнопкой отмены: {e!r}")
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        _log("Studio: отправлен Escape для закрытия диалога.")
    except Exception:
        pass


def _studio_file_input_frame(picker, select_btn, page) -> object:
    """
    Фрейм документа с Filedata (часто iframe). У Studio поле иногда монтируется только после
    клика по «Выбрать файлы»; без клика wait_for(attached) висит до таймаута.
    """
    finp = picker.first.locator('input[type="file"][name="Filedata"]')
    _log("Studio: ожидание появления input Filedata в DOM…")
    try:
        finp.wait_for(state="attached", timeout=10_000)
    except PlaywrightError as e:
        _log(
            f"Studio: за 10 с Filedata не в DOM ({e!r}) — dispatch_event(click) по «Выбрать файлы»…"
        )
        try:
            select_btn.first.dispatch_event("click")
        except PlaywrightError as e2:
            _log(f"Studio: dispatch_event не удался ({e2!r}), обычный click по кнопке…")
            select_btn.first.click(timeout=30_000)
        page.wait_for_timeout(1_000)
        try:
            finp.wait_for(state="attached", timeout=120_000)
        except PlaywrightError as e3:
            _log(f"Studio: после клика Filedata так и не появился: {e3!r}")
            raise YoutubeStudioError(
                "Не найдено поле загрузки Filedata в диалоге Studio. "
                "Проверьте язык/версию интерфейса YouTube или повторите после обновления страницы."
            ) from e3
    try:
        handle = finp.element_handle(timeout=30_000)
    except PlaywrightError as e:
        _log(f"Studio: element_handle для Filedata: {e!r}")
        raise YoutubeStudioError("Не удалось получить элемент Filedata для CDP.") from e
    frame = handle.owner_frame()
    if frame is None:
        raise YoutubeStudioError("Не удалось определить фрейм для поля загрузки Filedata.")
    return frame


def _studio_cdp_chrome_file_path(local_path: str) -> str:
    """Абсолютный путь в форме, удобной для Chromium на Windows."""
    p = Path(local_path).expanduser().resolve()
    return os.path.normpath(str(p))


def _studio_cdp_set_file_input_on_target_once(target, files_path: str) -> bool:
    """
    Одна попытка: CDP-сессия к конкретному Page|Frame и DOM.setFileInputFiles.
    Любое необработанное исключение логируется.
    """
    ctx = getattr(target, "context", None) or target.page.context
    session = None
    search_id: str | None = None
    try:
        session = ctx.new_cdp_session(target)
        session.send("DOM.enable", {})

        doc_params: dict = {"depth": -1}
        try:
            snap = session.send("DOM.getDocument", {**doc_params, "pierce": True})
        except Exception:
            snap = session.send("DOM.getDocument", doc_params)
        root_id = int((snap.get("root") or {}).get("nodeId") or 0)
        if root_id > 0:
            for sel in ('input[type="file"][name="Filedata"]', 'input[type="file"]'):
                try:
                    qs = session.send(
                        "DOM.querySelector", {"nodeId": root_id, "selector": sel}
                    )
                except Exception as qe:
                    _log(f"Studio: CDP DOM.querySelector({sel!r}): {qe!r}")
                    continue
                nid = int(qs.get("nodeId") or 0)
                if nid <= 0:
                    continue
                try:
                    session.send(
                        "DOM.setFileInputFiles",
                        {"nodeId": nid, "files": [files_path]},
                    )
                    _log(
                        f"Studio: CDP getDocument+querySelector({sel!r}) → setFileInputFiles ок."
                    )
                    return True
                except Exception as e:
                    _log(
                        f"Studio: CDP setFileInputFiles после querySelector({sel!r}): {e!r}"
                    )
                    continue

        session.send("Runtime.enable", {})

        def _discard() -> None:
            nonlocal search_id
            if search_id is None:
                return
            try:
                session.send("DOM.discardSearchResults", {"searchId": search_id})
            except Exception:
                pass
            search_id = None

        params: dict = {"query": 'input[type="file"][name="Filedata"]'}
        try:
            search = session.send(
                "DOM.performSearch",
                {**params, "includeUserAgentShadowDOM": True},
            )
        except Exception:
            search = session.send("DOM.performSearch", params)
        search_id = search.get("searchId")
        count = int(search.get("resultCount") or 0)
        node_id: int | None = None
        if search_id is not None and count > 0:
            nodes = session.send(
                "DOM.getSearchResults",
                {"searchId": search_id, "fromIndex": 0, "toIndex": count},
            )
            ids = nodes.get("nodeIds") or []
            if ids:
                node_id = int(ids[0])

        if node_id is None:
            _discard()
            expr = r"""(() => {
                const find = (root) => {
                    const q = root.querySelector('input[type="file"][name="Filedata"]');
                    if (q) return q;
                    const all = root.querySelectorAll('*');
                    for (let i = 0; i < all.length; i++) {
                        const el = all[i];
                        if (el.shadowRoot) {
                            const r = find(el.shadowRoot);
                            if (r) return r;
                        }
                    }
                    return null;
                };
                return find(document);
            })()"""
            ev = session.send(
                "Runtime.evaluate",
                {"expression": expr, "returnByValue": False, "awaitPromise": False},
            )
            if ev.get("exceptionDetails"):
                _log(f"Studio: CDP Runtime.evaluate — {ev.get('exceptionDetails')!r}")
                return False
            res = ev.get("result") or {}
            if res.get("subtype") != "node" or not res.get("objectId"):
                _log("Studio: CDP — input Filedata не найден (performSearch и обход shadow).")
                return False
            rn = session.send("DOM.requestNode", {"objectId": res["objectId"]})
            node_id = int(rn.get("nodeId") or 0) or None

        if node_id is None:
            _discard()
            _log("Studio: CDP — после requestNode нет валидного nodeId для Filedata.")
            return False

        try:
            session.send(
                "DOM.setFileInputFiles", {"nodeId": node_id, "files": [files_path]}
            )
        except Exception as e:
            _log(f"Studio: CDP DOM.setFileInputFiles отклонён: {e!r}")
            _discard()
            return False
        _discard()
        _log("Studio: DOM.setFileInputFiles (CDP, локальный путь) выполнен.")
        return True
    except Exception as e:
        _log(f"Studio: CDP исключение на цели {type(target).__name__}: {e!r}")
        return False
    finally:
        if search_id is not None and session is not None:
            try:
                session.send("DOM.discardSearchResults", {"searchId": search_id})
            except Exception:
                pass
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass


def _studio_set_file_input_via_cdp(page, preferred_frame, resolved_local_path: str) -> bool:
    """
    DOM.setFileInputFiles по локальному пути; перебираем цели CDP (Page и Frame).
    """
    _log("Studio: CDP — сбор целей (Page / Frame)…")
    files_path = _studio_cdp_chrome_file_path(resolved_local_path)
    order: list = []
    seen: set[int] = set()

    def _add(t) -> None:
        if t is None:
            return
        k = id(t)
        if k in seen:
            return
        seen.add(k)
        order.append(t)

    _add(preferred_frame)
    _add(page)
    try:
        _add(page.main_frame())
    except Exception as e:
        _log(f"Studio: CDP — main_frame(): {e!r}")
    try:
        for fr in page.frames():
            _add(fr)
    except Exception as e:
        _log(f"Studio: CDP — page.frames() пропущен: {e!r}")

    _log(f"Studio: CDP — целей в очереди: {len(order)}")
    for i, tgt in enumerate(order):
        _log(
            f"Studio: CDP setFileInputFiles — цель {i + 1}/{len(order)} ({type(tgt).__name__})…"
        )
        if _studio_cdp_set_file_input_on_target_once(tgt, files_path):
            return True
    _log("Studio: CDP setFileInputFiles — все цели исчерпаны, успеха нет.")
    return False


def _studio_validate_video_file_path(video_path: str | Path) -> Path:
    """Проверить, что файл существует и доступен для чтения (до открытия диалога загрузки)."""
    p = Path(video_path).expanduser()
    _log(
        f"Studio: проверка файла перед загрузкой: raw={video_path!r}, expanded={str(p)!r}"
    )
    file_wait_deadline = time.monotonic() + 6.0
    last_stat_err: Exception | None = None
    while True:
        try:
            if p.is_file():
                return p
        except Exception as e:
            last_stat_err = e
        if time.monotonic() >= file_wait_deadline:
            raise YoutubeStudioError(
                f"Видеофайл не найден/не доступен: {video_path!r}. "
                f"expanded={str(p)!r}, last_stat_err={last_stat_err!r}"
            )
        time.sleep(0.25)


def _studio_upload_pick_file(
    page,
    video_path: str | Path,
    *,
    login_credentials=None,
    skip_validation: bool = False,
    title: str | None = None,
    description: str | None = None,
) -> tuple[bool, bool, bool]:
    """Диалог ytcp-uploads-file-picker: файл через CDP (локальный путь) или fallback file chooser.

    Если заданы title/description — сразу после передачи файла начинает заполнять метаданные.
    Возвращает (title_done, description_done, not_for_kids_done).
    """
    p = (
        Path(video_path).expanduser()
        if skip_validation
        else _studio_validate_video_file_path(video_path)
    )
    _studio_wait_upload_file_picker_visible(page, login_credentials=login_credentials)
    picker = _studio_upload_file_picker_locator(page)

    select_btn = (
        picker.first.locator(
            "#select-files-button button[aria-label='Выбрать файлы']"
        )
        .or_(
            picker.first.locator("#select-files-button button[aria-label='Select files']")
        )
        .or_(picker.first.locator("ytcp-button#select-files-button button"))
        .or_(
            picker.first.get_by_role(
                "button", name=re.compile(r"выбрать файлы|select files", re.I)
            )
        )
    )
    try:
        resolved = str(p.resolve())
    except OSError:
        resolved = str(p)
    try:
        sz = p.stat().st_size
    except OSError:
        sz = -1
    _log(
        "Studio: файл перед выбором: "
        f"resolved={resolved!r}, exists={p.exists()}, is_file={p.is_file()}, size={sz}"
    )

    _log(
        f"Studio: DOM.setFileInputFiles по локальному пути (байт: {sz}) — "
        "Chromium читает файл с диска, без передачи тела по CDP…"
    )
    frame = _studio_file_input_frame(picker, select_btn, page)
    try:
        fu = frame.url
    except Exception:
        fu = "(url недоступен)"
    _log(f"Studio: CDP — фрейм поля Filedata: {fu!r}")
    file_submitted = False
    if _studio_set_file_input_via_cdp(page, frame, resolved):
        file_submitted = True
    else:
        _log(
            "Studio: CDP DOM.setFileInputFiles не удался — "
            "fallback на file chooser / set_input_files (медленнее: тело файла по CDP)…"
        )
        last_pick_err: Exception | None = None
        for attempt in range(1, 4):
            try:
                _log(f"Studio: «Выбрать файлы» + file chooser… (попытка {attempt}/3)")
                with page.expect_file_chooser(timeout=600_000) as fc_info:
                    select_btn.first.click(timeout=600_000)
                fc_info.value.set_files(resolved, timeout=_STUDIO_FILE_PICKER_TRANSFER_MS)
                last_pick_err = None
                file_submitted = True
                break
            except Exception as e:
                last_pick_err = e
                _log(
                    "Studio: file chooser не сработал "
                    f"(attempt={attempt}/3, err={e!r}). "
                    f"Файл сейчас: exists={p.exists()}, is_file={p.is_file()} — пробуем fallback…"
                )
                try:
                    picker.first.locator('input[type="file"][name="Filedata"]').set_input_files(
                        resolved, timeout=_STUDIO_FILE_PICKER_TRANSFER_MS
                    )
                    last_pick_err = None
                    file_submitted = True
                    break
                except Exception as e2:
                    last_pick_err = e2
                    err_t = str(e2).lower()
                    if "50" in err_t and "mb" in err_t:
                        raise YoutubeStudioError(
                            "Видео слишком велико для передачи в браузер через Playwright по CDP; "
                            "обход через DOM.setFileInputFiles не удался. "
                            "Нужен доступ к тому же диску, что и у Chromium (обычно тот же ПК, что и Zaliver)."
                        ) from e2
                    _log(
                        f"Studio: fallback set_input_files не удался: {e2!r}. "
                        "Ждём 0.5s и повторяем…"
                    )
                    page.wait_for_timeout(500)
        if last_pick_err is not None:
            raise last_pick_err

    metadata_state = (True, True, False)
    if file_submitted and (
        (title or "").strip() or (description or "").strip()
    ):
        metadata_state = _studio_prepare_upload_details_during_transfer(
            page, title=title, description=description
        )

    try:
        sz_log = p.stat().st_size
    except OSError:
        sz_log = -1
    _log(f"Studio: файл передан — {p.name!r}, байт: {sz_log}.")
    return metadata_state


def _studio_normalize_upload_title(title: str | None) -> str:
    """Название для Studio: без краевых пробелов, в конце всегда один пробел."""
    t = (title or "").strip()
    return f"{t} " if t else ""


def _studio_upload_title_box_locator(page):
    return page.locator("ytcp-uploads-dialog ytcp-video-title #textbox").or_(
        page.locator("ytcp-uploads-dialog #title-wrapper #textbox")
    ).or_(
        page.locator("ytcp-video-metadata-editor ytcp-video-title #textbox")
    ).or_(
        page.locator("ytcp-video-metadata-editor #title-wrapper #textbox")
    ).or_(page.locator("ytcp-video-title #textbox"))


def _studio_upload_description_box_locator(page):
    return page.locator("ytcp-uploads-dialog ytcp-video-description #textbox").or_(
        page.locator("ytcp-uploads-dialog #description-wrapper #textbox")
    ).or_(
        page.locator("ytcp-video-metadata-editor ytcp-video-description #textbox")
    ).or_(
        page.locator("ytcp-video-metadata-editor #description-wrapper #textbox")
    ).or_(page.locator("ytcp-video-description #textbox"))


def _studio_not_for_kids_button_locator(page):
    kids_select = page.locator("ytkc-made-for-kids-select").or_(
        page.locator(".made-for-kids-rating-container")
    )
    return (
        kids_select.locator(
            f'tp-yt-paper-radio-button[name="{_NOT_FOR_KIDS_RADIO_NAME}"]'
        )
        .or_(
            page.locator(
                f'.made-for-kids-group tp-yt-paper-radio-button[name="{_NOT_FOR_KIDS_RADIO_NAME}"]'
            )
        )
        .or_(page.locator(f'tp-yt-paper-radio-button[name="{_NOT_FOR_KIDS_RADIO_NAME}"]'))
        .or_(
            kids_select.locator("tp-yt-paper-radio-button").filter(
                has_text=_NOT_FOR_KIDS_LABEL_RE
            )
        )
        .or_(page.get_by_role("radio", name=_NOT_FOR_KIDS_LABEL_RE))
    )


def _studio_wizard_next_button_locator(page):
    dialog = page.locator("ytcp-uploads-dialog")
    return (
        dialog.locator("ytcp-button#next-button button")
        .or_(dialog.locator("#next-button button"))
        .or_(
            dialog.get_by_role("button", name=re.compile(r"^далее$|^next$", re.I))
        )
        .or_(page.get_by_role("button", name=re.compile(r"^далее$|^next$", re.I)))
    )


def _studio_prepare_upload_details_during_transfer(
    page,
    *,
    title: str | None,
    description: str | None,
    timeout_sec: float = 180.0,
) -> tuple[bool, bool, bool]:
    """
    Пока идёт загрузка: как только видны поля — сразу очищаем название и вводим метаданные.
    Возвращает (title_done, description_done, not_for_kids_done).
    """
    t = _studio_normalize_upload_title(title)
    d = (description or "").strip()
    if not t and not d:
        return True, True, False

    title_done = not t
    desc_done = not d
    kids_done = False
    _log("Studio: ожидание полей метаданных во время загрузки…")
    deadline = time.monotonic() + timeout_sec
    poll_n = 0
    while time.monotonic() < deadline:
        poll_n += 1
        if poll_n % _STUDIO_INTERRUPT_DIALOG_EVERY_N_POLLS == 1:
            _studio_handle_interrupt_dialogs_if_present(page)

        if not title_done:
            title_box = _studio_upload_title_box_locator(page)
            try:
                if title_box.first.is_visible(timeout=0):
                    title_box.first.click(timeout=5_000)
                    old_title = _studio_read_contenteditable_text(title_box)
                    if (old_title or "").strip():
                        _log(
                            "Studio: старое название в поле: "
                            f"{(old_title or '').strip()!r} — очистка…"
                        )
                    _studio_clear_contenteditable_until_old_title_gone(
                        page,
                        title_box,
                        old_title=old_title,
                        right_slack=10,
                        backspace_extra=15,
                    )
                    page.keyboard.type(t, delay=0)
                    title_done = True
                    _log("Studio: название введено.")
            except Exception:
                pass

        if not desc_done and (title_done or not t):
            desc_box = _studio_upload_description_box_locator(page)
            try:
                if desc_box.first.is_visible(timeout=0):
                    desc_box.first.click(timeout=5_000)
                    if d:
                        _studio_clear_contenteditable_like_user(page, desc_box)
                        page.wait_for_timeout(80)
                        page.keyboard.type(d, delay=0)
                    desc_done = True
                    _log("Studio: описание введено.")
            except Exception:
                pass

        if not kids_done:
            btn = _studio_not_for_kids_button_locator(page).first
            try:
                if btn.is_visible(timeout=0):
                    btn.click(timeout=5_000)
                    try:
                        if (btn.get_attribute("aria-checked") or "").lower() != "true":
                            btn.locator("#radioContainer").click(timeout=5_000)
                    except Exception:
                        pass
                    kids_done = True
                    _log("Studio: выбрано «Не для детей».")
            except Exception:
                pass

        if title_done and desc_done:
            return title_done, desc_done, kids_done

        page.wait_for_timeout(_STUDIO_UPLOAD_DETAILS_POLL_MS)

    if not title_done and t:
        _log("Studio: поле «Название» не появилось вовремя.")
    if not desc_done and d:
        _log("Studio: поле «Описание» не появилось вовремя.")
    return title_done, desc_done, kids_done


def _studio_set_title_and_description(
    page,
    *,
    title: str | None,
    description: str | None,
    metadata_state: tuple[bool, bool, bool] | None = None,
) -> bool:
    """
    Заполнение полей «Название» и «Описание» в диалоге загрузки Studio.

    Поля в Studio — contenteditable div#textbox внутри ytcp-social-suggestion-input.

    Возвращает True, если «Не для детей» уже выбрано в ходе подготовки метаданных.
    """
    t = _studio_normalize_upload_title(title)
    d = (description or "").strip()
    if not t and not d:
        return metadata_state[2] if metadata_state else False

    if metadata_state is None:
        title_done, desc_done, kids_done = _studio_prepare_upload_details_during_transfer(
            page, title=title, description=description
        )
    else:
        title_done, desc_done, kids_done = metadata_state

    if title_done and desc_done:
        return kids_done

    editor = page.locator("ytcp-video-metadata-editor#details").or_(
        page.locator("ytcp-video-metadata-editor")
    )

    def _fill(
        contenteditable,
        text: str,
        *,
        clear_first: bool = False,
        right_slack: int = 8,
        backspace_extra: int = 0,
    ) -> None:
        contenteditable.first.wait_for(state="visible", timeout=60_000)
        contenteditable.first.click(timeout=30_000)
        if clear_first:
            _studio_clear_contenteditable_like_user(
                page,
                contenteditable,
                right_slack=right_slack,
                backspace_extra=backspace_extra,
            )
            page.wait_for_timeout(80)
        page.keyboard.type(text, delay=0)
        page.wait_for_timeout(150)

    try:
        editor.first.wait_for(state="visible", timeout=180_000)
    except Exception:
        _log("Studio: метаданные (details) не видны — пропуск заполнения title/description.")
        return kids_done

    if t and not title_done:
        _log("Studio: заполнение поля «Название» (fallback)…")
        title_box = (
            editor.first.locator("ytcp-video-title #textbox")
            .or_(editor.first.locator("#title-wrapper #textbox"))
            .or_(_studio_upload_title_box_locator(page))
        )
        title_box.first.wait_for(state="visible", timeout=60_000)
        title_box.first.click(timeout=30_000)
        old_title = _studio_read_contenteditable_text(title_box)
        if (old_title or "").strip():
            _log(
                f"Studio: старое название в поле: {(old_title or '').strip()!r} — очистка…"
            )
        _studio_clear_contenteditable_until_old_title_gone(
            page,
            title_box,
            old_title=old_title,
            right_slack=10,
            backspace_extra=15,
        )
        page.keyboard.type(t, delay=0)
        page.wait_for_timeout(150)

    if d and not desc_done:
        _log("Studio: заполнение поля «Описание» (fallback)…")
        desc_box = (
            editor.first.locator("ytcp-video-description #textbox")
            .or_(editor.first.locator("#description-wrapper #textbox"))
            .or_(_studio_upload_description_box_locator(page))
        )
        _fill(desc_box, d, clear_first=bool(d))

    return kids_done


def _studio_select_not_for_kids(page, *, skip_if_done: bool = False) -> None:
    """«Нет, это видео не для детей» / «No, it's not made for kids»."""
    if skip_if_done:
        return

    _log(
        "Studio: «Нет, это видео не для детей» / "
        "«No, it's not made for kids»…"
    )
    btn = _studio_not_for_kids_button_locator(page).first
    deadline = time.monotonic() + 90.0
    poll_n = 0
    while time.monotonic() < deadline:
        poll_n += 1
        if poll_n % _STUDIO_INTERRUPT_DIALOG_EVERY_N_POLLS == 1:
            _studio_handle_interrupt_dialogs_if_present(page)
        try:
            if btn.is_visible(timeout=0):
                break
        except Exception:
            pass
        page.wait_for_timeout(100)
    else:
        raise YoutubeStudioError(
            "YouTube Studio: не появился выбор «Не для детей» (made-for-kids)."
        )
    btn.click(timeout=15_000)
    try:
        if (btn.get_attribute("aria-checked") or "").lower() != "true":
            btn.locator("#radioContainer").click(timeout=15_000)
    except Exception:
        pass


def _studio_click_next_until_visibility(page, *, fast_if_upload_done: bool = False) -> None:
    """«Далее» / Next пока не появится выбор доступа (#privacy-radios / PUBLIC)."""
    _log("Studio: «Далее» до экрана доступа…")
    public_radio = (
        page.locator(
            "ytcp-video-visibility-select tp-yt-paper-radio-group#privacy-radios "
            'tp-yt-paper-radio-button[name="PUBLIC"]'
        )
        .or_(page.locator('ytcp-video-visibility-select tp-yt-paper-radio-button[name="PUBLIC"]'))
    )
    nxt = _studio_wizard_next_button_locator(page)
    poll_n = 0
    for i in range(_STUDIO_WIZARD_NEXT_MAX):
        poll_n += 1
        if poll_n % _STUDIO_INTERRUPT_DIALOG_EVERY_N_POLLS == 1:
            _studio_handle_interrupt_dialogs_if_present(page)
        try:
            if public_radio.first.is_visible(timeout=0):
                _log(f"Studio: экран доступа виден (шаг {i}).")
                return
        except Exception:
            pass
        if fast_if_upload_done and _studio_is_upload_file_transfer_complete(page):
            status = _studio_read_upload_progress_label(page)
            if status and _studio_is_upload_past_percent_phase(status):
                _log(
                    f"Studio: загрузка завершена ({status!r}) — ускоряем проход мастера…"
                )
        try:
            if nxt.first.is_visible(timeout=0):
                _log(f"Studio: «Далее» ({i + 1}/{_STUDIO_WIZARD_NEXT_MAX})…")
                nxt.first.click(timeout=15_000)
                page.wait_for_timeout(_STUDIO_WIZARD_NEXT_AFTER_CLICK_MS)
                continue
        except Exception:
            pass
        page.wait_for_timeout(_STUDIO_WIZARD_NEXT_POLL_MS)

    if not public_radio.first.is_visible():
        raise YoutubeStudioError(
            "Не появился экран выбора доступа к видео (ytcp-video-visibility-select / PUBLIC)."
        )


def _studio_log_video_link_before_public(page) -> str:
    """На экране доступа: вытащить ссылку из ytcp-video-info (и залогировать)."""
    candidates = (
        page.locator("ytcp-video-info .video-url-fadeable a[href]")
        .or_(page.locator("ytcp-video-info .value a[href]"))
        .or_(page.locator('ytcp-video-info a[target="_blank"][href*="youtu"]'))
    )
    href = ""
    try:
        candidates.first.wait_for(state="visible", timeout=20_000)
        href = (candidates.first.get_attribute("href") or "").strip()
        if not href:
            href = (candidates.first.inner_text(timeout=3_000) or "").strip()
    except Exception:
        pass
    if not href:
        _log("Studio: ссылка на видео (ytcp-video-info) не найдена — ставим доступ без URL.")
        return ""
    _log(f"Studio: ссылка на видео: {href}")
    try:
        page.evaluate(
            """(url) => {
                try {
                    if (navigator.clipboard && navigator.clipboard.writeText)
                        void navigator.clipboard.writeText(url);
                } catch (e) {}
            }""",
            href,
        )
    except Exception:
        pass
    return href


def _studio_try_extract_video_url(page) -> str:
    """
    Best-effort extraction of the uploaded video's URL from the Studio upload dialog.
    """
    candidates = (
        page.locator("ytcp-video-info .video-url-fadeable a[href]")
        .or_(page.locator("ytcp-video-info .value a[href]"))
        .or_(page.locator('ytcp-video-info a[target="_blank"][href*="youtu"]'))
        .or_(page.locator("ytcp-uploads-dialog ytcp-video-info a[href]"))
        .or_(page.locator("ytcp-uploads-dialog a[href*='youtu']"))
    )
    try:
        if candidates.count() <= 0:
            return ""
        if not candidates.first.is_visible(timeout=1_500):
            return ""
        href = (candidates.first.get_attribute("href") or "").strip()
        if href:
            return href
        return (candidates.first.inner_text(timeout=1_500) or "").strip()
    except Exception:
        return ""


def _studio_select_public_visibility(page) -> str:
    """Ссылка на видео в лог, затем «Открытый доступ» (PUBLIC). Возвращает href (если нашли)."""
    _studio_handle_interrupt_dialogs_if_present(page)
    _log("Studio: экран доступа — фиксируем ссылку на видео…")
    href = _studio_log_video_link_before_public(page)
    _log("Studio: «Открытый доступ»…")
    pub = (
        page.locator(
            "ytcp-video-visibility-select #privacy-radios tp-yt-paper-radio-button[name='PUBLIC']"
        )
        .or_(page.locator('ytcp-video-visibility-select tp-yt-paper-radio-button[name="PUBLIC"]'))
        .or_(page.get_by_role("radio", name=re.compile(r"открытый доступ|^public$", re.I)))
    )
    pub.first.wait_for(state="visible", timeout=30_000)
    pub.first.click(timeout=15_000)
    return href


def _studio_click_publish(page) -> None:
    """Кнопка «Опубликовать» / Publish."""
    _studio_handle_interrupt_dialogs_if_present(page)
    _log("Studio: «Опубликовать»…")
    btn = (
        page.locator('ytcp-button-shape button[aria-label="Опубликовать"]')
        .or_(page.locator('ytcp-button-shape button[aria-label="Publish"]'))
        .or_(page.get_by_role("button", name=re.compile(r"опубликовать|publish", re.I)))
    )
    btn.first.wait_for(state="visible", timeout=90_000)
    btn.first.click(timeout=60_000)
    _log("Studio: «Опубликовать» нажата.")


def _studio_is_upload_unavailable_dialog(page) -> bool:
    """Диалог ytcp-uploads-dialog: .error-short «Загрузка недоступна» (лимит / проверка канала)."""
    short = page.locator("ytcp-uploads-dialog .error-short").or_(
        page.locator("ytcp-ve.error-area .error-short")
    )
    try:
        if short.count() == 0:
            return False
        if not short.first.is_visible(timeout=2_000):
            return False
        text = (short.first.inner_text(timeout=3_000) or "").strip().lower()
    except Exception:
        return False
    if not text:
        return False
    markers = (
        "загрузка недоступна",
        "upload unavailable",
        "upload isn't available",
        "upload is not available",
    )
    return any(m in text for m in markers)


def _studio_upload_unavailable_extra_text(page) -> str:
    for sel in (
        "ytcp-uploads-dialog yt-formatted-string.error-details",
        "ytcp-uploads-dialog .error-details",
        "ytcp-uploads-dialog #error-message",
    ):
        loc = page.locator(sel).first
        try:
            if loc.is_visible(timeout=500):
                t = (loc.inner_text(timeout=2_000) or "").strip()
                if t:
                    return t
        except Exception:
            continue
    return ""


def _studio_upload_checks_status_text(page) -> str:
    """Текст статуса проверок: progress-label, tooltip и hover-блок copyright."""
    chunks: list[str] = []
    for sel in (
        "ytcp-uploads-dialog ytcp-video-upload-progress .progress-label",
        "ytcp-uploads-dialog ytcp-video-upload-progress-hover",
        "ytcp-uploads-dialog #checks-tooltip",
        "ytcp-uploads-dialog ytcp-paper-tooltip[for='checks-badge']",
    ):
        loc = page.locator(sel)
        try:
            if loc.count() == 0:
                continue
            el = loc.first
            if not el.is_visible(timeout=500):
                continue
            txt = (el.inner_text(timeout=2_000) or "").strip()
            if txt and txt not in chunks:
                chunks.append(txt)
        except Exception:
            continue
    return " ".join(chunks).strip()


def _studio_is_copyright_claims_checks_completed_text(text: str) -> bool:
    """
    Проверка авторских прав завершена, но найден защищённый контент — считаем успехом.
    EN: «Checks complete. Copyright-protected content found.»
    RU: «Проверка завершена. Найден контент, защищенный авторским правом.»
    """
    t = (text or "").strip().lower()
    if not t:
        return False
    if re.search(r"checks?\s+complete.*copyright[- ]protected", t):
        return True
    if re.search(r"copyright[- ]protected\s+conten[dt]\s+found", t):
        return True
    if re.search(r"copyright[- ]protected\s+material\s+found", t):
        return True
    if re.search(r"проверка на соблюдение авторских прав завершена", t):
        return True
    if "проверка завершена" in t and re.search(r"авторск|защищённ|защищенн", t):
        return True
    if "найден контент" in t and "авторск" in t:
        return True
    return False


def _studio_is_upload_checks_completed(page) -> bool:
    """
    ytcp-video-upload-progress: проверки завершены (атрибут или подпись «Проверка завершена…»).
    Найденный защищённый авторским правом контент — тоже считаем завершением.
    """
    by_attr = page.locator(
        'ytcp-uploads-dialog ytcp-video-upload-progress'
        '[checks-summary-status-v2="UPLOAD_CHECKS_DATA_SUMMARY_STATUS_COMPLETED"]'
    )
    try:
        if by_attr.count() > 0 and by_attr.first.is_visible(timeout=800):
            return True
    except Exception:
        pass

    t = _studio_upload_checks_status_text(page).lower()
    if not t:
        return False

    if _studio_is_copyright_claims_checks_completed_text(t):
        _log(
            "Studio: проверка авторских прав завершена (найден защищённый контент) — "
            "считаем успехом, продолжаем пайплайн."
        )
        return True

    if "проверка завершена" in t and "нарушен" in t and "не найден" in t:
        return True
    if ("check" in t and "complete" in t) and (
        "no issues" in t
        or "no violation" in t
        or "not found" in t
    ):
        return True
    if "checks complete" in t:
        return True
    if "copyright check" in t and "complete" in t:
        return True
    return False


def _studio_read_upload_progress_label(page) -> str:
    label = page.locator(
        "ytcp-uploads-dialog ytcp-video-upload-progress .progress-label"
    )
    try:
        if label.count() > 0 and label.first.is_visible(timeout=500):
            return (label.first.inner_text(timeout=1_500) or "").strip()
    except Exception:
        pass
    return ""


def _studio_is_upload_past_percent_phase(text: str) -> bool:
    """Фаза процентов прошла: в статуге есть текст, но нет «%»."""
    t = (text or "").strip()
    if not t or "%" in t:
        return False
    if re.search(
        r"starting|preparing|getting ready|начина|подготов|will begin shortly",
        t,
        re.I,
    ):
        return False
    return True


def _studio_is_upload_file_transfer_complete_text(text: str) -> bool:
    """
    Файл передан на сервер YouTube; проверки ещё не завершены.
    EN: «Upload complete ... Processing will begin shortly»
    RU: «Загрузка завершена... Скоро начнётся обработка» (и похожие формулировки).
    """
    t = (text or "").strip().lower()
    if not t:
        return False
    post_upload_markers = (
        "upload complete",
        "processing will begin shortly",
        "загрузка завершена",
        "загрузка завершена.",
        "обработка скоро начн",
        "скоро начнётся обработка",
        "скоро начнется обработка",
    )
    if any(m in t for m in post_upload_markers):
        return True
    if re.search(r"(?:загрузк|upload)", t) and re.search(r"\b100\s*%", t):
        return True
    if "checks complete" in t:
        return True
    if _studio_is_upload_past_percent_phase(text):
        return True
    return False


def _studio_is_upload_file_transfer_complete(page) -> bool:
    """Загрузка файла завершена: 100%, «загрузка завершена», проверки или статус без «%»."""
    t = _studio_read_upload_progress_label(page)
    if t and _studio_is_upload_file_transfer_complete_text(t):
        return True
    if _studio_is_upload_checks_completed(page):
        return True
    return False


def _studio_try_extract_video_id_from_url(url: str) -> str:
    """
    Пытаемся вытащить videoId из URL/текста ссылки:
    - https://youtu.be/<id>
    - https://www.youtube.com/watch?v=<id>
    - https://www.youtube.com/shorts/<id>
    - /video/<id>/edit (Studio)
    """
    u = (url or "").strip()
    if not u:
        return ""
    m = re.search(r"(?:youtu\.be/|[?&]v=)([A-Za-z0-9_-]{6,})", u)
    if m:
        return m.group(1)
    m_sh = re.search(r"/shorts/([A-Za-z0-9_-]{6,})", u)
    if m_sh:
        return m_sh.group(1)
    m2 = re.search(r"/video/([A-Za-z0-9_-]{6,})", u)
    if m2:
        return m2.group(1)
    return ""


def _studio_is_probably_youtube_video_url(url: str) -> bool:
    u = (url or "").strip().lower()
    if not u:
        return False
    # Avoid unrelated help/support links.
    if "support.google.com" in u:
        return False
    return (
        "youtube.com/watch" in u
        or "youtu.be/" in u
        or "youtube.com/shorts/" in u
        or "/video/" in u
    )


def _studio_canonical_watch_url(video_id: str) -> str:
    vid = (video_id or "").strip()
    if not vid:
        return ""
    return f"https://www.youtube.com/watch?v={vid}"


def _studio_try_log_video_id_from_progress_dialog(page) -> None:
    """
    В диалоге загрузки иногда уже есть ссылка/ID (ytcp-video-info) до завершения мастера.
    Ничего не падает — только логирует, если удалось.
    """
    candidates = (
        page.locator("ytcp-uploads-dialog ytcp-video-info a[href]")
        .or_(page.locator("ytcp-uploads-dialog a[href*='youtu']"))
        .or_(page.locator("ytcp-uploads-dialog a[href*='/video/']"))
    )
    try:
        if candidates.count() == 0:
            return
        if not candidates.first.is_visible(timeout=500):
            return
        href = (candidates.first.get_attribute("href") or "").strip()
        vid = _studio_try_extract_video_id_from_url(href)
        if vid:
            _log(f"Studio: videoId (из диалога прогресса): {vid}")
    except Exception:
        return


def _studio_fatal_error_text(page) -> str:
    """
    Фатальные ошибки загрузки/обработки в Studio.

    Условия:
    - Встречается слово "ошибка"/"error" (любой регистр) в progress label
    - Или есть видимый контейнер //*[@id="error-message"] (часто в ytcp-uploads-dialog)
    """
    # 1) Явное сообщение ошибки (встречалось у тебя как //*[@id="error-message"])
    err_box = page.locator("ytcp-uploads-dialog #error-message").or_(
        page.locator("#error-message")
    )
    try:
        if err_box.count() > 0 and err_box.first.is_visible(timeout=300):
            t = (err_box.first.inner_text(timeout=1_500) or "").strip()
            return t or "YouTube Studio: error-message"
    except Exception:
        pass

    # 2) Текст прогресса
    label = page.locator(
        "ytcp-uploads-dialog ytcp-video-upload-progress .progress-label"
    ).or_(page.locator("ytcp-video-upload-progress .progress-label"))
    try:
        if label.count() > 0 and label.first.is_visible(timeout=300):
            t = (label.first.inner_text(timeout=1_500) or "").strip()
            tl = t.lower()
            if "ошибка" in tl or "error" in tl:
                return t or "YouTube Studio: error in progress label"
    except Exception:
        pass

    return ""


def _studio_abort_upload_unavailable(page, browser) -> None:
    """Лог, закрытие браузера, исключение для UI/потока."""
    extra = _studio_upload_unavailable_extra_text(page)
    _log(
        "Studio: YouTube — «Загрузка недоступна» (лимит загрузок, проверка канала или пауза 24 ч)."
    )
    if extra:
        _log(f"Studio: текст из диалога YouTube: {extra!r}")
    _log("Playwright: закрытие браузера из-за недоступности загрузки в Studio.")
    try:
        browser.close()
    except Exception:
        pass
    raise YoutubeStudioError(
        "YouTube Studio: «Загрузка недоступна». "
        "Обычно это дневной лимит видео или нужна проверка канала (в Studio есть «Пройти проверку»). "
        f"Дополнительно: {extra or '—'}"
    )


def _studio_abort_fatal_error(page, browser, error_text: str) -> None:
    _log("Studio: обнаружена ошибка в процессе загрузки/обработки — прерывание.")
    if error_text:
        _log(f"Studio: ошибка: {error_text!r}")
    _log("Playwright: закрытие браузера из-за ошибки в Studio.")
    try:
        browser.close()
    except Exception:
        pass
    raise YoutubeStudioError(f"YouTube Studio: ошибка загрузки/обработки: {error_text or '—'}")


def _studio_poll_upload_fatal(page, browser) -> None:
    """Фатальные ошибки / «Загрузка недоступна» во время залива."""
    fatal = _studio_fatal_error_text(page)
    if fatal:
        _studio_abort_fatal_error(page, browser, fatal)
    if _studio_is_upload_unavailable_dialog(page):
        _studio_abort_upload_unavailable(page, browser)


def _studio_wait_for_upload_file_transfer_complete(
    page, browser, max_wait_sec: float
) -> None:
    """Ждём 100% передачи файла или «Загрузка завершена… обработка скоро начнётся»."""
    _log(
        "Studio: ожидание завершения передачи файла "
        "(100% / «Загрузка завершена…»)…"
    )
    deadline = time.monotonic() + max_wait_sec
    last_label: str = ""
    while time.monotonic() < deadline:
        _studio_handle_interrupt_dialogs_if_present(page)
        _studio_poll_upload_fatal(page, browser)
        try:
            label = page.locator(
                "ytcp-uploads-dialog ytcp-video-upload-progress .progress-label"
            )
            if label.count() > 0 and label.first.is_visible(timeout=300):
                t = (label.first.inner_text(timeout=1_500) or "").strip()
                if t and t != last_label:
                    last_label = t
                    _log(f"Studio: статус загрузки: {t}")
        except Exception:
            pass
        if _studio_is_upload_file_transfer_complete(page):
            _studio_try_log_video_id_from_progress_dialog(page)
            if last_label and _studio_is_upload_past_percent_phase(last_label):
                _log(
                    f"Studio: статус без «%» ({last_label!r}) — передача завершена, публикуем."
                )
            else:
                _log("Studio: передача файла завершена.")
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        page.wait_for_timeout(
            int(min(_POST_UPLOAD_QUOTA_POLL_S, max(0.3, remaining)) * 1000)
        )
    raise YoutubeStudioError(
        f"За {max_wait_sec:.0f} с не завершилась передача файла на YouTube. "
        "Проверьте диалог загрузки вручную."
    )


def _studio_upload_publish_button(page):
    return (
        page.locator("ytcp-uploads-dialog ytcp-button#done-button button")
        .or_(page.locator('ytcp-uploads-dialog ytcp-button-shape button[aria-label="Опубликовать"]'))
        .or_(page.locator('ytcp-uploads-dialog ytcp-button-shape button[aria-label="Publish"]'))
        .or_(page.locator('ytcp-button-shape button[aria-label="Опубликовать"]'))
        .or_(page.locator('ytcp-button-shape button[aria-label="Publish"]'))
        .or_(page.get_by_role("button", name=re.compile(r"^опубликовать$|^publish$", re.I)))
    )


def _studio_click_publish_when_enabled(page, max_wait_sec: float) -> None:
    """«Опубликовать» / Publish — ждём, пока кнопка станет активной."""
    _log("Studio: ожидание активной кнопки «Опубликовать»…")
    btn = _studio_upload_publish_button(page)
    deadline = time.monotonic() + max_wait_sec
    while time.monotonic() < deadline:
        _studio_handle_interrupt_dialogs_if_present(page)
        try:
            if btn.count() > 0 and btn.first.is_visible(timeout=500):
                if btn.first.is_enabled():
                    btn.first.click(timeout=60_000)
                    _log("Studio: «Опубликовать» нажата.")
                    return
        except Exception:
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        page.wait_for_timeout(
            int(min(_POST_UPLOAD_QUOTA_POLL_S, max(0.3, remaining)) * 1000)
        )
    raise YoutubeStudioError(
        "YouTube Studio: кнопка «Опубликовать» так и не стала активной."
    )


def _studio_publish_flow_before_checks(
    page, browser, max_wait_sec: float, *, kids_already_selected: bool = False
) -> str:
    """
    Публикация до проверок: сразу после названия — мастер до «Открытый доступ»
    (пока идёт загрузка), затем «Опубликовать» после 100% / завершения загрузки.
    """
    _log(
        "Studio: публикация до проверок — проходим мастер до «Открытый доступ» "
        "параллельно загрузке файла…"
    )
    _studio_poll_upload_fatal(page, browser)
    _studio_select_not_for_kids(page, skip_if_done=kids_already_selected)
    _studio_poll_upload_fatal(page, browser)
    _studio_click_next_until_visibility(page, fast_if_upload_done=True)
    _studio_poll_upload_fatal(page, browser)
    href = _studio_select_public_visibility(page)
    if _studio_is_upload_file_transfer_complete(page):
        status = _studio_read_upload_progress_label(page)
        if status and _studio_is_upload_past_percent_phase(status):
            _log(
                f"Studio: загрузка уже завершена ({status!r}) — ждать 100% не нужно."
            )
        else:
            _log("Studio: передача файла уже завершена — ждать 100% не нужно.")
    else:
        _studio_wait_for_upload_file_transfer_complete(page, browser, max_wait_sec)
    _studio_click_publish_when_enabled(page, max_wait_sec)
    return href


def _studio_wait_after_upload_studio_outcome(
    page,
    browser,
    max_wait_sec: float,
) -> None:
    """
    После передачи файла ждём один из исходов Studio:
    — «Загрузка недоступна» → исключение;
    — успешные проверки → выход, дальше мастер.
    """
    _log(
        "Studio: ожидание результата после загрузки — «Загрузка недоступна», "
        "«Проверка завершена…» (в т.ч. найден защищённый авторским правом контент)…"
    )
    deadline = time.monotonic() + max_wait_sec
    last_label: str = ""
    while time.monotonic() < deadline:
        _studio_handle_interrupt_dialogs_if_present(page)
        fatal = _studio_fatal_error_text(page)
        if fatal:
            _studio_abort_fatal_error(page, browser, fatal)

        if _studio_is_upload_unavailable_dialog(page):
            _studio_abort_upload_unavailable(page, browser)

        # Логируем смену стадий снизу (проценты / обработка / etc.)
        try:
            label = page.locator(
                "ytcp-uploads-dialog ytcp-video-upload-progress .progress-label"
            )
            if label.count() > 0 and label.first.is_visible(timeout=300):
                t = (label.first.inner_text(timeout=1_500) or "").strip()
                if t and t != last_label:
                    last_label = t
                    _log(f"Studio: статус: {t}")
        except Exception:
            pass

        if _studio_is_upload_checks_completed(page):
            _studio_try_log_video_id_from_progress_dialog(page)
            _log("Studio: проверки видео завершены — переход к шагу «не для детей».")
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        page.wait_for_timeout(
            int(min(_POST_UPLOAD_QUOTA_POLL_S, max(0.3, remaining)) * 1000)
        )
    raise YoutubeStudioError(
        f"За {max_wait_sec:.0f} с не появился ни блок «Загрузка недоступна», "
        "ни успешное завершение проверок Studio (прогресс / подпись). "
        "Проверьте диалог загрузки вручную."
    )


def _studio_publish_flow_after_upload(page) -> str:
    """После паузы: не для детей → Далее… → открытый доступ → Опубликовать. Возвращает href (если нашли)."""
    _studio_select_not_for_kids(page)
    _studio_click_next_until_visibility(page)
    href = _studio_select_public_visibility(page)
    _studio_click_publish(page)
    return href


class UploadedStudioResult(dict):
    """
    Minimal result of a successful Studio publish flow.
    Keys:
      - video_id: str
      - url: str
    """


@_studio_entrypoint
def run_upload_latest_ready_video(
    *,
    page,
    browser,
    zaliver_db_path: Path | None,
    video_path: str | None = None,
    title: str | None = None,
    description: str | None = None,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    on_oldest_channel_name=None,
    search_oldest_channel: bool = True,
    profile_id: str | None = None,
    publish_before_checks: bool = False,
) -> None:
    """
    Полный сценарий Studio: Create → Upload → ждать outcome → мастер → Publish.
    """
    best_url = ""
    best_vid = ""

    chosen = (video_path or "").strip()
    if not chosen:
        chosen = resolve_latest_zaliver_video_on_disk(db_path=zaliver_db_path)
    upload_file = _studio_validate_video_file_path(chosen)
    _log(f"Studio: файл для загрузки: {str(upload_file)!r}")

    try:
        _studio_click_create_then_add_video(
            page,
            login_credentials=login_credentials,
            yt_oldest_name=yt_oldest_name,
            on_oldest_channel_name=on_oldest_channel_name,
            search_oldest_channel=search_oldest_channel,
            wait_for_upload_picker=False,
        )
    except YoutubeAllChannelsRemovedError:
        _studio_abort_all_channels_removed(page, browser=browser)
    metadata_state = _studio_upload_pick_file(
        page,
        upload_file,
        login_credentials=login_credentials,
        skip_validation=True,
        title=title,
        description=description,
    )
    kids_already_selected = _studio_set_title_and_description(
        page,
        title=title,
        description=description,
        metadata_state=metadata_state,
    )
    href_before_public = ""
    if publish_before_checks:
        href_before_public = _studio_publish_flow_before_checks(
            page,
            browser,
            _POST_UPLOAD_STUDIO_OUTCOME_MAX_S,
            kids_already_selected=kids_already_selected,
        )
    else:
        _log(
            f"Ожидание до {_POST_UPLOAD_STUDIO_OUTCOME_MAX_S:.0f} с исхода Studio "
            f"(опрос ~каждые {_POST_UPLOAD_QUOTA_POLL_S:.0f} с)…"
        )
        _studio_wait_after_upload_studio_outcome(
            page, browser, _POST_UPLOAD_STUDIO_OUTCOME_MAX_S
        )
        # Иногда ссылка/ID доступны уже на этапе прогресса.
        try:
            u0 = _studio_try_extract_video_url(page)
            if u0:
                best_url = u0
                v0 = _studio_try_extract_video_id_from_url(u0)
                if v0:
                    best_vid = v0
        except Exception:
            pass
        href_before_public = _studio_publish_flow_after_upload(page)
    if href_before_public:
        best_url = href_before_public
        try:
            vpub = _studio_try_extract_video_id_from_url(href_before_public)
            if vpub:
                best_vid = vpub
        except Exception:
            pass
    page.wait_for_timeout(5000)
    url = ""
    vid = ""
    try:
        url = _studio_try_extract_video_url(page)
        # Never overwrite the URL captured from ytcp-video-info with unrelated links.
        if url and not best_url and _studio_is_probably_youtube_video_url(url):
            best_url = url
    except Exception:
        url = ""

    try:
        vid = _studio_try_extract_video_id_from_url(best_url or url)
        if not vid:
            try:
                vid = _studio_try_extract_video_id_from_url(str(page.url or ""))
            except Exception:
                vid = ""
        if vid:
            best_vid = vid
    except Exception:
        vid = ""

    # IMPORTANT: итоговая ссылка должна совпадать со ссылкой из ytcp-video-info (если она была).
    # Канонический watch URL используем только как fallback.
    if not best_url and best_vid:
        best_url = _studio_canonical_watch_url(best_vid)

    if best_url:
        _log(f"Studio: итоговая ссылка: {best_url}")
    if best_vid:
        _log(f"Studio: итоговый videoId: {best_vid}")
    return UploadedStudioResult(video_id=best_vid, url=best_url)


_YOUTUBE_HOME_URL = "https://www.youtube.com/"
_YOUTUBE_SHORTS_FEED_URL = "https://www.youtube.com/shorts/"
_HORIZONTAL_WARMUP_DEFAULT_COUNT = 3
_HORIZONTAL_WARMUP_MIN_WATCH_S = 180.0
_HORIZONTAL_WARMUP_MAX_WATCH_S = 300.0
_HORIZONTAL_SEARCH_RESULTS_WAIT_MS = 2_500
_WATCH_LIKE_BTN_SELECTORS = (
    "ytd-watch-metadata ytd-menu-renderer #top-level-buttons-computed like-button-view-model button",
    "ytd-menu-renderer #top-level-buttons-computed like-button-view-model button",
    "segmented-like-dislike-button-view-model like-button-view-model button",
    "like-button-view-model button.ytSpecButtonShapeNextHost",
    "#top-level-buttons-computed like-button-view-model button",
    "#like-button button",
)
_WATCH_LIKE_BTN_RE = re.compile(
    r"like\s+this\s+video|"
    r"нравится|"
    r"лайк|"
    r"поставить\s+отметку",
    re.I,
)
_WATCH_ALREADY_LIKED_RE = re.compile(
    r"unlike|remove\s+like|"
    r"убрать\s+отметку|"
    r"удалить\s+лайк|"
    r"убрать.*нравится",
    re.I,
)
_YOUTUBE_SEARCH_INPUT_SELECTORS = (
    "input.ytSearchboxComponentInput",
    "input.yt-searchbox-input",
    'input[name="search_query"]',
    "#search-input input",
    'ytd-searchbox input[name="search_query"]',
)
_YOUTUBE_SEARCH_BUTTON_SELECTORS = (
    "button.ytSearchboxComponentSearchButton",
    "#search-icon-legacy",
    'button[aria-label="Search"]',
    'button[aria-label="Поиск"]',
)
_SHORTS_WARMUP_DEFAULT_COUNT = 10
_SHORTS_WARMUP_MIN_WATCH_S = 5.0
_SHORTS_WARMUP_MAX_WATCH_S = 25.0
_SHORTS_WARMUP_DEFAULT_LIKE_PROB_PCT = 10.0
_SHORTS_WARMUP_DEFAULT_SUBSCRIBE_PROB_PCT = 10.0
_SHORTS_LIKE_BTN_RE = re.compile(
    r"like\s+this\s+video|"
    r"лайк|"
    r"нравится|"
    r"поставить\s+лайк",
    re.I,
)
_SHORTS_ALREADY_LIKED_RE = re.compile(
    r"unlike|remove\s+like|убрать|удалить\s+лайк",
    re.I,
)
_SHORTS_SUBSCRIBE_BTN_RE = re.compile(
    r"subscribe\s+to|"
    r"^subscribe\b|"
    r"join\s+.*channel|"
    r"оформить\s+подписку|"
    r"подписаться\s+на|"
    r"^подписаться\b",
    re.I,
)
_SHORTS_ALREADY_SUBSCRIBED_RE = re.compile(
    r"unsubscribe|"
    r"отменить\s+подписку|"
    r"отписаться|"
    r"^subscribed\b|"
    r"^подписан\b|"
    r"^подписаны\b|"
    r"вы\s+подписаны",
    re.I,
)
_SHORTS_SUBSCRIBE_BTN_SELECTORS = (
    "yt-subscribe-button-view-model button",
    ".ytReelChannelBarViewModelReelSubscribeButton button",
    "yt-reel-channel-bar-view-model yt-subscribe-button-view-model button",
    "yt-reel-channel-bar-view-model button.ytSpecButtonShapeNextHost",
    "#subscribe-button button",
    "ytd-subscribe-button-renderer button",
    "ytd-reel-player-overlay-renderer #subscribe-button button",
)
_SHORTS_AD_SELECTORS = (
    "reels-ad-card-buttoned-view-model",
    "yt-ad-metadata-shape",
    "ad-badge-view-model",
    "ad-button-view-model",
    ".ytBadgeShapeAd",
    "[class*='ReelsAdCard']",
    "[class*='ytwReelsAdCard']",
)
_SHORTS_AD_BADGE_RE = re.compile(
    r"^ad$|"
    r"^sponsored$|"
    r"^реклама$|"
    r"реклам",
    re.I,
)


def _studio_is_current_short_an_ad(page) -> bool:
    """Текущий Short — реклама (reels-ad-card / yt-ad-metadata-shape)."""
    for sel in _SHORTS_AD_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=400):
                return True
        except Exception:
            continue
    try:
        badge = page.locator(
            ".ytBadgeShapeAd .ytBadgeShapeText, "
            "ad-badge-view-model .ytBadgeShapeText, "
            "badge-shape.ytBadgeShapeAd .ytBadgeShapeText"
        ).first
        if badge.is_visible(timeout=400):
            text = (badge.inner_text(timeout=500) or "").strip()
            if text and _SHORTS_AD_BADGE_RE.search(text):
                return True
    except Exception:
        pass
    return False


def _studio_advance_shorts_feed(
    page, *, prev_video_id: str = "", log_label: str = ""
) -> str:
    """Прокрутка к следующему Short; возвращает id нового ролика или ''."""
    next_id = _studio_scroll_shorts_to_next(page, prev_video_id=prev_video_id)
    if next_id:
        if log_label:
            _log(f"Shorts: {log_label} ({next_id})")
        return next_id
    _log(
        "Shorts: не удалось перейти к следующему ролику"
        + (f" ({log_label})" if log_label else "")
        + " — повторяем прокрутку…"
    )
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(500)
    return _studio_read_active_short_video_id(page)


def _studio_read_active_short_video_id(page) -> str:
    url = (page.url or "").strip()
    m = re.search(r"/shorts/([A-Za-z0-9_-]{6,})", url, re.I)
    if m:
        return m.group(1)
    try:
        link = page.locator(
            'a.ytp-title-link[href*="/shorts/"], a[href*="/shorts/"][target="_blank"]'
        ).first
        href = (link.get_attribute("href") or "").strip()
        m = re.search(r"/shorts/([A-Za-z0-9_-]{6,})", href, re.I)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


def _studio_dismiss_shorts_player_overlays(page) -> None:
    for sel in ("button.ytp-unmute", ".ytp-unmute"):
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=400):
                btn.click(timeout=2_000)
                page.wait_for_timeout(300)
                return
        except Exception:
            continue


def _studio_page_on_youtube_shorts(page) -> bool:
    try:
        url = (page.url or "").lower()
        return "www.youtube.com" in url and "/shorts" in url
    except Exception:
        return False


def _studio_goto_youtube_shorts_feed(page, *, login_credentials=None) -> None:
    """Открыть ленту YouTube Shorts (не из Studio — иначе goto часто не срабатывает)."""
    if _studio_page_on_youtube_shorts(page):
        _log(f"Shorts: уже на ленте — URL={page.url!r}")
    else:
        _log(f"Shorts: переход на {_YOUTUBE_SHORTS_FEED_URL}…")
        last_url = ""
        for attempt in range(1, 4):
            try:
                page.goto(
                    _YOUTUBE_SHORTS_FEED_URL,
                    wait_until="commit",
                    timeout=45_000,
                )
            except Exception as e:
                _log(f"Shorts: goto попытка {attempt}/3: {e!r}")
            page.wait_for_timeout(600)
            try:
                last_url = page.url or ""
            except Exception:
                last_url = ""
            if _studio_page_on_youtube_shorts(page):
                _log(f"Shorts: лента открыта — URL={last_url!r}")
                break
            if attempt < 3:
                _log(f"Shorts: URL не лента Shorts ({last_url!r}) — повтор…")
        else:
            raise YoutubeStudioError(
                f"Shorts: не удалось открыть ленту Shorts (URL={last_url!r})."
            )
    _studio_try_google_login_if_needed(page, login_credentials)
    if _studio_on_google_auth_page(page):
        _studio_wait_for_google_session(page, login_credentials=login_credentials)
    _studio_wait_shorts_feed_ready(page)


def _studio_wait_shorts_feed_ready(page) -> None:
    _log("Shorts: ожидание ленты…")
    container = page.locator("#shorts-container")
    container.first.wait_for(state="visible", timeout=120_000)
    player = page.locator(
        "#shorts-player, video.html5-main-video, ytd-reel-video-renderer"
    )
    player.first.wait_for(state="visible", timeout=120_000)
    page.wait_for_timeout(1_500)
    _studio_dismiss_shorts_player_overlays(page)


def _studio_scroll_shorts_to_next(
    page, *, prev_video_id: str = "", timeout_s: float = 15.0
) -> str:
    """Прокрутка к следующему Short; возвращает id нового ролика или ''."""
    deadline = time.monotonic() + timeout_s
    container = page.locator("#shorts-container")
    focus = page.locator(
        "#shorts-player, #shorts-container, video.html5-main-video"
    )

    while time.monotonic() < deadline:
        try:
            focus.first.click(timeout=2_000)
        except Exception:
            try:
                container.first.click(timeout=2_000)
            except Exception:
                pass

        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(800)
        vid = _studio_read_active_short_video_id(page)
        if vid and vid != prev_video_id:
            return vid

        try:
            if container.count() > 0:
                container.first.hover(timeout=2_000)
            page.mouse.wheel(0, 900)
        except Exception:
            pass
        page.wait_for_timeout(800)
        vid = _studio_read_active_short_video_id(page)
        if vid and vid != prev_video_id:
            return vid

        page.keyboard.press("PageDown")
        page.wait_for_timeout(800)
        vid = _studio_read_active_short_video_id(page)
        if vid and vid != prev_video_id:
            return vid

    return ""


def _studio_try_like_current_short(page) -> bool:
    """Клик по лайку на текущем Short, если ещё не лайкнут."""
    like_btn = (
        page.locator("like-button-view-model button")
        .or_(page.locator("#like-button button"))
        .or_(
            page.locator(
                "ytd-segmented-like-dislike-button-renderer like-button-view-model button"
            )
        )
        .or_(page.get_by_role("button", name=_SHORTS_LIKE_BTN_RE))
    )
    try:
        btn = like_btn.first
        if not btn.is_visible(timeout=2_000):
            return False
        label = (btn.get_attribute("aria-label") or "").strip()
        if label and _SHORTS_ALREADY_LIKED_RE.search(label):
            _log("Shorts: ролик уже с лайком — пропуск.")
            return False
        btn.click(timeout=3_000)
        page.wait_for_timeout(400)
        _log("Shorts: лайк поставлен.")
        return True
    except Exception as e:
        _log(f"Shorts: не удалось поставить лайк: {type(e).__name__}")
        return False


def _studio_try_subscribe_current_short(page) -> bool:
    """Подписка на канал текущего Short, если ещё не подписан."""
    candidates: list = []
    for sel in _SHORTS_SUBSCRIBE_BTN_SELECTORS:
        candidates.append(page.locator(sel))
    candidates.append(page.get_by_role("button", name=_SHORTS_SUBSCRIBE_BTN_RE))

    for loc in candidates:
        try:
            btn = loc.first
            if not btn.is_visible(timeout=1_000):
                continue
            label = (
                btn.get_attribute("aria-label") or btn.inner_text(timeout=500) or ""
            ).strip()
            if label and _SHORTS_ALREADY_SUBSCRIBED_RE.search(label):
                _log("Shorts: уже подписан на канал — пропуск.")
                return False
            btn.scroll_into_view_if_needed(timeout=2_000)
            btn.click(timeout=3_000)
            page.wait_for_timeout(400)
            _log("Shorts: подписка оформлена.")
            return True
        except Exception:
            continue

    _log("Shorts: кнопка подписки не найдена.")
    return False


def _studio_browse_youtube_shorts(
    page,
    *,
    count: int = _SHORTS_WARMUP_DEFAULT_COUNT,
    like_probability_pct: float = _SHORTS_WARMUP_DEFAULT_LIKE_PROB_PCT,
    subscribe_probability_pct: float = _SHORTS_WARMUP_DEFAULT_SUBSCRIBE_PROB_PCT,
    min_watch_s: float = _SHORTS_WARMUP_MIN_WATCH_S,
    max_watch_s: float = _SHORTS_WARMUP_MAX_WATCH_S,
) -> None:
    """Просмотр count Shorts со случайной паузой min_watch_s–max_watch_s на каждом."""
    n = max(1, int(count))
    like_prob = min(100.0, max(0.0, float(like_probability_pct)))
    subscribe_prob = min(100.0, max(0.0, float(subscribe_probability_pct)))
    watch_min = max(0.1, float(min_watch_s))
    watch_max = max(watch_min, float(max_watch_s))

    log_extra = ""
    if like_prob > 0:
        log_extra += f", лайк {like_prob:g}%"
    if subscribe_prob > 0:
        log_extra += f", подписка {subscribe_prob:g}%"
    _log(
        f"Shorts: просмотр {n} роликов, "
        f"{watch_min:.0f}–{watch_max:.0f} с на каждом"
        f" (лайк/подписка после просмотра{log_extra})…"
    )
    _studio_wait_shorts_feed_ready(page)

    prev_id = _studio_read_active_short_video_id(page)
    watched = 0
    skip_attempts = 0
    max_skip_attempts = max(n * 5, 10)

    while watched < n:
        _studio_dismiss_shorts_player_overlays(page)

        if _studio_is_current_short_an_ad(page):
            skip_attempts += 1
            if skip_attempts > max_skip_attempts:
                _log("Shorts: слишком много рекламы подряд — остановка.")
                break
            _log("Shorts: реклама — пролистываем без просмотра.")
            next_id = _studio_advance_shorts_feed(
                page, prev_video_id=prev_id, log_label=""
            )
            if next_id:
                prev_id = next_id
            continue

        skip_attempts = 0
        watched += 1
        _log(
            f"Shorts: ролик {watched}/{n}"
            + (f" ({prev_id})" if prev_id else "")
        )

        watch_s = random.uniform(watch_min, watch_max)
        _log(f"Shorts: смотрим ~{watch_s:.0f} с…")
        page.wait_for_timeout(int(watch_s * 1000))
        if like_prob > 0 and random.random() * 100.0 < like_prob:
            if _studio_try_like_current_short(page):
                page.wait_for_timeout(2_000)
        if subscribe_prob > 0 and random.random() * 100.0 < subscribe_prob:
            _studio_try_subscribe_current_short(page)

        if watched >= n:
            break

        next_id = _studio_advance_shorts_feed(
            page,
            prev_video_id=prev_id,
            log_label=f"ролик {watched + 1}/{n}",
        )
        if next_id:
            prev_id = next_id

    _log(f"Shorts: прогрев завершён (просмотрено {watched} из {n}).")


def _studio_type_youtube_search_input(page, search_input, query: str) -> bool:
    """Последовательный ввод запроса в строку поиска YouTube."""
    q = (query or "").strip()
    if not q:
        return False
    try:
        search_input.click(timeout=3_000)
        page.wait_for_timeout(120)
        try:
            search_input.evaluate("(node) => { node.focus(); node.select(); }")
        except Exception:
            pass
        page.wait_for_timeout(80)
        try:
            search_input.press("Control+A")
            search_input.press("Backspace")
        except Exception:
            pass
        page.wait_for_timeout(50)
        search_input.press_sequentially(q, delay=18)
        page.wait_for_timeout(400)
        actual = (search_input.input_value(timeout=3_000) or "").strip()
        if not actual:
            page.keyboard.type(q, delay=0)
            page.wait_for_timeout(200)
            actual = (search_input.input_value(timeout=3_000) or "").strip()
        if not actual:
            _log("Горизонтальные видео: поле поиска пустое после ввода.")
            return False
        _log(f"Горизонтальные видео: запрос введён: {actual!r}")
        return True
    except Exception as e:
        _log(f"Горизонтальные видео: не удалось ввести запрос: {type(e).__name__}")
        return False


def _studio_search_youtube(page, query: str) -> bool:
    """Главная YouTube → последовательный ввод запроса → клик по лупе."""
    q = (query or "").strip()
    if not q:
        return False
    _log(f"Горизонтальные видео: переход на {_YOUTUBE_HOME_URL}…")
    page.goto(
        _YOUTUBE_HOME_URL,
        wait_until="domcontentloaded",
        timeout=120_000,
    )
    page.wait_for_timeout(1_000)

    search_input = None
    for sel in _YOUTUBE_SEARCH_INPUT_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=2_000):
                search_input = loc
                break
        except Exception:
            continue
    if search_input is None:
        _log("Горизонтальные видео: поле поиска не найдено.")
        return False

    if not _studio_type_youtube_search_input(page, search_input, q):
        return False

    submitted = False
    for sel in _YOUTUBE_SEARCH_BUTTON_SELECTORS:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1_000):
                btn.click(timeout=3_000)
                submitted = True
                break
        except Exception:
            continue
    if not submitted:
        try:
            page.keyboard.press("Enter")
            submitted = True
        except Exception:
            pass
    if not submitted:
        _log("Горизонтальные видео: не удалось отправить поиск.")
        return False

    try:
        page.wait_for_load_state("domcontentloaded", timeout=120_000)
    except Exception:
        pass
    try:
        page.locator("ytd-search, ytd-video-renderer").first.wait_for(
            state="attached", timeout=15_000
        )
    except Exception:
        pass
    page.wait_for_timeout(_HORIZONTAL_SEARCH_RESULTS_WAIT_MS)
    _log(f"Горизонтальные видео: поиск «{q}» выполнен, выдача загружена.")
    return True


def _studio_collect_search_video_links(page) -> list:
    """Ссылки на ролики только из ytd-video-renderer (не реклама, не каналы)."""
    renderers = page.locator("ytd-video-renderer")
    count = renderers.count()
    links: list = []
    for i in range(count):
        renderer = renderers.nth(i)
        try:
            link_loc = renderer.locator(
                'a#video-title, a[href*="/watch?v="], a[href*="/watch/"]'
            )
            if link_loc.count() == 0:
                continue
            link = link_loc.first
            if not link.is_visible(timeout=500):
                continue
            href = (link.get_attribute("href") or "").strip()
            if "/watch" not in href:
                continue
            links.append(link)
        except Exception:
            continue
    return links


def _studio_pick_search_video_index(link_count: int, iteration: int) -> int:
    """Первое или второе доступное видео; для следующих — сдвиг по выдаче."""
    if link_count <= 0:
        return -1
    if iteration == 0:
        return random.randint(0, min(1, link_count - 1))
    return min(iteration + random.randint(0, 1), link_count - 1)


def _studio_try_like_current_watch_video(page) -> bool:
    """Лайк на странице обычного ролика (ytd-menu-renderer / like-button-view-model)."""
    try:
        page.locator(
            "ytd-watch-metadata ytd-menu-renderer, #top-level-buttons-computed"
        ).first.wait_for(state="attached", timeout=15_000)
    except Exception:
        pass
    page.wait_for_timeout(800)

    candidates: list = [page.locator(sel) for sel in _WATCH_LIKE_BTN_SELECTORS]
    candidates.append(page.get_by_role("button", name=_WATCH_LIKE_BTN_RE))

    for loc in candidates:
        try:
            btn = loc.first
            if not btn.is_visible(timeout=2_000):
                continue
            btn.scroll_into_view_if_needed(timeout=5_000)
            page.wait_for_timeout(200)
            pressed = (btn.get_attribute("aria-pressed") or "").strip().lower()
            if pressed == "true":
                _log("Горизонтальные видео: ролик уже с лайком — пропуск.")
                return False
            label = (btn.get_attribute("aria-label") or "").strip()
            if label and _WATCH_ALREADY_LIKED_RE.search(label):
                _log("Горизонтальные видео: ролик уже с лайком — пропуск.")
                return False
            btn.click(timeout=5_000)
            page.wait_for_timeout(500)
            pressed_after = (btn.get_attribute("aria-pressed") or "").strip().lower()
            if pressed_after == "true":
                _log("Горизонтальные видео: лайк поставлен.")
                return True
            label_after = (btn.get_attribute("aria-label") or "").strip()
            if label_after and _WATCH_ALREADY_LIKED_RE.search(label_after):
                _log("Горизонтальные видео: лайк поставлен.")
                return True
            _log("Горизонтальные видео: лайк поставлен.")
            return True
        except Exception:
            continue

    _log("Горизонтальные видео: кнопка лайка не найдена.")
    return False


def _studio_browse_horizontal_videos(
    page,
    *,
    count: int = _HORIZONTAL_WARMUP_DEFAULT_COUNT,
) -> None:
    """Просмотр горизонтальных роликов из текущей выдачи поиска."""
    n = max(1, int(count))
    _log(
        f"Горизонтальные видео: просмотр {n} роликов, "
        f"{_HORIZONTAL_WARMUP_MIN_WATCH_S:.0f}–{_HORIZONTAL_WARMUP_MAX_WATCH_S:.0f} с на каждом…"
    )

    results_url = (page.url or "").strip()
    watched = 0
    for i in range(n):
        try:
            links = _studio_collect_search_video_links(page)
            if not links:
                _log("Горизонтальные видео: в выдаче нет роликов (ytd-video-renderer).")
                break
            idx = _studio_pick_search_video_index(len(links), i)
            link = links[idx]
            title = (
                link.get_attribute("title")
                or link.get_attribute("aria-label")
                or link.inner_text(timeout=1_000)
                or f"#{idx + 1}"
            ).strip()
            _log(
                f"Горизонтальные видео: ролик {i + 1}/{n} "
                f"(позиция {idx + 1} в выдаче)"
                + (f" — {title[:100]}" if title else "")
            )
            link.click(timeout=5_000)
            page.wait_for_load_state("domcontentloaded", timeout=120_000)
            page.wait_for_timeout(1_000)
            try:
                page.locator("video.html5-main-video").first.wait_for(
                    state="visible", timeout=15_000
                )
            except Exception:
                pass

            watch_s = random.uniform(
                _HORIZONTAL_WARMUP_MIN_WATCH_S, _HORIZONTAL_WARMUP_MAX_WATCH_S
            )
            _log(f"Горизонтальные видео: смотрим ~{watch_s:.0f} с…")
            page.wait_for_timeout(int(watch_s * 1000))

            _studio_try_like_current_watch_video(page)
            page.wait_for_timeout(2_000)

            watched += 1
            if i >= n - 1:
                break
            page.goto(results_url, wait_until="domcontentloaded", timeout=120_000)
            page.wait_for_timeout(_HORIZONTAL_SEARCH_RESULTS_WAIT_MS)
        except Exception as e:
            _log(
                f"Горизонтальные видео: ошибка на ролике {i + 1}: "
                f"{type(e).__name__}"
            )
            break

    _log(f"Горизонтальные видео: просмотрено {watched} из {n}.")


@_studio_entrypoint
def run_youtube_shorts_warmup(
    page,
    *,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    on_oldest_channel_name=None,
    search_oldest_channel: bool = True,
    profile_id: str | None = None,
    shorts_count: int = _SHORTS_WARMUP_DEFAULT_COUNT,
    like_probability_pct: float = _SHORTS_WARMUP_DEFAULT_LIKE_PROB_PCT,
    subscribe_probability_pct: float = _SHORTS_WARMUP_DEFAULT_SUBSCRIBE_PROB_PCT,
    shorts_watch_min_s: float = _SHORTS_WARMUP_MIN_WATCH_S,
    shorts_watch_max_s: float = _SHORTS_WARMUP_MAX_WATCH_S,
    watch_horizontal_videos: bool = False,
    horizontal_search_query: str | None = None,
    horizontal_videos_count: int = _HORIZONTAL_WARMUP_DEFAULT_COUNT,
) -> None:
    """Авторизация, выбор канала → лента Shorts → просмотр с прокруткой."""
    if search_oldest_channel:
        _studio_ensure_correct_studio_channel(
            page,
            yt_oldest_name=yt_oldest_name,
            login_credentials=login_credentials,
            on_oldest_channel_name=on_oldest_channel_name,
            search_oldest_channel=True,
        )
    else:
        _log(
            "Shorts: поиск старого канала отключён — "
            "youtube.com → лента Shorts (без Studio)…"
        )
        _studio_goto_youtube_home(
            page, login_credentials=login_credentials, for_channel_scan=False
        )
    _studio_goto_youtube_shorts_feed(page, login_credentials=login_credentials)
    _studio_browse_youtube_shorts(
        page,
        count=shorts_count,
        like_probability_pct=like_probability_pct,
        subscribe_probability_pct=subscribe_probability_pct,
        min_watch_s=shorts_watch_min_s,
        max_watch_s=shorts_watch_max_s,
    )
    if watch_horizontal_videos:
        query = (horizontal_search_query or "").strip()
        if not query:
            _log("Горизонтальные видео: поисковый запрос пуст — пропуск.")
            return
        if _studio_search_youtube(page, query):
            _studio_browse_horizontal_videos(
                page,
                count=horizontal_videos_count,
            )

