"""Залив видео в Instagram Reels: главная → «Новая публикация» → файл → Share."""

from __future__ import annotations

import os
import re
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
_FILE_PICKER_TRANSFER_MS = 600_000

_NEW_POST_ARIA = (
    "Новая публикация",
    "New post",
    "Create",
    "Создать",
)
# Подменю после Create: Post / Live video / Ad (EN) или Публикация / …
_CREATE_MENU_POST_ARIA = (
    "Post",
    "Публикация",
)
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
_NEXT_RE = re.compile(r"^(далее|next)$", re.I)
_SHARE_RE = re.compile(r"^(поделиться|share)$", re.I)
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
    deadline = time.monotonic() + 6.0
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


def _try_click_new_post_once(page) -> bool:
    """Одна попытка клика по «Новая публикация»; True если клик прошёл."""
    for aria in _NEW_POST_ARIA:
        try:
            svg = page.locator(f'svg[aria-label="{aria}"]').first
            if not svg.count() or not svg.is_visible(timeout=800):
                continue
            # Кликаем по кликабельному предку (div[role]/ / ссылка / кнопка).
            clickable = svg.locator(
                "xpath=ancestor::*[@role='button' or @role='link' or self::a or self::button][1]"
            )
            target = clickable if clickable.count() else svg
            target.click(timeout=10_000)
            _log(f"Reels upload: клик по «{aria}».")
            return True
        except Exception:
            continue

    # Fallback: любой svg с title «Новая публикация».
    try:
        titled = page.locator('svg[aria-label] title').filter(
            has_text=re.compile(r"новая публикация|new post|создать|create", re.I)
        )
        if titled.count():
            svg = titled.first.locator("xpath=ancestor::svg[1]")
            clickable = svg.locator(
                "xpath=ancestor::*[@role='button' or @role='link' or self::a or self::button][1]"
            )
            target = clickable if clickable.count() else svg
            target.click(timeout=10_000)
            _log("Reels upload: клик по svg через <title>.")
            return True
    except Exception:
        pass
    return False


def _click_new_post_in_sidebar(page, *, max_seconds: float = 45.0) -> None:
    """Сайдбар главной: svg «Новая публикация» / New post → открыть диалог создания."""
    _log("Reels upload: ищем кнопку «Новая публикация» в сайдбаре…")
    deadline = time.monotonic() + max(5.0, float(max_seconds))
    last_url = ""
    while time.monotonic() < deadline:
        try:
            last_url = (page.url or "").strip()
        except Exception:
            last_url = ""
        if _try_click_new_post_once(page):
            return
        try:
            page.wait_for_timeout(400)
        except Exception:
            time.sleep(0.4)

    raise InstagramReelsUploadError(
        "Не удалось нажать «Новая публикация» в сайдбаре Instagram."
        + (f" URL={last_url!r}" if last_url else "")
    )


def _click_create_submenu_post_if_present(
    page, *, timeout_ms: float = 4_000
) -> bool:
    """
    После Create иногда выпадает меню (Post / Live video / Ad).
    Кликаем Post / Публикация; если меню нет — False (диалог уже открыт).
    """
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        # Диалог уже открыт — подменю не нужно.
        try:
            if _create_dialog_locator(page).first.is_visible(timeout=200):
                return False
        except Exception:
            pass

        for aria in _CREATE_MENU_POST_ARIA:
            try:
                svg = page.locator(f'svg[aria-label="{aria}"]').first
                if not svg.count() or not svg.is_visible(timeout=400):
                    continue
                clickable = svg.locator(
                    "xpath=ancestor::*[@role='button' or @role='link' "
                    "or self::a or self::button][1]"
                )
                target = clickable if clickable.count() else svg
                target.click(timeout=8_000)
                _log(f"Reels upload: в меню Create выбрали «{aria}».")
                return True
            except Exception:
                continue

        try:
            link = page.get_by_role("link", name=_CREATE_MENU_POST_RE).first
            if link.count() and link.is_visible(timeout=400):
                link.click(timeout=8_000)
                _log("Reels upload: в меню Create выбрали Post/Публикация (link).")
                return True
        except Exception:
            pass

        time.sleep(0.2)

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


