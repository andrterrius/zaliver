from __future__ import annotations

import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError

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
_STUDIO_WARM_WELCOME_NEXT_MAX = 10
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
_SWITCH_ACCOUNT_LABEL_RE = re.compile(
    r"сменить\s+аккаунт|switch\s+account",
    re.I,
)

# Playwright при connect_over_cdp шлёт тело файла по CDP и режет ~50 MiB.
# DOM.setFileInputFiles с путями на хосте браузера обходит это (Chromium читает файл сам).
_PLAYWRIGHT_REMOTE_UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024
# set_files / set_input_files по CDP для крупных файлов; дефолт Playwright 30 с часто мало.
_STUDIO_FILE_PICKER_TRANSFER_MS = 600_000


class YoutubeStudioError(RuntimeError):
    pass


_LOG_SINK = None


def set_log_sink(sink) -> None:
    """
    Optional log sink callback.
    If set, each `_log()` line will be forwarded to `sink(str)`.
    """
    global _LOG_SINK
    _LOG_SINK = sink


def _log(message: str) -> None:
    line = f"[youtube_studio] {message}"
    print(line)
    sink = _LOG_SINK
    if sink is not None:
        try:
            sink(line)
        except Exception:
            # Logging must not break automation flow.
            pass


def _studio_try_google_login_if_needed(page, login_credentials) -> bool:
    """
    При необходимости проходит Google-вход (личность → пароль → 2FA → канал).
    Возвращает True, если попытка была и экран входа снят.
    """
    from zaliver.youtube_upload.google_login import (
        GoogleLoginPasswordMissingError,
        attempt_google_login_for_studio,
        google_auth_interaction_visible,
        handle_channel_switcher_if_present,
    )

    if handle_channel_switcher_if_present(page):
        return True
    if login_credentials is None:
        return False

    if not (_studio_login_required(page) or google_auth_interaction_visible(page)):
        return False
    try:
        attempt_google_login_for_studio(page, login_credentials)
    except GoogleLoginPasswordMissingError:
        raise
    except RuntimeError as e:
        raise YoutubeStudioError(str(e)) from e
    return True


def _studio_login_required(page) -> bool:
    """
    Иногда вместо Studio открывается окно логина Google/YouTube.
    В этом случае на профиле нет активной сессии → залив нужно завершать.
    """
    try:
        url = (page.url or "").lower()
        if "accounts.google.com" in url or "servicelogin" in url:
            return True
    except Exception:
        pass
    try:
        # Пример из репорта пользователя:
        # <h1 id="headingText"><span>Вход</span></h1>
        # "Для перехода к YouTube войдите в свой аккаунт Google."
        login_block = page.locator("div.ObDc3.ZYOIke").first
        if login_block.count() > 0 and login_block.is_visible():
            return True
    except Exception:
        pass
    try:
        if page.locator("#headingText", has_text=re.compile(r"вход|sign\s*in", re.I)).first.is_visible():
            return True
    except Exception:
        pass
    try:
        if page.get_by_text(
            re.compile(r"для\s+перехода\s+к\s+youtube\s+войдите", re.I)
        ).first.is_visible():
            return True
    except Exception:
        pass
    return False


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


def _studio_account_item_removed_label(item) -> str:
    for sel in (
        "yt-formatted-string[secondary]",
        "tp-yt-paper-item-body yt-formatted-string[secondary]",
    ):
        loc = item.locator(sel)
        try:
            if loc.count() > 0:
                label = (loc.first.inner_text(timeout=1_500) or "").strip()
                if label:
                    return label
        except Exception:
            continue
    try:
        body_lines = item.locator("tp-yt-paper-item-body yt-formatted-string")
        if body_lines.count() >= 3:
            label = (body_lines.nth(2).inner_text(timeout=1_500) or "").strip()
            if label:
                return label
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


