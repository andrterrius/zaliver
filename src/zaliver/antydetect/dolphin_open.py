from __future__ import annotations

import re
import time
from pathlib import Path

from zaliver.antydetect.api import DolphinAntyError, DolphinAntyLocalAPI

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

_STUDIO_UI_MS = 120_000
_POST_UPLOAD_WAIT_S = 60.0
_STUDIO_WIZARD_NEXT_MAX = 30


_LOG_SINK = None


def set_log_sink(sink) -> None:
    """
    Optional log sink callback.
    If set, each `_log()` line will be forwarded to `sink(str)`.
    """
    global _LOG_SINK
    _LOG_SINK = sink


def _log(message: str) -> None:
    line = f"[dolphin_open] {message}"
    print(line)
    sink = _LOG_SINK
    if sink is not None:
        try:
            sink(line)
        except Exception:
            # Logging must not break automation flow.
            pass


def _resolve_latest_zaliver_video_on_disk(*, db_path: Path | None = None) -> str:
    """Путь к последнему по БД видео, для которого файл ещё есть на диске."""
    from zaliver.db.video_store import VideoStore

    store = VideoStore(db_path=db_path)
    for row in store.list_videos(500):
        try:
            p = Path(str(row.path)).expanduser()
            if p.is_file():
                _log(f"Каталог Zaliver: последний файл на диске id={row.id}, длина пути {len(str(p))} симв.")
                return str(p.resolve())
        except OSError:
            continue
    raise DolphinAntyError(
        "В каталоге обработанных видео Zaliver нет записи с существующим файлом на диске. "
        "Добавьте результат в «Готовые видео» или проверьте, что файлы не удалены."
    )


def _studio_click_create_then_add_video(page) -> None:
    """
    studio.youtube.com → кнопка «Создать» (ytcp-button-shape) → меню ytcp-text-menu
    → пункт «Добавить видео» (test-id=upload).
    Сессия Google должна уже быть в профиле Dolphin (без логина из Zaliver).
    """
    _log("Studio: переход на https://studio.youtube.com/ …")
    page.goto("https://studio.youtube.com/", wait_until="domcontentloaded")
    _log(f"Studio: после загрузки URL: {page.url!r}")

    create = (
        page.locator('ytcp-button-shape button[aria-label="Создать"]')
        .or_(page.locator('ytcp-button-shape button[aria-label="Create"]'))
        .or_(page.get_by_role("button", name=re.compile(r"^создать$|^create$", re.I)))
    )
    _log("Studio: ожидание кнопки «Создать»…")
    create.first.wait_for(state="visible", timeout=_STUDIO_UI_MS)
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
        .or_(menu.first.get_by_role("menuitem", name=re.compile(r"добавить видео|upload\s*video", re.I)))
    )
    _log("Studio: клик по пункту «Добавить видео»…")
    upload_item.first.wait_for(state="visible", timeout=20_000)
    upload_item.first.click(timeout=30_000)
    page.wait_for_timeout(500)
    _log(f"Studio: после «Добавить видео» URL: {page.url!r}")


def _studio_upload_pick_file(page, video_path: str) -> None:
    """Диалог ytcp-uploads-file-picker: «Выбрать файлы» + файл (file chooser или input Filedata)."""
    p = Path(video_path).expanduser()
    if not p.is_file():
        raise DolphinAntyError(f"Видеофайл не найден: {video_path!r}")

    _log("Studio: ожидание ytcp-uploads-file-picker…")
    picker = page.locator("ytcp-uploads-file-picker#ytcp-uploads-dialog-file-picker").or_(
        page.locator("ytcp-uploads-file-picker")
    )
    picker.first.wait_for(state="visible", timeout=120_000)

    select_btn = (
        picker.first.locator("#select-files-button button[aria-label='Выбрать файлы']")
        .or_(picker.first.locator("#select-files-button button[aria-label='Select files']"))
        .or_(picker.first.locator("ytcp-button#select-files-button button"))
        .or_(picker.first.get_by_role("button", name=re.compile(r"выбрать файлы|select files", re.I)))
    )
    resolved = str(p.resolve())
    try:
        _log("Studio: «Выбрать файлы» + file chooser…")
        with page.expect_file_chooser(timeout=60_000) as fc_info:
            select_btn.first.click(timeout=30_000)
        fc_info.value.set_files(resolved)
    except Exception as e:
        _log(f"Studio: file chooser не сработал ({e!r}), set_input_files на input[name=Filedata]…")
        picker.first.locator('input[type="file"][name="Filedata"]').set_input_files(resolved)
    try:
        sz = p.stat().st_size
    except OSError:
        sz = -1
    _log(f"Studio: файл передан — {p.name!r}, байт: {sz}.")