def _wait_create_dialog(page, *, timeout_ms: float = 45_000) -> Any:
    dialog = _create_dialog_locator(page)
    dialog.first.wait_for(state="visible", timeout=timeout_ms)
    _log("Reels upload: диалог «Создание публикации» открыт.")
    return dialog


def _create_file_input_locator(dialog):
    """form > input[type=file] внутри диалога создания."""
    return (
        dialog.locator('form input[type="file"]')
        .or_(dialog.locator('input[type="file"][accept*="video"]'))
        .or_(dialog.locator('input[type="file"]'))
    )


def _cdp_set_file_input_on_target_once(target, files_path: str) -> bool:
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
        if root_id > 0:
            for sel in (
                'form input[type="file"]',
                'input[type="file"][accept*="video"]',
                'input[type="file"]',
            ):
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

        params: dict = {"query": 'input[type="file"]'}
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


def _set_file_input_via_cdp(page, preferred_frame, resolved_local_path: str) -> bool:
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
        if _cdp_set_file_input_on_target_once(tgt, files_path):
            return True
    return False


def _dismiss_info_dialogs(page) -> None:
    """OK / «Понятно» на подсказках вроде «видеопубликации теперь как Reels»."""
    try:
        dlg = page.locator('[role="dialog"]')
        if not dlg.count():
            return
        btn = dlg.get_by_role("button", name=_OK_DISMISS_RE).first
        if btn.count() and btn.is_visible(timeout=800):
            btn.click(timeout=3_000)
            page.wait_for_timeout(400)
            _log("Reels upload: закрыт информационный диалог.")
    except Exception:
        pass


def _attach_video_file(page, dialog, video_path: Path) -> None:
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

    file_input = _create_file_input_locator(dialog)
    preferred_frame = page
    try:
        if file_input.count():
            preferred_frame = file_input.first.element_handle().owner_frame() or page
    except Exception:
        preferred_frame = page

    file_submitted = False
    if _set_file_input_via_cdp(page, preferred_frame, resolved):
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
                with page.expect_file_chooser(timeout=120_000) as fc_info:
                    if select_btn.count() and select_btn.first.is_visible(timeout=2_000):
                        select_btn.first.click(timeout=30_000)
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

    page.wait_for_timeout(1_200)
    _dismiss_info_dialogs(page)
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


def _select_crop_9_16(page) -> None:
    """Сразу после файла: Select Crop → 9:16 (портрет для Reels)."""
    _log("Reels upload: выбираем обрезку 9:16…")
    # Дождаться экрана кропа (кнопка Select Crop / Выбрать размер и обрезать).
    crop_btn = _crop_btn_svg_locator(page)
    try:
        crop_btn.first.wait_for(state="visible", timeout=60_000)
    except Exception as e:
        raise InstagramReelsUploadError(
            "Кнопка Select Crop не появилась после передачи файла."
        ) from e

    try:
        svg = _crop_btn_svg_locator(page).first
        btn = svg.locator("xpath=ancestor::button[1]")
        target = btn if btn.count() else svg.locator(
            "xpath=ancestor::*[@role='button'][1]"
        )
        if not target.count():
            target = svg
        target.click(timeout=10_000)
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
        if text_opt.count() and text_opt.first.is_visible(timeout=3_000):
            text_opt.first.click(timeout=10_000)
        else:
            svg9 = page.locator(_CROP_9_16_SVG_SEL).first
            if svg9.count() and svg9.is_visible(timeout=2_000):
                clickable = svg9.locator(
                    "xpath=ancestor::*[@role='button'][1]"
                )
                (clickable if clickable.count() else svg9).click(timeout=10_000)
            else:
                option.first.click(timeout=10_000)
        _log("Reels upload: выбрано 9:16.")
    except Exception as e:
        raise InstagramReelsUploadError(
            f"Не удалось выбрать обрезку 9:16: {e!r}"
        ) from e

    page.wait_for_timeout(600)


