"""Залив видео в Instagram Reels: главная → «Новая публикация» → файл → Share."""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from zaliver.instagram_upload.instagram_availability import (
    verify_instagram_home_available,
)
from zaliver.instagram_upload.logutil import emit_instagram_log, instagram_entrypoint

# Playwright при connect_over_cdp шлёт тело файла по CDP и режет ~50 MiB.
# DOM.setFileInputFiles с путями на хосте браузера обходит это.
_PLAYWRIGHT_REMOTE_UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024
_FILE_PICKER_TRANSFER_MS = 1_200_000

_NEW_POST_ARIA = (
    "Новая публикация",
    "New post",
    "Create",
    "Создать",
)
# Подменю после Create: Post / Live video / Ad (EN) или Публикация / …
_CREATE_MENU_POST_RE = re.compile(r"^(post|публикация)$", re.I)
_CREATE_DIALOG_ARIA = (
    "Создание публикации",
    "Create new post",
    "New post",
)
_SELECT_FILE_BTN_RE = re.compile(
    r"выбрать на компьютере|select from computer|select files?",
    re.I,
)
_NEXT_RE = re.compile(r"^\s*(Далее|далее|Next|next)\s*$")
_SHARE_RE = re.compile(r"^\s*(Поделиться|поделиться|Share|share)\s*$")
_OK_DISMISS_RE = re.compile(r"^(ок|ok|понятно|got it)$", re.I)
_DONE_RE = re.compile(r"^(done|готово)$", re.I)
_POST_SHARED_ARIA = (
    "Post shared",
    "Reel shared",
    "Публикация отправлена",
    "Reel опубликован",
    "Видео Reels опубликовано",
    "Ваше видео Reels опубликовано",
)
_POST_SHARED_HEADING_RE = re.compile(
    r"reel shared|post shared|your reel has been shared|"
    r"публикация отправлена|рилс опубликован|"
    r"ваше видео(?:\s+reels)?\s+опубликовано|"
    r"видео\s+reels\s+опубликовано",
    re.I,
)
# Ошибка после Share: «Не удалось разместить публикацию» + кнопка «Повторить».
_POST_FAILED_HEADING_RE = re.compile(
    r"не удалось разместить публикацию|"
    r"could(?:\s+not|n't)\s+(?:share|post)|"
    r"unable to (?:share|post)|"
    r"your post could not be shared|"
    r"we could(?:\s+not|n't)\s+post",
    re.I,
)
_POST_FAILED_ARIA_RE = re.compile(
    r"произошла ошибка|something went wrong|повторите попытку|please try again",
    re.I,
)
_RETRY_BTN_RE = re.compile(r"^(повторить|retry|try again)$", re.I)
_REEL_HREF_RE = re.compile(r"/reel/([^/?#]+)/?", re.I)


class InstagramReelsUploadError(RuntimeError):
    """Ошибка сценария залива Reels."""


def _log(message: str) -> None:
    emit_instagram_log(message, tag="[instagram]")


def _cdp_chrome_file_path(local_path: str) -> str:
    p = Path(local_path).expanduser().resolve()
    return os.path.normpath(str(p))


def _validate_video_file_path(video_path: str | Path) -> Path:
    p = Path(video_path).expanduser()
    deadline = time.monotonic() + 12.0
    last_err: Exception | None = None
    while True:
        try:
            if p.is_file():
                return p
        except Exception as e:
            last_err = e
        if time.monotonic() >= deadline:
            raise InstagramReelsUploadError(
                f"Видеофайл не найден/не доступен: {video_path!r}. "
                f"expanded={str(p)!r}, last_stat_err={last_err!r}"
            )
        time.sleep(0.25)


def _new_post_svg_locator(page):
    """Любой svg «Новая публикация» / New post / Create / Создать."""
    loc = page.locator(f'svg[aria-label="{_NEW_POST_ARIA[0]}"]')
    for aria in _NEW_POST_ARIA[1:]:
        loc = loc.or_(page.locator(f'svg[aria-label="{aria}"]'))
    return loc


def _click_new_post_target(page, svg) -> str:
    """Клик по svg или кликабельному предку. Возвращает aria-label для лога."""
    label = ""
    try:
        label = (svg.get_attribute("aria-label") or "").strip()
    except Exception:
        label = ""
    clickable = svg.locator(
        "xpath=ancestor::*[@role='button' or @role='link' or self::a or self::button][1]"
    )
    # DOM/JS клик: на фоновой вкладке Playwright visible/force по координатам
    # часто бесполезен — кликаем сам узел.
    target = clickable.first if clickable.count() else svg
    try:
        _dom_click(target)
    except Exception:
        try:
            target.click(timeout=5_000, force=True)
        except Exception:
            svg.click(timeout=5_000, force=True)
    return label or "Новая публикация"


def _try_click_new_post_once(page, *, appear_timeout_ms: float = 250) -> bool:
    """
    Одна попытка: дождаться кнопки в DOM (attached) и кликнуть.
    Не требуем visible — фоновые вкладки часто «невидимы» для Playwright.
    """
    wait_ms = max(50, int(appear_timeout_ms))
    try:
        svg = _new_post_svg_locator(page).first
        svg.wait_for(state="attached", timeout=wait_ms)
        label = _click_new_post_target(page, svg)
        _log(f"Reels upload: клик по «{label}».")
        return True
    except Exception:
        pass

    # Fallback: любой svg с title «Новая публикация».
    try:
        titled = page.locator("svg[aria-label] title").filter(
            has_text=re.compile(r"новая публикация|new post|создать|create", re.I)
        )
        if int(titled.count()) <= 0:
            return False
        svg = titled.first.locator("xpath=ancestor::svg[1]")
        try:
            svg.wait_for(state="attached", timeout=min(400, wait_ms))
        except Exception:
            if int(svg.count()) <= 0:
                return False
        _click_new_post_target(page, svg)
        _log("Reels upload: клик по svg через <title>.")
        return True
    except Exception:
        return False


def _create_wizard_already_open(page) -> bool:
    """Мастер создания реально виден — повторный Create даст «Отменить»."""
    try:
        dlg = _create_dialog_locator(page)
        n = int(dlg.count())
        for i in range(min(n, 4)):
            try:
                if dlg.nth(i).is_visible(timeout=300):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    # Только видимые маркеры кропа/подписи (скрытый DOM прошлого залива — не считаем).
    try:
        crop = page.locator(_CROP_BTN_SVG_SEL)
        n = int(crop.count())
        for i in range(min(n, 3)):
            try:
                if crop.nth(i).is_visible(timeout=200):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    try:
        cap = page.locator(_CAPTION_FIELD_CSS)
        n = int(cap.count())
        for i in range(min(n, 3)):
            try:
                if cap.nth(i).is_visible(timeout=200):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _dismiss_discard_create_dialog(page, *, prefer_keep: bool = False) -> None:
    """Закрыть «Отменить публикацию?» / Discard post, если всплыл."""
    discard_re = re.compile(
        r"отменить|discard|leave|удалить|delete\s*post|не\s*сохранять|"
        r"выйти|throw\s*away|unsaved",
        re.I,
    )
    keep_re = re.compile(
        r"продолжить\s*редактирование|continue\s*editing|остаться|keep\s*editing",
        re.I,
    )
    if prefer_keep:
        try:
            keep = page.get_by_role("button", name=keep_re)
            if keep.count() and keep.first.is_visible(timeout=400):
                _dom_click(keep.first)
                page.wait_for_timeout(300)
                _log(
                    "Reels upload: диалог отмены — продолжаем редактирование."
                )
                return
        except Exception:
            pass
    try:
        btn = page.get_by_role("button", name=discard_re)
        if btn.count() and btn.first.is_visible(timeout=400):
            _dom_click(btn.first)
            page.wait_for_timeout(400)
            _log("Reels upload: закрыли диалог отмены публикации.")
    except Exception:
        pass