def _studio_account_item_is_removed(item) -> bool:
    return bool(_CHANNEL_REMOVED_LABEL_RE.search(_studio_account_item_removed_label(item)))


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
    switcher = page.locator(
        'ytd-multi-page-menu-renderer[menu-style="multi-page-menu-style-type-switcher"]'
    ).or_(
        page.locator("ytd-multi-page-menu-renderer").filter(
            has=page.locator(
                "ytd-simple-menu-header-renderer yt-formatted-string",
                has_text=re.compile(r"аккаунты|accounts", re.I),
            )
        )
    )
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
            _log(f"Studio: канал «{name}» пропущен — помечен как удалённый.")
            continue
        available.append((i, name))
    return available


def _studio_open_account_switcher_menu(page) -> None:
    """Профиль → «Сменить аккаунт» → меню выбора канала."""
    avatar = (
        page.locator("yttou-channel-appeal-app #avatar-btn")
        .or_(page.locator("ytd-topbar-menu-button-renderer #avatar-btn"))
        .or_(page.locator("button#avatar-btn"))
    )
    avatar.first.wait_for(state="visible", timeout=15_000)
    avatar.first.click(timeout=30_000)
    page.wait_for_timeout(600)

    switch_item = (
        page.locator("ytd-compact-link-renderer")
        .filter(
            has=page.locator(
                "yt-formatted-string#label", has_text=_SWITCH_ACCOUNT_LABEL_RE
            )
        )
        .locator("tp-yt-paper-item")
        .or_(
            page.locator("ytd-compact-link-renderer yt-formatted-string#label").filter(
                has_text=_SWITCH_ACCOUNT_LABEL_RE
            )
        )
        .or_(page.get_by_text(_SWITCH_ACCOUNT_LABEL_RE))
    )
    switch_item.first.wait_for(state="visible", timeout=15_000)
    switch_item.first.click(timeout=30_000)
    page.wait_for_timeout(800)


def _studio_click_account_switcher_channel(page, item_index: int, channel_name: str) -> None:
    switcher = page.locator(
        'ytd-multi-page-menu-renderer[menu-style="multi-page-menu-style-type-switcher"]'
    ).or_(
        page.locator("ytd-multi-page-menu-renderer").filter(
            has=page.locator(
                "ytd-simple-menu-header-renderer yt-formatted-string",
                has_text=re.compile(r"аккаунты|accounts", re.I),
            )
        )
    )
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


def _studio_wait_after_account_switch(page, *, timeout_s: float = 60.0) -> None:
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
        time.sleep(0.5)


def _studio_handle_channel_removed_if_present(page) -> bool:
    """
    Канал удалён при входе в Studio: профиль → сменить аккаунт → другой канал.
    После переключения возвращаемся в Studio и продолжаем сценарий.
    """
    if not _studio_channel_removed_page_visible(page):
        return False

    _log("Studio: канал удалён/заблокирован — пробуем сменить аккаунт…")
    _studio_open_account_switcher_menu(page)
    available = _studio_collect_available_account_switcher_channels(page)
    if not available:
        raise YoutubeStudioError(
            "YouTube Studio: все каналы в аккаунте удалены или заблокированы — "
            "сменить аккаунт на доступный канал не удалось."
        )

    switcher = page.locator(
        'ytd-multi-page-menu-renderer[menu-style="multi-page-menu-style-type-switcher"]'
    ).or_(
        page.locator("ytd-multi-page-menu-renderer").filter(
            has=page.locator(
                "ytd-simple-menu-header-renderer yt-formatted-string",
                has_text=re.compile(r"аккаунты|accounts", re.I),
            )
        )
    )
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