def _dialog_action_button(page, name_re: re.Pattern[str]):
    """Кнопка Далее/Поделиться в шапке мастера создания."""
    dlg = page.locator('[role="dialog"]').first
    return (
        dlg.get_by_role("button", name=name_re)
        .or_(dlg.locator('[role="button"]').filter(has_text=name_re))
        .or_(page.get_by_role("button", name=name_re))
    )


def _click_next_until_caption_or_share(page, *, max_clicks: int = 6) -> None:
    """Прокликать «Далее» (обрезка / фильтры) до экрана подписи или Share."""
    for i in range(max_clicks):
        _dismiss_info_dialogs(page)
        share = _dialog_action_button(page, _SHARE_RE)
        try:
            if share.count() and share.first.is_visible(timeout=800):
                _log("Reels upload: экран Share / подписи.")
                return
        except Exception:
            pass

        # textarea caption — тоже стоп
        try:
            cap = page.locator(
                '[role="dialog"] textarea, [role="dialog"] [contenteditable="true"]'
            ).first
            if cap.count() and cap.is_visible(timeout=600):
                # Есть caption, но Share может появиться чуть позже
                if share.count() and share.first.is_visible(timeout=400):
                    return
        except Exception:
            pass

        nxt = _dialog_action_button(page, _NEXT_RE)
        try:
            if not nxt.count() or not nxt.first.is_visible(timeout=2_000):
                _log(f"Reels upload: «Далее» не видно (шаг {i + 1}).")
                break
            nxt.first.click(timeout=15_000)
            _log(f"Reels upload: «Далее» ({i + 1}/{max_clicks}).")
            page.wait_for_timeout(900)
        except Exception as e:
            _log(f"Reels upload: клик «Далее» не удался: {e!r}")
            break


def _fill_caption(page, caption: str) -> None:
    text = (caption or "").strip()
    if not text:
        return
    try:
        area = (
            page.locator('[role="dialog"] textarea')
            .or_(page.locator('[role="dialog"] [aria-label*="Подпись" i]'))
            .or_(page.locator('[role="dialog"] [aria-label*="Caption" i]'))
            .or_(page.locator('[role="dialog"] [contenteditable="true"]'))
            .first
        )
        if not area.count() or not area.is_visible(timeout=3_000):
            _log("Reels upload: поле подписи не найдено — пропускаем.")
            return
        area.click(timeout=5_000)
        try:
            area.fill(text, timeout=10_000)
        except Exception:
            area.press_sequentially(text, delay=15)
        _log(f"Reels upload: подпись задана ({len(text)} символов).")
        page.wait_for_timeout(400)
    except Exception as e:
        _log(f"Reels upload: не удалось ввести подпись: {e!r}")


def _click_share(page) -> None:
    share = _dialog_action_button(page, _SHARE_RE)
    if not share.count() or not share.first.is_visible(timeout=15_000):
        raise InstagramReelsUploadError(
            "Кнопка «Поделиться» / Share не найдена после мастера создания."
        )
    share.first.click(timeout=30_000)
    _log("Reels upload: нажали «Поделиться».")


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
        if not btn.count() or not btn.first.is_visible(timeout=3_000):
            return False
        btn.first.click(timeout=15_000)
        return True
    except Exception as e:
        _log(f"Reels upload: клик «Повторить» не удался: {e!r}")
        return False


def _click_post_shared_done(page, dialog) -> None:
    done = (
        dialog.first.get_by_role("button", name=_DONE_RE)
        .or_(dialog.first.locator('[role="button"]').filter(has_text=_DONE_RE))
        .or_(page.get_by_role("button", name=_DONE_RE))
        .or_(page.locator('[role="button"]').filter(has_text=_DONE_RE))
    )
    try:
        if done.count() and done.first.is_visible(timeout=5_000):
            done.first.click(timeout=15_000)
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