def _click_new_post_in_sidebar(page, *, max_seconds: float = 90.0) -> None:
    """Сайдбар главной: svg «Новая публикация» / New post → открыть диалог создания."""
    # Хвост прошлого залива на этой вкладке.
    _dismiss_discard_create_dialog(page, prefer_keep=False)
    if _create_wizard_already_open(page):
        _log(
            "Reels upload: мастер создания уже открыт — "
            "«Новая публикация» не нажимаем (иначе «Отменить»)."
        )
        return

    _log("Reels upload: ищем кнопку «Новая публикация» в сайдбаре…")
    if _try_click_new_post_once(page, appear_timeout_ms=200):
        # Повторный Create на занятой вкладке → «Отменить»: остаёмся в мастере.
        _dismiss_discard_create_dialog(page, prefer_keep=True)
        return
    deadline = time.monotonic() + max(10.0, float(max_seconds))
    last_url = ""
    poll_chunk_ms = 300.0
    while time.monotonic() < deadline:
        if _create_wizard_already_open(page):
            _log("Reels upload: мастер создания открылся — Create больше не жмём.")
            return
        try:
            last_url = (page.url or "").strip()
        except Exception:
            last_url = ""
        remaining_ms = max(50.0, (deadline - time.monotonic()) * 1000.0)
        chunk = min(poll_chunk_ms, remaining_ms)
        if _try_click_new_post_once(page, appear_timeout_ms=chunk):
            _dismiss_discard_create_dialog(page, prefer_keep=True)
            return

    if _create_wizard_already_open(page):
        return
    raise InstagramReelsUploadError(
        "Не удалось нажать «Новая публикация» в сайдбаре Instagram."
        + (f" URL={last_url!r}" if last_url else "")
    )


def _try_click_create_submenu_once(page) -> bool:
    """Быстрый клик Post/Публикация в выпадающем меню Create (без долгих wait)."""
    menu_svg = (
        page.locator('svg[aria-label="Post"]')
        .or_(page.locator('svg[aria-label="Публикация"]'))
        .first
    )
    try:
        if not menu_svg.is_visible(timeout=80):
            return False
    except Exception:
        return False
    clickable = menu_svg.locator(
        "xpath=ancestor::*[@role='button' or @role='link' "
        "or self::a or self::button][1]"
    )
    try:
        clickable.first.click(timeout=4_000, force=True)
    except Exception:
        try:
            menu_svg.click(timeout=4_000, force=True)
        except Exception:
            return False
    try:
        label = (menu_svg.get_attribute("aria-label") or "").strip() or "Post"
    except Exception:
        label = "Post"
    _log(f"Reels upload: в меню Create выбрали «{label}».")
    return True


def _click_create_submenu_post_if_present(
    page, *, timeout_ms: float = 2_400
) -> bool:
    """
    После Create иногда выпадает меню (Post / Live video / Ad).
    Кликаем Post / Публикация; если сразу открылся диалог — выходим быстро.
    """
    deadline = time.monotonic() + max(0.3, float(timeout_ms) / 1000.0)
    while time.monotonic() < deadline:
        try:
            if _create_dialog_locator(page).first.is_visible(timeout=80):
                return False
        except Exception:
            pass

        if _try_click_create_submenu_once(page):
            return True

        try:
            link = page.get_by_role("link", name=_CREATE_MENU_POST_RE).first
            if link.is_visible(timeout=80):
                link.click(timeout=4_000, force=True)
                _log("Reels upload: в меню Create выбрали Post/Публикация (link).")
                return True
        except Exception:
            pass

        time.sleep(0.05)

    return False

def _create_dialog_locator(page):
    parts = [
        page.locator(f'[role="dialog"][aria-label="{aria}"]')
        for aria in _CREATE_DIALOG_ARIA
    ]
    loc = parts[0]
    for p in parts[1:]:
        loc = loc.or_(p)
    return loc.or_(
        page.locator('[role="dialog"]').filter(
            has=page.get_by_role(
                "heading",
                name=re.compile(r"создание публикации|create new post|new post", re.I),
            )
        )
    )


def _wait_create_dialog(page, *, timeout_ms: float = 90_000) -> Any:
    dialog = _create_dialog_locator(page)
    # Как раньше: ждём visible, иначе CDP setFile до гидрации → залипает
    # экран «Выбрать на компьютере» поверх уже смонтированной обрезки.
    # Fallback attached — только для фоновых вкладок multi-tab.
    try:
        dialog.first.wait_for(state="visible", timeout=min(25_000, timeout_ms))
    except Exception:
        dialog.first.wait_for(state="attached", timeout=timeout_ms)
        _log(
            "Reels upload: диалог создания в DOM (attached) — "
            "visible не дождались (фон?)."
        )
    else:
        _log("Reels upload: диалог «Создание публикации» открыт.")
    return dialog


def _create_file_input_locator(dialog):
    """form > input[type=file] внутри диалога создания."""
    return (
        dialog.locator('form input[type="file"]')
        .or_(dialog.locator('input[type="file"][accept*="video"]'))
        .or_(dialog.locator('input[type="file"]'))
    )


_ZALIVER_FILE_INPUT_MARK = "data-zaliver-reels-file"