def _studio_select_not_for_kids(page) -> None:
    """Блок ytkc-made-for-kids-select: «Нет, это видео не для детей» (VIDEO_MADE_FOR_KIDS_NOT_MFK)."""
    _log("Studio: «Нет, это видео не для детей»…")
    not_kids = (
        page.locator(
            'ytkc-made-for-kids-select tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]'
        )
        .or_(page.locator('.made-for-kids-group tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]'))
        .or_(page.locator('tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]'))
        .or_(page.get_by_role("radio", name=re.compile(r"не для детей|not.*made for kids", re.I)))
    )
    not_kids.first.wait_for(state="visible", timeout=90_000)
    not_kids.first.click(timeout=15_000)


def _studio_click_next_until_visibility(page) -> None:
    """Кнопки «Далее» / Next, пока не появится выбор доступа (#privacy-radios / PUBLIC)."""
    _log("Studio: «Далее» до экрана доступа…")
    public_radio = (
        page.locator(
            "ytcp-video-visibility-select tp-yt-paper-radio-group#privacy-radios "
            'tp-yt-paper-radio-button[name="PUBLIC"]'
        )
        .or_(page.locator('ytcp-video-visibility-select tp-yt-paper-radio-button[name="PUBLIC"]'))
    )
    for i in range(_STUDIO_WIZARD_NEXT_MAX):
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
        raise DolphinAntyError(
            "Не появился экран выбора доступа к видео (ytcp-video-visibility-select / PUBLIC)."
        )


def _studio_select_public_visibility(page) -> None:
    """«Открытый доступ» — tp-yt-paper-radio-button[name=PUBLIC]."""
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


def _studio_click_publish(page) -> None:
    """Кнопка «Опубликовать» / Publish."""
    _log("Studio: «Опубликовать»…")
    btn = (
        page.locator('ytcp-button-shape button[aria-label="Опубликовать"]')
        .or_(page.locator('ytcp-button-shape button[aria-label="Publish"]'))
        .or_(page.get_by_role("button", name=re.compile(r"опубликовать|publish", re.I)))
    )
    btn.first.wait_for(state="visible", timeout=90_000)
    btn.first.click(timeout=30_000)
    _log("Studio: «Опубликовать» нажата.")


def _studio_publish_flow_after_upload(page) -> None:
    """После паузы: не для детей → Далее… → открытый доступ → Опубликовать."""
    _studio_select_not_for_kids(page)
    _studio_click_next_until_visibility(page)
    _studio_select_public_visibility(page)
    _studio_click_publish(page)


def open_google_in_profile(
    profile_id: str,
    *,
    local_token: str | None = None,
    headless: bool = True,
    upload_latest_zaliver_video: bool = True,
    zaliver_db_path: Path | None = None,
) -> None:

    _log(
        f"Старт open_google_in_profile: profile_id={profile_id!r}, headless={headless}, "
        f"local_token={'да' if (local_token or '').strip() else 'нет'}, "
        f"upload_latest_zaliver_video={upload_latest_zaliver_video}."
    )

    api = DolphinAntyLocalAPI()
    try:
        tok = (local_token or "").strip()
        if tok:
            _log("Local API: авторизация по токену…")
            api.login_with_token(tok)
            _log("Local API: login_with_token завершён.")

        _log(f"Dolphin: запуск профиля (headless={headless})…")
        conn = api.start_profile(profile_id, headless=headless)
        _log(f"Dolphin: профиль запущен, CDP port={conn.port}, ws_endpoint={conn.ws_endpoint!r}.")

        with sync_playwright() as p:
            browser = None
            last_err: Exception | None = None
            for endpoint in (conn.ws_url(), conn.http_url()):
                _log(f"Playwright: подключение CDP к {endpoint!r}…")
                try:
                    browser = p.chromium.connect_over_cdp(endpoint)
                    last_err = None
                    _log("Playwright: connect_over_cdp успешно.")
                    break
                except PlaywrightError as e:
                    last_err = e
                    _log(f"Playwright: ошибка подключения к {endpoint!r}: {e!r}")

            if browser is None:
                _log("Playwright: браузер не подключён ни по одному endpoint.")
                raise DolphinAntyError(f"CDP connect failed for both endpoints. Last error: {last_err!r}")

            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            _log(f"Playwright: страниц в контексте: {len(context.pages)}.")

            _studio_click_create_then_add_video(page)

            if upload_latest_zaliver_video:
                latest_path = _resolve_latest_zaliver_video_on_disk(db_path=zaliver_db_path)
                _studio_upload_pick_file(page, latest_path)
                _log(
                    f"Пауза {_POST_UPLOAD_WAIT_S:.0f} с после загрузки файла "
                    f"(только текущий воркер; UI приложения не блокируется)…"
                )
                time.sleep(_POST_UPLOAD_WAIT_S)
                _studio_publish_flow_after_upload(page)
                time.sleep(_STUDIO_WIZARD_NEXT_MAX)
            else:
                _log("Загрузка файла из каталога Zaliver отключена (upload_latest_zaliver_video=False).")

            _log("Playwright: browser.close…")
            browser.close()
            _log("Playwright: browser.close выполнен.")
    finally:
        _log("Local API: api.close…")
        api.close()
        _log("Local API: api.close завершён.")