def _wait_post_shared_and_done(page, *, timeout_s: float = 600.0) -> None:
    """
    Ждём диалог Post shared / Reel shared (до 10 мин) и жмём Done.

    Если после Share появляется «Не удалось разместить публикацию» —
    один раз жмём «Повторить»; при повторной ошибке — выход с исключением.
    """
    _log(
        "Reels upload: ждём экран «Reel shared» / Post shared "
        f"(таймаут {timeout_s:.0f} с)…"
    )
    deadline = time.monotonic() + max(15.0, float(timeout_s))
    retries_used = 0
    max_auto_retries = 1

    while time.monotonic() < deadline:
        dialog = _post_shared_dialog_locator(page)
        try:
            if dialog.count() and dialog.first.is_visible(timeout=400):
                _log("Reels upload: диалог Post shared виден.")
                _click_post_shared_done(page, dialog)
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


def _click_sidebar_own_profile(page) -> bool:
    """Клик по пункту своего профиля в сайдбаре. True если кликнули / уже на профиле."""
    from zaliver.instagram_upload.register import _extract_logged_in_username

    def _already_on_profile() -> bool:
        try:
            return bool(_username_from_url((page.url or "").strip()))
        except Exception:
            return False

    username = (_extract_logged_in_username(page) or "").strip().lstrip("@")

    # 1) Прямой href /username/
    if username:
        try:
            link = page.locator(
                f'a[href="/{username}/"], a[href="/{username}"]'
            ).first
            if link.count() and link.is_visible(timeout=2_000):
                try:
                    link.click(timeout=15_000)
                except Exception as e:
                    # После клика страница часто уходит с сайдбара — Playwright
                    # кидает navigation/detach, хотя переход уже случился.
                    _log(f"Reels upload: клик a[href=/username/] exception: {e!r}")
                page.wait_for_timeout(800)
                _log(f"Reels upload: клик профиля a[href=/{username}/].")
                return True
        except Exception as e:
            _log(f"Reels upload: клик a[href=/username/]: {e!r}")
            if _already_on_profile():
                return True

    # 2) aria-label / аватар
    for sel in (
        'a[aria-label="Profile" i]',
        'a[aria-label="Профиль" i]',
        'a[role="link"][aria-label*="Profile" i]',
        'a[role="link"][aria-label*="Профиль" i]',
        'svg[aria-label="Profile"]',
        'svg[aria-label="Профиль"]',
        'img[alt*="profile picture" i]',
        'img[alt*="фото профиля" i]',
        'img[alt*="Add a profile photo" i]',
        'img[alt*="Добавить фото профиля" i]',
        'span:text-is("Profile")',
        'span:text-is("Профиль")',
    ):
        try:
            loc = page.locator(sel).first
            if not loc.count() or not loc.is_visible(timeout=700):
                continue
            if sel.startswith(("img", "span", "svg")):
                clickable = loc.locator("xpath=ancestor::a[@href][1]")
                target = clickable if clickable.count() else loc
            else:
                target = loc
            try:
                target.click(timeout=10_000)
            except Exception as e:
                _log(f"Reels upload: клик профиля {sel!r} exception: {e!r}")
            page.wait_for_timeout(800)
            _log(f"Reels upload: клик профиля через {sel!r}.")
            if _already_on_profile():
                return True
            # Клик ушёл — даже если URL ещё не сменился.
            return True
        except Exception:
            if _already_on_profile():
                return True
            continue

    # 3) JS: ссылка сайдбара на один path-сегмент (свой профиль).
    try:
        href = page.evaluate(
            """() => {
              const reserved = new Set([
                'reels','explore','direct','accounts','stories','p','reel','tv',
                'tags','locations','about','legal','web','api','graphql','popular',
                'challenge','privacy','meta','ads','notifications','nametag',
                'directory','your_activity','professional_dashboard','archive'
              ]);
              const pick = (h) => {
                if (!h) return '';
                try {
                  const u = new URL(h, location.origin);
                  const m = (u.pathname || '').match(/^\\/([A-Za-z0-9._]{2,30})\\/?$/);
                  if (!m) return '';
                  const name = m[1];
                  if (reserved.has(name.toLowerCase())) return '';
                  return name;
                } catch (e) { return ''; }
              };
              const sels = [
                'a[aria-label="Profile" i]',
                'a[aria-label="Профиль" i]',
                'nav a[href^="/"]',
                'a[href^="/"]',
              ];
              for (const sel of sels) {
                for (const a of document.querySelectorAll(sel)) {
                  const name = pick(a.getAttribute('href') || a.href || '');
                  if (name) return a.getAttribute('href') || ('/' + name + '/');
                }
              }
              return '';
            }"""
        )
        if isinstance(href, str) and href.strip():
            a = page.locator(f'a[href="{href.strip()}"]').first
            if a.count() and a.is_visible(timeout=2_000):
                try:
                    a.click(timeout=10_000)
                except Exception as e:
                    _log(f"Reels upload: клик JS-профиля exception: {e!r}")
                page.wait_for_timeout(800)
                _log(f"Reels upload: клик профиля через JS href={href!r}.")
                return True
    except Exception as e:
        _log(f"Reels upload: JS поиск профиля: {e!r}")

    if _already_on_profile():
        _log("Reels upload: уже на URL профиля после попыток клика.")
        return True
    return False