def _cdp_set_file_input_on_target_once(
    target,
    files_path: str,
    *,
    prefer_selector: str | None = None,
) -> bool:
    """Одна попытка DOM.setFileInputFiles на Page|Frame."""
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
        selectors: tuple[str, ...] = (
            'form input[type="file"]',
            'input[type="file"][accept*="video"]',
            'input[type="file"]',
        )
        if prefer_selector:
            selectors = (prefer_selector,) + tuple(
                s for s in selectors if s != prefer_selector
            )
        if root_id > 0:
            for sel in selectors:
                try:
                    qs = session.send(
                        "DOM.querySelector", {"nodeId": root_id, "selector": sel}
                    )
                except Exception as qe:
                    _log(f"Reels upload: CDP querySelector({sel!r}): {qe!r}")
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
                        f"Reels upload: CDP querySelector({sel!r}) → setFileInputFiles ок."
                    )
                    return True
                except Exception as e:
                    _log(
                        f"Reels upload: setFileInputFiles после querySelector({sel!r}): {e!r}"
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

        params: dict = {"query": prefer_selector or 'input[type="file"]'}
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
                    const q = root.querySelector(
                        'form input[type="file"], input[type="file"]'
                    );
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
                _log(
                    f"Reels upload: CDP Runtime.evaluate — {ev.get('exceptionDetails')!r}"
                )
                return False
            res = ev.get("result") or {}
            if res.get("subtype") != "node" or not res.get("objectId"):
                _log("Reels upload: CDP — input[type=file] не найден.")
                return False
            rn = session.send("DOM.requestNode", {"objectId": res["objectId"]})
            node_id = int(rn.get("nodeId") or 0) or None

        if node_id is None:
            _discard()
            return False

        try:
            session.send(
                "DOM.setFileInputFiles", {"nodeId": node_id, "files": [files_path]}
            )
        except Exception as e:
            _log(f"Reels upload: CDP DOM.setFileInputFiles отклонён: {e!r}")
            _discard()
            return False
        _discard()
        _log("Reels upload: DOM.setFileInputFiles (CDP, локальный путь) выполнен.")
        return True
    except Exception as e:
        _log(f"Reels upload: CDP исключение на цели {type(target).__name__}: {e!r}")
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


def _set_file_input_via_cdp(
    page,
    preferred_frame,
    resolved_local_path: str,
    *,
    prefer_selector: str | None = None,
) -> bool:
    files_path = _cdp_chrome_file_path(resolved_local_path)
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
        mf = page.main_frame
        _add(mf() if callable(mf) else mf)
    except Exception:
        pass
    try:
        frames = page.frames
        frames_iter = frames() if callable(frames) else frames
        for fr in frames_iter:
            _add(fr)
    except Exception:
        pass

    _log(f"Reels upload: CDP — целей в очереди: {len(order)}")
    for i, tgt in enumerate(order):
        _log(
            f"Reels upload: CDP setFileInputFiles — цель {i + 1}/{len(order)} "
            f"({type(tgt).__name__})…"
        )
        if _cdp_set_file_input_on_target_once(
            tgt, files_path, prefer_selector=prefer_selector
        ):
            return True
    return False


def _mark_dialog_file_input(dialog) -> str | None:
    """Пометить input файла в диалоге Create — CDP не возьмёт чужой input на странице."""
    loc = _create_file_input_locator(dialog)
    try:
        if not loc.count():
            return None
        loc.first.evaluate(
            """(el, attr) => {
              document.querySelectorAll('[' + attr + ']').forEach(
                n => n.removeAttribute(attr)
              );
              el.setAttribute(attr, '1');
            }""",
            _ZALIVER_FILE_INPUT_MARK,
        )
        return f'input[{_ZALIVER_FILE_INPUT_MARK}="1"]'
    except Exception as e:
        _log(f"Reels upload: не удалось пометить file input: {e!r}")
        return None


def _dismiss_info_dialogs(page) -> None:
    """OK / «Понятно» на подсказках вроде «видеопубликации теперь как Reels»."""
    try:
        dlg = page.locator('[role="dialog"]')
        if not dlg.count():
            return
        btn = dlg.get_by_role("button", name=_OK_DISMISS_RE).first
        if btn.count() and btn.is_visible(timeout=120):
            btn.click(timeout=3_000)
            page.wait_for_timeout(150)
            _log("Reels upload: закрыт информационный диалог.")
    except Exception:
        pass


def _attach_video_file(
    page, dialog, video_path: Path, *, keep_in_background: bool = False
) -> None:
    try:
        resolved = str(video_path.resolve())
    except OSError:
        resolved = str(video_path)
    try:
        sz = video_path.stat().st_size
    except OSError:
        sz = -1
    _log(
        f"Reels upload: передаём файл resolved={resolved!r}, size={sz} "
        "(CDP DOM.setFileInputFiles)…"
    )

    if not keep_in_background:
        try:
            page.bring_to_front()
        except Exception:
            pass

    file_input = _create_file_input_locator(dialog)
    preferred_frame = page
    try:
        if file_input.count():
            preferred_frame = file_input.first.element_handle().owner_frame() or page
    except Exception:
        preferred_frame = page

    prefer_sel = _mark_dialog_file_input(dialog)
    if prefer_sel:
        _log(f"Reels upload: file input помечен для CDP: {prefer_sel}")

    file_submitted = False
    if _set_file_input_via_cdp(
        page, preferred_frame, resolved, prefer_selector=prefer_sel
    ):
        file_submitted = True
    else:
        _log(
            "Reels upload: CDP не удался — fallback file chooser / set_input_files…"
        )
        select_btn = (
            dialog.get_by_role("button", name=_SELECT_FILE_BTN_RE)
            .or_(dialog.locator("button").filter(has_text=_SELECT_FILE_BTN_RE))
        )
        last_err: Exception | None = None
        for attempt in range(1, 4):
            try:
                _log(f"Reels upload: file chooser… (попытка {attempt}/3)")
                with page.expect_file_chooser(timeout=240_000) as fc_info:
                    if select_btn.count() and select_btn.first.is_visible(timeout=4_000):
                        select_btn.first.click(timeout=60_000)
                    else:
                        # Скрытый input — клик через evaluate может не открыть chooser;
                        # пробуем set_input_files напрямую ниже.
                        raise RuntimeError("select button not visible")
                fc_info.value.set_files(resolved, timeout=_FILE_PICKER_TRANSFER_MS)
                file_submitted = True
                last_err = None
                break
            except Exception as e:
                last_err = e
                try:
                    file_input.first.set_input_files(
                        resolved, timeout=_FILE_PICKER_TRANSFER_MS
                    )
                    file_submitted = True
                    last_err = None
                    break
                except Exception as e2:
                    last_err = e2
                    err_t = str(e2).lower()
                    if "50" in err_t and "mb" in err_t:
                        raise InstagramReelsUploadError(
                            "Видео слишком велико для передачи через Playwright по CDP; "
                            "обход через DOM.setFileInputFiles не удался."
                        ) from e2
                    page.wait_for_timeout(500)
        if not file_submitted and last_err is not None:
            raise InstagramReelsUploadError(
                f"Не удалось передать файл в диалог создания: {last_err!r}"
            ) from last_err

    if sz > _PLAYWRIGHT_REMOTE_UPLOAD_LIMIT_BYTES and not file_submitted:
        raise InstagramReelsUploadError(
            f"Файл {sz} байт не передан (лимит Playwright ~50 MiB без CDP)."
        )

    page.wait_for_timeout(800)
    _dismiss_info_dialogs(page)
    _wait_crop_step_ready(page)
    _log(f"Reels upload: файл передан — {video_path.name!r}.")


# Кнопка «Select Crop» / «Выбрать размер и обрезать» (RU UI Instagram).
# aria-label EN: «Select Crop» (C заглавная) — селектор чувствителен к регистру.
_CROP_BTN_ARIA = (
    "Select Crop",
    "Select crop",
    "Выбрать размер и обрезать",
    "Выбрать обрезку",
    "Обрезка",
)
_CROP_BTN_ARIA_RE = re.compile(
    r"select\s*crop|выбрать\s+размер\s+и\s+обрезать|выбрать\s+обрезку|^обрезка$",
    re.I,
)
_CROP_BTN_SVG_SEL = ", ".join(f'svg[aria-label="{a}"]' for a in _CROP_BTN_ARIA)

# Пункт 9:16 — EN «Crop portrait icon», RU «…в портной ориентации» (опечатка IG).
_CROP_9_16_ARIA = (
    "Crop portrait icon",
    "Значок обрезки в портной ориентации",
    "Значок обрезки в портретной ориентации",
)
_CROP_9_16_SVG_SEL = ", ".join(f'svg[aria-label="{a}"]' for a in _CROP_9_16_ARIA)


def _crop_btn_svg_locator(page):
    """svg кнопки кропа: точные aria-label + case-insensitive fallback."""
    exact = page.locator(_CROP_BTN_SVG_SEL)
    by_role = page.get_by_role("img", name=_CROP_BTN_ARIA_RE)
    return exact.or_(by_role)


def _select_file_ui_locator(page):
    """Кнопка/текст первого шага мастера («Выбрать на компьютере»)."""
    return (
        page.get_by_role("button", name=_SELECT_FILE_BTN_RE)
        .or_(page.locator("button").filter(has_text=_SELECT_FILE_BTN_RE))
        .or_(page.get_by_text(_SELECT_FILE_BTN_RE))
    )


def _select_file_ui_still_up(page) -> bool:
    try:
        loc = _select_file_ui_locator(page)
        n = int(loc.count())
        for i in range(min(n, 4)):
            try:
                if loc.nth(i).is_visible(timeout=200):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


_CLEAR_CROP_COVER_JS = r"""() => {
  const cropRe = /select\s*crop|выбрать\s+размер|выбрать\s+обрезку|^обрезка$/i;
  const fileStepRe = /select from computer|выбрать на компьютере|drag photos|перетащите|select files|перетащите сюда/i;
  const svgs = Array.from(document.querySelectorAll('svg[aria-label]'));
  const cropSvg = svgs.find(s => cropRe.test(s.getAttribute('aria-label') || ''));
  if (!cropSvg) return {hidden: 0, reason: 'no-crop'};

  const hasCrop = (root) => {
    for (const svg of root.querySelectorAll('svg[aria-label]')) {
      if (cropRe.test(svg.getAttribute('aria-label') || '')) return true;
    }
    return false;
  };

  let hidden = 0;
  // 1) Отдельные dialog без кропа, пока кроп уже есть в другом.
  for (const dlg of document.querySelectorAll('[role="dialog"]')) {
    const text = (dlg.innerText || '').slice(0, 2500);
    if (hasCrop(dlg)) continue;
    if (!fileStepRe.test(text) && !dlg.querySelector('input[type="file"]')) continue;
    dlg.style.setProperty('visibility', 'hidden', 'important');
    dlg.style.setProperty('pointer-events', 'none', 'important');
    dlg.style.setProperty('opacity', '0', 'important');
    hidden++;
  }

  // 2) elementFromPoint: что реально лежит поверх кнопки кропа.
  const r = cropSvg.getBoundingClientRect();
  if (r.width > 0 && r.height > 0) {
    const x = r.left + r.width / 2;
    const y = r.top + r.height / 2;
    let top = document.elementFromPoint(x, y);
    let guard = 0;
    while (top && guard < 12) {
      guard++;
      if (cropSvg === top || cropSvg.contains(top) || (top.contains && top.contains(cropSvg))) {
        break;
      }
      const txt = ((top.innerText || '') + ' ' + (top.getAttribute('aria-label') || '')).slice(0, 400);
      const looksFileStep = fileStepRe.test(txt) || !!top.querySelector?.('input[type="file"]');
      const st = window.getComputedStyle(top);
      const covers =
        looksFileStep ||
        st.position === 'fixed' ||
        st.position === 'absolute' ||
        (parseFloat(st.opacity || '1') > 0.05 && top.getBoundingClientRect().height > 80);
      if (covers && !hasCrop(top)) {
        top.style.setProperty('visibility', 'hidden', 'important');
        top.style.setProperty('pointer-events', 'none', 'important');
        top.style.setProperty('opacity', '0', 'important');
        hidden++;
        top = document.elementFromPoint(x, y);
        continue;
      }
      top = top.parentElement;
    }
  }

  // 3) Внутри dialog с кропом спрятать панели первого шага (без кропа внутри).
  for (const dlg of document.querySelectorAll('[role="dialog"]')) {
    if (!hasCrop(dlg)) continue;
    for (const el of dlg.querySelectorAll('div, section, form, aside')) {
      if (hasCrop(el)) continue;
      const t = (el.innerText || '').trim();
      if (!t || t.length > 800) continue;
      if (!fileStepRe.test(t)) continue;
      const box = el.getBoundingClientRect();
      if (box.width < 60 || box.height < 40) continue;
      el.style.setProperty('visibility', 'hidden', 'important');
      el.style.setProperty('pointer-events', 'none', 'important');
      el.style.setProperty('opacity', '0', 'important');
      hidden++;
    }
  }
  return {hidden, reason: 'ok'};
}"""


def _clear_layers_covering_crop(page) -> int:
    """Спрятать слои, которые перекрывают Select Crop (elementFromPoint + file-step)."""
    try:
        res = page.evaluate(_CLEAR_CROP_COVER_JS)
    except Exception as e:
        _log(f"Reels upload: clear crop cover: {e!r}")
        return 0
    if not isinstance(res, dict):
        return 0
    n = int(res.get("hidden") or 0)
    if n:
        _log(f"Reels upload: скрыто слоёв поверх кропа: {n}")
    return n


def _wait_crop_step_ready(page, *, timeout_ms: float = 120_000) -> None:
    """
    После CDP setFile: дождаться смены шага (выбор файла → обрезка).

    Если кроп уже в DOM, а поверх него залип первый шаг — принудительно
    скрываем перекрывающие слои (по elementFromPoint).
    """
    crop = _crop_btn_svg_locator(page)
    deadline = time.monotonic() + max(5.0, float(timeout_ms) / 1000.0)
    saw_crop = False
    cleared_once = False
    while time.monotonic() < deadline:
        try:
            if crop.count():
                try:
                    crop.first.wait_for(state="attached", timeout=150)
                    saw_crop = True
                except Exception:
                    pass
                try:
                    if crop.first.is_visible(timeout=150):
                        saw_crop = True
                except Exception:
                    pass
        except Exception:
            pass

        select_up = _select_file_ui_still_up(page)
        if saw_crop and not select_up:
            # Даже без текста кнопки слой может перекрывать — проверим и снимем.
            n = _clear_layers_covering_crop(page)
            if n == 0 or cleared_once:
                _log("Reels upload: шаг обрезки готов.")
                return
            cleared_once = True
            page.wait_for_timeout(200)
            continue

        if saw_crop and select_up:
            _clear_layers_covering_crop(page)
            cleared_once = True
            if not _select_file_ui_still_up(page):
                _log("Reels upload: экран выбора файла снят, кроп доступен.")
                return
        page.wait_for_timeout(250)

    if not saw_crop:
        raise InstagramReelsUploadError(
            "Кнопка Select Crop не появилась после передачи файла."
        )
    _clear_layers_covering_crop(page)
    _log(
        "Reels upload: кроп в DOM после таймаута — сняли оверлеи, продолжаем."
    )


def _click_crop_target(target) -> None:
    """Обычный клик как раньше; force — только если перекрыт оверлеем."""
    try:
        target.click(timeout=10_000)
        return
    except Exception:
        pass
    try:
        _dom_click(target)
        return
    except Exception:
        pass
    target.click(timeout=20_000, force=True)


def _select_crop_9_16(page) -> None:
    """Сразу после файла: Select Crop → 9:16 (портрет для Reels)."""
    _log("Reels upload: выбираем обрезку 9:16…")
    _wait_crop_step_ready(page)

    try:
        svg = _crop_btn_svg_locator(page).first
        btn = svg.locator("xpath=ancestor::button[1]")
        target = btn if btn.count() else svg.locator(
            "xpath=ancestor::*[@role='button'][1]"
        )
        if not target.count():
            target = svg
        _click_crop_target(target)
        _log("Reels upload: открыли меню Select Crop.")
    except Exception as e:
        raise InstagramReelsUploadError(
            f"Не удалось нажать Select Crop: {e!r}"
        ) from e

    page.wait_for_timeout(500)

    # Пункт 9:16 / Crop portrait (текст «9:16» общий для EN/RU)
    option = (
        page.locator('[role="button"]').filter(has_text=re.compile(r"^9\s*:\s*16$"))
        .or_(page.locator(_CROP_9_16_SVG_SEL))
        .or_(page.get_by_text(re.compile(r"^9\s*:\s*16$"), exact=True))
    )
    try:
        # Предпочитаем клик по role=button с текстом 9:16
        text_opt = page.locator('[role="button"]').filter(
            has=page.locator("span", has_text=re.compile(r"^9\s*:\s*16$"))
        )
        if text_opt.count() and text_opt.first.is_visible(timeout=6_000):
            _click_crop_target(text_opt.first)
        else:
            svg9 = page.locator(_CROP_9_16_SVG_SEL).first
            if svg9.count() and svg9.is_visible(timeout=4_000):
                clickable = svg9.locator(
                    "xpath=ancestor::*[@role='button'][1]"
                )
                _click_crop_target(clickable if clickable.count() else svg9)
            else:
                _click_crop_target(option.first)
        _log("Reels upload: выбрано 9:16.")
    except Exception as e:
        raise InstagramReelsUploadError(
            f"Не удалось выбрать обрезку 9:16: {e!r}"
        ) from e

    page.wait_for_timeout(400)
    # Меню аспекта часто остаётся поверх «Далее» — клик по превью закрывает его.
    try:
        preview = (
            page.locator('[role="dialog"]')
            .locator("img, video, canvas, [style*='background-image']")
            .first
        )
        if preview.count():
            try:
                preview.click(timeout=3_000)
            except Exception:
                preview.click(timeout=3_000, force=True)
            page.wait_for_timeout(300)
    except Exception:
        pass


def _upload_wizard_dialog(page):
    """Диалог мастера создания (Create / crop / caption), не любой dialog на странице."""
    return (
        _create_dialog_locator(page)
        .or_(
            page.locator('[role="dialog"]').filter(
                has=page.locator(_CROP_BTN_SVG_SEL)
            )
        )
        .or_(
            page.locator('[role="dialog"]').filter(
                has=page.locator('[role="button"]').filter(has_text=_NEXT_RE).or_(
                    page.locator('[role="button"]').filter(has_text=_SHARE_RE)
                )
            )
        )
        .last
    )


def _dialog_action_button(page, name_re: re.Pattern[str]):
    """Кнопка Далее/Поделиться в шапке мастера создания."""
    dlg = _upload_wizard_dialog(page)
    return (
        dlg.locator('[role="button"]').filter(has_text=name_re)
        .or_(dlg.get_by_role("button", name=name_re))
        .or_(dlg.locator("button").filter(has_text=name_re))
        .or_(page.locator('[role="dialog"] [role="button"]').filter(has_text=name_re))
        .or_(page.get_by_role("button", name=name_re))
    )


def _next_button_locator(page):
    """
    «Далее» / Next в мастере — как в DOM IG:
    <div role="button" tabindex="0">Далее</div>
    """
    in_dialog = page.locator('[role="dialog"] [role="button"]').filter(
        has_text=_NEXT_RE
    )
    return (
        in_dialog
        .or_(page.get_by_role("button", name=_NEXT_RE))
        .or_(page.locator('[role="button"]').filter(has_text=_NEXT_RE))
    )


def _first_visible_locator(loc, *, probes: int = 8, timeout_ms: int = 150):
    """Первый реально видимый матч (не .first из скрытого DOM)."""
    try:
        n = int(loc.count())
    except Exception:
        return None
    for i in range(min(n, max(1, probes))):
        cand = loc.nth(i)
        try:
            if cand.is_visible(timeout=timeout_ms):
                return cand
        except Exception:
            continue
    return None


def _pick_action_target(loc, *, prefer_last: bool = True):
    """Видимый кандидат, иначе последний в DOM."""
    visible = _first_visible_locator(loc, probes=12, timeout_ms=50)
    if visible is not None:
        return visible
    try:
        n = int(loc.count())
    except Exception:
        return None
    if n <= 0:
        return None
    return loc.last if prefer_last else loc.first


def _dom_click(locator) -> None:
    """
    Клик напрямую по элементу в DOM (без hit-test по координатам).

    Playwright force=True всё равно кликает в точку на экране: если меню кропа
    поверх «Далее», событие получает оверлей, а не кнопка.
    """
    locator.evaluate(
        """(el) => {
            el.focus?.();
            const opts = { bubbles: true, cancelable: true, view: window, buttons: 1 };
            for (const type of [
                'pointerover', 'pointerenter', 'mouseover', 'mouseenter',
                'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click',
            ]) {
                el.dispatchEvent(new MouseEvent(type, opts));
            }
            if (typeof el.click === 'function') el.click();
        }"""
    )


def _click_next_button(page, *, find_timeout_s: float = 8.0) -> bool:
    """Найти и нажать «Далее». True если клик выполнен."""
    nxt = _next_button_locator(page)
    deadline = time.monotonic() + max(0.5, float(find_timeout_s))
    target = None
    while time.monotonic() < deadline:
        # Сначала видимая в диалоге, иначе последний DOM-кандидат.
        target = _pick_action_target(nxt, prefer_last=True)
        if target is not None:
            break
        try:
            if int(nxt.count()) > 0:
                target = nxt.last
                break
        except Exception:
            pass
        page.wait_for_timeout(80)
    if target is None:
        return False

    # 1) DOM-клик (главный путь при перекрытом меню кропа)
    try:
        _dom_click(target)
        return True
    except Exception as e1:
        _log(f"Reels upload: DOM-клик «Далее» не удался: {e1!r}")

    # 2) Playwright force — запасной
    try:
        target.click(timeout=6_000, force=True)
        return True
    except Exception as e2:
        _log(f"Reels upload: force-клик «Далее» не удался: {e2!r}")
    return False


# Lexical caption: aria-label/placeholder «Добавьте подпись…» (многоточие …).
_CAPTION_ARIA_RE = re.compile(
    r"добавьте\s+подпись|напишите\s+подпись|write\s+a\s+caption|"
    r"caption|подпись",
    re.I,
)

_CAPTION_FIELD_CSS = (
    '[data-lexical-editor="true"][role="textbox"][contenteditable="true"], '
    '[role="textbox"][contenteditable="true"][aria-label*="подпись" i], '
    '[role="textbox"][contenteditable="true"][aria-label*="caption" i], '
    '[role="textbox"][contenteditable="true"][aria-placeholder*="подпись" i], '
    '[role="textbox"][contenteditable="true"][aria-placeholder*="caption" i]'
)


def _on_caption_or_share_screen(page) -> bool:
    """
    Экран подписи/Share внутри мастера создания.

    Важно: не искать Share по всей странице — в ленте IG полно кнопок
    «Поделиться», из‑за них цикл «Далее» сразу выходил без клика.
    """
    try:
        dlg = page.locator('[role="dialog"]')
        if int(dlg.locator(_CAPTION_FIELD_CSS).count()) > 0:
            return True
    except Exception:
        pass
    try:
        # Только шапка мастера / dialog, не лента.
        share = page.locator(
            '[role="dialog"] div[style*="--x-height"] > [role="button"], '
            '[role="dialog"] [role="button"]'
        ).filter(has_text=_SHARE_RE)
        n = int(share.count())
        for i in range(min(n, 6)):
            try:
                raw = (share.nth(i).inner_text(timeout=200) or "").strip()
            except Exception:
                continue
            if _SHARE_RE.match(raw):
                return True
    except Exception:
        pass
    return False


def _close_crop_aspect_menu_if_open(page) -> None:
    """Меню 9:16 часто перекрывает «Далее» — закрыть кликом по превью / Escape."""
    try:
        preview = (
            page.locator('[role="dialog"]')
            .locator("img, video, canvas, [style*='background-image']")
            .first
        )
        if preview.count():
            try:
                preview.click(timeout=2_000)
            except Exception:
                try:
                    preview.click(timeout=2_000, force=True)
                except Exception:
                    pass
            page.wait_for_timeout(200)
    except Exception:
        pass
    # Если меню ещё открыто — лёгкий Escape (не закрывает весь мастер).
    try:
        menu_916 = page.locator('[role="button"]').filter(
            has_text=re.compile(r"^9\s*:\s*16$")
        )
        if menu_916.count() and menu_916.first.is_visible(timeout=150):
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
    except Exception:
        pass


def _click_next_until_caption_or_share(page, *, max_clicks: int = 6) -> None:
    """Прокликать «Далее» (обрезка / фильтры) до экрана подписи или Share."""
    clicked = 0
    _close_crop_aspect_menu_if_open(page)
    for i in range(max_clicks):
        _dismiss_info_dialogs(page)
        if _on_caption_or_share_screen(page):
            _log("Reels upload: экран Share / подписи.")
            return

        if i == 0:
            _close_crop_aspect_menu_if_open(page)

        if not _click_next_button(page, find_timeout_s=8.0 if clicked == 0 else 4.0):
            _log(f"Reels upload: «Далее» не найдена (шаг {i + 1}).")
            # Меню кропа могло перехватить — закрыть и ещё раз.
            if clicked == 0:
                _close_crop_aspect_menu_if_open(page)
                if _click_next_button(page, find_timeout_s=5.0):
                    clicked += 1
                    _log("Reels upload: «Далее» после закрытия меню кропа.")
                else:
                    break
            else:
                break
        else:
            clicked += 1
            _log(f"Reels upload: «Далее» ({clicked}/{max_clicks}).")

        # Короткая пауза + опрос; не путать с Share из ленты.
        page.wait_for_timeout(250)
        settle_deadline = time.monotonic() + 2.5
        while time.monotonic() < settle_deadline:
            if _on_caption_or_share_screen(page):
                _log("Reels upload: экран Share / подписи.")
                return
            # Экран сменился, «Далее» снова в DOM — можно жать следующий шаг.
            if time.monotonic() - (settle_deadline - 2.5) >= 0.35:
                try:
                    vis = _pick_action_target(
                        _next_button_locator(page), prefer_last=True
                    )
                    if vis is not None:
                        break
                except Exception:
                    pass
            page.wait_for_timeout(80)

    if _on_caption_or_share_screen(page):
        return
    raise InstagramReelsUploadError(
        "Не удалось нажать «Далее» после обрезки 9:16 "
        f"(кликов={clicked}). Мастер застрял на экране кропа."
    )


def _caption_input_locator(page):
    """
    Поле подписи Reels — Lexical editor:
    <div role="textbox" contenteditable data-lexical-editor
         aria-label="Добавьте подпись…">
    Ищем по всей странице (не только role=dialog).
    """
    return (
        page.locator(_CAPTION_FIELD_CSS)
        .or_(page.get_by_role("textbox", name=_CAPTION_ARIA_RE))
        .or_(
            page.locator('[role="dialog"]').locator(
                '[data-lexical-editor="true"], '
                '[role="textbox"][contenteditable="true"], '
                "textarea"
            )
        )
    )


def _js_set_caption(locator, text: str) -> None:
    """Ввод подписи через JS в Lexical / textarea (без hit-test)."""
    locator.evaluate(
        """(el, value) => {
            const fire = (type, init) => {
                try {
                    el.dispatchEvent(new InputEvent(type, Object.assign({ bubbles: true, cancelable: true }, init || {})));
                } catch (_) {
                    el.dispatchEvent(new Event(type, { bubbles: true }));
                }
            };
            el.focus();
            const tag = (el.tagName || '').toUpperCase();
            if (tag === 'TEXTAREA' || tag === 'INPUT') {
                const proto = tag === 'TEXTAREA'
                    ? window.HTMLTextAreaElement.prototype
                    : window.HTMLInputElement.prototype;
                const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                if (desc && desc.set) desc.set.call(el, value);
                else el.value = value;
                fire('input', { inputType: 'insertText', data: value });
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return;
            }
            // Lexical contenteditable
            try {
                const sel = window.getSelection();
                const range = document.createRange();
                range.selectNodeContents(el);
                sel.removeAllRanges();
                sel.addRange(range);
            } catch (_) {}
            let ok = false;
            try { ok = document.execCommand('selectAll', false, null); } catch (_) {}
            try { document.execCommand('delete', false, null); } catch (_) {}
            try {
                ok = document.execCommand('insertText', false, value);
            } catch (_) {
                ok = false;
            }
            if (!ok) {
                // Fallback: заменить содержимое <p>
                const p = el.querySelector('p') || el;
                p.textContent = value;
                fire('input', { inputType: 'insertText', data: value });
                fire('beforeinput', { inputType: 'insertText', data: value });
            }
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.blur?.();
            el.focus?.();
        }""",
        text,
    )


def _fill_caption(page, caption: str) -> None:
    text = (caption or "").strip()
    if not text:
        return
    try:
        area = None
        deadline = time.monotonic() + 24.0
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                loc = _caption_input_locator(page)
                n = int(loc.count())
                if n <= 0:
                    page.wait_for_timeout(250)
                    continue
                # Предпочитаем Lexical с aria-label подписи.
                picked = None
                for i in range(min(n, 10)):
                    cand = loc.nth(i)
                    try:
                        label = (
                            (cand.get_attribute("aria-label") or "")
                            + " "
                            + (cand.get_attribute("aria-placeholder") or "")
                        ).lower()
                        if "подпись" in label or "caption" in label:
                            picked = cand
                            break
                    except Exception:
                        continue
                area = picked or loc.first
                break
            except Exception as e:
                last_err = e
            page.wait_for_timeout(250)

        if area is None:
            _log(
                "Reels upload: поле подписи не найдено — пропускаем."
                + (f" last_err={last_err!r}" if last_err else "")
            )
            return

        try:
            _dom_click(area)
        except Exception:
            pass

        try:
            _js_set_caption(area, text)
            _log(f"Reels upload: подпись задана через JS ({len(text)} символов).")
            page.wait_for_timeout(300)
            return
        except Exception as e:
            _log(f"Reels upload: JS-ввод подписи не удался: {e!r}")

        try:
            area.fill(text, timeout=16_000)
            _log(f"Reels upload: подпись задана через fill ({len(text)} символов).")
            page.wait_for_timeout(300)
        except Exception as e:
            _log(f"Reels upload: не удалось ввести подпись: {e!r}")
    except Exception as e:
        _log(f"Reels upload: не удалось ввести подпись: {e!r}")


def _pick_header_share_button(page):
    """
    Шапка мастера — именно эта кнопка:
    <div style="--x-height: 100%;">
      <div role="button" tabindex="0">Поделиться</div>
    </div>
    Не путать с другими Share на странице / в меню.
    """
    # Сначала самый точный селектор под DOM пользователя.
    header = page.locator(
        '[role="dialog"] div[style*="--x-height"] > [role="button"], '
        'div[style*="--x-height"] > [role="button"]'
    ).filter(has_text=_SHARE_RE)
    try:
        n = int(header.count())
    except Exception:
        n = 0
    for i in range(n):
        cand = header.nth(i)
        try:
            raw = (cand.inner_text(timeout=800) or "").strip()
        except Exception:
            continue
        if not _SHARE_RE.match(raw):
            continue
        return cand

    # Запас: любой [role=button] в диалоге с точным текстом Поделиться/Share.
    in_dlg = page.locator('[role="dialog"] [role="button"]').filter(
        has_text=_SHARE_RE
    )
    try:
        n = int(in_dlg.count())
    except Exception:
        n = 0
    best = None
    for i in range(n):
        cand = in_dlg.nth(i)
        try:
            raw = (cand.inner_text(timeout=500) or "").strip()
        except Exception:
            continue
        if not _SHARE_RE.match(raw):
            continue
        try:
            parent_style = cand.evaluate(
                "el => (el.parentElement && el.parentElement.getAttribute('style')) || ''"
            )
        except Exception:
            parent_style = ""
        if "--x-height" in (parent_style or ""):
            return cand
        best = cand
    if best is not None:
        return best

    # Совсем запасной: точный текст на странице (не aria-label «Поделиться профилем» и т.п.).
    loose = page.locator('[role="button"]').filter(has_text=_SHARE_RE)
    try:
        n = int(loose.count())
    except Exception:
        n = 0
    for i in range(n - 1, -1, -1):
        cand = loose.nth(i)
        try:
            raw = (cand.inner_text(timeout=400) or "").strip()
        except Exception:
            continue
        if _SHARE_RE.match(raw):
            return cand
    return None


def _click_share(page) -> None:
    target = None
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        target = _pick_header_share_button(page)
        if target is not None:
            break
        page.wait_for_timeout(250)
    if target is None:
        raise InstagramReelsUploadError(
            "Кнопка «Поделиться» / Share не найдена после мастера создания."
        )

    try:
        _dom_click(target)
        _log("Reels upload: нажали «Поделиться» в шапке (JS).")
        return
    except Exception as e1:
        _log(f"Reels upload: DOM-клик «Поделиться» не удался: {e1!r}")

    try:
        target.click(timeout=60_000, force=True)
        _log("Reels upload: нажали «Поделиться» в шапке (force).")
    except Exception as e2:
        raise InstagramReelsUploadError(
            f"Не удалось нажать «Поделиться» / Share: {e2!r}"
        ) from e2


def _post_shared_dialog_locator(page):
    loc = None
    for aria in _POST_SHARED_ARIA:
        part = page.locator(f'[role="dialog"][aria-label="{aria}"]')
        loc = part if loc is None else loc.or_(part)
    heading = page.locator('[role="dialog"]').filter(
        has=page.get_by_role("heading", name=_POST_SHARED_HEADING_RE)
    )
    text = page.locator('[role="dialog"]').filter(
        has_text=_POST_SHARED_HEADING_RE
    )
    assert loc is not None
    return loc.or_(heading).or_(text)


def _post_share_failed_visible(page) -> bool:
    """Экран «Не удалось разместить публикацию» / Something went wrong."""
    try:
        h = page.get_by_role("heading", name=_POST_FAILED_HEADING_RE)
        if h.count() and h.first.is_visible(timeout=250):
            return True
    except Exception:
        pass
    try:
        # Текст без role=heading (как в разметке Instagram).
        txt = page.get_by_text(_POST_FAILED_HEADING_RE)
        if txt.count() and txt.first.is_visible(timeout=250):
            return True
    except Exception:
        pass
    try:
        # aria-label на svg: «Произошла ошибка. Повторите попытку.»
        labeled = page.locator("svg[aria-label]")
        n = min(int(labeled.count()), 12)
        for i in range(n):
            el = labeled.nth(i)
            try:
                if not el.is_visible(timeout=150):
                    continue
                aria = (el.get_attribute("aria-label") or "").strip()
                if aria and _POST_FAILED_ARIA_RE.search(aria):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _click_post_failed_retry(page) -> bool:
    """Нажать «Повторить» / Retry на экране ошибки публикации."""
    btn = (
        page.get_by_role("button", name=_RETRY_BTN_RE)
        .or_(page.locator("button").filter(has_text=_RETRY_BTN_RE))
        .or_(page.locator('[role="button"]').filter(has_text=_RETRY_BTN_RE))
    )
    try:
        if not btn.count() or not btn.first.is_visible(timeout=6_000):
            return False
        btn.first.click(timeout=30_000)
        return True
    except Exception as e:
        _log(f"Reels upload: клик «Повторить» не удался: {e!r}")
        return False


def _click_post_shared_done(
    page, dialog, *, keep_in_background: bool = False
) -> None:
    done = (
        dialog.first.get_by_role("button", name=_DONE_RE)
        .or_(dialog.first.locator('[role="button"]').filter(has_text=_DONE_RE))
        .or_(page.get_by_role("button", name=_DONE_RE))
        .or_(page.locator('[role="button"]').filter(has_text=_DONE_RE))
    )
    try:
        if done.count() and done.first.is_visible(timeout=10_000):
            if keep_in_background:
                # JS-клик без активации вкладки (не срывает YouTube Studio).
                done.first.evaluate("el => el.click()")
            else:
                done.first.click(timeout=30_000)
            _log("Reels upload: нажали Done.")
            # Дать Instagram время проставить Reel в сетку профиля.
            page.wait_for_timeout(2_500)
            return
    except Exception as e:
        _log(f"Reels upload: клик Done не удался ({e!r}) — пробуем Escape.")

    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(1_000)
    except Exception:
        pass
    page.wait_for_timeout(2_000)


def _wait_post_shared_and_done(
    page,
    *,
    timeout_s: float = 1_200.0,
    keep_in_background: bool = False,
    wait_before_done: threading.Event | None = None,
    wait_before_done_timeout_s: float = 3_600.0,
) -> None:
    """
    Ждём диалог Post shared / Reel shared (до 10 мин) и жмём Done.

    Если после Share появляется «Не удалось разместить публикацию» —
    один раз жмём «Повторить»; при повторной ошибке — выход с исключением.

    wait_before_done — для Yt+Inst: не жать Done / не уходить на /reels/,
    пока YouTube этого же ролика не закончил (иначе фокус срывает Studio).
    """
    _log(
        "Reels upload: ждём экран «Reel shared» / Post shared "
        f"(таймаут {timeout_s:.0f} с)…"
    )
    deadline = time.monotonic() + max(30.0, float(timeout_s))
    retries_used = 0
    max_auto_retries = 1

    while time.monotonic() < deadline:
        dialog = _post_shared_dialog_locator(page)
        try:
            if dialog.count() and dialog.first.is_visible(timeout=400):
                _log("Reels upload: диалог Post shared виден.")
                if wait_before_done is not None:
                    _log(
                        "Reels upload: ждём завершения YouTube перед Done /reels/…"
                    )
                    if not wait_before_done.wait(
                        timeout=max(30.0, float(wait_before_done_timeout_s))
                    ):
                        _log(
                            "Reels upload: таймаут ожидания YouTube — "
                            "продолжаем Done /reels/."
                        )
                    else:
                        _log(
                            "Reels upload: YouTube готов — Done /reels/."
                        )
                _click_post_shared_done(
                    page, dialog, keep_in_background=keep_in_background
                )
                return
        except Exception:
            pass

        if _post_share_failed_visible(page):
            if retries_used >= max_auto_retries:
                raise InstagramReelsUploadError(
                    "Не удалось разместить публикацию: после «Повторить» "
                    "ошибка появилась снова."
                )
            _log(
                "Reels upload: ошибка публикации "
                "(«Не удалось разместить…») — жмём «Повторить» "
                f"({retries_used + 1}/{max_auto_retries})…"
            )
            if not _click_post_failed_retry(page):
                raise InstagramReelsUploadError(
                    "Не удалось разместить публикацию: "
                    "кнопка «Повторить» не найдена."
                )
            retries_used += 1
            page.wait_for_timeout(1_200)
            continue

        page.wait_for_timeout(500)

    raise InstagramReelsUploadError(
        "Не дождались диалога «Post shared» / «Reel shared» после Share."
    )


def _absolute_instagram_url(href: str) -> str:
    h = (href or "").strip()
    if not h:
        return ""
    if h.startswith("/"):
        h = "https://www.instagram.com" + h
    return h.split("?")[0]


def _normalize_reel_url(url: str) -> str:
    """Канонический URL вида https://www.instagram.com/reel/<id>/."""
    m = _REEL_HREF_RE.search(url or "")
    if not m:
        return (url or "").strip().split("?")[0].rstrip("/")
    return f"https://www.instagram.com/reel/{m.group(1)}/"


def _video_id_from_reel_url(url: str) -> str:
    m = _REEL_HREF_RE.search(url or "")
    return m.group(1) if m else ""


_PROFILE_URL_RESERVED = frozenset(
    {
        "reels",
        "explore",
        "direct",
        "accounts",
        "stories",
        "p",
        "reel",
        "tv",
        "tags",
        "locations",
        "about",
        "legal",
        "web",
        "api",
        "graphql",
        "popular",
        "challenge",
        "privacy",
        "meta",
        "ads",
        "notifications",
        "nametag",
        "directory",
        "your_activity",
        "professional_dashboard",
        "archive",
    }
)


def _username_from_url(url: str) -> str:
    """Из https://www.instagram.com/sedaguler7602026[/reels/] → sedaguler7602026."""
    try:
        from urllib.parse import urlparse

        path = urlparse((url or "").strip()).path or ""
    except Exception:
        path = ""
    parts = [p for p in path.split("/") if p]
    if not parts:
        return ""
    name = parts[0].strip().lstrip("@")
    if not name or name.lower() in _PROFILE_URL_RESERVED:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9._]{2,30}", name):
        return ""
    return name


