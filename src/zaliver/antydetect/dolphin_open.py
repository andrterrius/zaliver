from __future__ import annotations

import os
import re
import time
from pathlib import Path

from zaliver.antydetect.api import DolphinAntyError, DolphinAntyLocalAPI

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

_STUDIO_UI_MS = 120_000
# После передачи файла ждём в Studio один из исходов: лимит или завершение проверок (часто >1 мин).
_POST_UPLOAD_STUDIO_OUTCOME_MAX_S = 1800.0
_POST_UPLOAD_QUOTA_POLL_S = 2.0
_STUDIO_WIZARD_NEXT_MAX = 30

# Playwright при connect_over_cdp шлёт тело файла по CDP и режет ~50 MiB.
# DOM.setFileInputFiles с путями на хосте браузера обходит это (Chromium читает файл сам).
_PLAYWRIGHT_REMOTE_UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024


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


def _studio_file_input_frame(picker, select_btn, page) -> object:
    """
    Фрейм документа с Filedata (часто iframe). У Studio поле иногда монтируется только после
    клика по «Выбрать файлы»; без клика wait_for(attached) висит до таймаута.
    Сначала пробуем короткое ожидание, затем DOM-событие click (не тот же путь, что
    expect_file_chooser + set_files).
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
            raise DolphinAntyError(
                "Не найдено поле загрузки Filedata в диалоге Studio. "
                "Проверьте язык/версию интерфейса YouTube или повторите после обновления страницы."
            ) from e3
    try:
        handle = finp.element_handle(timeout=30_000)
    except PlaywrightError as e:
        _log(f"Studio: element_handle для Filedata: {e!r}")
        raise DolphinAntyError("Не удалось получить элемент Filedata для CDP.") from e
    frame = handle.owner_frame()
    if frame is None:
        raise DolphinAntyError("Не удалось определить фрейм для поля загрузки Filedata.")
    return frame


def _studio_cdp_chrome_file_path(local_path: str) -> str:
    """Абсолютный путь в форме, удобной для Chromium на Windows (Dolphin = локальный диск)."""
    p = Path(local_path).expanduser().resolve()
    s = os.path.normpath(str(p))
    return s


def _studio_cdp_set_file_input_on_target_once(target, files_path: str) -> bool:
    """
    Одна попытка: CDP-сессия к конкретному Page|Frame и DOM.setFileInputFiles.
    Любое необработанное исключение (в т.ч. new_cdp_session для части фреймов Dolphin) логируется.
    """
    ctx = getattr(target, "context", None) or target.page.context
    session = None
    search_id: str | None = None
    try:
        session = ctx.new_cdp_session(target)
        session.send("DOM.enable", {})

        # Классический CDP-путь без Playwright set_input_files (лимит ~50 MiB при connect_over_cdp):
        # DOM.getDocument → DOM.querySelector(document) → DOM.setFileInputFiles(пути на диске браузера).
        doc_params: dict = {"depth": -1}
        try:
            snap = session.send("DOM.getDocument", {**doc_params, "pierce": True})
        except Exception:
            snap = session.send("DOM.getDocument", doc_params)
        root_id = int((snap.get("root") or {}).get("nodeId") or 0)
        if root_id > 0:
            for sel in ('input[type="file"][name="Filedata"]', 'input[type="file"]'):
                try:
                    qs = session.send("DOM.querySelector", {"nodeId": root_id, "selector": sel})
                except Exception as qe:
                    _log(f"Studio: CDP DOM.querySelector({sel!r}): {qe!r}")
                    continue
                nid = int(qs.get("nodeId") or 0)
                if nid <= 0:
                    continue
                try:
                    session.send("DOM.setFileInputFiles", {"nodeId": nid, "files": [files_path]})
                    _log(f"Studio: CDP getDocument+querySelector({sel!r}) → setFileInputFiles ок.")
                    return True
                except Exception as e:
                    _log(f"Studio: CDP setFileInputFiles после querySelector({sel!r}): {e!r}")
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
            search = session.send("DOM.performSearch", {**params, "includeUserAgentShadowDOM": True})
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
            session.send("DOM.setFileInputFiles", {"nodeId": node_id, "files": [files_path]})
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
    У Dolphin connect_over_cdp иногда зависает или падает page.frames() — не полагаемся на него слепо.
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
        _log(f"Studio: CDP setFileInputFiles — цель {i + 1}/{len(order)} ({type(tgt).__name__})…")
        if _studio_cdp_set_file_input_on_target_once(tgt, files_path):
            return True
    _log("Studio: CDP setFileInputFiles — все цели исчерпаны, успеха нет.")
    return False


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
        sz = p.stat().st_size
    except OSError:
        sz = -1

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
            raise DolphinAntyError(
                "Не удалось привязать большой файл к полю загрузки Studio через CDP. "
                "Нужен доступ к тому же диску, что и у Chromium Dolphin (обычно тот же ПК, что и Zaliver)."
            )
    else:
        try:
            _log("Studio: «Выбрать файлы» + file chooser…")
            with page.expect_file_chooser(timeout=60_000) as fc_info:
                select_btn.first.click(timeout=30_000)
            fc_info.value.set_files(resolved)
        except Exception as e:
            _log(f"Studio: file chooser не сработал ({e!r}), set_input_files на input[name=Filedata]…")
            try:
                picker.first.locator('input[type="file"][name="Filedata"]').set_input_files(resolved)
            except Exception as e2:
                err_t = str(e2).lower()
                if "50" in err_t and "mb" in err_t:
                    _log("Studio: срабатывает обход лимита ~50 MiB — CDP DOM.setFileInputFiles…")
                    frame = _studio_file_input_frame(picker, select_btn, page)
                    if not _studio_set_file_input_via_cdp(page, frame, resolved):
                        raise DolphinAntyError(
                            "Видео слишком велико для передачи в браузер через Playwright по CDP; "
                            "обход через DOM.setFileInputFiles не удался."
                        ) from e2
                else:
                    raise e2 from e
    try:
        sz_log = p.stat().st_size
    except OSError:
        sz_log = -1
    _log(f"Studio: файл передан — {p.name!r}, байт: {sz_log}.")


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


def _studio_log_video_link_before_public(page) -> None:
    """
    На экране доступа: ссылка из ytcp-video-info (блок «Ссылка на видео») — в консоль и в буфер страницы.
    """
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
        return
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


def _studio_select_public_visibility(page) -> None:
    """Ссылка на видео в консоль, затем «Открытый доступ» — tp-yt-paper-radio-button[name=PUBLIC]."""
    _log("Studio: экран доступа — фиксируем ссылку на видео…")
    _studio_log_video_link_before_public(page)
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
    label = page.locator("ytcp-uploads-dialog ytcp-video-upload-progress .progress-label")
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
    if ("check" in t and "complete" in t) and ("no issues" in t or "no violation" in t or "not found" in t):
        return True
    if "checks complete" in t:
        return True
    if "copyright check" in t and "complete" in t:
        return True
    return False


def _studio_abort_upload_unavailable(page, browser) -> None:
    """Лог в консоль, закрытие браузера, исключение для UI/потока."""
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
    raise DolphinAntyError(
        "YouTube Studio: «Загрузка недоступна». "
        "Обычно это дневной лимит видео или нужна проверка канала (в Studio есть «Пройти проверку»). "
        f"Дополнительно: {extra or '—'}"
    )


def _studio_wait_after_upload_studio_outcome(page, browser, max_wait_sec: float) -> None:
    """
    После передачи файла ждём один из исходов Studio:
    — «Загрузка недоступна» → лог, закрытие браузера, исключение;
    — успешные проверки (атрибут COMPLETED / подпись «Проверка завершена…») → выход, дальше мастер.
    """
    _log(
        "Studio: ожидание результата после загрузки — «Загрузка недоступна» "
        "или «Проверка завершена… нарушений не найдено»…"
    )
    deadline = time.monotonic() + max_wait_sec
    while time.monotonic() < deadline:
        if _studio_is_upload_unavailable_dialog(page):
            _studio_abort_upload_unavailable(page, browser)
        if _studio_is_upload_checks_completed(page):
            _log("Studio: проверки видео завершены успешно — переход к шагу «не для детей».")
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(_POST_UPLOAD_QUOTA_POLL_S, max(0.3, remaining)))
    raise DolphinAntyError(
        f"За {max_wait_sec:.0f} с не появился ни блок «Загрузка недоступна», "
        "ни успешное завершение проверок Studio (прогресс / подпись). "
        "Проверьте диалог загрузки вручную."
    )


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
                    f"Ожидание до {_POST_UPLOAD_STUDIO_OUTCOME_MAX_S:.0f} с исхода Studio "
                    f"(опрос ~каждые {_POST_UPLOAD_QUOTA_POLL_S:.0f} с)…"
                )
                _studio_wait_after_upload_studio_outcome(page, browser, _POST_UPLOAD_STUDIO_OUTCOME_MAX_S)
                _studio_publish_flow_after_upload(page)
                time.sleep(_STUDIO_WIZARD_NEXT_MAX)
            else:
                _log("Загрузка файла из каталога Zaliver отключена (upload_latest_zaliver_video=False).")

            _log("Playwright: browser.close…")
            try:
                browser.close()
                _log("Playwright: browser.close выполнен.")
            except Exception:
                _log("Playwright: browser.close пропущен (браузер уже закрыт или CDP недоступен).")
    finally:
        _log("Local API: api.close…")
        api.close()
        _log("Local API: api.close завершён.")