def _open_own_profile(page, *, session_login: str = "") -> str:
    """
    Сайдбар → свой профиль.
    Возвращает username; URL вида https://www.instagram.com/{username}.
    """
    from zaliver.instagram_upload.register import (
        _extract_logged_in_username,
        _navigate_page_to,
    )

    hint = _username_hint_from_login(session_login)
    extracted = (_extract_logged_in_username(page) or "").strip().lstrip("@")
    username = extracted or hint
    _log(
        f"Reels upload: открываем свой профиль "
        f"(extracted={extracted!r}, hint={hint!r})…"
    )

    clicked = _click_sidebar_own_profile(page)

    # Клик мог «упасть» из‑за навигации, но URL уже профиль.
    try:
        cur0 = (page.url or "").strip()
    except Exception:
        cur0 = ""
    from_url0 = _username_from_url(cur0)
    if from_url0:
        clicked = True
        username = from_url0
        _log(f"Reels upload: после клика уже на профиле @{username} URL={cur0!r}.")

    if not clicked and username:
        _navigate_page_to(
            page, f"https://www.instagram.com/{username}/", label="IG profile"
        )
        clicked = True

    if not clicked:
        raise InstagramReelsUploadError(
            "Не удалось открыть свой профиль Instagram после Share."
        )

    # Ждём URL профиля /{username}/
    deadline = time.monotonic() + 30.0
    cur = cur0 if from_url0 else ""
    while time.monotonic() < deadline:
        try:
            cur = (page.url or "").strip()
        except Exception:
            cur = ""
        from_url = _username_from_url(cur)
        if from_url:
            username = from_url
            break
        page.wait_for_timeout(400)
    else:
        from_url = _username_from_url(cur)
        if from_url:
            username = from_url

    if not username:
        username = (
            (_extract_logged_in_username(page) or "").strip().lstrip("@") or hint
        )
    if not username:
        raise InstagramReelsUploadError(
            f"Профиль открыт, но username не определён (URL={cur!r})."
        )

    # Если оказались не на странице профиля — явный goto.
    if _username_from_url(cur) != username:
        _navigate_page_to(
            page, f"https://www.instagram.com/{username}/", label="IG profile"
        )
        page.wait_for_timeout(1_000)

    _log(
        f"Reels upload: профиль открыт @{username} "
        f"URL={(getattr(page, 'url', None) or '')!r}."
    )
    return username