def _username_hint_from_login(session_login: str) -> str:
    login = (session_login or "").strip().lstrip("@")
    if not login or "@" in login:  # email
        return ""
    if not re.fullmatch(r"[A-Za-z0-9._]{2,30}", login):
        return ""
    return login


def _resolve_own_username(page, *, session_login: str = "") -> str:
    """
    Username своего профиля без клика по сайдбару:
    href кнопки Profile → extract → подсказка из session_login → URL.
    """
    from zaliver.instagram_upload.register import _extract_logged_in_username

    hint = _username_hint_from_login(session_login)
    extracted = (_extract_logged_in_username(page) or "").strip().lstrip("@")
    from_url = ""
    try:
        from_url = _username_from_url((page.url or "").strip())
    except Exception:
        from_url = ""
    username = (extracted or from_url or hint or "").strip().lstrip("@")
    _log(
        "Reels upload: username без клика по профилю "
        f"(extracted={extracted!r}, url={from_url!r}, hint={hint!r}) → "
        f"{username!r}"
    )
    return username


def _open_own_profile(page, *, session_login: str = "") -> str:
    """
    Резолвит username своего профиля БЕЗ клика по кнопке Profile.
    Переход на страницу профиля не делаем — сразу идём на /reels/
    через ``_open_profile_reels_tab``.
    """
    username = _resolve_own_username(page, session_login=session_login)
    if not username:
        raise InstagramReelsUploadError(
            "Не удалось получить username из ссылки профиля в сайдбаре "
            "(без клика). Проверьте, что сессия Instagram активна."
        )
    return username