def _studio_handle_channel_creation_dialog_if_present(page) -> bool:
    """
    Аккаунт без канала: при входе в Studio — диалог «Основные сведения».
    Нажимаем «Создать канал» (имя/псевдоним обычно уже предзаполнены Google).
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
            time.sleep(0.5)
        else:
            raise YoutubeStudioError(
                "YouTube Studio: диалог создания канала не закрылся после «Создать канал»."
            )

    page.wait_for_timeout(800)
    _log("Studio: диалог создания канала закрыт.")
    return True


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
        time.sleep(0.3)
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


def _studio_wait_create_or_login(page, create_locator, *, login_credentials=None) -> None:
    """
    Ждём появления кнопки «Создать», но параллельно проверяем, что нас не выкинуло на логин.
    """
    deadline = time.monotonic() + (_STUDIO_UI_MS / 1000.0)
    while True:
        if _studio_try_google_login_if_needed(page, login_credentials):
            continue
        if _studio_login_required(page):
            raise YoutubeStudioError(
                "YouTube Studio: требуется вход в Google (профиль без активной сессии). "
                "Останавливаем залив для этого профиля."
            )
        if _studio_handle_onboarding_dialogs_if_present(page):
            continue
        try:
            if create_locator.count() > 0 and create_locator.first.is_visible():
                return
        except Exception:
            # transient detach / navigation; continue polling
            pass

        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)

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


def _studio_goto_studio_home_ready(page, *, login_credentials=None):
    """
    studio.youtube.com → логин / онбординг → кнопка «Создать» видна.
    Возвращает локатор кнопки «Создать».
    """
    _log("Studio: переход на https://studio.youtube.com/ …")
    page.goto(
        "https://studio.youtube.com/",
        wait_until="domcontentloaded",
        timeout=120_000,
    )
    _log(f"Studio: после загрузки URL: {page.url!r}")
    _studio_try_google_login_if_needed(page, login_credentials)
    _studio_handle_channel_removed_if_present(page)
    _studio_handle_onboarding_dialogs_if_present(page)

    create = (
        page.locator('ytcp-button-shape button[aria-label="Создать"]')
        .or_(page.locator('ytcp-button-shape button[aria-label="Create"]'))
        .or_(page.get_by_role("button", name=re.compile(r"^создать$|^create$", re.I)))
    )
    _log("Studio: ожидание кнопки «Создать»…")
    _studio_wait_create_or_login(page, create, login_credentials=login_credentials)
    return create


def _studio_click_create_then_add_video(page, *, login_credentials=None) -> None:
    """
    studio.youtube.com → кнопка «Создать» (ytcp-button-shape) → меню ytcp-text-menu
    → пункт «Добавить видео» (test-id=upload).
    Сессия Google должна уже быть в профиле антидетекта (без логина из Zaliver).
    """
    create = _studio_goto_studio_home_ready(page, login_credentials=login_credentials)
    create.first.scroll_into_view_if_needed(timeout=15_000)
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
    _studio_wait_upload_file_picker_visible(
        page, timeout_ms=_STUDIO_UI_MS, login_credentials=login_credentials
    )


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
        time.sleep(0.5)

    raise YoutubeStudioError(
        "YouTube Studio: не дождались окна загрузки (ytcp-uploads-file-picker). "
        "Возможны диалог создания канала, приветствие «Далее» или блокировка аккаунта."
    )


def _studio_clear_contenteditable_like_user(page, contenteditable, *, right_slack: int = 8) -> None:
    """
    Очистка contenteditable: читаем текст, End + запас вправо, Backspace по числу символов.
    """
    try:
        current = contenteditable.first.evaluate(
            "(el) => (el && (el.innerText ?? el.textContent) ? String(el.innerText ?? el.textContent) : '')"
        )
    except Exception:
        current = ""
    n = len(current or "")
    try:
        page.keyboard.press("End")
        for _ in range(right_slack):
            page.keyboard.press("ArrowRight")
    except Exception:
        for _ in range(n + right_slack):
            page.keyboard.press("ArrowRight")
    for _ in range(n):
        page.keyboard.press("Backspace")


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


def _studio_navigate_to_channel_customization(page, *, login_credentials=None) -> None:
    """Studio → «Настройка канала» (тот же путь входа, что проверка доступности)."""
    _studio_goto_studio_home_ready(page, login_credentials=login_credentials)
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
        time.sleep(0.2)
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


def _studio_click_channel_customization_publish(page) -> None:
    """Publish на странице «Настройка канала» (#publish-button, shadow DOM)."""
    _studio_handle_interrupt_dialogs_if_present(page)
    _log("Studio: публикация настроек канала…")

    def _publish_state() -> dict:
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

    pub = _publish_state()
    if pub.get("found") and pub.get("enabled") and _click_publish():
        _log("Studio: ожидание 5 с после «Опубликовать»…")
        time.sleep(5.0)
        _log("Studio: настройки канала опубликованы.")
        return

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        pub = _publish_state()
        if pub.get("found") and pub.get("enabled") and _click_publish():
            _log("Studio: ожидание 5 с после «Опубликовать»…")
            time.sleep(5.0)
            _log("Studio: настройки канала опубликованы.")
            return

        try:
            btn = page.locator("#publish-button button").first
            if btn.is_enabled():
                btn.scroll_into_view_if_needed(timeout=2_000)
                btn.click(timeout=10_000)
                _log("Studio: ожидание 5 с после «Опубликовать»…")
                time.sleep(5.0)
                _log("Studio: настройки канала опубликованы.")
                return
        except Exception:
            pass

        page.wait_for_timeout(150)

    pub = _publish_state()
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


def run_studio_channel_description_and_link(
    page,
    *,
    description: str | None = None,
    link_title: str | None = None,
    link_url: str | None = None,
    login_credentials=None,
) -> None:
    """Studio → «Настройка канала» → описание, ссылка → «Опубликовать»."""
    _studio_navigate_to_channel_customization(page, login_credentials=login_credentials)
    _studio_fill_channel_description_and_link(
        page,
        description=description,
        link_title=link_title,
        link_url=link_url,
    )


def verify_studio_upload_dialog_available(page, *, login_credentials=None) -> None:
    """
    Проверка доступности YouTube Studio до окна загрузки (без выбора файла).
    Успех — видим ytcp-uploads-file-picker («Выбрать файлы»).
    Тот же путь, что залив: Studio → создание канала / «Далее» → Создать → Добавить видео.
    """
    _studio_click_create_then_add_video(page, login_credentials=login_credentials)
    _log("Studio: окно загрузки видео доступно — проверка успешна.")


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


def _studio_upload_pick_file(page, video_path: str, *, login_credentials=None) -> None:
    """Диалог ytcp-uploads-file-picker: «Выбрать файлы» + файл (file chooser или input Filedata)."""
    p = Path(video_path).expanduser()
    # На Windows иногда бывает гонка: файл только что "сохранён",
    # но ещё недоступен для открытия из другого процесса на мгновение.
    _log(f"Studio: проверка файла перед загрузкой: raw={video_path!r}, expanded={str(p)!r}")
    file_wait_deadline = time.monotonic() + 6.0
    last_stat_err: Exception | None = None
    while True:
        try:
            if p.is_file():
                break
        except Exception as e:
            last_stat_err = e
        if time.monotonic() >= file_wait_deadline:
            raise YoutubeStudioError(
                f"Видеофайл не найден/не доступен: {video_path!r}. "
                f"expanded={str(p)!r}, last_stat_err={last_stat_err!r}"
            )
        time.sleep(0.5)

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

    if sz >= _PLAYWRIGHT_REMOTE_UPLOAD_LIMIT_BYTES:
        _log(
            f"Studio: файл {sz} байт (не меньше лимита Playwright для передачи по CDP) — "
            "DOM.setFileInputFiles по локальному пути…"
        )
        frame = _studio_file_input_frame(picker, select_btn, page)
        try:
            fu = frame.url
        except Exception:
            fu = "(url недоступен)"
        _log(f"Studio: CDP — фрейм поля Filedata: {fu!r}")
        if not _studio_set_file_input_via_cdp(page, frame, resolved):
            raise YoutubeStudioError(
                "Не удалось привязать большой файл к полю загрузки Studio через CDP. "
                "Нужен доступ к тому же диску, что и у Chromium (обычно тот же ПК, что и Zaliver)."
            )
    else:
        last_pick_err: Exception | None = None
        for attempt in range(1, 4):
            try:
                _log(f"Studio: «Выбрать файлы» + file chooser… (попытка {attempt}/3)")
                with page.expect_file_chooser(timeout=600_000) as fc_info:
                    select_btn.first.click(timeout=600_000)
                fc_info.value.set_files(resolved, timeout=_STUDIO_FILE_PICKER_TRANSFER_MS)
                last_pick_err = None
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
                    break
                except Exception as e2:
                    last_pick_err = e2
                    err_t = str(e2).lower()
                    if "50" in err_t and "mb" in err_t:
                        _log(
                            "Studio: срабатывает обход лимита ~50 MiB — CDP DOM.setFileInputFiles…"
                        )
                        frame = _studio_file_input_frame(picker, select_btn, page)
                        if _studio_set_file_input_via_cdp(page, frame, resolved):
                            last_pick_err = None
                            break
                        raise YoutubeStudioError(
                            "Видео слишком велико для передачи в браузер через Playwright по CDP; "
                            "обход через DOM.setFileInputFiles не удался."
                        ) from e2
                    _log(
                        f"Studio: fallback set_input_files не удался: {e2!r}. "
                        "Ждём 0.5s и повторяем…"
                    )
                    time.sleep(0.5)
        if last_pick_err is not None:
            raise last_pick_err
    try:
        sz_log = p.stat().st_size
    except OSError:
        sz_log = -1
    _log(f"Studio: файл передан — {p.name!r}, байт: {sz_log}.")


def _studio_set_title_and_description(page, *, title: str | None, description: str | None) -> None:
    """
    Заполнение полей «Название» и «Описание» в диалоге загрузки Studio.

    Поля в Studio — contenteditable div#textbox внутри ytcp-social-suggestion-input.
    Для надёжности используем клик → Ctrl+A → ввод текста.
    """
    t = (title or "").strip()
    d = (description or "").strip()
    if not t and not d:
        return

    _studio_handle_interrupt_dialogs_if_present(page)

    editor = page.locator("ytcp-video-metadata-editor#details").or_(
        page.locator("ytcp-video-metadata-editor")
    )
    try:
        editor.first.wait_for(state="visible", timeout=180_000)
    except Exception:
        # Studio иногда показывает мастера позже; не делаем это фатальным.
        _log("Studio: метаданные (details) не видны — пропуск заполнения title/description.")
        return

    def _clear_like_user(contenteditable, *, right_slack: int = 8) -> None:
        """
        Очистка: читаем содержимое поля, ставим курсор в конец (End + запас вправо)
        и нажимаем Backspace ровно столько раз, сколько символов в поле.
        """
        try:
            current = contenteditable.first.evaluate(
                "(el) => (el && (el.innerText ?? el.textContent) ? String(el.innerText ?? el.textContent) : '')"
            )
        except Exception:
            current = ""
        n = len(current or "")
        # Фокус уже должен быть в поле.
        try:
            page.keyboard.press("End")
            for _ in range(right_slack):
                page.keyboard.press("ArrowRight")
        except Exception:
            # Иногда End не отрабатывает (layout/OS), тогда дожимаем стрелкой.
            for _ in range(n + right_slack):
                page.keyboard.press("ArrowRight")
        for _ in range(n):
            page.keyboard.press("Backspace")

    def _fill(
        contenteditable,
        text: str,
        *,
        clear_first: bool = False,
        right_slack: int = 8,
    ) -> None:
        contenteditable.first.wait_for(state="visible", timeout=60_000)
        contenteditable.first.click(timeout=30_000)
        if clear_first:
            _clear_like_user(contenteditable, right_slack=right_slack)
            page.wait_for_timeout(80)
        page.keyboard.type(text, delay=0)
        page.wait_for_timeout(150)

    if t:
        _log("Studio: заполнение поля «Название»…")
        title_box = (
            editor.first.locator("ytcp-video-title #textbox")
            .or_(editor.first.locator("#title-wrapper #textbox"))
            .or_(page.locator("ytcp-video-title #textbox"))
        )
        _fill(title_box, t, clear_first=True, right_slack=10)

    if d:
        _log("Studio: заполнение поля «Описание»…")
        desc_box = (
            editor.first.locator("ytcp-video-description #textbox")
            .or_(editor.first.locator("#description-wrapper #textbox"))
            .or_(page.locator("ytcp-video-description #textbox"))
        )
        _fill(desc_box, d, clear_first=bool(d))


def _studio_select_not_for_kids(page) -> None:
    """«Нет, это видео не для детей» / «No, it's not made for kids»."""
    _log(
        "Studio: «Нет, это видео не для детей» / "
        "«No, it's not made for kids»…"
    )
    kids_select = page.locator("ytkc-made-for-kids-select").or_(
        page.locator(".made-for-kids-rating-container")
    )
    not_kids = (
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
    btn = not_kids.first
    deadline = time.monotonic() + 90.0
    while time.monotonic() < deadline:
        _studio_handle_interrupt_dialogs_if_present(page)
        try:
            if btn.is_visible():
                break
        except Exception:
            pass
        time.sleep(0.5)
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


def _studio_click_next_until_visibility(page) -> None:
    """«Далее» / Next пока не появится выбор доступа (#privacy-radios / PUBLIC)."""
    _log("Studio: «Далее» до экрана доступа…")
    public_radio = (
        page.locator(
            "ytcp-video-visibility-select tp-yt-paper-radio-group#privacy-radios "
            'tp-yt-paper-radio-button[name="PUBLIC"]'
        )
        .or_(page.locator('ytcp-video-visibility-select tp-yt-paper-radio-button[name="PUBLIC"]'))
    )
    for i in range(_STUDIO_WIZARD_NEXT_MAX):
        _studio_handle_interrupt_dialogs_if_present(page)
        if public_radio.first.is_visible():
            _log(f"Studio: экран доступа виден (шаг {i}).")
            return
        nxt = page.get_by_role("button", name=re.compile(r"^далее$|^next$", re.I))
        if nxt.count() > 0 and nxt.first.is_visible():
            _log(f"Studio: «Далее» ({i + 1}/{_STUDIO_WIZARD_NEXT_MAX})…")
            nxt.first.click(timeout=15_000)
            page.wait_for_timeout(500)
            continue
        page.wait_for_timeout(400)

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


def _studio_wait_after_upload_studio_outcome(page, browser, max_wait_sec: float) -> None:
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
        time.sleep(min(_POST_UPLOAD_QUOTA_POLL_S, max(0.3, remaining)))
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


def run_upload_latest_ready_video(
    *,
    page,
    browser,
    zaliver_db_path: Path | None,
    video_path: str | None = None,
    title: str | None = None,
    description: str | None = None,
    login_credentials=None,
) -> None:
    """
    Полный сценарий Studio: Create → Upload → ждать outcome → мастер → Publish.
    """
    best_url = ""
    best_vid = ""

    _studio_click_create_then_add_video(page, login_credentials=login_credentials)
    chosen = (video_path or "").strip()
    if not chosen:
        chosen = resolve_latest_zaliver_video_on_disk(db_path=zaliver_db_path)
    _log(f"Studio: файл для загрузки: {chosen!r}")
    _studio_upload_pick_file(page, chosen, login_credentials=login_credentials)
    _studio_set_title_and_description(page, title=title, description=description)
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
    time.sleep(5.0)
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