def _open_profile_reels_tab(page, username: str) -> None:
    """К текущему URL профиля просто добавляем /reels/."""
    uname = (username or "").strip().lstrip("@")
    if not uname:
        raise InstagramReelsUploadError(
            "Не удалось открыть /reels/ профиля (username пуст)."
        )
    from zaliver.instagram_upload.register import _navigate_page_to

    # Берём текущий URL профиля и дописываем /reels/.
    try:
        cur = (page.url or "").strip()
    except Exception:
        cur = ""
    cur_user = _username_from_url(cur) or uname
    reels_url = f"https://www.instagram.com/{cur_user}/reels/"
    _log(f"Reels upload: открываем {reels_url} (profile URL + /reels/)…")
    _navigate_page_to(page, reels_url, label="IG profile reels")
    page.wait_for_timeout(1_500)

    try:
        page.wait_for_selector('a[href*="/reel/"]', timeout=60_000)
    except Exception as e:
        raise InstagramReelsUploadError(
            f"На {reels_url} нет видео (сетка не прогрузилась)."
        ) from e


def _first_profile_reel_url(page, *, retries: int = 8, wait_ms: int = 2000) -> str:
    """Клик по первому видео в сетке /username/reels/ → ссылка Reel."""
    last_err: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            links = page.locator('a[href*="/reel/"]')
            if links.count() <= 0:
                raise RuntimeError("no reel links")
            first = links.first
            href = (first.get_attribute("href") or "").strip()
            abs_url = _absolute_instagram_url(href)
            canon = _normalize_reel_url(abs_url)
            if not _video_id_from_reel_url(canon):
                raise RuntimeError(f"bad href={href!r}")

            # Клик по первому видео (как просил сценарий).
            try:
                first.click(timeout=15_000)
                page.wait_for_timeout(1_200)
                try:
                    cur = (page.url or "").strip()
                except Exception:
                    cur = ""
                if _video_id_from_reel_url(cur):
                    canon = _normalize_reel_url(cur)
            except Exception as e:
                _log(f"Reels upload: клик первого Reel не обязателен: {e!r}")

            _log(
                f"Reels upload: первое видео — {canon!r} "
                f"(попытка {attempt}/{retries})."
            )
            return canon
        except Exception as e:
            last_err = e
            _log(
                f"Reels upload: первое Reel ещё не видно "
                f"(попытка {attempt}/{retries}): {e!r}"
            )
            page.wait_for_timeout(wait_ms)
            try:
                # Остаёмся на /username/reels/, только reload.
                page.reload(wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(1_500)
            except Exception:
                pass
    raise InstagramReelsUploadError(
        "Не удалось открыть/прочитать первое Reel в профиле."
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
) -> dict[str, str]:
    """
    Главная → «Новая публикация» → файл → Share → Post shared →
    свой профиль → URL+/reels/ → первое видео.

    Возвращает dict: video_id, url, title, description.
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
    _click_create_submenu_post_if_present(page)
    dialog = _wait_create_dialog(page)
    _attach_video_file(page, dialog, upload_file)
    _select_crop_9_16(page)
    _click_next_until_caption_or_share(page)
    _fill_caption(page, caption)
    _click_share(page)
    _wait_post_shared_and_done(page)
    username = _open_own_profile(page, session_login=session_login)
    _open_profile_reels_tab(page, username)
    url = _first_profile_reel_url(page)
    vid = _video_id_from_reel_url(url)
    if not vid:
        raise InstagramReelsUploadError(f"Не удалось извлечь video_id из {url!r}.")

    _log(f"Reels upload: готово video_id={vid!r} url={url!r}")
    return {
        "video_id": vid,
        "url": url,
        "title": (title or "").strip() or upload_file.stem,
        "description": (description or "").strip(),
    }