def _open_profile_reels_tab(
    page, username: str, *, keep_in_background: bool = False
) -> None:
    """Сразу https://www.instagram.com/{username}/reels/ (без захода на профиль)."""
    uname = (username or "").strip().lstrip("@")
    if not uname:
        raise InstagramReelsUploadError(
            "Не удалось открыть /reels/ профиля (username пуст)."
        )
    from zaliver.instagram_upload.register import _navigate_page_to

    reels_url = f"https://www.instagram.com/{uname}/reels/"
    _log(f"Reels upload: сразу открываем {reels_url} (href профиля + /reels/)…")
    _navigate_page_to(
        page,
        reels_url,
        label="IG profile reels",
        keep_in_background=keep_in_background,
    )
    page.wait_for_timeout(1_500)

    try:
        page.wait_for_selector('a[href*="/reel/"]', timeout=120_000)
    except Exception as e:
        raise InstagramReelsUploadError(
            f"На {reels_url} нет видео (сетка не прогрузилась)."
        ) from e


def _first_profile_reel_url(page, *, retries: int = 8, wait_ms: int = 4000) -> str:
    """Первый Reel-URL из сетки /username/reels/ (без клика по карточке)."""
    urls = _collect_profile_reel_urls(
        page, limit=1, retries=retries, wait_ms=wait_ms
    )
    return urls[0]


