from __future__ import annotations

import os
import re
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError


_STUDIO_UI_MS = 120_000
# После передачи файла ждём в Studio один из исходов: лимит или завершение проверок (часто >1 мин).
_POST_UPLOAD_STUDIO_OUTCOME_MAX_S = 3600.0
_POST_UPLOAD_QUOTA_POLL_S = 2.0
_STUDIO_WIZARD_NEXT_MAX = 30

# Playwright при connect_over_cdp шлёт тело файла по CDP и режет ~50 MiB.
# DOM.setFileInputFiles с путями на хосте браузера обходит это (Chromium читает файл сам).
_PLAYWRIGHT_REMOTE_UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024


class YoutubeStudioError(RuntimeError):
    pass


# Тег профиля локального антидетекта при неуспешной проверке доступности Studio.
STUDIO_AVAILABILITY_ERROR_TAG = "ОШИБКА ПРОВЕРКИ ДОСТУПНОСТИ"


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


def _studio_wait_create_or_login(page, create_locator) -> None:
    """
    Ждём появления кнопки «Создать», но параллельно проверяем, что нас не выкинуло на логин.
    """
    deadline = time.monotonic() + (_STUDIO_UI_MS / 1000.0)
    while True:
        if _studio_login_required(page):
            raise YoutubeStudioError(
                "YouTube Studio: требуется вход в Google (профиль без активной сессии). "
                "Останавливаем залив для этого профиля."
            )
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


def _studio_click_create_then_add_video(page) -> None:
    """
    studio.youtube.com → кнопка «Создать» (ytcp-button-shape) → меню ytcp-text-menu
    → пункт «Добавить видео» (test-id=upload).
    Сессия Google должна уже быть в профиле антидетекта (без логина из Zaliver).
    """
    _log("Studio: переход на https://studio.youtube.com/ …")
    page.goto(
        "https://studio.youtube.com/",
        wait_until="domcontentloaded",
        timeout=120_000,
    )
    _log(f"Studio: после загрузки URL: {page.url!r}")

    create = (
        page.locator('ytcp-button-shape button[aria-label="Создать"]')
        .or_(page.locator('ytcp-button-shape button[aria-label="Create"]'))
        .or_(page.get_by_role("button", name=re.compile(r"^создать$|^create$", re.I)))
    )
    _log("Studio: ожидание кнопки «Создать»…")
    _studio_wait_create_or_login(page, create)
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


def _studio_upload_file_picker_locator(page):
    return page.locator(
        "ytcp-uploads-file-picker#ytcp-uploads-dialog-file-picker"
    ).or_(page.locator("ytcp-uploads-file-picker"))


def verify_studio_upload_dialog_available(page) -> None:
    """
    Проверка доступности YouTube Studio до окна загрузки (без выбора файла).
    Успех — видим ytcp-uploads-file-picker («Выбрать файлы»).
    """
    _studio_click_create_then_add_video(page)
    picker = _studio_upload_file_picker_locator(page)
    _log("Studio: ожидание окна загрузки видео (ytcp-uploads-file-picker)…")
    picker.first.wait_for(state="visible", timeout=120_000)
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


def _studio_upload_pick_file(page, video_path: str) -> None:
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

    _log("Studio: ожидание ytcp-uploads-file-picker…")
    picker = _studio_upload_file_picker_locator(page)
    picker.first.wait_for(state="visible", timeout=120_000)

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
                fc_info.value.set_files(resolved)
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
                        resolved
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

    editor = page.locator("ytcp-video-metadata-editor#details").or_(
        page.locator("ytcp-video-metadata-editor")
    )
    try:
        editor.first.wait_for(state="visible", timeout=180_000)
    except Exception:
        # Studio иногда показывает мастера позже; не делаем это фатальным.
        _log("Studio: метаданные (details) не видны — пропуск заполнения title/description.")
        return

    def _clear_like_user(contenteditable) -> None:
        """
        Очистка как просили: читаем содержимое поля на странице, ставим курсор в конец
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
        except Exception:
            # Иногда End не отрабатывает (layout/OS), тогда дожимаем стрелкой.
            for _ in range(n + 8):
                page.keyboard.press("ArrowRight")
        for _ in range(n):
            page.keyboard.press("Backspace")

    def _fill(contenteditable, text: str, *, clear_first: bool = False) -> None:
        contenteditable.first.wait_for(state="visible", timeout=60_000)
        contenteditable.first.click(timeout=30_000)
        if clear_first:
            _clear_like_user(contenteditable)
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
        _fill(title_box, t, clear_first=True)

    if d:
        _log("Studio: заполнение поля «Описание»…")
        desc_box = (
            editor.first.locator("ytcp-video-description #textbox")
            .or_(editor.first.locator("#description-wrapper #textbox"))
            .or_(page.locator("ytcp-video-description #textbox"))
        )
        _fill(desc_box, d, clear_first=bool(d))


def _studio_select_not_for_kids(page) -> None:
    """«Нет, это видео не для детей» (VIDEO_MADE_FOR_KIDS_NOT_MFK)."""
    _log("Studio: «Нет, это видео не для детей»…")
    not_kids = (
        page.locator(
            'ytkc-made-for-kids-select tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]'
        )
        .or_(
            page.locator(
                '.made-for-kids-group tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]'
            )
        )
        .or_(page.locator('tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]'))
        .or_(
            page.get_by_role(
                "radio", name=re.compile(r"не для детей|not.*made for kids", re.I)
            )
        )
    )
    not_kids.first.wait_for(state="visible", timeout=90_000)
    not_kids.first.click(timeout=15_000)


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
    _log("Studio: «Опубликовать»…")
    btn = (
        page.locator('ytcp-button-shape button[aria-label="Опубликовать"]')
        .or_(page.locator('ytcp-button-shape button[aria-label="Publish"]'))
        .or_(page.get_by_role("button", name=re.compile(r"опубликовать|publish", re.I)))
    )
    btn.first.wait_for(state="visible", timeout=90_000)
    btn.first.click(timeout=30_000)
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


def _studio_is_upload_checks_completed(page) -> bool:
    """
    ytcp-video-upload-progress: проверки завершены (атрибут или подпись «Проверка завершена…»).
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
    label = page.locator(
        "ytcp-uploads-dialog ytcp-video-upload-progress .progress-label"
    )
    try:
        if label.count() == 0:
            return False
        if not label.first.is_visible(timeout=2_000):
            return False
        t = (label.first.inner_text(timeout=3_000) or "").strip().lower()
    except Exception:
        return False
    if not t:
        return False
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
        "Studio: ожидание результата после загрузки — «Загрузка недоступна» "
        "или «Проверка завершена… нарушений не найдено»…"
    )
    deadline = time.monotonic() + max_wait_sec
    last_label: str = ""
    while time.monotonic() < deadline:
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
            _log("Studio: проверки видео завершены успешно — переход к шагу «не для детей».")
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
) -> None:
    """
    Полный сценарий Studio: Create → Upload → ждать outcome → мастер → Publish.
    """
    best_url = ""
    best_vid = ""

    _studio_click_create_then_add_video(page)
    chosen = (video_path or "").strip()
    if not chosen:
        chosen = resolve_latest_zaliver_video_on_disk(db_path=zaliver_db_path)
    _log(f"Studio: файл для загрузки: {chosen!r}")
    _studio_upload_pick_file(page, chosen)
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