def _collect_profile_reel_urls(
    page,
    *,
    limit: int = 5,
    retries: int = 8,
    wait_ms: int = 4000,
    keep_in_background: bool = False,
) -> list[str]:
    """
    Собрать до ``limit`` уникальных Reel-URL из сетки /username/reels/
    (в порядке DOM: сверху / слева направо).

    Без клика по первому Reel: клик уводит вкладку в viewer /reel/… и ломает
    следующий залив на той же (первой) вкладке.
    """
    want = max(1, int(limit))
    last_err: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            links = page.locator('a[href*="/reel/"]')
            n = int(links.count())
            if n <= 0:
                raise RuntimeError("no reel links")
            seen: set[str] = set()
            out: list[str] = []
            # Запас на дубли href в сетке.
            scan = min(n, max(want * 4, want))
            for i in range(scan):
                if len(out) >= want:
                    break
                href = (links.nth(i).get_attribute("href") or "").strip()
                abs_url = _absolute_instagram_url(href)
                canon = _normalize_reel_url(abs_url)
                vid = _video_id_from_reel_url(canon)
                if not vid or vid in seen:
                    continue
                seen.add(vid)
                out.append(canon)
            if not out:
                raise RuntimeError(f"no valid reel hrefs (links={n})")

            _log(
                f"Reels upload: кандидаты в сетке ({len(out)}/{want}) — "
                + ", ".join(repr(u) for u in out)
                + f" (попытка {attempt}/{retries})."
            )
            return out
        except Exception as e:
            last_err = e
            _log(
                f"Reels upload: сетка Reel ещё не готова "
                f"(попытка {attempt}/{retries}): {e!r}"
            )
            page.wait_for_timeout(wait_ms)
            try:
                # reload тоже может вытащить вкладку на передний план — в Yt+Inst
                # лучше подождать без reload.
                if keep_in_background:
                    page.wait_for_timeout(1_500)
                else:
                    page.reload(wait_until="domcontentloaded", timeout=120_000)
                    page.wait_for_timeout(1_500)
            except Exception:
                pass
    raise InstagramReelsUploadError(
        "Не удалось открыть/прочитать Reel в профиле."
        + (f" last_err={last_err!r}" if last_err else "")
    )


@instagram_entrypoint
def run_instagram_reels_upload(
    page,
    *,
    video_path: str | Path,
    title: str = "",
    description: str = "",
    session_login: str = "",
    session_password: str = "",
    session_twofa: str = "",
    profile_id: str | None = None,
    top_reels_scan: int = 1,
    on_new_post_clicked=None,
    keep_in_background: bool = False,
    wait_youtube_before_done: threading.Event | None = None,
) -> dict[str, Any]:
    """
    Главная → «Новая публикация» → файл → Share → Post shared →
    username из href Profile → сразу /{username}/reels/ → кандидаты из сетки.

    Возвращает dict: video_id, url, title, description, candidate_reels.
    При ``top_reels_scan`` > 1 собирает несколько первых роликов
    (для multi-tab: если первый уже в БД — взять следующий).
    ``on_new_post_clicked`` — сразу после клика Create (открыть соседние вкладки).
    ``keep_in_background`` — не переключать фокус браузера на эту вкладку
    (Yt+Inst: фокус остаётся на YouTube).
    ``wait_youtube_before_done`` — не жать Done / не открывать /reels/,
    пока YouTube этого ролика не завершится.
    """
    upload_file = _validate_video_file_path(video_path)
    caption = (title or "").strip() or (description or "").strip()
    if (description or "").strip() and (title or "").strip():
        caption = f"{(title or '').strip()}\n\n{(description or '').strip()}".strip()

    _log("Reels upload: проверка сессии / главной Instagram…")
    verify_instagram_home_available(
        page,
        session_login=session_login,
        session_password=session_password,
        session_twofa=session_twofa,
        profile_id=profile_id,
    )

    _click_new_post_in_sidebar(page)
    if callable(on_new_post_clicked):
        try:
            on_new_post_clicked()
        except Exception as e:
            _log(f"Reels upload: on_new_post_clicked: {e!r}")
    _click_create_submenu_post_if_present(page)
    dialog = _wait_create_dialog(page)
    _attach_video_file(
        page, dialog, upload_file, keep_in_background=keep_in_background
    )
    _select_crop_9_16(page)
    _click_next_until_caption_or_share(page)
    _fill_caption(page, caption)
    _click_share(page)
    _wait_post_shared_and_done(
        page,
        keep_in_background=keep_in_background,
        wait_before_done=wait_youtube_before_done,
    )
    username = _open_own_profile(page, session_login=session_login)
    _open_profile_reels_tab(
        page, username, keep_in_background=keep_in_background
    )
    scan_n = max(1, int(top_reels_scan or 1))
    urls = _collect_profile_reel_urls(
        page, limit=scan_n, keep_in_background=keep_in_background
    )
    candidates: list[dict[str, str]] = []
    for u in urls:
        vid_i = _video_id_from_reel_url(u)
        if not vid_i:
            continue
        candidates.append({"video_id": vid_i, "url": u})
    if not candidates:
        raise InstagramReelsUploadError("Не удалось извлечь video_id из сетки Reels.")
    url = candidates[0]["url"]
    vid = candidates[0]["video_id"]

    _log(
        f"Reels upload: готово video_id={vid!r} url={url!r} "
        f"candidates={len(candidates)}"
    )
    # video_id получен — алгоритм залива завершён (без возврата на главную).
    return {
        "video_id": vid,
        "url": url,
        "title": (title or "").strip() or upload_file.stem,
        "description": (description or "").strip(),
        "candidate_reels": candidates,
    }
