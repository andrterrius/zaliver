from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from patchright.sync_api import sync_playwright

from zaliver.antydetect.api import DolphinAntyError, DolphinAntyLocalAPI
from zaliver.antydetect.cookie_farm import run_cookie_farm
from zaliver.log_format import with_log_profile
from zaliver.youtube_upload.studio import (
    PromotionTargetVideo,
    YoutubeAllChannelsRemovedError,
    YoutubeStudioError,
    run_studio_channel_description_and_link,
    run_studio_channel_profile_customization,
    run_studio_channel_profile_picture,
    run_studio_upload_default_title,
    run_upload_latest_ready_video,
    run_youtube_interface_language_to_russian,
    run_youtube_profiles_promotion,
    run_youtube_shorts_warmup,
    run_youtube_shorts_warmup_during_upload,
    set_log_sink,
    verify_studio_upload_dialog_available,
)

from zaliver.youtube_upload import studio as _studio


def _log(message: str) -> None:
    # Пишем в тот же sink, что и `youtube_upload.studio`,
    # чтобы UI показывал логи единым потоком.
    _studio._log(f"[antic_open] {message}")


def _try_enlarge_browser_os_window(page) -> None:
    """
    Обычное desktop-окно Chromium (не на весь экран), viewport страницы не трогаем.

    Для Instagram check с iPhone preset: мобильный viewport остаётся,
    окно — как у Gmail/YouTube (~1280×900).
    """
    cdp = None
    width, height = 1280, 900
    try:
        cdp = page.context.new_cdp_session(page)
        info = cdp.send("Browser.getWindowForTarget")
        win_id = info.get("windowId") if isinstance(info, dict) else None
        if not win_id:
            return
        cdp.send(
            "Browser.setWindowBounds",
            {"windowId": win_id, "bounds": {"windowState": "normal"}},
        )
        cdp.send(
            "Browser.setWindowBounds",
            {
                "windowId": win_id,
                "bounds": {
                    "left": 0,
                    "top": 0,
                    "width": width,
                    "height": height,
                },
            },
        )
        _log(f"OS-окно браузера: {width}x{height} (viewport не меняли).")
    except Exception as e:
        _log(f"Не удалось увеличить OS-окно браузера: {e!r}")
    finally:
        if cdp is not None:
            try:
                cdp.detach()
            except Exception:
                pass


def _close_playwright_browser(
    browser, *, timeout_s: float = 8.0, shared_cdp: bool = False
) -> None:
    """
    Отключить Playwright от CDP-браузера.

    Важно: вызывать только из потока, где создан sync_playwright.
    shared_cdp=True: НЕ вызывать browser.close() — для connect_over_cdp это
    часто шлёт Browser.close и гасит весь Chrome (вторую вкладку YouTube тоже).
    Достаточно pw.stop() у владельца соединения.
    """
    del timeout_s  # совместимость вызовов; таймаут через чужой поток нельзя
    if browser is None:
        return
    if shared_cdp:
        return
    try:
        browser.close()
    except Exception:
        pass


def _stop_playwright_driver(playwright, *, timeout_s: float = 8.0) -> None:
    """Остановить драйвер Playwright в том же потоке, где был start()."""
    del timeout_s
    if playwright is None:
        return
    try:
        playwright.stop()
    except Exception:
        pass


def _with_sync_playwright(
    job_fn: Callable[[Any], Any],
    *,
    label: str = "Playwright",
    release_before_stop: bool = False,
):
    """
    Выполнить job_fn(pw) в отдельном потоке с собственным sync_playwright.

    Нужно, чтобы:
    - stop()/close() всегда шли в потоке-владельце loop;
    - keep-open + «брошенный» stop в фоне не травили worker MultiProfileUploader
      ошибкой Sync API inside the asyncio loop.

    release_before_stop=True (keep-open): вернуть результат сразу после job_fn,
    pw.stop() доработает в фоне на этом же dedicated-потоке (не на worker).
    """
    box: dict[str, Any] = {}
    done = threading.Event()

    def _runner() -> None:
        pw = None
        try:
            pw = sync_playwright().start()
            box["result"] = job_fn(pw)
        except BaseException as e:
            box["error"] = e
        finally:
            done.set()
            if pw is not None:
                try:
                    pw.stop()
                except Exception as se:
                    try:
                        _log(f"{label}: pw.stop(): {se!r}")
                    except Exception:
                        pass

    t = threading.Thread(
        target=_runner,
        name=f"sync-pw-{(label or 'job')[:24]}",
        daemon=True,
    )
    t.start()
    done.wait()
    if not release_before_stop:
        t.join(timeout=120.0)
    err = box.get("error")
    if isinstance(err, BaseException):
        raise err
    return box.get("result")


# Сериализация start_profile / launch при параллельных вкладках одного профиля.
_PROFILE_LAUNCH_LOCKS: dict[str, threading.Lock] = {}
_PROFILE_LAUNCH_LOCKS_GUARD = threading.Lock()
# CDP endpoints keep-open для Dolphin (повторный start не всегда нужен).
_DOLPHIN_KEEP_OPEN_CDP: dict[str, tuple[str, ...]] = {}
_DOLPHIN_KEEP_OPEN_CDP_LOCK = threading.Lock()


def _profile_launch_lock(profile_id: str) -> threading.Lock:
    pid = (profile_id or "").strip()
    with _PROFILE_LAUNCH_LOCKS_GUARD:
        lock = _PROFILE_LAUNCH_LOCKS.get(pid)
        if lock is None:
            lock = threading.Lock()
            _PROFILE_LAUNCH_LOCKS[pid] = lock
        return lock


def _cache_dolphin_keep_open_cdp(profile_id: str, endpoints: tuple[str, ...]) -> None:
    pid = (profile_id or "").strip()
    cleaned = tuple(e.strip() for e in endpoints if (e or "").strip())
    if not pid or not cleaned:
        return
    with _DOLPHIN_KEEP_OPEN_CDP_LOCK:
        _DOLPHIN_KEEP_OPEN_CDP[pid] = cleaned


def _get_dolphin_keep_open_cdp(profile_id: str) -> tuple[str, ...] | None:
    pid = (profile_id or "").strip()
    if not pid:
        return None
    with _DOLPHIN_KEEP_OPEN_CDP_LOCK:
        cached = _DOLPHIN_KEEP_OPEN_CDP.get(pid)
    if not cached:
        return None
    return tuple(cached)


def clear_dolphin_keep_open_cdp(profile_id: str) -> None:
    """Сбросить кэш CDP после stop_profile (в т.ч. close_kept_browser)."""
    pid = (profile_id or "").strip()
    if not pid:
        return
    with _DOLPHIN_KEEP_OPEN_CDP_LOCK:
        _DOLPHIN_KEEP_OPEN_CDP.pop(pid, None)


# Keep-open: метаданные сессии + событие «вкладки заранее открыты».
# Долгий CDP-клиент не держим: каждый залив connect'ится сам (лок только на connect).
_IG_KEEP_OPEN_META: dict[str, dict] = {}
_IG_KEEP_OPEN_META_GUARD = threading.Lock()


def _ig_meta_get(profile_id: str) -> dict | None:
    pid = (profile_id or "").strip()
    if not pid:
        return None
    with _IG_KEEP_OPEN_META_GUARD:
        return _IG_KEEP_OPEN_META.get(pid)


def _ig_meta_set(profile_id: str, meta: dict) -> None:
    pid = (profile_id or "").strip()
    if not pid:
        return
    with _IG_KEEP_OPEN_META_GUARD:
        _IG_KEEP_OPEN_META[pid] = meta


def close_instagram_keep_open_hub(profile_id: str) -> None:
    """Сбросить keep-open метаданные профиля (браузер гасит вызывающий код)."""
    pid = (profile_id or "").strip()
    if not pid:
        return
    # Дождаться фоновой очереди Instagram Yt+Inst, иначе потеряем заливы.
    try:
        drain_yt_inst_ig_pipeline(pid, timeout_s=3600.0)
    except Exception as e:
        _log(f"Yt+Inst: drain IG pipeline перед close: {e!r}")
    with _IG_KEEP_OPEN_META_GUARD:
        meta = _IG_KEEP_OPEN_META.pop(pid, None)
    if not meta:
        return
    ready = meta.get("tabs_ready")
    if isinstance(ready, threading.Event):
        ready.set()
    _log(f"Instagram Reels: keep-open meta сброшена profile_id={pid!r}.")


def _ig_hub_page_alive(page) -> bool:
    if page is None:
        return False
    try:
        return not page.is_closed()
    except Exception:
        return False


def _ig_hub_navigate_home_quick(page) -> None:
    """Быстро открыть главную на новой вкладке (без полной verify-сессии)."""
    try:
        from zaliver.instagram_upload.register import INSTAGRAM_URL

        page.goto(INSTAGRAM_URL, wait_until="domcontentloaded", timeout=45_000)
    except Exception as e:
        _log(f"Instagram Reels: goto главной на новой вкладке: {e!r}")
    # Уникальная метка вкладки (для claim между connect'ами).
    try:
        _ig_page_target_id(page)
    except Exception:
        pass


def _ig_alive_context_pages(context) -> list:
    pages: list = []
    for pg in list(getattr(context, "pages", None) or []):
        if _ig_hub_page_alive(pg):
            pages.append(pg)
    return pages


def _ig_page_url_lower(page) -> str:
    try:
        return (page.url or "").strip().lower()
    except Exception:
        return ""


def _ig_instagram_pages(context) -> list:
    """Только вкладки Instagram — служебные Dolphin/chrome в лимит не считаем."""
    out: list = []
    for pg in _ig_alive_context_pages(context):
        if "instagram.com" in _ig_page_url_lower(pg):
            out.append(pg)
    return out


def _ig_reusable_blank_pages(context) -> list:
    """about:blank / пустой URL — можно превратить в IG вместо new_page."""
    out: list = []
    for pg in _ig_alive_context_pages(context):
        url = _ig_page_url_lower(pg)
        if url in ("about:blank", "about:srcdoc", ""):
            out.append(pg)
    return out


def _ig_new_page_background(context, *, seed_page=None, url: str = "about:blank"):
    """
    Новая вкладка БЕЗ переключения на неё (CDP Target.createTarget background=true).
    Fallback: context.new_page() — в Chrome обычно активирует вкладку.
    Если createTarget создал target, но Playwright его не увидел — закрываем orphan,
    иначе остаётся лишняя about:blank рядом с вкладкой от new_page().
    """
    seed = seed_page
    if seed is None:
        alive = _ig_alive_context_pages(context)
        seed = alive[0] if alive else None
    if seed is None:
        return context.new_page()

    # Уже есть Instagram — вторую не открываем.
    existing_ig = _ig_instagram_pages(context)
    if existing_ig:
        return existing_ig[0]

    before_ids = {id(p) for p in _ig_alive_context_pages(context)}
    want_url = (url or "about:blank").strip() or "about:blank"
    cdp = None
    target_id: str | None = None

    def _find_new_page():
        for p in _ig_alive_context_pages(context):
            if id(p) not in before_ids:
                return p
        return None

    def _close_orphan_target(session, tid: str | None) -> None:
        if session is None or not tid:
            return
        try:
            session.send("Target.closeTarget", {"targetId": tid})
            _log(
                "Instagram Reels: закрыт orphan createTarget "
                f"(targetId={tid!r})."
            )
        except Exception as e:
            _log(
                f"Instagram Reels: не удалось закрыть orphan createTarget: {e!r}"
            )

    try:
        cdp = context.new_cdp_session(seed)
        params: dict = {
            "url": want_url,
            "background": True,
        }
        try:
            info = cdp.send("Target.getTargetInfo")
            ti = (info or {}).get("targetInfo") if isinstance(info, dict) else None
            if isinstance(ti, dict):
                bcid = ti.get("browserContextId")
                if bcid:
                    params["browserContextId"] = bcid
        except Exception:
            pass

        # Только sync CDP в том же потоке, что sync_playwright (не Thread!).
        created = cdp.send("Target.createTarget", params)
        if isinstance(created, dict):
            tid = created.get("targetId")
            if isinstance(tid, str) and tid.strip():
                target_id = tid.strip()

        # background createTarget часто появляется в context.pages с задержкой —
        # НЕ закрываем targetId (это и была живая вкладка Instagram).
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            found = _find_new_page()
            if found is not None:
                _log("Instagram Reels: вкладка открыта в фоне (CDP background).")
                return found
            ig_now = _ig_instagram_pages(context)
            if ig_now:
                return ig_now[0]
            time.sleep(0.1)

        _log(
            "Instagram Reels: CDP createTarget ещё не в context.pages "
            f"(targetId={target_id!r}) — fallback new_page() без closeTarget."
        )
        # closeTarget здесь нельзя: гасит единственную IG-вкладку, потом
        # pipeline видит только Studio и browser.close() убивал YouTube.
        found = _find_new_page()
        if found is not None:
            return found
        ig_now = _ig_instagram_pages(context)
        if ig_now:
            return ig_now[0]
    except Exception as e:
        _log(
            f"Instagram Reels: фоновое createTarget не удалось ({e!r}) — "
            "fallback new_page() только если IG ещё нет."
        )
        found = _find_new_page()
        if found is not None:
            return found
        ig_now = _ig_instagram_pages(context)
        if ig_now:
            return ig_now[0]
    finally:
        if cdp is not None:
            try:
                cdp.detach()
            except Exception:
                pass

    ig_now = _ig_instagram_pages(context)
    if ig_now:
        return ig_now[0]
    return context.new_page()


def _ig_preopen_sibling_tabs(context, tabs_per_profile: int, meta: dict | None) -> None:
    """
    После «Новая публикация» добрать вкладки до лимита tabs_per_profile.
    Лимит считаем только по Instagram-вкладкам (не по всем context.pages:
    иначе служебные вкладки Dolphin «съедают» слоты и new_page не вызывается).
    Открываем в фоне — без переключения активной вкладки.
    """
    want = max(1, int(tabs_per_profile or 1))
    if want <= 1 or context is None:
        if meta is not None:
            ready = meta.get("tabs_ready")
            if isinstance(ready, threading.Event):
                ready.set()
            meta["preopened"] = True
        return

    ig_have = len(_ig_instagram_pages(context))
    all_have = len(_ig_alive_context_pages(context))
    if meta is not None and meta.get("preopened") and ig_have >= want:
        ready = meta.get("tabs_ready")
        if isinstance(ready, threading.Event):
            ready.set()
        _log(
            f"Instagram Reels: вкладки уже готовы "
            f"(ig={ig_have}/{want}, all={all_have}) — новые не открываем."
        )
        return

    need = max(0, want - ig_have)
    if need <= 0:
        if meta is not None:
            ready = meta.get("tabs_ready")
            if isinstance(ready, threading.Event):
                ready.set()
            meta["preopened"] = True
        _log(
            f"Instagram Reels: лимит IG-вкладок уже достигнут "
            f"(ig={ig_have}/{want}, all={all_have}) — new_page пропущен."
        )
        return

    alive = _ig_alive_context_pages(context)
    seed = (_ig_instagram_pages(context) or alive or [None])[0]
    from zaliver.instagram_upload.register import INSTAGRAM_URL

    blanks = _ig_reusable_blank_pages(context)
    # Primary IG не трогаем как blank (её нет в blanks).
    opened = 0
    _log(
        f"Instagram Reels: preopen — нужно ещё {need} "
        f"(сейчас ig={ig_have}/{want}, all={all_have}, blank={len(blanks)})."
    )
    for _ in range(need):
        ig_now = len(_ig_instagram_pages(context))
        if ig_now >= want:
            break
        page = None
        reused_blank = False
        if blanks:
            page = blanks.pop(0)
            reused_blank = True
            try:
                _ig_hub_navigate_home_quick(page)
            except Exception as e:
                _log(f"Instagram Reels: blank→IG не удалось: {e!r}")
                page = None
                reused_blank = False
        if page is None:
            try:
                page = _ig_new_page_background(
                    context, seed_page=seed, url=INSTAGRAM_URL
                )
            except Exception as e:
                _log(f"Instagram Reels: не удалось заранее открыть вкладку: {e!r}")
                break
        # Если createTarget уже с url — только метка; иначе goto (без activate).
        try:
            cur = (page.url or "").strip().lower()
        except Exception:
            cur = ""
        if "instagram.com" not in cur:
            _ig_hub_navigate_home_quick(page)
        else:
            try:
                _ig_page_target_id(page)
            except Exception:
                pass
        opened += 1
        how = "blank→IG" if reused_blank else "new"
        _log(
            f"Instagram Reels: заранее открыта вкладка +{opened} "
            f"({how}, ig→{len(_ig_instagram_pages(context))}/{want}, фон)."
        )

    if meta is not None:
        ready = meta.get("tabs_ready")
        if isinstance(ready, threading.Event):
            ready.set()
        meta["preopened"] = True
        total_ig = len(_ig_instagram_pages(context))
        _log(
            f"Instagram Reels: соседние вкладки готовы "
            f"(ig={total_ig}/{want}, all={len(_ig_alive_context_pages(context))}) "
            "— можно стартовать параллельный залив."
        )


def _ig_page_target_id(page) -> str:
    """Стабильный уникальный id вкладки (не URL — у всех home он одинаковый)."""
    # 1) Метка, которую ставим сами при open/preopen.
    try:
        marked = page.evaluate(
            """() => {
                try {
                    const k = '__zaliver_tab_id';
                    if (!window[k]) {
                        window[k] = 'z' + Math.random().toString(36).slice(2)
                            + Date.now().toString(36);
                    }
                    return String(window[k]);
                } catch (e) {
                    return '';
                }
            }"""
        )
        if isinstance(marked, str) and marked.strip():
            return marked.strip()
    except Exception:
        pass
    # 2) CDP targetId
    try:
        session = page.context.new_cdp_session(page)
        try:
            info = session.send("Target.getTargetInfo")
            tid = ""
            if isinstance(info, dict):
                ti = info.get("targetInfo") or info
                if isinstance(ti, dict):
                    tid = str(ti.get("targetId") or "").strip()
            if tid:
                return tid
        finally:
            try:
                session.detach()
            except Exception:
                pass
    except Exception:
        pass
    return f"obj:{id(page)}"


def _ig_claim_page_for_tab(meta: dict | None, page, tab_index: int) -> bool:
    """
    Занять вкладку за tab_index. False если её уже держит другой tab.
    """
    if meta is None or page is None:
        return True
    tid = _ig_page_target_id(page)
    with _IG_KEEP_OPEN_META_GUARD:
        busy: dict = meta.setdefault("busy_targets", {})
        owner = busy.get(tid)
        if owner is not None and int(owner) != int(tab_index):
            return False
        busy[tid] = int(tab_index)
        meta.setdefault("tab_targets", {})[int(tab_index)] = tid
    return True


def _ig_release_page_for_tab(meta: dict | None, tab_index: int) -> None:
    if meta is None:
        return
    with _IG_KEEP_OPEN_META_GUARD:
        tab_targets: dict = meta.get("tab_targets") or {}
        tid = tab_targets.pop(int(tab_index), None)
        busy: dict = meta.get("busy_targets") or {}
        if tid is not None and busy.get(tid) == int(tab_index):
            busy.pop(tid, None)


def _ig_pick_page_for_tab(
    context,
    *,
    tab_index: int,
    dedicated_tab: bool,
    fallback_page=None,
    tabs_per_profile: int = 1,
    meta: dict | None = None,
):
    """Выбрать страницу для tab_index среди уже открытых context.pages."""
    tab_i = max(0, int(tab_index))
    want = max(1, int(tabs_per_profile or 1))
    pages = _ig_alive_context_pages(context)

    if tab_i == 0 and not dedicated_tab:
        primary = _pick_primary_instagram_page(context, fallback_page)
        chosen = primary if primary is not None else fallback_page
        if chosen is not None and not _ig_claim_page_for_tab(meta, chosen, tab_i):
            _log(
                f"Instagram Reels: tab={tab_i} primary уже занята другим воркером."
            )
        return chosen

    # tab>=1: только отдельные вкладки, primary никогда не трогаем.
    ig_pages: list = []
    primary = _pick_primary_instagram_page(context, None)
    for pg in pages:
        if primary is not None and pg is primary:
            continue
        try:
            url = (pg.url or "").strip().lower()
        except Exception:
            url = ""
        if "instagram.com" in url or url in ("about:blank", "about:srcdoc", ""):
            ig_pages.append(pg)

    def _try_claim(candidates: list):
        for pg in candidates:
            if pg is None:
                continue
            if primary is not None and pg is primary:
                continue
            if _ig_claim_page_for_tab(meta, pg, tab_i):
                return pg
        return None

    extra_i = max(0, tab_i - 1)
    if extra_i < len(ig_pages):
        # Сначала «своя» по индексу, иначе любая свободная доп. вкладка.
        ordered = [ig_pages[extra_i]] + [
            p for j, p in enumerate(ig_pages) if j != extra_i
        ]
        chosen = _try_claim(ordered)
        if chosen is not None:
            _log(f"Instagram Reels: взяли заранее открытую вкладку tab={tab_i}.")
            return chosen

    have = len(_ig_instagram_pages(context))
    if have < want:
        page, _own = _open_dedicated_instagram_tab(
            context, fallback_page=None
        )
        if page is not None and page is not fallback_page and page is not primary:
            _ig_hub_navigate_home_quick(page)
            if _ig_claim_page_for_tab(meta, page, tab_i):
                _log(
                    f"Instagram Reels: открыта новая вкладка tab={tab_i} "
                    f"(ig={have + 1}/{want})."
                )
                return page

    # Свободная доп. вкладка (не primary).
    chosen = _try_claim(ig_pages)
    if chosen is not None:
        _log(
            f"Instagram Reels: tab={tab_i} взял свободную доп. вкладку "
            f"(лимит ig={want})."
        )
        return chosen

    _log(
        f"Instagram Reels: tab={tab_i} нет свободной вкладки "
        f"(не используем primary, чтобы не сбить залив tab0)."
    )
    return None

def _open_dedicated_instagram_tab(context, *, fallback_page=None):
    """
    Новая вкладка для параллельного залива Reels (отдельное UI-состояние).
    Стараемся открыть в фоне, без переключения активной вкладки.
    """
    try:
        from zaliver.instagram_upload.register import INSTAGRAM_URL

        page = _ig_new_page_background(
            context, seed_page=fallback_page, url=INSTAGRAM_URL
        )
        try:
            cur = (page.url or "").strip().lower()
        except Exception:
            cur = ""
        if "instagram.com" not in cur:
            _ig_hub_navigate_home_quick(page)
        else:
            try:
                _ig_page_target_id(page)
            except Exception:
                pass
        _log("Instagram Reels: открыта отдельная вкладка для залива (фон).")
        return page, True
    except Exception as e:
        _log(f"Instagram Reels: new_page не удался ({e!r}) — используем fallback.")
        if fallback_page is not None:
            return fallback_page, False
        return None, False


def _pick_primary_instagram_page(context, fallback_page=None):
    """
    Стартовая вкладка Instagram: первая незакрытая с instagram.com
    (не about:blank и не поздние new_page() других воркеров).
    """
    first_any = None
    first_ig = None
    for pg in list(getattr(context, "pages", None) or []):
        try:
            if pg.is_closed():
                continue
        except Exception:
            continue
        if first_any is None:
            first_any = pg
        try:
            url = (pg.url or "").strip().lower()
        except Exception:
            url = ""
        if "instagram.com" in url and first_ig is None:
            first_ig = pg
            break
    chosen = first_ig or first_any or fallback_page
    if chosen is not None:
        try:
            _log(
                "Instagram Reels: используем первичную вкладку "
                f"url={(chosen.url or '')!r}"
            )
        except Exception:
            _log("Instagram Reels: используем первичную вкладку профиля.")
    return chosen


def _is_scheduled_studio_upload(schedule_publish_at, scheduled_batch) -> bool:
    if scheduled_batch:
        return True
    return schedule_publish_at is not None


def _pick_studio_page_from_context(context, *, exclude=None):
    """Вкладка YouTube Studio в контексте браузера (не вкладка Shorts)."""
    fallback = None
    for pg in context.pages:
        if pg is exclude:
            continue
        try:
            if pg.is_closed():
                continue
        except Exception:
            continue
        try:
            url = pg.url or ""
        except Exception:
            url = ""
        if "studio.youtube.com" in url:
            return pg
        if fallback is None:
            fallback = pg
    return fallback


def _bring_studio_tab_to_front(page, *, log_label: str = "Upload") -> None:
    """Активировать вкладку YouTube Studio в браузере."""
    try:
        page.bring_to_front()
        url = page.url or ""
        if "studio.youtube.com" in url:
            _log(f"{log_label}: фокус на вкладке YouTube Studio ({url!r}).")
        else:
            _log(f"{log_label}: фокус возвращён на вкладку заливки ({url!r}).")
    except Exception as e:
        _log(f"{log_label}: не удалось вернуть фокус на Studio: {e!r}")


def _refocus_studio_tab_after_warmup_start(studio_page, *, timeout_s: float = 5.0) -> None:
    """Дождаться открытия вкладки Shorts и вернуть фокус на Studio."""
    deadline = time.monotonic() + max(0.5, float(timeout_s))
    while time.monotonic() < deadline:
        _bring_studio_tab_to_front(studio_page, log_label="Upload")
        try:
            if "studio.youtube.com" in (studio_page.url or ""):
                return
        except Exception:
            pass
        time.sleep(0.15)
    _bring_studio_tab_to_front(studio_page, log_label="Upload")


class ParallelShortsWarmupRunner:
    """Прогрев Shorts на второй вкладке профиля (отдельное CDP-подключение Playwright)."""

    def __init__(
        self,
        *,
        cdp_endpoints: tuple[str, ...],
        login_credentials=None,
        shorts_recommendations: bool = True,
        search_query: str | None = None,
        shorts_batch_count: int = 5,
        like_probability_pct: float = 10.0,
        subscribe_probability_pct: float = 10.0,
        shorts_watch_min_s: float = 5.0,
        shorts_watch_max_s: float = 25.0,
    ) -> None:
        self._cdp_endpoints = tuple(
            e.strip() for e in cdp_endpoints if (e or "").strip()
        )
        self._login_credentials = login_credentials
        self._shorts_recommendations = bool(shorts_recommendations)
        self._search_query = (search_query or "").strip() or None
        self._shorts_batch_count = max(1, int(shorts_batch_count))
        self._like_probability_pct = float(like_probability_pct)
        self._subscribe_probability_pct = float(subscribe_probability_pct)
        self._shorts_watch_min_s = float(shorts_watch_min_s)
        self._shorts_watch_max_s = float(shorts_watch_max_s)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: BaseException | None = None

    def start(self) -> None:
        if not self._cdp_endpoints:
            raise DolphinAntyError("Parallel Shorts warmup: пустой CDP endpoint.")
        self._thread = threading.Thread(
            target=self._worker,
            name="parallel-shorts-warmup",
            daemon=True,
        )
        self._thread.start()
        _log("Parallel Shorts warmup: фоновая вкладка Shorts запущена.")

    def stop(self, *, timeout_s: float = 90.0) -> None:
        self._stop.set()
        th = self._thread
        if th is not None and th.is_alive():
            th.join(timeout=max(1.0, float(timeout_s)))
        if self.error is not None:
            _log(
                "Parallel Shorts warmup: завершено с ошибкой "
                f"{type(self.error).__name__}: {self.error!r}"
            )
        else:
            _log("Parallel Shorts warmup: фоновая вкладка Shorts остановлена.")

    def _worker(self) -> None:
        try:
            with sync_playwright() as p:
                browser, context, _page = _playwright_page_from_cdp(
                    p, self._cdp_endpoints
                )
                shorts_page = context.new_page()
                studio_page = _pick_studio_page_from_context(
                    context, exclude=shorts_page
                )
                if studio_page is not None:
                    _bring_studio_tab_to_front(
                        studio_page, log_label="Parallel Shorts warmup"
                    )
                try:
                    run_youtube_shorts_warmup_during_upload(
                        shorts_page,
                        should_stop=self._stop.is_set,
                        login_credentials=self._login_credentials,
                        shorts_recommendations=self._shorts_recommendations,
                        search_query=self._search_query,
                        shorts_batch_count=self._shorts_batch_count,
                        like_probability_pct=self._like_probability_pct,
                        subscribe_probability_pct=self._subscribe_probability_pct,
                        shorts_watch_min_s=self._shorts_watch_min_s,
                        shorts_watch_max_s=self._shorts_watch_max_s,
                    )
                finally:
                    try:
                        shorts_page.close()
                    except Exception:
                        pass
                    # Shared CDP с основным заливом — не browser.close().
                    _close_playwright_browser(browser, shared_cdp=True)
        except Exception as e:
            self.error = e
            if not self._stop.is_set():
                _log(
                    "Parallel Shorts warmup: ошибка в фоне "
                    f"{type(e).__name__}: {e!r}"
                )


def _maybe_start_parallel_shorts_warmup(
    *,
    enabled: bool,
    schedule_publish_at,
    scheduled_batch,
    cdp_endpoints: tuple[str, ...],
    login_credentials,
    shorts_recommendations: bool = True,
    search_query: str | None = None,
    shorts_batch_count: int = 5,
    like_probability_pct: float = 10.0,
    subscribe_probability_pct: float = 10.0,
    shorts_watch_min_s: float = 5.0,
    shorts_watch_max_s: float = 25.0,
) -> ParallelShortsWarmupRunner | None:
    if not enabled:
        return None
    if not _is_scheduled_studio_upload(schedule_publish_at, scheduled_batch):
        return None
    runner = ParallelShortsWarmupRunner(
        cdp_endpoints=cdp_endpoints,
        login_credentials=login_credentials,
        shorts_recommendations=shorts_recommendations,
        search_query=search_query,
        shorts_batch_count=shorts_batch_count,
        like_probability_pct=like_probability_pct,
        subscribe_probability_pct=subscribe_probability_pct,
        shorts_watch_min_s=shorts_watch_min_s,
        shorts_watch_max_s=shorts_watch_max_s,
    )
    runner.start()
    return runner


def _stop_parallel_shorts_warmup(runner: ParallelShortsWarmupRunner | None) -> None:
    if runner is None:
        return
    runner.stop()


def _wrap_exc(e: Exception) -> DolphinAntyError:
    # UI в приложении ловит DolphinAntyError и показывает аккуратный текст.
    if isinstance(e, DolphinAntyError):
        return e
    if isinstance(e, YoutubeStudioError):
        return DolphinAntyError(str(e))
    from zaliver.youtube_upload.google_login import GoogleLoginCredentialsMissingError

    if isinstance(e, GoogleLoginCredentialsMissingError):
        return DolphinAntyError(str(e))
    return DolphinAntyError(repr(e))


def _save_yt_oldest_name_to_profile(api, profile_id: str, name: str) -> None:
    from zaliver.youtube_upload.google_login import YT_OLDEST_NAME_KEY

    n = (name or "").strip()
    if not n:
        return
    try:
        api.merge_profile_custom_data(profile_id, {YT_OLDEST_NAME_KEY: n})
        _log(f"Local antidetect: в custom_data сохранён {YT_OLDEST_NAME_KEY}={n!r}")
    except Exception as e:
        _log(
            f"Local antidetect: не удалось сохранить {YT_OLDEST_NAME_KEY} "
            f"для profile_id={profile_id!r}: {e!r}"
        )


def _save_instagram_credentials_to_profile(api, profile_id: str, credentials) -> None:
    """Сохранить inst_login / inst_password в custom_data (yt_* и inst_2fa не трогаем)."""
    from zaliver.core.profiles.account_data import INST_LOGIN_KEY, INST_PASSWORD_KEY

    if credentials is None:
        return
    email = str(getattr(credentials, "email", "") or "").strip()
    password = str(getattr(credentials, "password", "") or "")
    if not email and not password:
        return
    # inst_2fa не пишем при регистрации — не затираем вручную введённый секрет.
    payload = {
        INST_LOGIN_KEY: email,
        INST_PASSWORD_KEY: password,
    }
    try:
        api.merge_profile_custom_data(profile_id, payload)
        _log(
            "Local antidetect: в custom_data сохранены Instagram-данные "
            f"({INST_LOGIN_KEY}={email!r}) для profile_id={profile_id!r}."
        )
    except Exception as e:
        _log(
            "Local antidetect: не удалось сохранить Instagram-данные "
            f"для profile_id={profile_id!r}: {e!r}"
        )


def _make_save_yt_oldest_name_handler(api, profile_id: str):
    def save(name: str) -> None:
        _save_yt_oldest_name_to_profile(api, profile_id, name)

    return save


def _make_save_name_change_cooldown_handler(api, profile_id: str):
    from zaliver.ui.profile_avatar_data import (
        NAME_CHANGE_COOLDOWN_DAYS,
        channel_name_change_cooldown_payload,
    )

    def save() -> None:
        try:
            api.merge_profile_custom_data(
                profile_id, channel_name_change_cooldown_payload()
            )
            _log(
                "Local antidetect: в custom_data сохранён лимит смены названия канала "
                f"({NAME_CHANGE_COOLDOWN_DAYS} дн.)."
            )
        except Exception as e:
            _log(
                f"Local antidetect: не удалось сохранить лимит смены названия "
                f"для profile_id={profile_id!r}: {e!r}"
            )

    return save


def _local_studio_workflow_kwargs(
    api,
    profile_id: str,
    *,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    search_oldest_channel: bool = False,
    include_name_change_cooldown: bool = False,
) -> dict:
    kw: dict = {
        "profile_id": profile_id,
        "login_credentials": login_credentials,
        "yt_oldest_name": (yt_oldest_name or "").strip() or None,
        "search_oldest_channel": search_oldest_channel,
        "on_oldest_channel_name": _make_save_yt_oldest_name_handler(api, profile_id),
    }
    if include_name_change_cooldown:
        kw["on_name_change_cooldown"] = _make_save_name_change_cooldown_handler(
            api, profile_id
        )
    return kw


def _run_profile_studio_upload(
    *,
    page,
    browser,
    zaliver_db_path: Path | None,
    video_path: str | None,
    title: str | None,
    description: str | None,
    publish_before_checks: bool,
    keep_studio_title: bool,
    schedule_publish_at,
    scheduled_batch=None,
    stats_server_username: str | None,
    studio_kw: dict,
):
    if scheduled_batch:
        from zaliver.youtube_upload.studio import (
            ScheduledStudioUpload,
            run_upload_scheduled_video_batch,
        )

        uploads = [
            ScheduledStudioUpload(
                video_path=item.video_path,
                title=item.title,
                description=item.description,
                schedule_publish_at=item.schedule_publish_at,
            )
            for item in scheduled_batch
        ]
        results = run_upload_scheduled_video_batch(
            page=page,
            browser=browser,
            uploads=uploads,
            publish_before_checks=publish_before_checks,
            keep_studio_title=keep_studio_title,
            stats_server_username=stats_server_username,
            **studio_kw,
        )
        last = results[-1] if results else {}
        out = dict(last)
        out["batch_results"] = results
        return out
    return run_upload_latest_ready_video(
        page=page,
        browser=browser,
        zaliver_db_path=zaliver_db_path,
        video_path=video_path,
        title=title,
        description=description,
        publish_before_checks=publish_before_checks,
        keep_studio_title=keep_studio_title,
        schedule_publish_at=schedule_publish_at,
        stats_server_username=stats_server_username,
        **studio_kw,
    )


_CDP_CONNECT_TIMEOUT_MS = 60_000
_CDP_CONNECT_ATTEMPTS = 8
_CDP_CONNECT_RETRY_POLL_S = 0.6
_CDP_REFRESH_TIMEOUT_S = 15.0


def _cdp_connect_is_conn_refused(err: Exception) -> bool:
    msg = str(err).upper()
    return "ECONNREFUSED" in msg


def _page_objects_from_connected_browser(browser):
    _log(
        "Playwright: CDP подключение успешно. "
        f"contexts={len(browser.contexts)}"
    )
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = None
    blank = None
    for pg in context.pages:
        try:
            if pg.is_closed():
                continue
        except Exception:
            continue
        try:
            url = (pg.url or "").strip().lower()
        except Exception:
            url = ""
        if "studio.youtube.com" in url:
            page = pg
            break
        if "accountscenter.instagram.com" in url:
            # Целевая страница 2FA / Accounts Center.
            page = pg
            break
        if "instagram.com" in url:
            # Предпочитаем Instagram blank/чужим вкладкам (проверка IG).
            page = pg
            continue
        if url in ("about:blank", "about:srcdoc", ""):
            if blank is None:
                blank = pg
            continue
        if page is None:
            page = pg
    if page is None:
        page = blank or (context.pages[0] if context.pages else context.new_page())
    _log(
        "Playwright: выбраны объекты. "
        f"context_pages={len(context.pages)}, page_url={page.url!r}"
    )
    return browser, context, page


def _playwright_page_from_cdp(p, endpoint_candidates: tuple[str, ...]):
    """Подключение к браузеру по CDP; возвращает (browser, context, page)."""
    browser = None
    last_err: Exception | None = None
    for endpoint in endpoint_candidates:
        if not (endpoint or "").strip():
            continue
        try:
            _log(f"Playwright: connect_over_cdp endpoint={endpoint!r}…")
            browser = p.chromium.connect_over_cdp(
                endpoint, timeout=_CDP_CONNECT_TIMEOUT_MS
            )
            last_err = None
            break
        except Exception as e:
            last_err = e
    if browser is None:
        raise DolphinAntyError(
            f"CDP connect failed for all endpoints. Last error: {last_err!r}"
        )
    return _page_objects_from_connected_browser(browser)


def _playwright_page_from_local_session_cdp(
    p,
    api,
    session_id: str,
    ws_url: str,
    *,
    connect_attempts: int = _CDP_CONNECT_ATTEMPTS,
    retry_poll_s: float = _CDP_CONNECT_RETRY_POLL_S,
) -> tuple:
    """CDP через локальный антидетект; при ECONNREFUSED — повторный опрос cdp_ws_url."""
    current_url = (ws_url or "").strip()
    if not current_url:
        raise DolphinAntyError("cdp_ws_url пуст.")
    last_err: Exception | None = None
    sid = (session_id or "").strip()
    for attempt in range(1, max(1, int(connect_attempts)) + 1):
        try:
            _log(f"Playwright: connect_over_cdp endpoint={current_url!r}…")
            browser = p.chromium.connect_over_cdp(
                current_url, timeout=_CDP_CONNECT_TIMEOUT_MS
            )
            return _page_objects_from_connected_browser(browser)
        except Exception as e:
            last_err = e
            if attempt >= connect_attempts or not _cdp_connect_is_conn_refused(e):
                break
            _log(
                "Playwright: ECONNREFUSED — повторный опрос cdp_ws_url "
                f"(попытка {attempt + 1}/{connect_attempts})…"
            )
            time.sleep(retry_poll_s)
            if not sid:
                continue
            try:
                refreshed = api.refresh_cdp_ws_url(
                    sid, timeout_s=_CDP_REFRESH_TIMEOUT_S, poll_s=retry_poll_s
                )
            except Exception as refresh_err:
                last_err = refresh_err
                break
            if refreshed != current_url:
                _log(f"Local antidetect: cdp_ws_url обновлён: {refreshed!r}")
            current_url = refreshed
    raise DolphinAntyError(
        f"CDP connect failed after {connect_attempts} attempts. Last error: {last_err!r}"
    )


@with_log_profile
def check_studio_availability_in_profile(
    profile_id: str,
    *,
    local_token: str | None = None,
    headless: bool = True,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    search_oldest_channel: bool = False,
) -> None:
    """
    Запуск профиля Dolphin → Studio → ожидание URL канала или channel-appeal.
    """
    _log(
        "Dolphin: проверка доступности Studio. "
        f"profile_id={profile_id!r}, headless={headless}"
    )
    api = DolphinAntyLocalAPI()
    try:
        tok = (local_token or "").strip()
        if tok:
            _log("Dolphin: login_with_token…")
            api.login_with_token(tok)
        _log("Dolphin: start_profile…")
        conn = api.start_profile(profile_id, headless=headless)
        _log(
            "Dolphin: профиль запущен. "
            f"ws_url={conn.ws_url()!r}, http_url={conn.http_url()!r}"
        )
        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_cdp(
                p, (conn.ws_url(), conn.http_url())
            )
            try:
                verify_studio_upload_dialog_available(
                    page,
                    profile_id=profile_id,
                    login_credentials=login_credentials,
                    yt_oldest_name=yt_oldest_name,
                    search_oldest_channel=search_oldest_channel,
                )
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        _log(f"Ошибка проверки: {type(e).__name__}: {e!r}")
        raise _wrap_exc(e) from e
    finally:
        try:
            api.stop_profile(profile_id)
        except Exception as e:
            _log(f"Dolphin: stop_profile: {e!r}")
        api.close()


@with_log_profile
def check_gmail_availability_in_profile(
    profile_id: str,
    *,
    local_token: str | None = None,
    headless: bool = True,
    login_credentials=None,
) -> None:
    """Dolphin → Gmail workspace → Войти → Google login → Inbox."""
    from zaliver.instagram_upload.gmail_availability import (
        KEEP_PROFILE_OPEN_AFTER_GMAIL_CHECK,
        verify_gmail_inbox_available,
    )

    _log(
        "Dolphin: проверка доступности Gmail. "
        f"profile_id={profile_id!r}, headless={headless}"
    )
    api = DolphinAntyLocalAPI()
    keep_open = bool(KEEP_PROFILE_OPEN_AFTER_GMAIL_CHECK)
    try:
        tok = (local_token or "").strip()
        if tok:
            _log("Dolphin: login_with_token…")
            api.login_with_token(tok)
        _log("Dolphin: start_profile…")
        conn = api.start_profile(profile_id, headless=headless)
        _log(
            "Dolphin: профиль запущен. "
            f"ws_url={conn.ws_url()!r}, http_url={conn.http_url()!r}"
        )
        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_cdp(
                p, (conn.ws_url(), conn.http_url())
            )
            try:
                verify_gmail_inbox_available(
                    page,
                    login_credentials=login_credentials,
                    profile_id=profile_id,
                )
            finally:
                if keep_open:
                    _log(
                        "Dolphin: профиль оставлен открытым для теста "
                        f"(profile_id={profile_id!r}) — Playwright отключаемся без stop."
                    )
                else:
                    try:
                        browser.close()
                    except Exception:
                        pass
    except Exception as e:
        _log(f"Ошибка проверки Gmail: {type(e).__name__}: {e!r}")
        raise _wrap_exc(e) from e
    finally:
        if keep_open:
            _log(
                "Dolphin: stop_profile пропущен (KEEP_PROFILE_OPEN_AFTER_GMAIL_CHECK)."
            )
            api.close()
        else:
            try:
                api.stop_profile(profile_id)
            except Exception as e:
                _log(f"Dolphin: stop_profile: {e!r}")
            api.close()


@with_log_profile
def check_studio_availability_in_local_antidetect_profile(
    profile_id: str,
    *,
    base_url: str,
    headless: bool = True,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    search_oldest_channel: bool = False,
    remote_cdp=None,
) -> None:
    """Локальный антидетект → Studio → ожидание URL канала или channel-appeal."""
    _log(
        "Local antidetect: проверка доступности Studio. "
        f"profile_id={profile_id!r}, base_url={base_url!r}, headless={headless}"
    )
    from zaliver.antydetect.local_antidetect_api import (
        LocalAntidetectError,
        LocalAntidetectHttpAPI,
    )
    from zaliver.antydetect.local_active_sessions import (
        register_local_session,
        unregister_local_session,
    )

    api = LocalAntidetectHttpAPI(base_url)
    session_id: str | None = None
    started_at = time.perf_counter()
    try:
        acc = api.launch_profile(
            profile_id, headless=headless, expose_cdp=True, remote_cdp=remote_cdp
        )
        sid = acc.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            raise LocalAntidetectError(f"Нет session_id в ответе launch: {acc!r}")
        session_id = sid.strip()
        bu = (base_url or "").strip() or "http://127.0.0.1:18765"
        register_local_session(profile_id=profile_id, base_url=bu, session_id=session_id)
        ws_url = api.wait_for_cdp_ws_url(session_id, timeout_s=120.0)
        _log(f"Local antidetect: cdp_ws_url={ws_url!r}")

        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_local_session_cdp(
                p, api, session_id, ws_url
            )
            try:
                studio_kw = _local_studio_workflow_kwargs(
                    api,
                    profile_id,
                    login_credentials=login_credentials,
                    yt_oldest_name=yt_oldest_name,
                    search_oldest_channel=search_oldest_channel,
                )
                verify_studio_upload_dialog_available(page, **studio_kw)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        _log(f"Ошибка проверки: {type(e).__name__}: {e!r}")
        raise LocalAntidetectError(f"Ошибка проверки доступности Studio: {e}") from e
    finally:
        if session_id:
            unregister_local_session(profile_id=profile_id)
            try:
                api.stop_session(session_id)
            except Exception:
                pass
        try:
            _log(f"Local antidetect: проверка завершена за {time.perf_counter() - started_at:.1f} с.")
        except Exception:
            pass
        api.close()


@with_log_profile
def check_gmail_availability_in_local_antidetect_profile(
    profile_id: str,
    *,
    base_url: str,
    headless: bool = True,
    login_credentials=None,
    remote_cdp=None,
) -> None:
    """Локальный антидетект → Gmail workspace → Войти → Google login → Inbox."""
    from zaliver.instagram_upload.gmail_availability import (
        KEEP_PROFILE_OPEN_AFTER_GMAIL_CHECK,
        verify_gmail_inbox_available,
    )
    from zaliver.antydetect.local_antidetect_api import (
        LocalAntidetectError,
        LocalAntidetectHttpAPI,
    )
    from zaliver.antydetect.local_active_sessions import (
        register_local_session,
        unregister_local_session,
    )

    _log(
        "Local antidetect: проверка доступности Gmail. "
        f"profile_id={profile_id!r}, base_url={base_url!r}, headless={headless}"
    )
    api = LocalAntidetectHttpAPI(base_url)
    session_id: str | None = None
    started_at = time.perf_counter()
    keep_open = bool(KEEP_PROFILE_OPEN_AFTER_GMAIL_CHECK)
    try:
        acc = api.launch_profile(
            profile_id, headless=headless, expose_cdp=True, remote_cdp=remote_cdp
        )
        sid = acc.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            raise LocalAntidetectError(f"Нет session_id в ответе launch: {acc!r}")
        session_id = sid.strip()
        bu = (base_url or "").strip() or "http://127.0.0.1:18765"
        register_local_session(profile_id=profile_id, base_url=bu, session_id=session_id)
        ws_url = api.wait_for_cdp_ws_url(session_id, timeout_s=120.0)
        _log(f"Local antidetect: cdp_ws_url={ws_url!r}")

        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_local_session_cdp(
                p, api, session_id, ws_url
            )
            try:
                verify_gmail_inbox_available(
                    page,
                    login_credentials=login_credentials,
                    profile_id=profile_id,
                )
            finally:
                if keep_open:
                    _log(
                        "Local antidetect: профиль оставлен открытым для теста "
                        f"(profile_id={profile_id!r}) — Playwright отключаемся без stop."
                    )
                else:
                    try:
                        browser.close()
                    except Exception:
                        pass
    except Exception as e:
        _log(f"Ошибка проверки Gmail: {type(e).__name__}: {e!r}")
        raise LocalAntidetectError(f"Ошибка проверки доступности Gmail: {e}") from e
    finally:
        if keep_open:
            _log(
                "Local antidetect: stop_session пропущен "
                "(KEEP_PROFILE_OPEN_AFTER_GMAIL_CHECK)."
            )
            # Сессию не unregister — профиль ещё жив; иначе UI может сбросить учёт.
        elif session_id:
            unregister_local_session(profile_id=profile_id)
            try:
                api.stop_session(session_id)
            except Exception:
                pass
        try:
            _log(
                f"Local antidetect: проверка Gmail завершена за "
                f"{time.perf_counter() - started_at:.1f} с."
            )
        except Exception:
            pass
        api.close()


def _instagram_session_creds_from_profile_dict(
    profile: dict | None,
) -> tuple[str, str, str]:
    """(login, password, twofa) из custom_data для re-login вне регистрации."""
    from zaliver.instagram_upload.instagram_availability import (
        session_login_from_custom_data,
        session_password_from_custom_data,
        session_twofa_from_custom_data,
    )

    if not isinstance(profile, dict):
        return "", "", ""
    cd = profile.get("custom_data")
    if not isinstance(cd, dict):
        return "", "", ""
    return (
        session_login_from_custom_data(cd),
        session_password_from_custom_data(cd),
        session_twofa_from_custom_data(cd),
    )


@with_log_profile
def check_instagram_availability_in_profile(
    profile_id: str,
    *,
    local_token: str | None = None,
    headless: bool = True,
    session_login: str = "",
    session_password: str = "",
    session_twofa: str = "",
    login_credentials=None,
) -> None:
    """Dolphin → instagram.com → проверка входа в аккаунт → закрытие профиля."""
    from zaliver.instagram_upload.instagram_availability import (
        verify_instagram_home_available,
    )

    _log(
        "Dolphin: проверка доступности Instagram. "
        f"profile_id={profile_id!r}, headless={headless}"
    )
    api = DolphinAntyLocalAPI()
    try:
        tok = (local_token or "").strip()
        if tok:
            _log("Dolphin: login_with_token…")
            api.login_with_token(tok)
        _log("Dolphin: start_profile…")
        conn = api.start_profile(profile_id, headless=headless)
        _log(
            "Dolphin: профиль запущен. "
            f"ws_url={conn.ws_url()!r}, http_url={conn.http_url()!r}"
        )
        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_cdp(
                p, (conn.ws_url(), conn.http_url())
            )
            try:
                verify_instagram_home_available(
                    page,
                    session_login=session_login,
                    session_password=session_password,
                    session_twofa=session_twofa,
                    profile_id=profile_id,
                    login_credentials=login_credentials,
                )
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        _log(f"Ошибка проверки Instagram: {type(e).__name__}: {e!r}")
        raise _wrap_exc(e) from e
    finally:
        try:
            api.stop_profile(profile_id)
        except Exception as e:
            _log(f"Dolphin: stop_profile: {e!r}")
        api.close()


@with_log_profile
def check_instagram_availability_in_local_antidetect_profile(
    profile_id: str,
    *,
    base_url: str,
    headless: bool = True,
    remote_cdp=None,
    session_login: str = "",
    session_password: str = "",
    session_twofa: str = "",
    login_credentials=None,
) -> None:
    """Локальный антидетект → instagram.com → проверка входа → закрытие профиля."""
    from zaliver.instagram_upload.instagram_availability import (
        verify_instagram_home_available,
    )
    from zaliver.antydetect.local_antidetect_api import (
        LocalAntidetectError,
        LocalAntidetectHttpAPI,
    )
    from zaliver.antydetect.local_active_sessions import (
        register_local_session,
        unregister_local_session,
    )
    from zaliver.youtube_upload.google_login import (
        gmail_or_yt_credentials_from_custom_data,
        has_login_credentials,
    )

    _log(
        "Local antidetect: проверка доступности Instagram. "
        f"profile_id={profile_id!r}, base_url={base_url!r}, headless={headless}"
    )
    api = LocalAntidetectHttpAPI(base_url)
    session_id: str | None = None
    started_at = time.perf_counter()
    try:
        login = (session_login or "").strip()
        pwd = (session_password or "").strip()
        twofa = (session_twofa or "").strip()
        google_creds = login_credentials
        if not pwd or not login or not has_login_credentials(google_creds):
            try:
                prof = api.get_profile(profile_id)
                loaded_login, loaded_pwd, loaded_twofa = (
                    _instagram_session_creds_from_profile_dict(prof)
                )
                if not login:
                    login = loaded_login
                if not pwd:
                    pwd = loaded_pwd
                if not twofa:
                    twofa = loaded_twofa
                if not has_login_credentials(google_creds):
                    cd = prof.get("custom_data") if isinstance(prof, dict) else None
                    google_creds = gmail_or_yt_credentials_from_custom_data(cd)
            except Exception as e:
                _log(f"Local antidetect: не удалось прочитать custom_data: {e!r}")

        # device_preset на launch: Playwright is_mobile + мобильный UA/Client Hints.
        # start_url=Instagram — первый HTML сразу под мобильный контекст.
        _log("Local antidetect: launch Instagram check с device_preset=iPhone 12 Pro")
        acc = api.launch_profile(
            profile_id,
            headless=headless,
            expose_cdp=True,
            remote_cdp=remote_cdp,
            device_preset="iPhone 12 Pro",
            start_url="https://www.instagram.com/",
        )
        sid = acc.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            raise LocalAntidetectError(f"Нет session_id в ответе launch: {acc!r}")
        session_id = sid.strip()
        bu = (base_url or "").strip() or "http://127.0.0.1:18765"
        register_local_session(profile_id=profile_id, base_url=bu, session_id=session_id)
        ws_url = api.wait_for_cdp_ws_url(session_id, timeout_s=120.0)
        _log(f"Local antidetect: cdp_ws_url={ws_url!r}")

        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_local_session_cdp(
                p, api, session_id, ws_url
            )
            try:
                # Mobile preset даёт маленький viewport; OS-окно — как у desktop.
                _try_enlarge_browser_os_window(page)
                verify_instagram_home_available(
                    page,
                    session_login=login,
                    session_password=pwd,
                    session_twofa=twofa,
                    profile_id=profile_id,
                    login_credentials=google_creds,
                )
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        _log(f"Ошибка проверки Instagram: {type(e).__name__}: {e!r}")
        raise LocalAntidetectError(
            f"Ошибка проверки доступности Instagram: {e}"
        ) from e
    finally:
        if session_id:
            unregister_local_session(profile_id=profile_id)
            try:
                api.stop_session(session_id)
            except Exception:
                pass
        try:
            _log(
                f"Local antidetect: проверка Instagram завершена за "
                f"{time.perf_counter() - started_at:.1f} с."
            )
        except Exception:
            pass
        api.close()


@with_log_profile
def warmup_instagram_reels_in_profile(
    profile_id: str,
    *,
    local_token: str | None = None,
    headless: bool = True,
    session_login: str = "",
    session_password: str = "",
    session_twofa: str = "",
    reels_count: int | None = None,
    like_probability_pct: float | None = None,
    follow_probability_pct: float | None = None,
    watch_min_s: float | None = None,
    watch_max_s: float | None = None,
    watch_full: bool = True,
    reels_recommendations: bool = True,
    search_query: str | None = None,
) -> None:
    """Dolphin → Instagram → /reels/ или keyword search → прогрев."""
    from zaliver.instagram_upload.reels_warmup import run_instagram_reels_warmup

    _log(
        "Dolphin: прогрев Instagram Reels. "
        f"profile_id={profile_id!r}, headless={headless}, "
        f"recommendations={reels_recommendations}"
    )
    api = DolphinAntyLocalAPI()
    try:
        tok = (local_token or "").strip()
        if tok:
            _log("Dolphin: login_with_token…")
            api.login_with_token(tok)
        _log("Dolphin: start_profile…")
        conn = api.start_profile(profile_id, headless=headless)
        _log(
            "Dolphin: профиль запущен. "
            f"ws_url={conn.ws_url()!r}, http_url={conn.http_url()!r}"
        )
        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_cdp(
                p, (conn.ws_url(), conn.http_url())
            )
            try:
                kw: dict = {
                    "session_login": session_login,
                    "session_password": session_password,
                    "session_twofa": session_twofa,
                    "watch_full": bool(watch_full),
                    "reels_recommendations": bool(reels_recommendations),
                    "search_query": (search_query or "").strip(),
                    "profile_id": profile_id,
                }
                if reels_count is not None:
                    kw["reels_count"] = reels_count
                if like_probability_pct is not None:
                    kw["like_probability_pct"] = like_probability_pct
                if follow_probability_pct is not None:
                    kw["follow_probability_pct"] = follow_probability_pct
                if watch_min_s is not None:
                    kw["watch_min_s"] = watch_min_s
                if watch_max_s is not None:
                    kw["watch_max_s"] = watch_max_s
                run_instagram_reels_warmup(page, **kw)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        _log(f"Ошибка прогрева Reels: {type(e).__name__}: {e!r}")
        raise _wrap_exc(e) from e
    finally:
        try:
            api.stop_profile(profile_id)
        except Exception as e:
            _log(f"Dolphin: stop_profile: {e!r}")
        api.close()


@with_log_profile
def warmup_instagram_reels_in_local_antidetect_profile(
    profile_id: str,
    *,
    base_url: str,
    headless: bool = True,
    remote_cdp=None,
    session_login: str = "",
    session_password: str = "",
    session_twofa: str = "",
    reels_count: int | None = None,
    like_probability_pct: float | None = None,
    follow_probability_pct: float | None = None,
    watch_min_s: float | None = None,
    watch_max_s: float | None = None,
    watch_full: bool = True,
    reels_recommendations: bool = True,
    search_query: str | None = None,
) -> None:
    """Локальный антидетект → Instagram → /reels/ или keyword search → прогрев."""
    from zaliver.instagram_upload.reels_warmup import run_instagram_reels_warmup
    from zaliver.antydetect.local_antidetect_api import (
        LocalAntidetectError,
        LocalAntidetectHttpAPI,
    )
    from zaliver.antydetect.local_active_sessions import (
        register_local_session,
        unregister_local_session,
    )

    _log(
        "Local antidetect: прогрев Instagram Reels. "
        f"profile_id={profile_id!r}, base_url={base_url!r}, headless={headless}"
    )
    api = LocalAntidetectHttpAPI(base_url)
    session_id: str | None = None
    started_at = time.perf_counter()
    try:
        login = (session_login or "").strip()
        pwd = (session_password or "").strip()
        twofa = (session_twofa or "").strip()
        if not pwd or not login:
            try:
                prof = api.get_profile(profile_id)
                loaded_login, loaded_pwd, loaded_twofa = (
                    _instagram_session_creds_from_profile_dict(prof)
                )
                if not login:
                    login = loaded_login
                if not pwd:
                    pwd = loaded_pwd
                if not twofa:
                    twofa = loaded_twofa
            except Exception as e:
                _log(f"Local antidetect: не удалось прочитать custom_data: {e!r}")

        acc = api.launch_profile(
            profile_id,
            headless=headless,
            expose_cdp=True,
            remote_cdp=remote_cdp,
        )
        sid = acc.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            raise LocalAntidetectError(f"Нет session_id в ответе launch: {acc!r}")
        session_id = sid.strip()
        bu = (base_url or "").strip() or "http://127.0.0.1:18765"
        register_local_session(profile_id=profile_id, base_url=bu, session_id=session_id)
        ws_url = api.wait_for_cdp_ws_url(session_id, timeout_s=120.0)
        _log(f"Local antidetect: cdp_ws_url={ws_url!r}")

        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_local_session_cdp(
                p, api, session_id, ws_url
            )
            try:
                kw: dict = {
                    "session_login": login,
                    "session_password": pwd,
                    "session_twofa": twofa,
                    "watch_full": bool(watch_full),
                    "reels_recommendations": bool(reels_recommendations),
                    "search_query": (search_query or "").strip(),
                    "profile_id": profile_id,
                }
                if reels_count is not None:
                    kw["reels_count"] = reels_count
                if like_probability_pct is not None:
                    kw["like_probability_pct"] = like_probability_pct
                if follow_probability_pct is not None:
                    kw["follow_probability_pct"] = follow_probability_pct
                if watch_min_s is not None:
                    kw["watch_min_s"] = watch_min_s
                if watch_max_s is not None:
                    kw["watch_max_s"] = watch_max_s
                run_instagram_reels_warmup(page, **kw)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        _log(f"Ошибка прогрева Reels: {type(e).__name__}: {e!r}")
        raise LocalAntidetectError(f"Ошибка прогрева Instagram Reels: {e}") from e
    finally:
        if session_id:
            unregister_local_session(profile_id=profile_id)
            try:
                api.stop_session(session_id)
            except Exception:
                pass
        try:
            _log(
                "Local antidetect: прогрев Reels завершён за "
                f"{time.perf_counter() - started_at:.1f} с."
            )
        except Exception:
            pass
        api.close()


@with_log_profile
def upload_instagram_reel_in_profile(
    profile_id: str,
    *,
    video_path: str,
    title: str = "",
    description: str = "",
    local_token: str | None = None,
    headless: bool = True,
    session_login: str = "",
    session_password: str = "",
    session_twofa: str = "",
    keep_browser_open: bool = False,
    dedicated_tab: bool = False,
    top_reels_scan: int = 1,
    tab_index: int = 0,
    tabs_per_profile: int = 1,
) -> dict:
    """Dolphin → Instagram → «Новая публикация» → файл → Share (Reels)."""
    from zaliver.instagram_upload.reels_upload import run_instagram_reels_upload

    keep_open = bool(keep_browser_open)
    use_tab = bool(dedicated_tab)
    scan_n = max(1, int(top_reels_scan or 1))
    tab_i = max(0, int(tab_index or 0))
    tabs_n = max(1, int(tabs_per_profile or 1))
    _log(
        "Dolphin: залив Instagram Reels. "
        f"profile_id={profile_id!r}, headless={headless}, "
        f"keep_browser_open={keep_open}, dedicated_tab={use_tab}, "
        f"tab={tab_i}/{tabs_n}, top_reels_scan={scan_n}, video_path={video_path!r}"
    )
    api = DolphinAntyLocalAPI()
    pw_cm = None
    try:
        tok = (local_token or "").strip()
        if tok:
            _log("Dolphin: login_with_token…")
            api.login_with_token(tok)

        if not keep_open:
            with _profile_launch_lock(profile_id):
                _log("Dolphin: start_profile…")
                conn = api.start_profile(profile_id, headless=headless)
                endpoints = (conn.ws_url(), conn.http_url())
                _log(
                    "Dolphin: профиль запущен. "
                    f"ws_url={conn.ws_url()!r}, http_url={conn.http_url()!r}"
                )
            with sync_playwright() as p:
                browser, context, page = _playwright_page_from_cdp(p, endpoints)
                if use_tab:
                    upload_page, own_page = _open_dedicated_instagram_tab(
                        context, fallback_page=page
                    )
                else:
                    upload_page = _pick_primary_instagram_page(context, page)
                    own_page = False
                try:
                    return run_instagram_reels_upload(
                        upload_page,
                        video_path=video_path,
                        title=title,
                        description=description,
                        session_login=session_login,
                        session_password=session_password,
                        session_twofa=session_twofa,
                        profile_id=profile_id,
                        top_reels_scan=scan_n,
                    )
                finally:
                    if own_page:
                        try:
                            upload_page.close()
                        except Exception:
                            pass
                    try:
                        browser.close()
                    except Exception:
                        pass

        # keep_open / multi-tab: лок только на launch+CDP connect, залив — параллельно.
        meta = _ig_meta_get(profile_id)
        if meta is None:
            meta = {"tabs_ready": threading.Event(), "preopened": False}
            _ig_meta_set(profile_id, meta)
        tabs_ready: threading.Event = meta["tabs_ready"]

        if tab_i > 0 and tabs_n > 1 and not meta.get("preopened"):
            _log(
                f"Dolphin: tab={tab_i} ждём заранее открытых вкладок "
                "(после «Новая публикация» на tab=0)…"
            )
            tabs_ready.wait(timeout=180.0)

        context = None
        with _profile_launch_lock(profile_id):
            cached = _get_dolphin_keep_open_cdp(profile_id)
            if cached:
                endpoints = cached
                _log(
                    "Dolphin: переиспользуем CDP keep-open "
                    f"profile_id={profile_id!r}, endpoints={endpoints!r}"
                )
            else:
                _log("Dolphin: start_profile…")
                conn = api.start_profile(profile_id, headless=headless)
                endpoints = (conn.ws_url(), conn.http_url())
                _log(
                    "Dolphin: профиль запущен. "
                    f"ws_url={conn.ws_url()!r}, http_url={conn.http_url()!r}"
                )
                _cache_dolphin_keep_open_cdp(profile_id, endpoints)

            pw_cm = sync_playwright()
            p = pw_cm.__enter__()
            try:
                _browser, context, seed = _playwright_page_from_cdp(p, endpoints)
            except Exception:
                try:
                    pw_cm.__exit__(None, None, None)
                except Exception:
                    pass
                pw_cm = None
                raise
            upload_page = _ig_pick_page_for_tab(
                context,
                tab_index=tab_i,
                dedicated_tab=use_tab,
                fallback_page=seed,
                tabs_per_profile=tabs_n,
                meta=meta,
            )
            if upload_page is None:
                raise DolphinAntyError(
                    f"Нет свободной вкладки для tab={tab_i} "
                    "(primary занята / лимит вкладок)."
                )
            _log(
                f"Dolphin: CDP готов tab={tab_i} — отпускаем лок, "
                "залив может идти параллельно с другими вкладками."
            )

        def _on_new_post() -> None:
            # Добираем вкладки до лимита только с tab0 (один раз).
            if tab_i == 0 and tabs_n > 1 and context is not None:
                _ig_preopen_sibling_tabs(context, tabs_n, meta)

        try:
            result = run_instagram_reels_upload(
                upload_page,
                video_path=video_path,
                title=title,
                description=description,
                session_login=session_login,
                session_password=session_password,
                session_twofa=session_twofa,
                profile_id=profile_id,
                top_reels_scan=scan_n,
                on_new_post_clicked=_on_new_post if (tab_i == 0 and tabs_n > 1) else None,
            )
            _log(
                "Dolphin: браузер оставлен открытым "
                f"(profile_id={profile_id!r}) — следующий залив без stop."
            )
            return result
        finally:
            _ig_release_page_for_tab(meta, tab_i)
            if pw_cm is not None:
                try:
                    pw_cm.__exit__(None, None, None)
                except Exception:
                    pass
                pw_cm = None
    except Exception as e:
        _log(f"Ошибка залива Reels: {type(e).__name__}: {e!r}")
        raise _wrap_exc(e) from e
    finally:
        if keep_open:
            _log(
                "Dolphin: stop_profile пропущен (keep_browser_open) "
                f"profile_id={profile_id!r}."
            )
            api.close()
        else:
            clear_dolphin_keep_open_cdp(profile_id)
            try:
                api.stop_profile(profile_id)
            except Exception as e:
                _log(f"Dolphin: stop_profile: {e!r}")
            api.close()


def _local_launch_or_reuse_instagram_session(
    api,
    *,
    profile_id: str,
    base_url: str,
    headless: bool,
    remote_cdp=None,
) -> tuple[str, str]:
    """
    launch_profile; при 409 (профиль уже запущен) — переиспользовать CDP
    или stop + повторный launch.
    """
    from zaliver.antydetect.local_antidetect_api import LocalAntidetectError
    from zaliver.antydetect.local_active_sessions import register_local_session

    bu = (base_url or "").strip() or "http://127.0.0.1:18765"

    def _register(sid: str, ws: str) -> tuple[str, str]:
        register_local_session(profile_id=profile_id, base_url=bu, session_id=sid)
        _log(f"Local antidetect: cdp_ws_url={ws!r}")
        return sid, ws

    def _from_running() -> tuple[str, str] | None:
        ws_existing, sid_existing, _msg = (
            api.resolve_running_cdp_ws_url_for_profile(profile_id)
        )
        if (
            isinstance(ws_existing, str)
            and ws_existing.strip()
            and isinstance(sid_existing, str)
            and sid_existing.strip()
        ):
            sid = sid_existing.strip()
            ws = ws_existing.strip()
            _log(
                "Local antidetect: переиспользуем уже запущенную сессию "
                f"session_id={sid!r}, cdp_ws_url={ws!r}"
            )
            return _register(sid, ws)
        return None

    try:
        acc = api.launch_profile(
            profile_id,
            headless=headless,
            expose_cdp=True,
            remote_cdp=remote_cdp,
            start_url="https://www.instagram.com/",
        )
    except LocalAntidetectError as e:
        err = str(e)
        if "409" not in err and "already running" not in err.lower():
            raise
        _log(
            "Local antidetect: launch 409 (профиль уже запущен) — "
            f"ищем живую сессию: {e!r}"
        )
        reused = _from_running()
        if reused is not None:
            return reused
        sid_stop = api.find_running_session_id_for_profile(profile_id)
        if sid_stop:
            try:
                api.stop_session(sid_stop)
                _log(
                    "Local antidetect: stop_session перед повторным launch "
                    f"session_id={sid_stop!r}"
                )
            except Exception as stop_e:
                _log(f"Local antidetect: stop_session после 409: {stop_e!r}")
            time.sleep(0.9)
        acc = api.launch_profile(
            profile_id,
            headless=headless,
            expose_cdp=True,
            remote_cdp=remote_cdp,
            start_url="https://www.instagram.com/",
        )

    sid = acc.get("session_id")
    if not isinstance(sid, str) or not sid.strip():
        raise LocalAntidetectError(f"Нет session_id в ответе launch: {acc!r}")
    session_id = sid.strip()
    ws_url = api.wait_for_cdp_ws_url(session_id, timeout_s=120.0)
    return _register(session_id, ws_url)


@with_log_profile
def upload_instagram_reel_in_local_antidetect_profile(
    profile_id: str,
    *,
    video_path: str,
    base_url: str,
    title: str = "",
    description: str = "",
    headless: bool = True,
    remote_cdp=None,
    session_login: str = "",
    session_password: str = "",
    session_twofa: str = "",
    keep_browser_open: bool = False,
    dedicated_tab: bool = False,
    top_reels_scan: int = 1,
    tab_index: int = 0,
    tabs_per_profile: int = 1,
) -> dict:
    """Локальный антидетект → Instagram → «Новая публикация» → файл → Share (Reels)."""
    from zaliver.instagram_upload.reels_upload import run_instagram_reels_upload
    from zaliver.antydetect.local_antidetect_api import (
        LocalAntidetectError,
        LocalAntidetectHttpAPI,
    )
    from zaliver.antydetect.local_active_sessions import (
        register_local_session,
        unregister_local_session,
    )

    keep_open = bool(keep_browser_open)
    use_tab = bool(dedicated_tab)
    scan_n = max(1, int(top_reels_scan or 1))
    tab_i = max(0, int(tab_index or 0))
    tabs_n = max(1, int(tabs_per_profile or 1))
    _log(
        "Local antidetect: залив Instagram Reels. "
        f"profile_id={profile_id!r}, base_url={base_url!r}, headless={headless}, "
        f"keep_browser_open={keep_open}, dedicated_tab={use_tab}, "
        f"tab={tab_i}/{tabs_n}, top_reels_scan={scan_n}, video_path={video_path!r}"
    )
    api = LocalAntidetectHttpAPI(base_url)
    session_id: str | None = None
    started_at = time.perf_counter()
    pw_cm = None
    try:
        login = (session_login or "").strip()
        pwd = (session_password or "").strip()
        twofa = (session_twofa or "").strip()
        if not pwd or not login:
            try:
                prof = api.get_profile(profile_id)
                loaded_login, loaded_pwd, loaded_twofa = (
                    _instagram_session_creds_from_profile_dict(prof)
                )
                if not login:
                    login = loaded_login
                if not pwd:
                    pwd = loaded_pwd
                if not twofa:
                    twofa = loaded_twofa
            except Exception as e:
                _log(f"Local antidetect: не удалось прочитать custom_data: {e!r}")

        bu = (base_url or "").strip() or "http://127.0.0.1:18765"

        if not keep_open:
            with _profile_launch_lock(profile_id):
                session_id, ws_url = _local_launch_or_reuse_instagram_session(
                    api,
                    profile_id=profile_id,
                    base_url=bu,
                    headless=headless,
                    remote_cdp=remote_cdp,
                )
            with sync_playwright() as p:
                browser, context, page = _playwright_page_from_local_session_cdp(
                    p, api, session_id, ws_url
                )
                if use_tab:
                    upload_page, own_page = _open_dedicated_instagram_tab(
                        context, fallback_page=page
                    )
                else:
                    upload_page = _pick_primary_instagram_page(context, page)
                    own_page = False
                try:
                    return run_instagram_reels_upload(
                        upload_page,
                        video_path=video_path,
                        title=title,
                        description=description,
                        session_login=login,
                        session_password=pwd,
                        session_twofa=twofa,
                        profile_id=profile_id,
                        top_reels_scan=scan_n,
                    )
                finally:
                    if own_page:
                        try:
                            upload_page.close()
                        except Exception:
                            pass
                    try:
                        browser.close()
                    except Exception:
                        pass

        meta = _ig_meta_get(profile_id)
        if meta is None:
            meta = {
                "tabs_ready": threading.Event(),
                "preopened": False,
                "session_id": None,
                "ws_url": None,
            }
            _ig_meta_set(profile_id, meta)
        tabs_ready: threading.Event = meta["tabs_ready"]

        if tab_i > 0 and tabs_n > 1 and not meta.get("preopened"):
            _log(
                f"Local antidetect: tab={tab_i} ждём заранее открытых вкладок "
                "(после «Новая публикация» на tab=0)…"
            )
            tabs_ready.wait(timeout=180.0)

        context = None
        with _profile_launch_lock(profile_id):
            def _bind_running_or_launch() -> tuple[str, str]:
                ws_existing, sid_existing, _msg = (
                    api.resolve_running_cdp_ws_url_for_profile(profile_id)
                )
                if (
                    isinstance(ws_existing, str)
                    and ws_existing.strip()
                    and isinstance(sid_existing, str)
                    and sid_existing.strip()
                ):
                    sid = sid_existing.strip()
                    ws = ws_existing.strip()
                    _log(
                        "Local antidetect: переиспользуем уже запущенную сессию "
                        f"session_id={sid!r}, cdp_ws_url={ws!r}"
                    )
                    return sid, ws
                sid, ws = _local_launch_or_reuse_instagram_session(
                    api,
                    profile_id=profile_id,
                    base_url=bu,
                    headless=headless,
                    remote_cdp=remote_cdp,
                )
                return sid, ws

            def _clear_dead_keep_open_session(dead_sid: str | None) -> None:
                meta["session_id"] = None
                meta["ws_url"] = None
                meta["preopened"] = False
                unregister_local_session(profile_id=profile_id)
                sid_stop = (dead_sid or "").strip()
                if not sid_stop:
                    try:
                        sid_stop = (
                            api.find_running_session_id_for_profile(profile_id) or ""
                        ).strip()
                    except Exception:
                        sid_stop = ""
                if sid_stop:
                    try:
                        api.stop_session(sid_stop)
                        _log(
                            "Local antidetect: stop_session мёртвого CDP "
                            f"session_id={sid_stop!r}"
                        )
                    except Exception as stop_e:
                        _log(
                            "Local antidetect: stop_session мёртвого CDP "
                            f"не удался: {stop_e!r}"
                        )
                    time.sleep(0.9)

            ws_url = (meta.get("ws_url") or "").strip()
            session_id = (meta.get("session_id") or "").strip() or None
            if not ws_url or not session_id:
                session_id, ws_url = _bind_running_or_launch()
                register_local_session(
                    profile_id=profile_id, base_url=bu, session_id=session_id
                )
                meta["session_id"] = session_id
                meta["ws_url"] = ws_url
            else:
                register_local_session(
                    profile_id=profile_id, base_url=bu, session_id=session_id
                )

            pw_cm = sync_playwright()
            p = pw_cm.__enter__()
            try:
                try:
                    _browser, context, seed = _playwright_page_from_local_session_cdp(
                        p, api, session_id, ws_url
                    )
                except Exception as cdp_err:
                    # keep-open после внешнего stop / гонки: meta или API ещё
                    # держат мёртвый ws — иначе профиль крутит ECONNREFUSED.
                    _log(
                        "Local antidetect: CDP keep-open недоступен "
                        f"({type(cdp_err).__name__}: {cdp_err!r}) — relaunch…"
                    )
                    try:
                        pw_cm.__exit__(None, None, None)
                    except Exception:
                        pass
                    pw_cm = None
                    _clear_dead_keep_open_session(session_id)
                    session_id, ws_url = _local_launch_or_reuse_instagram_session(
                        api,
                        profile_id=profile_id,
                        base_url=bu,
                        headless=headless,
                        remote_cdp=remote_cdp,
                    )
                    register_local_session(
                        profile_id=profile_id, base_url=bu, session_id=session_id
                    )
                    meta["session_id"] = session_id
                    meta["ws_url"] = ws_url
                    pw_cm = sync_playwright()
                    p = pw_cm.__enter__()
                    try:
                        _browser, context, seed = (
                            _playwright_page_from_local_session_cdp(
                                p, api, session_id, ws_url
                            )
                        )
                    except Exception:
                        try:
                            pw_cm.__exit__(None, None, None)
                        except Exception:
                            pass
                        pw_cm = None
                        meta["session_id"] = None
                        meta["ws_url"] = None
                        raise
            except Exception:
                if pw_cm is not None:
                    try:
                        pw_cm.__exit__(None, None, None)
                    except Exception:
                        pass
                    pw_cm = None
                raise
            upload_page = _ig_pick_page_for_tab(
                context,
                tab_index=tab_i,
                dedicated_tab=use_tab,
                fallback_page=seed,
                tabs_per_profile=tabs_n,
                meta=meta,
            )
            if upload_page is None:
                raise LocalAntidetectError(
                    f"Нет свободной вкладки для tab={tab_i} "
                    "(primary занята / лимит вкладок)."
                )
            _log(
                f"Local antidetect: CDP готов tab={tab_i} — отпускаем лок, "
                "залив может идти параллельно с другими вкладками."
            )

        def _on_new_post() -> None:
            # Добираем вкладки до лимита только с tab0 (один раз).
            if tab_i == 0 and tabs_n > 1 and context is not None:
                _ig_preopen_sibling_tabs(context, tabs_n, meta)

        try:
            result = run_instagram_reels_upload(
                upload_page,
                video_path=video_path,
                title=title,
                description=description,
                session_login=login,
                session_password=pwd,
                session_twofa=twofa,
                profile_id=profile_id,
                top_reels_scan=scan_n,
                on_new_post_clicked=_on_new_post if (tab_i == 0 and tabs_n > 1) else None,
            )
            _log(
                "Local antidetect: браузер оставлен открытым "
                f"(profile_id={profile_id!r}) — следующий залив без stop."
            )
            return result
        finally:
            _ig_release_page_for_tab(meta, tab_i)
            if pw_cm is not None:
                try:
                    pw_cm.__exit__(None, None, None)
                except Exception:
                    pass
                pw_cm = None
    except Exception as e:
        _log(f"Ошибка залива Reels: {type(e).__name__}: {e!r}")
        raise LocalAntidetectError(f"Ошибка залива Instagram Reels: {e}") from e
    finally:
        if keep_open:
            _log(
                "Local antidetect: stop_session пропущен (keep_browser_open) "
                f"profile_id={profile_id!r}."
            )
        elif session_id:
            unregister_local_session(profile_id=profile_id)
            try:
                api.stop_session(session_id)
            except Exception:
                pass
        try:
            _log(
                "Local antidetect: залив Reels завершён за "
                f"{time.perf_counter() - started_at:.1f} с."
            )
        except Exception:
            pass
        api.close()


def _instagram_sessionid_from_page(page) -> str:
    """Cookie sessionid с instagram.com (пусто, если нет рабочей сессии)."""
    from zaliver.instagram_upload.instagrapi_session import normalize_instagram_sessionid

    cookies = []
    try:
        try:
            cookies = page.context.cookies(
                ["https://www.instagram.com", "https://instagram.com"]
            )
        except TypeError:
            cookies = page.context.cookies()
        except Exception:
            cookies = page.context.cookies()
    except Exception:
        cookies = []
    if not cookies:
        try:
            cookies = page.context.cookies()
        except Exception:
            cookies = []

    best = ""
    for c in cookies or []:
        if not isinstance(c, dict):
            continue
        if (c.get("name") or "").strip().lower() != "sessionid":
            continue
        domain = (c.get("domain") or "").lower()
        if domain and "instagram" not in domain:
            continue
        val = normalize_instagram_sessionid(c.get("value") or "")
        if val and val not in ("0", '""', "null") and len(val) > len(best):
            best = val
    return best


def _page_url_looks_like_instagram_home(url: str) -> bool:
    u = (url or "").strip().lower()
    if not u:
        return False
    if "instagram.com" not in u:
        return False
    # Google OAuth / login walls — не считаем «уже на Instagram».
    bad = (
        "accounts.google.com",
        "/accounts/login",
        "/accounts/emailsignup",
        "/challenge/",
        "/accounts/suspended",
        "flowName=GlifWebSignIn",
    )
    return not any(b.lower() in u for b in bad)


def _ensure_instagram_page_for_cookies(page) -> None:
    """
    Сначала открыть Instagram, потом читать sessionid.

    Иначе вкладка может висеть на Google chooser / blank, а старый
    cookie sessionid всё равно найдётся и чекер возьмёт мёртвую сессию.
    Не ждём networkidle — только commit + короткая пауза.
    """
    from zaliver.instagram_upload.register import INSTAGRAM_URL

    # После launch вкладка часто about:blank — короткая пауза.
    try:
        for _ in range(20):
            try:
                url = (page.url or "").lower()
            except Exception:
                url = ""
            if url and url not in ("about:blank", "about:srcdoc", ""):
                break
            try:
                page.wait_for_timeout(250)
            except Exception:
                time.sleep(0.25)
    except Exception:
        pass

    try:
        cur = (page.url or "").strip()
    except Exception:
        cur = ""
    if _page_url_looks_like_instagram_home(cur):
        _log(f"Instagram cookies: уже на Instagram ({cur[:120]}), goto не нужен.")
    else:
        _log(
            "Instagram cookies: короткий goto instagram.com "
            f"(было: {(cur or '—')[:160]})…"
        )
        try:
            # commit = первый байт ответа, не ждём DOM/networkidle (часто зависает).
            page.goto(INSTAGRAM_URL, wait_until="commit", timeout=25_000)
        except Exception as e:
            _log(f"Instagram cookies: goto commit failed: {e!r}")
            try:
                page.goto(INSTAGRAM_URL, wait_until="domcontentloaded", timeout=20_000)
            except Exception as e2:
                _log(f"Instagram cookies: goto domcontentloaded failed: {e2!r}")
        try:
            page.wait_for_timeout(1500)
        except Exception:
            time.sleep(1.5)

    try:
        after = (page.url or "").strip()
    except Exception:
        after = ""
    if after:
        _log(f"Instagram cookies: URL после перехода: {after[:180]}")
    if not _page_url_looks_like_instagram_home(after):
        _log(
            "Instagram cookies: после goto всё ещё не лента Instagram "
            "(login/Google/challenge) — sessionid может быть мёртвым."
        )


@with_log_profile
def extract_instagram_sessionid_from_profile(
    profile_id: str,
    *,
    local_token: str | None = None,
    headless: bool = True,
) -> str:
    """Dolphin: sessionid Instagram из cookies профиля."""
    # Для чекера всегда headless: видимое окно часто зависает на blank.
    use_headless = True
    _log(
        "Dolphin: извлечение Instagram sessionid. "
        f"profile_id={profile_id!r}, headless={use_headless} "
        f"(requested={headless})"
    )
    api = DolphinAntyLocalAPI()
    try:
        tok = (local_token or "").strip()
        if tok:
            api.login_with_token(tok)
        conn = api.start_profile(profile_id, headless=use_headless)
        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_cdp(
                p, (conn.ws_url(), conn.http_url())
            )
            try:
                _ensure_instagram_page_for_cookies(page)
                sid = _instagram_sessionid_from_page(page)
                if not sid:
                    raise DolphinAntyError(
                        "В профиле нет cookie sessionid Instagram "
                        "(войдите в аккаунт в этом профиле)."
                    )
                _log("Dolphin: Instagram sessionid получен.")
                return sid
            finally:
                _close_playwright_browser(browser)
    except Exception as e:
        _log(f"Ошибка извлечения sessionid: {type(e).__name__}: {e!r}")
        raise _wrap_exc(e) from e
    finally:
        try:
            api.stop_profile(profile_id)
        except Exception as e:
            _log(f"Dolphin: stop_profile: {e!r}")
        api.close()


@with_log_profile
def extract_instagram_sessionid_from_local_antidetect_profile(
    profile_id: str,
    *,
    base_url: str,
    headless: bool = True,
    remote_cdp=None,
) -> str:
    """Локальный/удалённый антидетект: sessionid Instagram из cookies профиля."""
    from zaliver.antydetect.local_antidetect_api import (
        LocalAntidetectError,
        LocalAntidetectHttpAPI,
    )
    from zaliver.antydetect.local_active_sessions import (
        register_local_session,
        unregister_local_session,
    )

    use_headless = True
    _log(
        "Local antidetect: извлечение Instagram sessionid. "
        f"profile_id={profile_id!r}, base_url={base_url!r}, "
        f"headless={use_headless} (requested={headless})"
    )
    api = LocalAntidetectHttpAPI(base_url)
    session_id: str | None = None
    try:
        acc = api.launch_profile(
            profile_id,
            headless=use_headless,
            expose_cdp=True,
            remote_cdp=remote_cdp,
        )
        sid = acc.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            raise LocalAntidetectError(f"Нет session_id в ответе launch: {acc!r}")
        session_id = sid.strip()
        bu = (base_url or "").strip() or "http://127.0.0.1:18765"
        register_local_session(profile_id=profile_id, base_url=bu, session_id=session_id)
        ws_url = api.wait_for_cdp_ws_url(session_id, timeout_s=120.0)
        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_local_session_cdp(
                p, api, session_id, ws_url
            )
            try:
                _ensure_instagram_page_for_cookies(page)
                cookie_sid = _instagram_sessionid_from_page(page)
                if not cookie_sid:
                    raise LocalAntidetectError(
                        "В профиле нет cookie sessionid Instagram "
                        "(войдите в аккаунт в этом профиле)."
                    )
                _log("Local antidetect: Instagram sessionid получен.")
                return cookie_sid
            finally:
                _close_playwright_browser(browser)
    except Exception as e:
        _log(f"Ошибка извлечения sessionid: {type(e).__name__}: {e!r}")
        if isinstance(e, LocalAntidetectError):
            raise
        raise LocalAntidetectError(f"Ошибка извлечения Instagram sessionid: {e}") from e
    finally:
        if session_id:
            unregister_local_session(profile_id=profile_id)
            try:
                api.stop_session(session_id)
            except Exception:
                pass
        api.close()


@with_log_profile
def register_instagram_account_in_profile(
    profile_id: str,
    *,
    local_token: str | None = None,
    headless: bool = True,
    login_credentials=None,
    on_manual_captcha=None,
) -> None:
    """Dolphin → Gmail inbox → Instagram signup → капча → код из почты."""
    from zaliver.instagram_upload.gmail_availability import verify_gmail_inbox_available
    from zaliver.instagram_upload.register import (
        KEEP_PROFILE_OPEN_AFTER_IG_REGISTER,
        InstagramRegistrationFailedError,
        InstagramSmsCaptchaError,
        run_instagram_registration_after_gmail,
    )

    _log(
        "Dolphin: регистрация Instagram. "
        f"profile_id={profile_id!r}, headless={headless}"
    )
    api = DolphinAntyLocalAPI()
    keep_open_on_error = bool(KEEP_PROFILE_OPEN_AFTER_IG_REGISTER)
    succeeded = False
    force_close = False
    try:
        tok = (local_token or "").strip()
        if tok:
            _log("Dolphin: login_with_token…")
            api.login_with_token(tok)

        _log("Dolphin: start_profile…")
        conn = api.start_profile(profile_id, headless=headless)
        _log(
            "Dolphin: профиль запущен. "
            f"ws_url={conn.ws_url()!r}, http_url={conn.http_url()!r}"
        )
        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_cdp(
                p, (conn.ws_url(), conn.http_url())
            )
            try:
                try:
                    verify_gmail_inbox_available(
                        page,
                        login_credentials=login_credentials,
                        profile_id=profile_id,
                    )
                    username = run_instagram_registration_after_gmail(
                        page,
                        login_credentials,
                        on_manual_captcha=on_manual_captcha,
                        profile_id=profile_id,
                    )
                    succeeded = True
                    _log(f"Dolphin: Instagram зарегистрирован, username={username!r}")
                except (InstagramSmsCaptchaError, InstagramRegistrationFailedError):
                    force_close = True
                    raise
            finally:
                # Успех / известная ошибка регистрации → закрыть.
                # Иная ошибка → оставить для ручной капчи.
                if succeeded or force_close or not keep_open_on_error:
                    try:
                        browser.close()
                    except Exception:
                        pass
                else:
                    _log(
                        "Dolphin: профиль оставлен открытым после ошибки "
                        f"(profile_id={profile_id!r})."
                    )
    except Exception as e:
        if not force_close and (
            isinstance(e, (InstagramSmsCaptchaError, InstagramRegistrationFailedError))
            or InstagramSmsCaptchaError.matches(str(e))
            or InstagramRegistrationFailedError.matches(str(e))
        ):
            force_close = True
        if force_close:
            _log(
                "Dolphin: известная ошибка регистрации — закрываем профиль "
                f"(profile_id={profile_id!r})."
            )
        _log(f"Ошибка регистрации Instagram: {type(e).__name__}: {e!r}")
        raise _wrap_exc(e) from e
    finally:
        if succeeded or force_close or not keep_open_on_error:
            try:
                api.stop_profile(profile_id)
                if succeeded:
                    _log(
                        "Dolphin: профиль закрыт после успешной регистрации "
                        f"(profile_id={profile_id!r})."
                    )
                elif force_close:
                    _log(
                        "Dolphin: профиль закрыт после ошибки регистрации "
                        f"(profile_id={profile_id!r})."
                    )
            except Exception as e:
                _log(f"Dolphin: stop_profile: {e!r}")
            api.close()
        else:
            _log(
                "Dolphin: stop_profile пропущен после ошибки "
                "(KEEP_PROFILE_OPEN_AFTER_IG_REGISTER)."
            )
            api.close()


@with_log_profile
def register_instagram_account_in_local_antidetect_profile(
    profile_id: str,
    *,
    base_url: str,
    headless: bool = True,
    login_credentials=None,
    remote_cdp=None,
    on_manual_captcha=None,
) -> None:
    """Локальный антидетект → Gmail → Instagram signup → капча → код из почты."""
    from zaliver.instagram_upload.gmail_availability import verify_gmail_inbox_available
    from zaliver.instagram_upload.register import (
        KEEP_PROFILE_OPEN_AFTER_IG_REGISTER,
        InstagramRegistrationFailedError,
        InstagramSmsCaptchaError,
        run_instagram_registration_after_gmail,
    )
    from zaliver.antydetect.local_antidetect_api import (
        LocalAntidetectError,
        LocalAntidetectHttpAPI,
    )
    from zaliver.antydetect.local_active_sessions import (
        register_local_session,
        unregister_local_session,
    )

    _log(
        "Local antidetect: регистрация Instagram. "
        f"profile_id={profile_id!r}, base_url={base_url!r}, headless={headless}"
    )
    api = LocalAntidetectHttpAPI(base_url)
    session_id: str | None = None
    started_at = time.perf_counter()
    keep_open_on_error = bool(KEEP_PROFILE_OPEN_AFTER_IG_REGISTER)
    succeeded = False
    force_close = False
    try:
        acc = api.launch_profile(
            profile_id, headless=headless, expose_cdp=True, remote_cdp=remote_cdp
        )
        sid = acc.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            raise LocalAntidetectError(f"Нет session_id в ответе launch: {acc!r}")
        session_id = sid.strip()
        bu = (base_url or "").strip() or "http://127.0.0.1:18765"
        register_local_session(profile_id=profile_id, base_url=bu, session_id=session_id)
        ws_url = api.wait_for_cdp_ws_url(session_id, timeout_s=120.0)
        _log(f"Local antidetect: cdp_ws_url={ws_url!r}")

        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_local_session_cdp(
                p, api, session_id, ws_url
            )
            try:
                try:
                    verify_gmail_inbox_available(
                        page,
                        login_credentials=login_credentials,
                        profile_id=profile_id,
                    )
                    username = run_instagram_registration_after_gmail(
                        page,
                        login_credentials,
                        on_manual_captcha=on_manual_captcha,
                        profile_id=profile_id,
                    )
                    succeeded = True
                    _save_instagram_credentials_to_profile(
                        api, profile_id, login_credentials
                    )
                    _log(
                        "Local antidetect: Instagram зарегистрирован, "
                        f"username={username!r}"
                    )
                except (InstagramSmsCaptchaError, InstagramRegistrationFailedError):
                    force_close = True
                    raise
            finally:
                # Успех / известная ошибка регистрации → закрыть.
                if succeeded or force_close or not keep_open_on_error:
                    try:
                        browser.close()
                    except Exception:
                        pass
                else:
                    _log(
                        "Local antidetect: профиль оставлен открытым после ошибки "
                        f"(profile_id={profile_id!r})."
                    )
    except Exception as e:
        if not force_close and (
            isinstance(e, (InstagramSmsCaptchaError, InstagramRegistrationFailedError))
            or InstagramSmsCaptchaError.matches(str(e))
            or InstagramRegistrationFailedError.matches(str(e))
        ):
            force_close = True
        if force_close:
            _log(
                "Local antidetect: известная ошибка регистрации — закрываем профиль "
                f"(profile_id={profile_id!r})."
            )
        _log(f"Ошибка регистрации Instagram: {type(e).__name__}: {e!r}")
        raise LocalAntidetectError(f"Ошибка регистрации Instagram: {e}") from e
    finally:
        if succeeded or force_close or not keep_open_on_error:
            if session_id:
                unregister_local_session(profile_id=profile_id)
                try:
                    api.stop_session(session_id)
                    if succeeded:
                        _log(
                            "Local antidetect: профиль закрыт после успешной "
                            f"регистрации (profile_id={profile_id!r})."
                        )
                    elif force_close:
                        _log(
                            "Local antidetect: профиль закрыт после ошибки "
                            f"регистрации (profile_id={profile_id!r})."
                        )
                except Exception:
                    pass
        else:
            _log(
                "Local antidetect: stop_session пропущен после ошибки "
                "(KEEP_PROFILE_OPEN_AFTER_IG_REGISTER)."
            )
        try:
            _log(
                f"Local antidetect: регистрация Instagram завершена за "
                f"{time.perf_counter() - started_at:.1f} с."
            )
        except Exception:
            pass
        api.close()


def _save_instagram_2fa_to_profile(api, profile_id: str, secret: str) -> None:
    """Сохранить inst_2fa в custom_data профиля."""
    from zaliver.core.profiles.account_data import INST_2FA_KEY

    s = (secret or "").strip()
    if not s:
        return
    try:
        api.merge_profile_custom_data(profile_id, {INST_2FA_KEY: s})
        _log(
            "Local antidetect: в custom_data сохранён "
            f"{INST_2FA_KEY} (len={len(s)}) для profile_id={profile_id!r}."
        )
    except Exception as e:
        _log(
            "Local antidetect: не удалось сохранить "
            f"{INST_2FA_KEY} для profile_id={profile_id!r}: {e!r}"
        )
        raise


@with_log_profile
def setup_instagram_2fa_in_profile(
    profile_id: str,
    *,
    local_token: str | None = None,
    headless: bool = True,
    login_credentials=None,
    session_login: str = "",
    session_password: str = "",
    session_twofa: str = "",
    keep_open_on_error: bool | None = None,
) -> str:
    """Dolphin → Accounts Center → подключить TOTP 2FA → вернуть секрет."""
    from zaliver.instagram_upload.setup_2fa import (
        KEEP_PROFILE_OPEN_AFTER_IG_2FA,
        setup_instagram_totp_2fa,
    )

    _log(
        "Dolphin: подключение 2FA Instagram. "
        f"profile_id={profile_id!r}, headless={headless}"
    )
    api = DolphinAntyLocalAPI()
    if keep_open_on_error is None:
        keep_open_on_error = bool(KEEP_PROFILE_OPEN_AFTER_IG_2FA)
    else:
        keep_open_on_error = bool(keep_open_on_error)
    succeeded = False
    secret = ""
    try:
        tok = (local_token or "").strip()
        if tok:
            _log("Dolphin: login_with_token…")
            api.login_with_token(tok)
        _log("Dolphin: start_profile…")
        conn = api.start_profile(profile_id, headless=headless)
        _log(
            "Dolphin: профиль запущен. "
            f"ws_url={conn.ws_url()!r}, http_url={conn.http_url()!r}"
        )
        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_cdp(
                p, (conn.ws_url(), conn.http_url())
            )
            try:
                secret = setup_instagram_totp_2fa(
                    page,
                    login_credentials=login_credentials,
                    session_login=session_login,
                    session_password=session_password,
                    session_twofa=session_twofa,
                    max_seconds=300.0,
                    profile_id=profile_id,
                )
                succeeded = True
                _log(
                    f"Dolphin: 2FA Instagram подключена (secret_len={len(secret)})."
                )
            finally:
                if succeeded or not keep_open_on_error:
                    try:
                        browser.close()
                    except Exception:
                        pass
                else:
                    _log(
                        "Dolphin: профиль оставлен открытым после ошибки 2FA "
                        f"(profile_id={profile_id!r})."
                    )
    except Exception as e:
        _log(f"Ошибка подключения 2FA Instagram: {type(e).__name__}: {e!r}")
        raise _wrap_exc(e) from e
    finally:
        if succeeded or not keep_open_on_error:
            try:
                api.stop_profile(profile_id)
            except Exception as e:
                _log(f"Dolphin: stop_profile: {e!r}")
            api.close()
        else:
            _log(
                "Dolphin: stop_profile пропущен после ошибки "
                "(keep_open_on_error)."
            )
            api.close()
    return secret


@with_log_profile
def setup_instagram_2fa_in_local_antidetect_profile(
    profile_id: str,
    *,
    base_url: str,
    headless: bool = True,
    remote_cdp=None,
    login_credentials=None,
    session_login: str = "",
    session_password: str = "",
    session_twofa: str = "",
    keep_open_on_error: bool | None = None,
) -> str:
    """Локальный антидетект → Accounts Center → TOTP 2FA → сохранить inst_2fa."""
    from zaliver.instagram_upload.setup_2fa import (
        KEEP_PROFILE_OPEN_AFTER_IG_2FA,
        setup_instagram_totp_2fa,
    )
    from zaliver.antydetect.local_antidetect_api import (
        LocalAntidetectError,
        LocalAntidetectHttpAPI,
    )
    from zaliver.antydetect.local_active_sessions import (
        register_local_session,
        unregister_local_session,
    )

    _log(
        "Local antidetect: подключение 2FA Instagram. "
        f"profile_id={profile_id!r}, base_url={base_url!r}, headless={headless}"
    )
    api = LocalAntidetectHttpAPI(base_url)
    session_id: str | None = None
    started_at = time.perf_counter()
    if keep_open_on_error is None:
        keep_open_on_error = bool(KEEP_PROFILE_OPEN_AFTER_IG_2FA)
    else:
        keep_open_on_error = bool(keep_open_on_error)
    succeeded = False
    secret = ""
    try:
        login = (session_login or "").strip()
        pwd = (session_password or "").strip()
        twofa = (session_twofa or "").strip()
        if not pwd or not login:
            try:
                prof = api.get_profile(profile_id)
                loaded_login, loaded_pwd, loaded_twofa = (
                    _instagram_session_creds_from_profile_dict(prof)
                )
                if not login:
                    login = loaded_login
                if not pwd:
                    pwd = loaded_pwd
                if not twofa:
                    twofa = loaded_twofa
            except Exception as e:
                _log(f"Local antidetect: не удалось прочитать custom_data: {e!r}")

        acc = api.launch_profile(
            profile_id,
            headless=headless,
            expose_cdp=True,
            remote_cdp=remote_cdp,
        )
        sid = acc.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            raise LocalAntidetectError(f"Нет session_id в ответе launch: {acc!r}")
        session_id = sid.strip()
        bu = (base_url or "").strip() or "http://127.0.0.1:18765"
        register_local_session(profile_id=profile_id, base_url=bu, session_id=session_id)
        ws_url = api.wait_for_cdp_ws_url(session_id, timeout_s=120.0)
        _log(f"Local antidetect: cdp_ws_url={ws_url!r}")

        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_local_session_cdp(
                p, api, session_id, ws_url
            )
            try:
                def _on_secret(s: str) -> None:
                    _save_instagram_2fa_to_profile(api, profile_id, s)

                secret = setup_instagram_totp_2fa(
                    page,
                    on_secret=_on_secret,
                    login_credentials=login_credentials,
                    session_login=login,
                    session_password=pwd,
                    session_twofa=twofa,
                    max_seconds=300.0,
                    profile_id=profile_id,
                )
                succeeded = True
                _log(
                    "Local antidetect: 2FA Instagram подключена "
                    f"(secret_len={len(secret)})."
                )
            finally:
                if succeeded or not keep_open_on_error:
                    try:
                        browser.close()
                    except Exception:
                        pass
                else:
                    _log(
                        "Local antidetect: профиль оставлен открытым после ошибки 2FA "
                        f"(profile_id={profile_id!r})."
                    )
    except Exception as e:
        _log(f"Ошибка подключения 2FA Instagram: {type(e).__name__}: {e!r}")
        raise LocalAntidetectError(f"Ошибка подключения 2FA Instagram: {e}") from e
    finally:
        if succeeded or not keep_open_on_error:
            if session_id:
                unregister_local_session(profile_id=profile_id)
                try:
                    api.stop_session(session_id)
                    if succeeded:
                        _log(
                            "Local antidetect: профиль закрыт после успешного "
                            f"подключения 2FA (profile_id={profile_id!r})."
                        )
                except Exception:
                    pass
        else:
            _log(
                "Local antidetect: stop_session пропущен после ошибки "
                "(keep_open_on_error)."
            )
        try:
            _log(
                f"Local antidetect: подключение 2FA Instagram завершено за "
                f"{time.perf_counter() - started_at:.1f} с."
            )
        except Exception:
            pass
        api.close()
    return secret


@with_log_profile
def fill_channel_description_and_link_in_profile(
    profile_id: str,
    *,
    description: str | None = None,
    link_title: str | None = None,
    link_url: str | None = None,
    local_token: str | None = None,
    headless: bool = True,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    search_oldest_channel: bool = True,
) -> None:
    """Dolphin → Studio → «Настройка канала» → описание и ссылка."""
    _log(
        "Dolphin: заполнение описания/ссылки канала. "
        f"profile_id={profile_id!r}, headless={headless}"
    )
    api = DolphinAntyLocalAPI()
    try:
        tok = (local_token or "").strip()
        if tok:
            _log("Dolphin: login_with_token…")
            api.login_with_token(tok)
        _log("Dolphin: start_profile…")
        conn = api.start_profile(profile_id, headless=headless)
        _log(
            "Dolphin: профиль запущен. "
            f"ws_url={conn.ws_url()!r}, http_url={conn.http_url()!r}"
        )
        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_cdp(
                p, (conn.ws_url(), conn.http_url())
            )
            try:
                run_studio_channel_description_and_link(
                    page,
                    profile_id=profile_id,
                    description=description,
                    link_title=link_title,
                    link_url=link_url,
                    login_credentials=login_credentials,
                    yt_oldest_name=yt_oldest_name,
                    search_oldest_channel=search_oldest_channel,
                )
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        _log(f"Ошибка заполнения канала: {type(e).__name__}: {e!r}")
        raise _wrap_exc(e) from e
    finally:
        try:
            api.stop_profile(profile_id)
        except Exception as e:
            _log(f"Dolphin: stop_profile: {e!r}")
        api.close()


@with_log_profile
def fill_channel_description_and_link_in_local_antidetect_profile(
    profile_id: str,
    *,
    description: str | None = None,
    link_title: str | None = None,
    link_url: str | None = None,
    base_url: str,
    headless: bool = True,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    search_oldest_channel: bool = True,
    remote_cdp=None,
) -> None:
    """Локальный антидетект → Studio → «Настройка канала» → описание и ссылка."""
    _log(
        "Local antidetect: заполнение описания/ссылки канала. "
        f"profile_id={profile_id!r}, base_url={base_url!r}, headless={headless}"
    )
    from zaliver.antydetect.local_antidetect_api import (
        LocalAntidetectError,
        LocalAntidetectHttpAPI,
    )
    from zaliver.antydetect.local_active_sessions import (
        register_local_session,
        unregister_local_session,
    )

    api = LocalAntidetectHttpAPI(base_url)
    session_id: str | None = None
    started_at = time.perf_counter()
    try:
        acc = api.launch_profile(
            profile_id, headless=headless, expose_cdp=True, remote_cdp=remote_cdp
        )
        sid = acc.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            raise LocalAntidetectError(f"Нет session_id в ответе launch: {acc!r}")
        session_id = sid.strip()
        bu = (base_url or "").strip() or "http://127.0.0.1:18765"
        register_local_session(profile_id=profile_id, base_url=bu, session_id=session_id)
        ws_url = api.wait_for_cdp_ws_url(session_id, timeout_s=120.0)
        _log(f"Local antidetect: cdp_ws_url={ws_url!r}")
        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_local_session_cdp(
                p, api, session_id, ws_url
            )
            try:
                studio_kw = _local_studio_workflow_kwargs(
                    api,
                    profile_id,
                    login_credentials=login_credentials,
                    yt_oldest_name=yt_oldest_name,
                    search_oldest_channel=search_oldest_channel,
                )
                run_studio_channel_description_and_link(
                    page,
                    description=description,
                    link_title=link_title,
                    link_url=link_url,
                    **studio_kw,
                )
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        _log(f"Ошибка заполнения канала: {type(e).__name__}: {e!r}")
        raise LocalAntidetectError(
            f"Ошибка заполнения описания/ссылки канала: {e}"
        ) from e
    finally:
        if session_id:
            unregister_local_session(profile_id=profile_id)
            try:
                api.stop_session(session_id)
            except Exception:
                pass
        try:
            _log(
                "Local antidetect: заполнение канала завершено за "
                f"{time.perf_counter() - started_at:.1f} с."
            )
        except Exception:
            pass
        api.close()


@with_log_profile
def upload_channel_avatar_in_profile(
    profile_id: str,
    *,
    avatar_path: str | Path | None = None,
    channel_name: str | None = None,
    skip_name_change: bool = False,
    local_token: str | None = None,
    headless: bool = True,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    search_oldest_channel: bool = True,
) -> None:
    """Dolphin → Studio → «Настройка канала» → аватарка и/или название."""
    has_avatar = bool(avatar_path)
    has_name = bool((channel_name or "").strip()) and not skip_name_change
    if not has_avatar and not has_name:
        raise DolphinAntyError("Не заданы ни аватарка, ни название канала.")
    _log(
        "Dolphin: настройка канала (аватарка/название). "
        f"profile_id={profile_id!r}, headless={headless}, "
        f"avatar={has_avatar}, name={has_name}"
    )
    api = DolphinAntyLocalAPI()
    try:
        tok = (local_token or "").strip()
        if tok:
            _log("Dolphin: login_with_token…")
            api.login_with_token(tok)
        _log("Dolphin: start_profile…")
        conn = api.start_profile(profile_id, headless=headless)
        _log(
            "Dolphin: профиль запущен. "
            f"ws_url={conn.ws_url()!r}, http_url={conn.http_url()!r}"
        )
        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_cdp(
                p, (conn.ws_url(), conn.http_url())
            )
            try:
                run_studio_channel_profile_customization(
                    page,
                    avatar_path=avatar_path,
                    channel_name=channel_name,
                    skip_name_change=skip_name_change,
                    profile_id=profile_id,
                    login_credentials=login_credentials,
                    yt_oldest_name=yt_oldest_name,
                    search_oldest_channel=search_oldest_channel,
                )
            finally:
                _close_playwright_browser(browser)
    except Exception as e:
        _log(f"Ошибка загрузки аватарки: {type(e).__name__}: {e!r}")
        raise _wrap_exc(e) from e
    finally:
        try:
            api.stop_profile(profile_id)
        except Exception as e:
            _log(f"Dolphin: stop_profile: {e!r}")
        api.close()


@with_log_profile
def upload_channel_avatar_in_local_antidetect_profile(
    profile_id: str,
    *,
    avatar_path: str | Path | None = None,
    channel_name: str | None = None,
    skip_name_change: bool = False,
    base_url: str,
    headless: bool = True,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    search_oldest_channel: bool = True,
    remote_cdp=None,
) -> None:
    """Локальный антидетект → Studio → «Настройка канала» → аватарка и/или название."""
    from zaliver.antydetect.local_antidetect_api import (
        LocalAntidetectError,
        LocalAntidetectHttpAPI,
    )
    from zaliver.antydetect.local_active_sessions import (
        register_local_session,
        unregister_local_session,
    )

    has_avatar = bool(avatar_path)
    has_name = bool((channel_name or "").strip()) and not skip_name_change
    if not has_avatar and not has_name:
        raise LocalAntidetectError("Не заданы ни аватарка, ни название канала.")
    _log(
        "Local antidetect: настройка канала (аватарка/название). "
        f"profile_id={profile_id!r}, base_url={base_url!r}, headless={headless}, "
        f"avatar={has_avatar}, name={has_name}"
    )

    api = LocalAntidetectHttpAPI(base_url)
    session_id: str | None = None
    started_at = time.perf_counter()
    try:
        acc = api.launch_profile(
            profile_id, headless=headless, expose_cdp=True, remote_cdp=remote_cdp
        )
        sid = acc.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            raise LocalAntidetectError(f"Нет session_id в ответе launch: {acc!r}")
        session_id = sid.strip()
        bu = (base_url or "").strip() or "http://127.0.0.1:18765"
        register_local_session(profile_id=profile_id, base_url=bu, session_id=session_id)
        ws_url = api.wait_for_cdp_ws_url(session_id, timeout_s=120.0)
        _log(f"Local antidetect: cdp_ws_url={ws_url!r}")
        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_local_session_cdp(
                p, api, session_id, ws_url
            )
            try:
                studio_kw = _local_studio_workflow_kwargs(
                    api,
                    profile_id,
                    login_credentials=login_credentials,
                    yt_oldest_name=yt_oldest_name,
                    search_oldest_channel=search_oldest_channel,
                    include_name_change_cooldown=True,
                )
                run_studio_channel_profile_customization(
                    page,
                    avatar_path=avatar_path,
                    channel_name=channel_name,
                    skip_name_change=skip_name_change,
                    **studio_kw,
                )
            finally:
                _close_playwright_browser(browser)
    except Exception as e:
        _log(f"Ошибка загрузки аватарки: {type(e).__name__}: {e!r}")
        raise LocalAntidetectError(f"Ошибка загрузки аватарки канала: {e}") from e
    finally:
        if session_id:
            unregister_local_session(profile_id=profile_id)
            try:
                api.stop_session(session_id)
            except Exception:
                pass
        try:
            _log(
                "Local antidetect: загрузка аватарки завершена за "
                f"{time.perf_counter() - started_at:.1f} с."
            )
        except Exception:
            pass
        api.close()


def _normalize_channel_links(
    *,
    link_title: str | None = None,
    link_url: str | None = None,
    channel_links: list[tuple[str, str]] | list[list[str]] | None = None,
) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for item in channel_links or []:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            lt = str(item[0] or "").strip()
            lu = str(item[1] or "").strip()
        else:
            continue
        if lt and lu:
            out.append((lt, lu))
    if out:
        return out
    lt = (link_title or "").strip()
    lu = (link_url or "").strip()
    if lt and lu:
        return [(lt, lu)]
    return []


def _channel_setup_work_flags(
    *,
    description: str | None,
    link_title: str | None,
    link_url: str | None,
    video_default_title: str | None,
    avatar_path: str | Path | None,
    channel_name: str | None,
    skip_name_change: bool,
    channel_links: list[tuple[str, str]] | list[list[str]] | None = None,
    change_language: bool = False,
) -> tuple[bool, bool, bool, bool, bool]:
    d = (description or "").strip()
    links = _normalize_channel_links(
        link_title=link_title,
        link_url=link_url,
        channel_links=channel_links,
    )
    has_text = bool(d) or bool(links)
    has_video_title = bool((video_default_title or "").strip())
    has_avatar = bool(avatar_path)
    has_name = bool((channel_name or "").strip()) and not skip_name_change
    return has_text, has_video_title, has_avatar, has_name, bool(change_language)


@with_log_profile
def setup_channel_in_profile(
    profile_id: str,
    *,
    description: str | None = None,
    link_title: str | None = None,
    link_url: str | None = None,
    channel_links: list[tuple[str, str]] | list[list[str]] | None = None,
    video_default_title: str | None = None,
    avatar_path: str | Path | None = None,
    channel_name: str | None = None,
    skip_name_change: bool = False,
    change_language: bool = False,
    local_token: str | None = None,
    headless: bool = True,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    search_oldest_channel: bool = True,
) -> None:
    """Dolphin → (опц. смена языка) → Studio → «Настройка канала» (один запуск профиля)."""
    has_text, has_video_title, has_avatar, has_name, do_lang = _channel_setup_work_flags(
        description=description,
        link_title=link_title,
        link_url=link_url,
        video_default_title=video_default_title,
        avatar_path=avatar_path,
        channel_name=channel_name,
        skip_name_change=skip_name_change,
        channel_links=channel_links,
        change_language=change_language,
    )
    if not has_text and not has_video_title and not has_avatar and not has_name and not do_lang:
        raise DolphinAntyError("Не заданы параметры настройки канала.")
    parts: list[str] = []
    if do_lang:
        parts.append("смена языка")
    if has_text:
        parts.append("описание/ссылка")
    if has_video_title:
        parts.append("название видео")
    if has_avatar:
        parts.append("фото профиля")
    if has_name:
        parts.append("название канала")
    _log(
        "Dolphin: настройка канала ("
        + ", ".join(parts)
        + f"). profile_id={profile_id!r}, headless={headless}"
    )
    api = DolphinAntyLocalAPI()
    try:
        tok = (local_token or "").strip()
        if tok:
            _log("Dolphin: login_with_token…")
            api.login_with_token(tok)
        _log("Dolphin: start_profile…")
        conn = api.start_profile(profile_id, headless=headless)
        _log(
            "Dolphin: профиль запущен. "
            f"ws_url={conn.ws_url()!r}, http_url={conn.http_url()!r}"
        )
        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_cdp(
                p, (conn.ws_url(), conn.http_url())
            )
            try:
                studio_kw = {
                    "profile_id": profile_id,
                    "login_credentials": login_credentials,
                    "yt_oldest_name": yt_oldest_name,
                    "search_oldest_channel": search_oldest_channel,
                }
                if do_lang:
                    run_youtube_interface_language_to_russian(
                        page, login_credentials=login_credentials
                    )
                if has_text:
                    if do_lang:
                        _log(
                            "Dolphin: переход в Studio после смены языка…"
                        )
                    run_studio_channel_description_and_link(
                        page,
                        description=description,
                        link_title=link_title,
                        link_url=link_url,
                        channel_links=channel_links,
                        **studio_kw,
                    )
                if has_video_title:
                    if has_text or do_lang:
                        _log(
                            "Dolphin: повторный переход в Studio "
                            "для названия видео (без перезапуска профиля)…"
                        )
                    run_studio_upload_default_title(
                        page,
                        title=video_default_title,
                        **studio_kw,
                    )
                if has_avatar or has_name:
                    if has_text or has_video_title or do_lang:
                        _log(
                            "Dolphin: повторный переход в Studio "
                            "для фото профиля/названия (без перезапуска профиля)…"
                        )
                    run_studio_channel_profile_customization(
                        page,
                        avatar_path=avatar_path,
                        channel_name=channel_name,
                        skip_name_change=skip_name_change,
                        **studio_kw,
                    )
            finally:
                _close_playwright_browser(browser)
    except Exception as e:
        _log(f"Ошибка настройки канала: {type(e).__name__}: {e!r}")
        raise _wrap_exc(e) from e
    finally:
        try:
            api.stop_profile(profile_id)
        except Exception as e:
            _log(f"Dolphin: stop_profile: {e!r}")
        api.close()


@with_log_profile
def setup_channel_in_local_antidetect_profile(
    profile_id: str,
    *,
    description: str | None = None,
    link_title: str | None = None,
    link_url: str | None = None,
    channel_links: list[tuple[str, str]] | list[list[str]] | None = None,
    video_default_title: str | None = None,
    avatar_path: str | Path | None = None,
    channel_name: str | None = None,
    skip_name_change: bool = False,
    change_language: bool = False,
    base_url: str,
    headless: bool = True,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    search_oldest_channel: bool = True,
    remote_cdp=None,
) -> None:
    """Локальный антидетект → (опц. смена языка) → Studio → «Настройка канала»."""
    from zaliver.antydetect.local_antidetect_api import (
        LocalAntidetectError,
        LocalAntidetectHttpAPI,
    )
    from zaliver.antydetect.local_active_sessions import (
        register_local_session,
        unregister_local_session,
    )

    has_text, has_video_title, has_avatar, has_name, do_lang = _channel_setup_work_flags(
        description=description,
        link_title=link_title,
        link_url=link_url,
        video_default_title=video_default_title,
        avatar_path=avatar_path,
        channel_name=channel_name,
        skip_name_change=skip_name_change,
        channel_links=channel_links,
        change_language=change_language,
    )
    if not has_text and not has_video_title and not has_avatar and not has_name and not do_lang:
        raise LocalAntidetectError("Не заданы параметры настройки канала.")
    parts: list[str] = []
    if do_lang:
        parts.append("смена языка")
    if has_text:
        parts.append("описание/ссылка")
    if has_video_title:
        parts.append("название видео")
    if has_avatar:
        parts.append("фото профиля")
    if has_name:
        parts.append("название канала")
    _log(
        "Local antidetect: настройка канала ("
        + ", ".join(parts)
        + f"). profile_id={profile_id!r}, base_url={base_url!r}, headless={headless}"
    )

    api = LocalAntidetectHttpAPI(base_url)
    session_id: str | None = None
    started_at = time.perf_counter()
    try:
        acc = api.launch_profile(
            profile_id, headless=headless, expose_cdp=True, remote_cdp=remote_cdp
        )
        sid = acc.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            raise LocalAntidetectError(f"Нет session_id в ответе launch: {acc!r}")
        session_id = sid.strip()
        bu = (base_url or "").strip() or "http://127.0.0.1:18765"
        register_local_session(profile_id=profile_id, base_url=bu, session_id=session_id)
        ws_url = api.wait_for_cdp_ws_url(session_id, timeout_s=120.0)
        _log(f"Local antidetect: cdp_ws_url={ws_url!r}")
        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_local_session_cdp(
                p, api, session_id, ws_url
            )
            try:
                studio_kw = _local_studio_workflow_kwargs(
                    api,
                    profile_id,
                    login_credentials=login_credentials,
                    yt_oldest_name=yt_oldest_name,
                    search_oldest_channel=search_oldest_channel,
                )
                if do_lang:
                    run_youtube_interface_language_to_russian(
                        page, login_credentials=login_credentials
                    )
                if has_text:
                    if do_lang:
                        _log(
                            "Local antidetect: переход в Studio после смены языка…"
                        )
                    run_studio_channel_description_and_link(
                        page,
                        description=description,
                        link_title=link_title,
                        link_url=link_url,
                        channel_links=channel_links,
                        **studio_kw,
                    )
                if has_video_title:
                    if has_text or do_lang:
                        _log(
                            "Local antidetect: повторный переход в Studio "
                            "для названия видео (без перезапуска профиля)…"
                        )
                    run_studio_upload_default_title(
                        page,
                        title=video_default_title,
                        **studio_kw,
                    )
                if has_avatar or has_name:
                    if has_text or has_video_title or do_lang:
                        _log(
                            "Local antidetect: повторный переход в Studio "
                            "для фото профиля/названия (без перезапуска профиля)…"
                        )
                    profile_kw = dict(studio_kw)
                    if has_name:
                        profile_kw["on_name_change_cooldown"] = (
                            _make_save_name_change_cooldown_handler(api, profile_id)
                        )
                    run_studio_channel_profile_customization(
                        page,
                        avatar_path=avatar_path,
                        channel_name=channel_name,
                        skip_name_change=skip_name_change,
                        **profile_kw,
                    )
            finally:
                _close_playwright_browser(browser)
    except Exception as e:
        _log(f"Ошибка настройки канала: {type(e).__name__}: {e!r}")
        raise LocalAntidetectError(f"Ошибка настройки канала: {e}") from e
    finally:
        if session_id:
            unregister_local_session(profile_id=profile_id)
            try:
                api.stop_session(session_id)
            except Exception:
                pass
        try:
            _log(
                "Local antidetect: настройка канала завершена за "
                f"{time.perf_counter() - started_at:.1f} с."
            )
        except Exception:
            pass
        api.close()


@with_log_profile
def setup_instagram_profile_in_profile(
    profile_id: str,
    *,
    description: str | None = None,
    avatar_path: str | Path | None = None,
    username: str | None = None,
    change_language: bool = False,
    local_token: str | None = None,
    headless: bool = True,
    session_login: str = "",
    session_password: str = "",
    session_twofa: str = "",
) -> None:
    """Dolphin → (опц. язык) → Edit profile + юзернейм → закрытие профиля."""
    from zaliver.instagram_upload.edit_profile import run_instagram_edit_profile

    bio = (description or "").strip()
    has_avatar = bool(avatar_path)
    uname = (username or "").strip().lstrip("@")
    do_lang = bool(change_language)
    if not bio and not has_avatar and not uname and not do_lang:
        raise DolphinAntyError(
            "Не заданы смена языка, bio, аватарка или юзернейм для Instagram."
        )
    parts: list[str] = []
    if do_lang:
        parts.append("язык → русский")
    if bio:
        parts.append("bio")
    if has_avatar:
        parts.append("фото профиля")
    if uname:
        parts.append("юзернейм")
    _log(
        "Dolphin: редактирование Instagram-профиля ("
        + ", ".join(parts)
        + f"). profile_id={profile_id!r}, headless={headless}"
    )
    api = DolphinAntyLocalAPI()
    try:
        tok = (local_token or "").strip()
        if tok:
            _log("Dolphin: login_with_token…")
            api.login_with_token(tok)
        _log("Dolphin: start_profile…")
        conn = api.start_profile(profile_id, headless=headless)
        _log(
            "Dolphin: профиль запущен. "
            f"ws_url={conn.ws_url()!r}, http_url={conn.http_url()!r}"
        )
        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_cdp(
                p, (conn.ws_url(), conn.http_url())
            )
            try:
                run_instagram_edit_profile(
                    page,
                    description=bio or None,
                    avatar_path=avatar_path,
                    username=uname or None,
                    change_language=do_lang,
                    session_login=session_login,
                    session_password=session_password,
                    session_twofa=session_twofa,
                    profile_id=profile_id,
                )
            finally:
                _close_playwright_browser(browser)
    except Exception as e:
        _log(f"Ошибка редактирования Instagram-профиля: {type(e).__name__}: {e!r}")
        raise _wrap_exc(e) from e
    finally:
        try:
            api.stop_profile(profile_id)
        except Exception as e:
            _log(f"Dolphin: stop_profile: {e!r}")
        api.close()


@with_log_profile
def setup_instagram_profile_in_local_antidetect_profile(
    profile_id: str,
    *,
    description: str | None = None,
    avatar_path: str | Path | None = None,
    username: str | None = None,
    change_language: bool = False,
    base_url: str,
    headless: bool = True,
    remote_cdp=None,
    session_login: str = "",
    session_password: str = "",
    session_twofa: str = "",
) -> None:
    """Локальный антидетект → (опц. язык) → Edit profile + юзернейм."""
    from zaliver.instagram_upload.edit_profile import run_instagram_edit_profile
    from zaliver.antydetect.local_antidetect_api import (
        LocalAntidetectError,
        LocalAntidetectHttpAPI,
    )
    from zaliver.antydetect.local_active_sessions import (
        register_local_session,
        unregister_local_session,
    )

    bio = (description or "").strip()
    has_avatar = bool(avatar_path)
    uname = (username or "").strip().lstrip("@")
    do_lang = bool(change_language)
    if not bio and not has_avatar and not uname and not do_lang:
        raise LocalAntidetectError(
            "Не заданы смена языка, bio, аватарка или юзернейм для Instagram."
        )
    parts: list[str] = []
    if do_lang:
        parts.append("язык → русский")
    if bio:
        parts.append("bio")
    if has_avatar:
        parts.append("фото профиля")
    if uname:
        parts.append("юзернейм")
    _log(
        "Local antidetect: редактирование Instagram-профиля ("
        + ", ".join(parts)
        + f"). profile_id={profile_id!r}, base_url={base_url!r}, headless={headless}"
    )

    api = LocalAntidetectHttpAPI(base_url)
    session_id: str | None = None
    started_at = time.perf_counter()
    try:
        login = (session_login or "").strip()
        pwd = (session_password or "").strip()
        twofa = (session_twofa or "").strip()
        if not pwd or not login:
            try:
                prof = api.get_profile(profile_id)
                loaded_login, loaded_pwd, loaded_twofa = (
                    _instagram_session_creds_from_profile_dict(prof)
                )
                if not login:
                    login = loaded_login
                if not pwd:
                    pwd = loaded_pwd
                if not twofa:
                    twofa = loaded_twofa
            except Exception as e:
                _log(f"Local antidetect: не удалось прочитать custom_data: {e!r}")

        acc = api.launch_profile(
            profile_id,
            headless=headless,
            expose_cdp=True,
            remote_cdp=remote_cdp,
        )
        sid = acc.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            raise LocalAntidetectError(f"Нет session_id в ответе launch: {acc!r}")
        session_id = sid.strip()
        bu = (base_url or "").strip() or "http://127.0.0.1:18765"
        register_local_session(profile_id=profile_id, base_url=bu, session_id=session_id)
        ws_url = api.wait_for_cdp_ws_url(session_id, timeout_s=120.0)
        _log(f"Local antidetect: cdp_ws_url={ws_url!r}")

        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_local_session_cdp(
                p, api, session_id, ws_url
            )
            try:
                run_instagram_edit_profile(
                    page,
                    description=bio or None,
                    avatar_path=avatar_path,
                    username=uname or None,
                    change_language=do_lang,
                    session_login=login,
                    session_password=pwd,
                    session_twofa=twofa,
                    profile_id=profile_id,
                )
            finally:
                _close_playwright_browser(browser)
    except Exception as e:
        _log(f"Ошибка редактирования Instagram-профиля: {type(e).__name__}: {e!r}")
        raise LocalAntidetectError(
            f"Ошибка редактирования Instagram-профиля: {e}"
        ) from e
    finally:
        if session_id:
            unregister_local_session(profile_id=profile_id)
            try:
                api.stop_session(session_id)
            except Exception:
                pass
        try:
            _log(
                "Local antidetect: редактирование Instagram-профиля завершено за "
                f"{time.perf_counter() - started_at:.1f} с."
            )
        except Exception:
            pass
        api.close()


@with_log_profile
def warmup_youtube_shorts_in_profile(
    profile_id: str,
    *,
    local_token: str | None = None,
    headless: bool = True,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    search_oldest_channel: bool = True,
    shorts_count: int | None = None,
    like_probability_pct: float | None = None,
    subscribe_probability_pct: float | None = None,
    shorts_watch_min_s: float | None = None,
    shorts_watch_max_s: float | None = None,
    watch_full_video: bool = False,
    shorts_recommendations: bool = True,
    search_query: str | None = None,
    watch_horizontal_videos: bool = False,
    horizontal_search_query: str | None = None,
    horizontal_videos_count: int | None = None,
) -> None:
    """Dolphin → авторизация/канал → лента YouTube Shorts."""
    _log(
        "Dolphin: прогрев Shorts. "
        f"profile_id={profile_id!r}, headless={headless}"
    )
    api = DolphinAntyLocalAPI()
    try:
        tok = (local_token or "").strip()
        if tok:
            _log("Dolphin: login_with_token…")
            api.login_with_token(tok)
        _log("Dolphin: start_profile…")
        conn = api.start_profile(profile_id, headless=headless)
        _log(
            "Dolphin: профиль запущен. "
            f"ws_url={conn.ws_url()!r}, http_url={conn.http_url()!r}"
        )
        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_cdp(
                p, (conn.ws_url(), conn.http_url())
            )
            try:
                kw: dict = {
                    "profile_id": profile_id,
                    "login_credentials": login_credentials,
                    "yt_oldest_name": yt_oldest_name,
                    "search_oldest_channel": search_oldest_channel,
                }
                if shorts_count is not None:
                    kw["shorts_count"] = shorts_count
                if like_probability_pct is not None:
                    kw["like_probability_pct"] = like_probability_pct
                if subscribe_probability_pct is not None:
                    kw["subscribe_probability_pct"] = subscribe_probability_pct
                if shorts_watch_min_s is not None:
                    kw["shorts_watch_min_s"] = shorts_watch_min_s
                if shorts_watch_max_s is not None:
                    kw["shorts_watch_max_s"] = shorts_watch_max_s
                if watch_full_video:
                    kw["watch_full_video"] = True
                kw["shorts_recommendations"] = shorts_recommendations
                if search_query is not None:
                    kw["search_query"] = search_query
                if watch_horizontal_videos:
                    kw["watch_horizontal_videos"] = True
                    kw["horizontal_search_query"] = horizontal_search_query
                if horizontal_videos_count is not None:
                    kw["horizontal_videos_count"] = horizontal_videos_count
                run_youtube_shorts_warmup(page, **kw)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        _log(f"Ошибка прогрева Shorts: {type(e).__name__}: {e!r}")
        raise _wrap_exc(e) from e
    finally:
        try:
            api.stop_profile(profile_id)
        except Exception as e:
            _log(f"Dolphin: stop_profile: {e!r}")
        api.close()


@with_log_profile
def warmup_youtube_shorts_in_local_antidetect_profile(
    profile_id: str,
    *,
    base_url: str,
    headless: bool = True,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    search_oldest_channel: bool = True,
    shorts_count: int | None = None,
    like_probability_pct: float | None = None,
    subscribe_probability_pct: float | None = None,
    shorts_watch_min_s: float | None = None,
    shorts_watch_max_s: float | None = None,
    watch_full_video: bool = False,
    shorts_recommendations: bool = True,
    search_query: str | None = None,
    watch_horizontal_videos: bool = False,
    horizontal_search_query: str | None = None,
    horizontal_videos_count: int | None = None,
    remote_cdp=None,
) -> None:
    """Локальный антидетект → авторизация/канал → лента YouTube Shorts."""
    _log(
        "Local antidetect: прогрев Shorts. "
        f"profile_id={profile_id!r}, base_url={base_url!r}, headless={headless}"
    )
    from zaliver.antydetect.local_antidetect_api import (
        LocalAntidetectError,
        LocalAntidetectHttpAPI,
    )
    from zaliver.antydetect.local_active_sessions import (
        register_local_session,
        unregister_local_session,
    )

    api = LocalAntidetectHttpAPI(base_url)
    session_id: str | None = None
    started_at = time.perf_counter()
    try:
        acc = api.launch_profile(
            profile_id, headless=headless, expose_cdp=True, remote_cdp=remote_cdp
        )
        sid = acc.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            raise LocalAntidetectError(f"Нет session_id в ответе launch: {acc!r}")
        session_id = sid.strip()
        bu = (base_url or "").strip() or "http://127.0.0.1:18765"
        register_local_session(profile_id=profile_id, base_url=bu, session_id=session_id)
        ws_url = api.wait_for_cdp_ws_url(session_id, timeout_s=120.0)
        _log(f"Local antidetect: cdp_ws_url={ws_url!r}")
        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_local_session_cdp(
                p, api, session_id, ws_url
            )
            try:
                studio_kw = _local_studio_workflow_kwargs(
                    api,
                    profile_id,
                    login_credentials=login_credentials,
                    yt_oldest_name=yt_oldest_name,
                    search_oldest_channel=search_oldest_channel,
                )
                if shorts_count is not None:
                    studio_kw["shorts_count"] = shorts_count
                if like_probability_pct is not None:
                    studio_kw["like_probability_pct"] = like_probability_pct
                if subscribe_probability_pct is not None:
                    studio_kw["subscribe_probability_pct"] = subscribe_probability_pct
                if shorts_watch_min_s is not None:
                    studio_kw["shorts_watch_min_s"] = shorts_watch_min_s
                if shorts_watch_max_s is not None:
                    studio_kw["shorts_watch_max_s"] = shorts_watch_max_s
                if watch_full_video:
                    studio_kw["watch_full_video"] = True
                studio_kw["shorts_recommendations"] = shorts_recommendations
                if search_query is not None:
                    studio_kw["search_query"] = search_query
                if watch_horizontal_videos:
                    studio_kw["watch_horizontal_videos"] = True
                    studio_kw["horizontal_search_query"] = horizontal_search_query
                if horizontal_videos_count is not None:
                    studio_kw["horizontal_videos_count"] = horizontal_videos_count
                run_youtube_shorts_warmup(page, **studio_kw)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        _log(f"Ошибка прогрева Shorts: {type(e).__name__}: {e!r}")
        raise LocalAntidetectError(f"Ошибка прогрева Shorts: {e}") from e
    finally:
        if session_id:
            unregister_local_session(profile_id=profile_id)
            try:
                api.stop_session(session_id)
            except Exception:
                pass
        try:
            _log(
                "Local antidetect: прогрев Shorts завершён за "
                f"{time.perf_counter() - started_at:.1f} с."
            )
        except Exception:
            pass
        api.close()


@with_log_profile
def promote_youtube_videos_in_profile(
    profile_id: str,
    *,
    videos: list[PromotionTargetVideo],
    subscribe_to_channels: bool = False,
    local_token: str | None = None,
    headless: bool = True,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    search_oldest_channel: bool = True,
    shorts_count: int | None = None,
    like_probability_pct: float | None = None,
    subscribe_probability_pct: float | None = None,
    shorts_watch_min_s: float | None = None,
    shorts_watch_max_s: float | None = None,
    watch_full_video: bool = False,
    enable_comments: bool = False,
    comments: list[str] | None = None,
    comment_probability_pct: float | None = None,
) -> None:
    """Dolphin → Studio → опц. подписки → лента подписок Shorts."""
    _log(
        "Dolphin: продвижение. "
        f"profile_id={profile_id!r}, headless={headless}, "
        f"videos={len(videos)}, subscribe={subscribe_to_channels}"
    )
    api = DolphinAntyLocalAPI()
    try:
        tok = (local_token or "").strip()
        if tok:
            _log("Dolphin: login_with_token…")
            api.login_with_token(tok)
        _log("Dolphin: start_profile…")
        conn = api.start_profile(profile_id, headless=headless)
        _log(
            "Dolphin: профиль запущен. "
            f"ws_url={conn.ws_url()!r}, http_url={conn.http_url()!r}"
        )
        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_cdp(
                p, (conn.ws_url(), conn.http_url())
            )
            try:
                kw: dict = {
                    "videos": videos,
                    "subscribe_to_channels": subscribe_to_channels,
                    "viewer_profile_id": profile_id,
                    "profile_id": profile_id,
                    "login_credentials": login_credentials,
                    "yt_oldest_name": yt_oldest_name,
                    "search_oldest_channel": search_oldest_channel,
                    "watch_full_video": watch_full_video,
                    "enable_comments": enable_comments,
                    "comments": comments,
                }
                if shorts_count is not None:
                    kw["shorts_count"] = shorts_count
                if like_probability_pct is not None:
                    kw["like_probability_pct"] = like_probability_pct
                if subscribe_probability_pct is not None:
                    kw["subscribe_probability_pct"] = subscribe_probability_pct
                if shorts_watch_min_s is not None:
                    kw["shorts_watch_min_s"] = shorts_watch_min_s
                if shorts_watch_max_s is not None:
                    kw["shorts_watch_max_s"] = shorts_watch_max_s
                if comment_probability_pct is not None:
                    kw["comment_probability_pct"] = comment_probability_pct
                run_youtube_profiles_promotion(page, **kw)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        _log(f"Ошибка продвижения: {type(e).__name__}: {e!r}")
        raise _wrap_exc(e) from e
    finally:
        try:
            api.stop_profile(profile_id)
        except Exception as e:
            _log(f"Dolphin: stop_profile: {e!r}")
        api.close()


@with_log_profile
def promote_youtube_videos_in_local_antidetect_profile(
    profile_id: str,
    *,
    base_url: str,
    videos: list[PromotionTargetVideo],
    subscribe_to_channels: bool = False,
    headless: bool = True,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    search_oldest_channel: bool = True,
    remote_cdp=None,
    shorts_count: int | None = None,
    like_probability_pct: float | None = None,
    subscribe_probability_pct: float | None = None,
    shorts_watch_min_s: float | None = None,
    shorts_watch_max_s: float | None = None,
    watch_full_video: bool = False,
    enable_comments: bool = False,
    comments: list[str] | None = None,
    comment_probability_pct: float | None = None,
) -> None:
    """Локальный антидетект → Studio → опц. подписки → лента подписок Shorts."""
    _log(
        "Local antidetect: продвижение. "
        f"profile_id={profile_id!r}, base_url={base_url!r}, headless={headless}, "
        f"videos={len(videos)}, subscribe={subscribe_to_channels}"
    )
    from zaliver.antydetect.local_antidetect_api import (
        LocalAntidetectError,
        LocalAntidetectHttpAPI,
    )
    from zaliver.antydetect.local_active_sessions import (
        register_local_session,
        unregister_local_session,
    )

    api = LocalAntidetectHttpAPI(base_url)
    session_id: str | None = None
    started_at = time.perf_counter()
    try:
        acc = api.launch_profile(
            profile_id, headless=headless, expose_cdp=True, remote_cdp=remote_cdp
        )
        sid = acc.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            raise LocalAntidetectError(f"Нет session_id в ответе launch: {acc!r}")
        session_id = sid.strip()
        bu = (base_url or "").strip() or "http://127.0.0.1:18765"
        register_local_session(profile_id=profile_id, base_url=bu, session_id=session_id)
        ws_url = api.wait_for_cdp_ws_url(session_id, timeout_s=120.0)
        _log(f"Local antidetect: cdp_ws_url={ws_url!r}")
        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_local_session_cdp(
                p, api, session_id, ws_url
            )
            try:
                studio_kw = _local_studio_workflow_kwargs(
                    api,
                    profile_id,
                    login_credentials=login_credentials,
                    yt_oldest_name=yt_oldest_name,
                    search_oldest_channel=search_oldest_channel,
                )
                if shorts_count is not None:
                    studio_kw["shorts_count"] = shorts_count
                if like_probability_pct is not None:
                    studio_kw["like_probability_pct"] = like_probability_pct
                if subscribe_probability_pct is not None:
                    studio_kw["subscribe_probability_pct"] = subscribe_probability_pct
                if shorts_watch_min_s is not None:
                    studio_kw["shorts_watch_min_s"] = shorts_watch_min_s
                if shorts_watch_max_s is not None:
                    studio_kw["shorts_watch_max_s"] = shorts_watch_max_s
                if watch_full_video:
                    studio_kw["watch_full_video"] = True
                studio_kw["enable_comments"] = enable_comments
                studio_kw["comments"] = comments
                if comment_probability_pct is not None:
                    studio_kw["comment_probability_pct"] = comment_probability_pct
                run_youtube_profiles_promotion(
                    page,
                    videos=videos,
                    subscribe_to_channels=subscribe_to_channels,
                    viewer_profile_id=profile_id,
                    **studio_kw,
                )
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        _log(f"Ошибка продвижения: {type(e).__name__}: {e!r}")
        raise LocalAntidetectError(f"Ошибка продвижения: {e}") from e
    finally:
        if session_id:
            unregister_local_session(profile_id=profile_id)
            try:
                api.stop_session(session_id)
            except Exception:
                pass
        try:
            _log(
                "Local antidetect: продвижение завершено за "
                f"{time.perf_counter() - started_at:.1f} с."
            )
        except Exception:
            pass
        api.close()


@with_log_profile
def promote_instagram_reels_in_profile(
    profile_id: str,
    *,
    videos: list[PromotionTargetVideo],
    subscribe_to_channels: bool = False,
    local_token: str | None = None,
    headless: bool = True,
    session_login: str = "",
    session_password: str = "",
    session_twofa: str = "",
    shorts_count: int | None = None,
    like_probability_pct: float | None = None,
    shorts_watch_min_s: float | None = None,
    shorts_watch_max_s: float | None = None,
    watch_full_video: bool = False,
    enable_comments: bool = False,
    comments: list[str] | None = None,
    comment_probability_pct: float | None = None,
) -> None:
    """Dolphin → Instagram → рилсы по ссылкам: подписка/лайк/коммент на странице."""
    from zaliver.instagram_upload.reels_promote import run_instagram_profiles_promotion

    _log(
        "Dolphin: продвижение Instagram Reels. "
        f"profile_id={profile_id!r}, headless={headless}, "
        f"videos={len(videos)}, subscribe={subscribe_to_channels}"
    )
    api = DolphinAntyLocalAPI()
    try:
        tok = (local_token or "").strip()
        if tok:
            _log("Dolphin: login_with_token…")
            api.login_with_token(tok)
        _log("Dolphin: start_profile…")
        conn = api.start_profile(profile_id, headless=headless)
        _log(
            "Dolphin: профиль запущен. "
            f"ws_url={conn.ws_url()!r}, http_url={conn.http_url()!r}"
        )
        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_cdp(
                p, (conn.ws_url(), conn.http_url())
            )
            try:
                kw: dict = {
                    "videos": videos,
                    "subscribe_to_channels": subscribe_to_channels,
                    "viewer_profile_id": profile_id,
                    "profile_id": profile_id,
                    "session_login": session_login,
                    "session_password": session_password,
                    "session_twofa": session_twofa,
                    "watch_full_video": watch_full_video,
                    "enable_comments": enable_comments,
                    "comments": comments,
                }
                if shorts_count is not None:
                    kw["shorts_count"] = shorts_count
                if like_probability_pct is not None:
                    kw["like_probability_pct"] = like_probability_pct
                if shorts_watch_min_s is not None:
                    kw["shorts_watch_min_s"] = shorts_watch_min_s
                if shorts_watch_max_s is not None:
                    kw["shorts_watch_max_s"] = shorts_watch_max_s
                if comment_probability_pct is not None:
                    kw["comment_probability_pct"] = comment_probability_pct
                run_instagram_profiles_promotion(page, **kw)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        _log(f"Ошибка продвижения Reels: {type(e).__name__}: {e!r}")
        raise _wrap_exc(e) from e
    finally:
        try:
            api.stop_profile(profile_id)
        except Exception as e:
            _log(f"Dolphin: stop_profile: {e!r}")
        api.close()


@with_log_profile
def promote_instagram_reels_in_local_antidetect_profile(
    profile_id: str,
    *,
    base_url: str,
    videos: list[PromotionTargetVideo],
    subscribe_to_channels: bool = False,
    headless: bool = True,
    remote_cdp=None,
    session_login: str = "",
    session_password: str = "",
    session_twofa: str = "",
    shorts_count: int | None = None,
    like_probability_pct: float | None = None,
    shorts_watch_min_s: float | None = None,
    shorts_watch_max_s: float | None = None,
    watch_full_video: bool = False,
    enable_comments: bool = False,
    comments: list[str] | None = None,
    comment_probability_pct: float | None = None,
) -> None:
    """Локальный антидетект → Instagram → рилсы по ссылкам: подписка/лайк/коммент."""
    from zaliver.instagram_upload.reels_promote import run_instagram_profiles_promotion
    from zaliver.antydetect.local_antidetect_api import (
        LocalAntidetectError,
        LocalAntidetectHttpAPI,
    )
    from zaliver.antydetect.local_active_sessions import (
        register_local_session,
        unregister_local_session,
    )

    _log(
        "Local antidetect: продвижение Instagram Reels. "
        f"profile_id={profile_id!r}, base_url={base_url!r}, headless={headless}, "
        f"videos={len(videos)}, subscribe={subscribe_to_channels}"
    )
    api = LocalAntidetectHttpAPI(base_url)
    session_id: str | None = None
    started_at = time.perf_counter()
    try:
        login = (session_login or "").strip()
        pwd = (session_password or "").strip()
        twofa = (session_twofa or "").strip()
        if not pwd or not login:
            try:
                prof = api.get_profile(profile_id)
                loaded_login, loaded_pwd, loaded_twofa = (
                    _instagram_session_creds_from_profile_dict(prof)
                )
                if not login:
                    login = loaded_login
                if not pwd:
                    pwd = loaded_pwd
                if not twofa:
                    twofa = loaded_twofa
            except Exception as e:
                _log(f"Local antidetect: не удалось прочитать custom_data: {e!r}")

        acc = api.launch_profile(
            profile_id,
            headless=headless,
            expose_cdp=True,
            remote_cdp=remote_cdp,
        )
        sid = acc.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            raise LocalAntidetectError(f"Нет session_id в ответе launch: {acc!r}")
        session_id = sid.strip()
        bu = (base_url or "").strip() or "http://127.0.0.1:18765"
        register_local_session(profile_id=profile_id, base_url=bu, session_id=session_id)
        ws_url = api.wait_for_cdp_ws_url(session_id, timeout_s=120.0)
        _log(f"Local antidetect: cdp_ws_url={ws_url!r}")

        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_local_session_cdp(
                p, api, session_id, ws_url
            )
            try:
                kw: dict = {
                    "videos": videos,
                    "subscribe_to_channels": subscribe_to_channels,
                    "viewer_profile_id": profile_id,
                    "profile_id": profile_id,
                    "session_login": login,
                    "session_password": pwd,
                    "session_twofa": twofa,
                    "watch_full_video": watch_full_video,
                    "enable_comments": enable_comments,
                    "comments": comments,
                }
                if shorts_count is not None:
                    kw["shorts_count"] = shorts_count
                if like_probability_pct is not None:
                    kw["like_probability_pct"] = like_probability_pct
                if shorts_watch_min_s is not None:
                    kw["shorts_watch_min_s"] = shorts_watch_min_s
                if shorts_watch_max_s is not None:
                    kw["shorts_watch_max_s"] = shorts_watch_max_s
                if comment_probability_pct is not None:
                    kw["comment_probability_pct"] = comment_probability_pct
                run_instagram_profiles_promotion(page, **kw)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        _log(f"Ошибка продвижения Reels: {type(e).__name__}: {e!r}")
        raise LocalAntidetectError(f"Ошибка продвижения Instagram Reels: {e}") from e
    finally:
        if session_id:
            unregister_local_session(profile_id=profile_id)
            try:
                api.stop_session(session_id)
            except Exception:
                pass
        try:
            _log(
                "Local antidetect: продвижение Reels завершено за "
                f"{time.perf_counter() - started_at:.1f} с."
            )
        except Exception:
            pass
        api.close()


@with_log_profile
def set_youtube_interface_language_in_profile(
    profile_id: str,
    *,
    local_token: str | None = None,
    headless: bool = True,
    login_credentials=None,
) -> None:
    """Dolphin → главная YouTube → смена языка интерфейса на русский."""
    _log(
        "Dolphin: смена языка YouTube. "
        f"profile_id={profile_id!r}, headless={headless}"
    )
    api = DolphinAntyLocalAPI()
    try:
        tok = (local_token or "").strip()
        if tok:
            _log("Dolphin: login_with_token…")
            api.login_with_token(tok)
        _log("Dolphin: start_profile…")
        conn = api.start_profile(profile_id, headless=headless)
        _log(
            "Dolphin: профиль запущен. "
            f"ws_url={conn.ws_url()!r}, http_url={conn.http_url()!r}"
        )
        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_cdp(
                p, (conn.ws_url(), conn.http_url())
            )
            try:
                run_youtube_interface_language_to_russian(
                    page, login_credentials=login_credentials
                )
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        _log(f"Ошибка смены языка YouTube: {type(e).__name__}: {e!r}")
        raise _wrap_exc(e) from e
    finally:
        try:
            api.stop_profile(profile_id)
        except Exception as e:
            _log(f"Dolphin: stop_profile: {e!r}")
        api.close()


@with_log_profile
def set_youtube_interface_language_in_local_antidetect_profile(
    profile_id: str,
    *,
    base_url: str,
    headless: bool = True,
    login_credentials=None,
    remote_cdp=None,
) -> None:
    """Локальный антидетект → главная YouTube → смена языка интерфейса на русский."""
    _log(
        "Local antidetect: смена языка YouTube. "
        f"profile_id={profile_id!r}, base_url={base_url!r}, headless={headless}"
    )
    from zaliver.antydetect.local_antidetect_api import (
        LocalAntidetectError,
        LocalAntidetectHttpAPI,
    )
    from zaliver.antydetect.local_active_sessions import (
        register_local_session,
        unregister_local_session,
    )

    api = LocalAntidetectHttpAPI(base_url)
    session_id: str | None = None
    started_at = time.perf_counter()
    try:
        acc = api.launch_profile(
            profile_id, headless=headless, expose_cdp=True, remote_cdp=remote_cdp
        )
        sid = acc.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            raise LocalAntidetectError(f"Нет session_id в ответе launch: {acc!r}")
        session_id = sid.strip()
        bu = (base_url or "").strip() or "http://127.0.0.1:18765"
        register_local_session(profile_id=profile_id, base_url=bu, session_id=session_id)
        ws_url = api.wait_for_cdp_ws_url(session_id, timeout_s=120.0)
        _log(f"Local antidetect: cdp_ws_url={ws_url!r}")
        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_local_session_cdp(
                p, api, session_id, ws_url
            )
            try:
                run_youtube_interface_language_to_russian(
                    page, login_credentials=login_credentials
                )
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        _log(f"Ошибка смены языка YouTube: {type(e).__name__}: {e!r}")
        raise LocalAntidetectError(f"Ошибка смены языка YouTube: {e}") from e
    finally:
        if session_id:
            unregister_local_session(profile_id=profile_id)
            try:
                api.stop_session(session_id)
            except Exception:
                pass
        try:
            _log(
                "Local antidetect: смена языка YouTube завершена за "
                f"{time.perf_counter() - started_at:.1f} с."
            )
        except Exception:
            pass
        api.close()


class CombinedPlatformUploadError(Exception):
    """Оба залива (YouTube и Instagram) завершились ошибкой."""

    def __init__(
        self,
        message: str,
        *,
        youtube_error: BaseException | None = None,
        instagram_error: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.youtube_error = youtube_error
        self.instagram_error = instagram_error


def _browser_cdp_alive(browser) -> bool:
    try:
        if browser is None:
            return False
        _ = browser.contexts
        return True
    except Exception:
        return False


def _page_still_open(page) -> bool:
    try:
        return page is not None and not page.is_closed()
    except Exception:
        return False


def _ensure_one_instagram_tab(
    context, *, seed_page=None, refocus_youtube: bool = True
):
    """
    Ровно одна вкладка Instagram в фоне: без перехвата фокуса у YouTube.
    Переиспользуем существующую / blank, иначе фоновый createTarget(IG URL)
    или один new_page с немедленным возвратом фокуса на seed.
    Лишние about:blank после неудачного CDP-attach закрываются.
    """
    from zaliver.instagram_upload.register import INSTAGRAM_URL, _navigate_page_to

    def _refocus_youtube() -> None:
        if not refocus_youtube:
            return
        if seed_page is not None and _page_still_open(seed_page):
            _bring_studio_tab_to_front(seed_page, log_label="Yt+Inst")

    def _close_orphan_blanks(*, keep) -> None:
        keep_ids = {id(p) for p in keep if p is not None}
        for blank in list(_ig_reusable_blank_pages(context)):
            if id(blank) in keep_ids:
                continue
            try:
                blank.close()
                _log("Yt+Inst: закрыта лишняя about:blank вкладка.")
            except Exception:
                pass

    ig_pages = _ig_instagram_pages(context)
    if ig_pages:
        chosen = ig_pages[0]
        for extra in ig_pages[1:]:
            try:
                extra.close()
                _log("Yt+Inst: закрыта лишняя вкладка Instagram.")
            except Exception:
                pass
        _log(
            "Yt+Inst: используем уже открытую вкладку Instagram "
            f"url={_ig_page_url_lower(chosen)!r}"
        )
        _close_orphan_blanks(keep=(seed_page, chosen))
        _refocus_youtube()
        return chosen

    blank_pages = _ig_reusable_blank_pages(context)
    for blank in blank_pages:
        if seed_page is not None and blank is seed_page:
            continue
        try:
            _navigate_page_to(
                blank,
                INSTAGRAM_URL,
                label="Yt+Inst IG tab",
                keep_in_background=True,
            )
            _log("Yt+Inst: blank-вкладка превращена в Instagram (фон).")
            _close_orphan_blanks(keep=(seed_page, blank))
            _refocus_youtube()
            return blank
        except Exception as e:
            _log(f"Yt+Inst: не удалось открыть IG на blank: {e!r}")

    # Сразу Instagram URL — не about:blank (иначе при fallback new_page
    # остаётся orphan blank + IG).
    page = _ig_new_page_background(
        context, seed_page=seed_page, url=INSTAGRAM_URL
    )
    try:
        cur = (page.url or "").strip().lower()
    except Exception:
        cur = ""
    if "instagram.com" not in cur:
        try:
            _navigate_page_to(
                page,
                INSTAGRAM_URL,
                label="Yt+Inst IG tab",
                keep_in_background=True,
            )
        except Exception as e:
            _log(f"Yt+Inst: goto Instagram на вкладке: {e!r}")
    else:
        _log("Yt+Inst: вкладка Instagram открыта в фоне.")

    # На случай гонки createTarget + new_page — оставляем ровно одну IG.
    ig_pages = _ig_instagram_pages(context)
    chosen = page
    if ig_pages:
        if page in ig_pages:
            chosen = page
        else:
            chosen = ig_pages[0]
        for extra in ig_pages:
            if extra is chosen:
                continue
            try:
                extra.close()
                _log("Yt+Inst: закрыта лишняя вкладка Instagram (после create).")
            except Exception:
                pass

    _close_orphan_blanks(keep=(seed_page, chosen))
    _refocus_youtube()
    return chosen


def _pick_non_instagram_page(context, *, prefer=None):
    """Вкладка для YouTube: не Instagram (Studio / blank / прочее)."""
    if prefer is not None and _page_still_open(prefer):
        if "instagram.com" not in _ig_page_url_lower(prefer):
            return prefer
    studio = None
    other = None
    for pg in _ig_alive_context_pages(context):
        url = _ig_page_url_lower(pg)
        if "instagram.com" in url:
            continue
        if "studio.youtube.com" in url:
            studio = pg
            break
        if other is None:
            other = pg
    return studio or other or prefer


class _YoutubeTabFocusKeeper:
    """
    Периодически возвращает фокус на вкладку YouTube, пока крутится Instagram.

    Нельзя вызывать Playwright API исходной page из другого потока/greenlet —
    поэтому keeper держит своё CDP-подключение и активирует Studio через него.
    """

    def __init__(
        self,
        page,
        *,
        cdp_endpoints: tuple[str, ...] = (),
        interval_s: float = 1.5,
    ) -> None:
        self._interval_s = max(0.5, float(interval_s))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cdp_endpoints = tuple(
            e.strip() for e in cdp_endpoints if (e or "").strip()
        )
        self._target_id = ""
        self._url_hint = "studio.youtube.com"
        # targetId снимаем на владеющем потоке (до старта keeper-thread).
        if page is not None:
            try:
                self._url_hint = (page.url or "").strip() or self._url_hint
            except Exception:
                pass
            try:
                session = page.context.new_cdp_session(page)
                try:
                    info = session.send("Target.getTargetInfo")
                    if isinstance(info, dict):
                        ti = info.get("targetInfo") or info
                        if isinstance(ti, dict):
                            tid = str(ti.get("targetId") or "").strip()
                            if tid:
                                self._target_id = tid
                finally:
                    try:
                        session.detach()
                    except Exception:
                        pass
            except Exception:
                pass
        self._err_logged = False

    def start(self) -> None:
        if not self._cdp_endpoints:
            _log(
                "Yt+Inst: focus keeper пропущен — нет CDP endpoint "
                "(нельзя трогать Playwright page из другого потока)."
            )
            return
        self._thread = threading.Thread(
            target=self._loop,
            name="yt-inst-focus-keeper",
            daemon=True,
        )
        self._thread.start()
        _log("Yt+Inst: фокус удерживается на вкладке YouTube (отдельный CDP).")

    def stop(self, *, timeout_s: float = 5.0) -> None:
        self._stop.set()
        th = self._thread
        if th is not None and th.is_alive():
            th.join(timeout=max(0.5, float(timeout_s)))

    def _activate_studio(self, context) -> None:
        # 1) Вкладка Studio в этом CDP-контексте.
        for pg in list(getattr(context, "pages", None) or []):
            try:
                if pg.is_closed():
                    continue
                url = (pg.url or "").strip().lower()
            except Exception:
                continue
            if "studio.youtube.com" in url or (
                "youtube.com" in url and "instagram.com" not in url
            ):
                try:
                    pg.bring_to_front()
                except Exception:
                    pass
                return
        # 2) Fallback: Target.activateTarget по id, снятому с YT-вкладки.
        if not self._target_id:
            return
        seed = None
        for pg in list(getattr(context, "pages", None) or []):
            try:
                if not pg.is_closed():
                    seed = pg
                    break
            except Exception:
                continue
        if seed is None:
            return
        cdp = None
        try:
            cdp = context.new_cdp_session(seed)
            cdp.send("Target.activateTarget", {"targetId": self._target_id})
        except Exception:
            pass
        finally:
            if cdp is not None:
                try:
                    cdp.detach()
                except Exception:
                    pass

    def _loop(self) -> None:
        try:
            with sync_playwright() as p:
                browser, context, _seed = _playwright_page_from_cdp(
                    p, self._cdp_endpoints
                )
                try:
                    while not self._stop.wait(self._interval_s):
                        try:
                            self._activate_studio(context)
                        except Exception as e:
                            if not self._err_logged:
                                self._err_logged = True
                                _log(
                                    "Yt+Inst: focus keeper — активация Studio "
                                    f"не удалась (дальше без спама): {e!r}"
                                )
                finally:
                    _close_playwright_browser(browser)
        except Exception as e:
            if not self._err_logged:
                _log(f"Yt+Inst: focus keeper остановлен: {e!r}")


@dataclass
class _YtInstIgJob:
    """Один (или batch) залив Instagram в очереди профиля Yt+Inst."""

    items: list[tuple[str, str, str]]
    done: threading.Event = field(default_factory=threading.Event)
    result: dict | None = None
    error: BaseException | None = None
    on_success: Callable[[dict], None] | None = None
    on_error: Callable[[BaseException], None] | None = None
    # Сигнал: YouTube этого же ролика завершён — можно Done /reels/.
    youtube_done: threading.Event | None = None


_YT_INST_IG_PIPELINES: dict[str, "_YtInstIgPipeline"] = {}
_YT_INST_IG_PIPELINES_GUARD = threading.Lock()


class _YtInstIgPipeline:
    """
    Серийная очередь Instagram на профиль.
    YouTube может уйти вперёд на следующее видео (pause 0 / keep-open);
    Instagram догоняет теми же роликами в том же порядке.
    """

    def __init__(
        self,
        profile_id: str,
        *,
        cdp_endpoints: tuple[str, ...],
        session_login: str = "",
        session_password: str = "",
        session_twofa: str = "",
    ) -> None:
        self.profile_id = (profile_id or "").strip()
        self._cdp_endpoints = tuple(
            e.strip() for e in cdp_endpoints if (e or "").strip()
        )
        self._session_login = session_login
        self._session_password = session_password
        self._session_twofa = session_twofa
        self._q: queue.Queue[_YtInstIgJob | None] = queue.Queue()
        self._idle = threading.Event()
        self._idle.set()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._worker,
            name=f"yt-inst-ig-{self.profile_id[:12] or 'x'}",
            daemon=True,
        )
        self._thread.start()
        _log(
            f"Yt+Inst: IG pipeline стартовал profile_id={self.profile_id!r}."
        )

    def update_endpoints(self, cdp_endpoints: tuple[str, ...]) -> None:
        cleaned = tuple(e.strip() for e in cdp_endpoints if (e or "").strip())
        if cleaned:
            self._cdp_endpoints = cleaned

    def enqueue(self, job: _YtInstIgJob) -> None:
        self._idle.clear()
        self._q.put(job)

    def wait_idle(self, *, timeout_s: float = 3600.0) -> None:
        if not self._idle.wait(timeout=max(1.0, float(timeout_s))):
            raise TimeoutError(
                f"Yt+Inst IG pipeline не освободился за {timeout_s:.0f} с "
                f"(profile={self.profile_id!r})"
            )

    def shutdown(self, *, timeout_s: float = 120.0) -> None:
        self._stop.set()
        try:
            self._q.put_nowait(None)
        except Exception:
            pass
        self._thread.join(timeout=max(1.0, float(timeout_s)))

    def _worker(self) -> None:
        from zaliver.instagram_upload.reels_upload import run_instagram_reels_upload

        while not self._stop.is_set():
            try:
                job = self._q.get(timeout=0.5)
            except queue.Empty:
                if self._q.empty():
                    self._idle.set()
                continue
            if job is None:
                self._idle.set()
                break
            self._idle.clear()
            signaled = False
            try:
                if not self._cdp_endpoints:
                    raise DolphinAntyError(
                        "Yt+Inst Instagram: пустой CDP endpoint."
                    )
                pw = sync_playwright().start()
                _browser = None
                try:
                    _browser, context, _seed = _playwright_page_from_cdp(
                        pw, self._cdp_endpoints
                    )
                    ig_page = None
                    deadline = time.monotonic() + 60.0
                    while time.monotonic() < deadline:
                        pages = _ig_instagram_pages(context)
                        if pages:
                            ig_page = pages[0]
                            break
                        time.sleep(0.2)
                    if ig_page is None:
                        n_pages = len(_ig_alive_context_pages(context))
                        _log(
                            "Yt+Inst: pipeline не видит вкладку Instagram "
                            f"(context_pages={n_pages}) — открываем заново…"
                        )
                        try:
                            ig_page = _ensure_one_instagram_tab(
                                context,
                                seed_page=_seed,
                                refocus_youtube=False,
                            )
                        except Exception as open_e:
                            _log(
                                f"Yt+Inst: pipeline не смог открыть IG: {open_e!r}"
                            )
                            ig_page = None
                    if ig_page is not None:
                        # Нельзя брать Studio как «Instagram» (_pick_primary fallback).
                        if "instagram.com" not in _ig_page_url_lower(ig_page):
                            _log(
                                "Yt+Inst: pipeline: страница не Instagram "
                                f"({_ig_page_url_lower(ig_page)!r}) — игнор."
                            )
                            ig_page = None
                    if ig_page is None or not _page_still_open(ig_page):
                        raise RuntimeError(
                            "Нет вкладки Instagram для pipeline-залива"
                        )
                    batch_results: list = []
                    for idx, (vp, tt, dd) in enumerate(job.items, start=1):
                        if not (vp or "").strip():
                            continue
                        _log(
                            f"Yt+Inst: Instagram queue "
                            f"{idx}/{len(job.items)} "
                            f"profile={self.profile_id!r}…"
                        )
                        one = run_instagram_reels_upload(
                            ig_page,
                            video_path=vp,
                            title=tt,
                            description=dd,
                            session_login=self._session_login,
                            session_password=self._session_password,
                            session_twofa=self._session_twofa,
                            profile_id=self.profile_id or None,
                            top_reels_scan=1,
                            keep_in_background=True,
                            wait_youtube_before_done=job.youtube_done,
                        )
                        batch_results.append(one)
                    if not batch_results:
                        raise RuntimeError(
                            "Instagram: нет результата pipeline-залива"
                        )
                    if len(batch_results) == 1:
                        job.result = batch_results[0]
                    else:
                        out = dict(batch_results[-1])
                        out["batch_results"] = list(batch_results)
                        job.result = out
                    _log(
                        f"Yt+Inst: Instagram — успех (pipeline) "
                        f"profile={self.profile_id!r}."
                    )
                    if job.on_success is not None and job.result is not None:
                        try:
                            job.on_success(job.result)
                        except Exception as cb_e:
                            _log(
                                f"Yt+Inst: on_instagram_success: {cb_e!r}"
                            )
                finally:
                    # Сначала будим waiters (YouTube / drain), потом disconnect.
                    # НИКОГДА browser.close() на shared CDP — гасит YouTube-вкладку.
                    job.done.set()
                    signaled = True
                    try:
                        self._q.task_done()
                    except Exception:
                        pass
                    if self._q.empty():
                        self._idle.set()
                    _close_playwright_browser(_browser, shared_cdp=True)
                    _stop_playwright_driver(pw)
            except Exception as e:
                job.error = e
                _log(
                    f"Yt+Inst: Instagram — ошибка (pipeline) "
                    f"profile={self.profile_id!r}. {type(e).__name__}: {e!r}"
                )
                if job.on_error is not None:
                    try:
                        job.on_error(e)
                    except Exception:
                        pass
            finally:
                if not signaled:
                    job.done.set()
                    try:
                        self._q.task_done()
                    except Exception:
                        pass
                    if self._q.empty():
                        self._idle.set()


def _get_yt_inst_ig_pipeline(
    profile_id: str,
    *,
    cdp_endpoints: tuple[str, ...],
    session_login: str = "",
    session_password: str = "",
    session_twofa: str = "",
) -> _YtInstIgPipeline:
    pid = (profile_id or "").strip() or "_unknown"
    with _YT_INST_IG_PIPELINES_GUARD:
        pipe = _YT_INST_IG_PIPELINES.get(pid)
        if pipe is None or not pipe._thread.is_alive():
            pipe = _YtInstIgPipeline(
                pid,
                cdp_endpoints=cdp_endpoints,
                session_login=session_login,
                session_password=session_password,
                session_twofa=session_twofa,
            )
            _YT_INST_IG_PIPELINES[pid] = pipe
        else:
            pipe.update_endpoints(cdp_endpoints)
            if session_login:
                pipe._session_login = session_login
            if session_password:
                pipe._session_password = session_password
            if session_twofa:
                pipe._session_twofa = session_twofa
        return pipe


def drain_yt_inst_ig_pipeline(
    profile_id: str, *, timeout_s: float = 3600.0
) -> None:
    """Дождаться очереди Instagram и остановить pipeline профиля."""
    pid = (profile_id or "").strip()
    if not pid:
        return
    with _YT_INST_IG_PIPELINES_GUARD:
        pipe = _YT_INST_IG_PIPELINES.pop(pid, None)
    if pipe is None:
        return
    try:
        pipe.wait_idle(timeout_s=timeout_s)
    finally:
        pipe.shutdown(timeout_s=min(120.0, max(5.0, float(timeout_s))))


def _yt_inst_ig_pipeline_busy(profile_id: str) -> bool:
    """True, если Instagram ещё заливает / в очереди на этом профиле."""
    pid = (profile_id or "").strip()
    if not pid:
        return False
    with _YT_INST_IG_PIPELINES_GUARD:
        pipe = _YT_INST_IG_PIPELINES.get(pid)
    if pipe is None:
        return False
    try:
        return not pipe._idle.is_set()
    except Exception:
        return False


class _ParallelInstagramUploadRunner:
    """Залив Instagram Reels на отдельном CDP-подключении (параллельно с YouTube)."""

    def __init__(
        self,
        *,
        cdp_endpoints: tuple[str, ...],
        video_items: list[tuple[str, str, str]],
        session_login: str = "",
        session_password: str = "",
        session_twofa: str = "",
        profile_id: str = "",
    ) -> None:
        self._cdp_endpoints = tuple(
            e.strip() for e in cdp_endpoints if (e or "").strip()
        )
        self._video_items = list(video_items)
        self._session_login = session_login
        self._session_password = session_password
        self._session_twofa = session_twofa
        self._profile_id = profile_id
        self._thread: threading.Thread | None = None
        self.result: dict | None = None
        self.error: BaseException | None = None
        self.batch_results: list = []

    def start(self) -> None:
        if not self._cdp_endpoints:
            raise DolphinAntyError("Yt+Inst Instagram: пустой CDP endpoint.")
        if not self._video_items:
            return
        self._thread = threading.Thread(
            target=self._worker,
            name="yt-inst-instagram-upload",
            daemon=True,
        )
        self._thread.start()
        _log("Yt+Inst: фоновый залив Instagram запущен (параллельно с YouTube).")

    def join(self, *, timeout_s: float = 3600.0) -> None:
        th = self._thread
        if th is not None and th.is_alive():
            th.join(timeout=max(1.0, float(timeout_s)))
            if th.is_alive():
                self.error = TimeoutError(
                    f"Instagram upload не завершился за {timeout_s:.0f} с"
                )

    def _wait_for_instagram_page(self, context, *, timeout_s: float = 60.0):
        deadline = time.monotonic() + max(5.0, float(timeout_s))
        while time.monotonic() < deadline:
            pages = _ig_instagram_pages(context)
            if pages:
                return pages[0]
            time.sleep(0.2)
        return _pick_primary_instagram_page(context)

    def _worker(self) -> None:
        from zaliver.instagram_upload.reels_upload import run_instagram_reels_upload

        try:
            with sync_playwright() as p:
                _browser, context, _seed = _playwright_page_from_cdp(
                    p, self._cdp_endpoints
                )
                ig_page = self._wait_for_instagram_page(context)
                if ig_page is None or not _page_still_open(ig_page):
                    raise RuntimeError(
                        "Нет вкладки Instagram для параллельного залива"
                    )
                for idx, (vp, tt, dd) in enumerate(self._video_items, start=1):
                    if not (vp or "").strip():
                        continue
                    _log(
                        f"Yt+Inst: Instagram параллельно "
                        f"{idx}/{len(self._video_items)}…"
                    )
                    one = run_instagram_reels_upload(
                        ig_page,
                        video_path=vp,
                        title=tt,
                        description=dd,
                        session_login=self._session_login,
                        session_password=self._session_password,
                        session_twofa=self._session_twofa,
                        profile_id=self._profile_id or None,
                        top_reels_scan=1,
                        keep_in_background=True,
                    )
                    self.batch_results.append(one)
                if len(self.batch_results) == 1:
                    self.result = self.batch_results[0]
                elif self.batch_results:
                    out = dict(self.batch_results[-1])
                    out["batch_results"] = list(self.batch_results)
                    self.result = out
                _log("Yt+Inst: Instagram — успех (параллельный поток).")
        except Exception as e:
            self.error = e
            _log(
                f"Yt+Inst: Instagram — ошибка (параллельный поток). "
                f"{type(e).__name__}: {e!r}"
            )


def _run_youtube_and_instagram_parallel(
    *,
    browser,
    context,
    page,
    cdp_endpoints: tuple[str, ...],
    zaliver_db_path: Path | None,
    video_path: str | None,
    title: str | None,
    description: str | None,
    publish_before_checks: bool,
    keep_studio_title: bool,
    schedule_publish_at,
    scheduled_batch,
    stats_server_username: str | None,
    studio_kw: dict,
    session_login: str = "",
    session_password: str = "",
    session_twofa: str = "",
    profile_id: str = "",
    wait_for_instagram: bool = True,
    on_youtube_success: Callable[[dict], None] | None = None,
    on_instagram_success: Callable[[dict], None] | None = None,
    on_instagram_error: Callable[[BaseException], None] | None = None,
) -> dict:
    """
    Вкладка 1 — YouTube, вкладка 2 — Instagram (отдельный CDP / pipeline).

    wait_for_instagram=False (pause 0 / keep-open на тот же профиль):
      после успеха YouTube сразу возвращаемся — можно брать следующее видео
      из очереди; Instagram догоняет тем же роликом через pipeline.
    wait_for_instagram=True: ждём Instagram перед возвратом.
    """
    yt_page = _pick_non_instagram_page(context, prefer=page)
    if yt_page is None:
        yt_page = page

    # Сначала Instagram-вкладка — до долгого Studio / channel-appeal.
    try:
        ig_busy = _yt_inst_ig_pipeline_busy(profile_id)
        ig_tab = _ensure_one_instagram_tab(
            context,
            seed_page=yt_page,
            refocus_youtube=not ig_busy,
        )
        try:
            ig_url = (ig_tab.url or "").strip() if ig_tab is not None else ""
        except Exception:
            ig_url = ""
        _log(
            "Yt+Inst: вкладки готовы — 1=YouTube, 2=Instagram "
            f"(url={ig_url!r}, pipeline / параллельный залив)."
        )
    except Exception as e:
        _log(f"Yt+Inst: заранее открыть Instagram не удалось: {e!r}")
        # Не открываем вторую вкладку через new_page, если IG уже есть
        # (гонка createTarget / частичный успех).
        ig_existing = _ig_instagram_pages(context)
        if ig_existing:
            _log(
                "Yt+Inst: Instagram уже есть после ошибки ensure — "
                "новую вкладку не открываем."
            )
            if not _yt_inst_ig_pipeline_busy(profile_id):
                _bring_studio_tab_to_front(yt_page, log_label="Yt+Inst")
        else:
            try:
                from zaliver.instagram_upload.register import (
                    INSTAGRAM_URL,
                    _navigate_page_to,
                )

                ig_tab = context.new_page()
                _navigate_page_to(ig_tab, INSTAGRAM_URL, label="Yt+Inst IG fallback")
                _log("Yt+Inst: Instagram открыт через new_page() fallback.")
                if not _yt_inst_ig_pipeline_busy(profile_id):
                    _bring_studio_tab_to_front(yt_page, log_label="Yt+Inst")
            except Exception as e2:
                _log(
                    f"Yt+Inst: fallback new_page Instagram тоже не удался: {e2!r}"
                )

    ig_items: list[tuple[str, str, str]] = []
    if scheduled_batch:
        for item in scheduled_batch:
            ig_items.append(
                (
                    str(getattr(item, "video_path", "") or ""),
                    str(getattr(item, "title", "") or ""),
                    str(getattr(item, "description", "") or ""),
                )
            )
    elif video_path:
        ig_items.append(
            (
                str(video_path),
                str(title or ""),
                str(description or ""),
            )
        )

    ig_job: _YtInstIgJob | None = None
    yt_done_event = threading.Event()
    if ig_items:
        pipe = _get_yt_inst_ig_pipeline(
            profile_id,
            cdp_endpoints=cdp_endpoints,
            session_login=session_login,
            session_password=session_password,
            session_twofa=session_twofa,
        )
        ig_job = _YtInstIgJob(
            items=ig_items,
            on_success=on_instagram_success,
            on_error=on_instagram_error,
            youtube_done=yt_done_event,
        )
        pipe.enqueue(ig_job)
        _log(
            "Yt+Inst: Instagram поставлен в pipeline "
            f"({len(ig_items)} шт., wait={wait_for_instagram})."
        )

    yt_res = None
    yt_err: BaseException | None = None
    try:
        # Если уже channel-appeal — не уходим в долгий скан каналов / «Создать».
        from zaliver.youtube_upload.studio import (
            YoutubeStudioError as _YoutubeStudioError,
            _studio_channel_removed_page_visible,
            _studio_handle_channel_removed_if_present,
        )

        if _studio_channel_removed_page_visible(yt_page):
            _log(
                "Yt+Inst: YouTube уже на channel-appeal — "
                "быстрая попытка сменить канал, иначе пропускаем YT."
            )
            _studio_handle_channel_removed_if_present(yt_page)
            if _studio_channel_removed_page_visible(yt_page):
                raise _YoutubeStudioError(
                    "YouTube Studio: открыта страница апелляции "
                    "(channel-appeal) — канал удалён или заблокирован."
                )

        _log("Yt+Inst: залив YouTube (вкладка 1)…")
        # Не перехватываем фокус, пока Instagram ещё на /reels/ и т.п. —
        # Studio через CDP работает и в фоне.
        if _yt_inst_ig_pipeline_busy(profile_id):
            _log(
                "Yt+Inst: Instagram ещё в pipeline — "
                "фокус на Studio не переключаем."
            )
        else:
            _bring_studio_tab_to_front(yt_page, log_label="Yt+Inst")
        yt_res = _run_profile_studio_upload(
            page=yt_page,
            browser=browser,
            zaliver_db_path=zaliver_db_path,
            video_path=video_path,
            title=title,
            description=description,
            publish_before_checks=publish_before_checks,
            keep_studio_title=keep_studio_title,
            schedule_publish_at=schedule_publish_at,
            scheduled_batch=scheduled_batch,
            stats_server_username=stats_server_username,
            studio_kw=studio_kw,
        )
        _log("Yt+Inst: YouTube — успех.")
        if on_youtube_success is not None and isinstance(yt_res, dict):
            try:
                on_youtube_success(yt_res)
            except Exception as cb_e:
                _log(f"Yt+Inst: on_youtube_success: {cb_e!r}")
                # Запись/уведомление обязательны — считаем залив YT сорванным.
                yt_err = cb_e
                yt_res = None
    except Exception as e:
        yt_err = e
        _log(
            "Yt+Inst: YouTube — ошибка (Instagram продолжает в pipeline). "
            f"{type(e).__name__}: {e!r}"
        )
        # Не переключаем фокус на IG при ошибке YouTube, если залив YT ещё
        # мог быть в мастере — bring_to_front срывает Studio.
        try:
            _log(
                "Yt+Inst: YouTube ошибка — фокус оставляем как есть "
                "(Instagram продолжает в фоне)."
            )
        except Exception:
            pass
    finally:
        # Разрешаем IG Done /reels/ только после конца YouTube этого ролика.
        yt_done_event.set()
        _log("Yt+Inst: сигнал YouTube готов (для IG Done /reels/).")
    # Не возвращаем фокус на Studio, пока Instagram ещё в pipeline —
    # иначе зависает навигация на /reels/.

    ig_res = None
    ig_err: BaseException | None = None
    instagram_pending = False
    if ig_job is not None:
        # Pause 0 + успех YT: не ждём IG — следующее видео можно брать сразу.
        # Иначе (закрываем браузер / YT ошибка) — дожидаемся текущего IG.
        should_wait = bool(wait_for_instagram) or yt_res is None
        if should_wait:
            if not ig_job.done.wait(timeout=3600.0):
                ig_err = TimeoutError(
                    "Instagram upload не завершился за 3600 с"
                )
            else:
                ig_res = ig_job.result
                ig_err = ig_job.error
                if ig_res is None and ig_err is None:
                    ig_err = RuntimeError(
                        "Instagram: нет результата pipeline-залива"
                    )
        else:
            instagram_pending = True
            _log(
                "Yt+Inst: YouTube готов — не ждём Instagram "
                "(pause 0 / keep-open, IG догонит в pipeline)."
            )

    if yt_res is None and ig_res is None and not instagram_pending:
        parts = []
        if yt_err is not None:
            parts.append(f"YouTube: {yt_err}")
        if ig_err is not None:
            parts.append(f"Instagram: {ig_err}")
        detail = "; ".join(parts) if parts else "нет результата"
        raise CombinedPlatformUploadError(
            f"Yt+Inst: обе площадки не залиты ({detail})",
            youtube_error=yt_err,
            instagram_error=ig_err,
        )

    out: dict = {
        "youtube": yt_res,
        "instagram": ig_res,
        "instagram_pending": instagram_pending,
        "youtube_error": (
            f"{type(yt_err).__name__}: {yt_err}" if yt_err is not None else None
        ),
        "instagram_error": (
            f"{type(ig_err).__name__}: {ig_err}" if ig_err is not None else None
        ),
    }
    if isinstance(yt_res, dict):
        out["video_id"] = yt_res.get("video_id")
        out["url"] = yt_res.get("url")
        if yt_res.get("batch_results") is not None:
            out["batch_results"] = yt_res.get("batch_results")
    elif isinstance(ig_res, dict):
        out["video_id"] = ig_res.get("video_id")
        out["url"] = ig_res.get("url")
    return out


def _run_youtube_then_instagram_session(
    *,
    browser,
    context,
    page,
    cdp_endpoints: tuple[str, ...] = (),
    zaliver_db_path: Path | None,
    video_path: str | None,
    title: str | None,
    description: str | None,
    publish_before_checks: bool,
    keep_studio_title: bool,
    schedule_publish_at,
    scheduled_batch,
    stats_server_username: str | None,
    studio_kw: dict,
    session_login: str = "",
    session_password: str = "",
    session_twofa: str = "",
    profile_id: str = "",
    wait_for_instagram: bool = True,
    on_youtube_success: Callable[[dict], None] | None = None,
    on_instagram_success: Callable[[dict], None] | None = None,
    on_instagram_error: Callable[[BaseException], None] | None = None,
) -> dict:
    """Совместимая обёртка → параллельный / pipeline залив YT+Inst."""
    return _run_youtube_and_instagram_parallel(
        browser=browser,
        context=context,
        page=page,
        cdp_endpoints=cdp_endpoints,
        zaliver_db_path=zaliver_db_path,
        video_path=video_path,
        title=title,
        description=description,
        publish_before_checks=publish_before_checks,
        keep_studio_title=keep_studio_title,
        schedule_publish_at=schedule_publish_at,
        scheduled_batch=scheduled_batch,
        stats_server_username=stats_server_username,
        studio_kw=studio_kw,
        session_login=session_login,
        session_password=session_password,
        session_twofa=session_twofa,
        profile_id=profile_id,
        wait_for_instagram=wait_for_instagram,
        on_youtube_success=on_youtube_success,
        on_instagram_success=on_instagram_success,
        on_instagram_error=on_instagram_error,
    )


def _fallback_instagram_only_upload(
    *,
    profile_id: str,
    video_path: str,
    title: str,
    description: str,
    headless: bool,
    session_login: str,
    session_password: str,
    session_twofa: str,
    local_token: str | None = None,
    base_url: str = "",
    remote_cdp=None,
    own_antidetect: bool = False,
) -> dict:
    """Отдельный запуск профиля только для Instagram (если общая сессия умерла)."""
    if own_antidetect:
        return upload_instagram_reel_in_local_antidetect_profile(
            profile_id,
            video_path=video_path,
            base_url=base_url,
            title=title,
            description=description,
            headless=headless,
            remote_cdp=remote_cdp,
            session_login=session_login,
            session_password=session_password,
            session_twofa=session_twofa,
            keep_browser_open=False,
        )
    return upload_instagram_reel_in_profile(
        profile_id,
        video_path=video_path,
        title=title,
        description=description,
        local_token=local_token,
        headless=headless,
        session_login=session_login,
        session_password=session_password,
        session_twofa=session_twofa,
        keep_browser_open=False,
    )


@with_log_profile
def upload_youtube_and_instagram_in_profile(
    profile_id: str,
    *,
    local_token: str | None = None,
    headless: bool = True,
    zaliver_db_path: Path | None = None,
    video_path: str | None = None,
    title: str | None = None,
    description: str | None = None,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    search_oldest_channel: bool = True,
    publish_before_checks: bool = False,
    keep_studio_title: bool = False,
    schedule_publish_at=None,
    scheduled_batch=None,
    stats_server_username: str | None = None,
    warmup_during_schedule: bool = False,
    warmup_shorts_recommendations: bool = True,
    warmup_search_query: str | None = None,
    warmup_shorts_batch_count: int = 5,
    warmup_like_probability_pct: float = 10.0,
    warmup_subscribe_probability_pct: float = 10.0,
    warmup_shorts_watch_min_s: float = 5.0,
    warmup_shorts_watch_max_s: float = 25.0,
    session_login: str = "",
    session_password: str = "",
    session_twofa: str = "",
    keep_browser_open: bool = False,
    on_youtube_success=None,
    on_instagram_success=None,
    on_instagram_error=None,
) -> dict:
    """
    Dolphin: один профиль, 2 вкладки — YouTube Studio затем Instagram Reels.
    При ошибке одной площадки вторая всё равно заливается.
    keep_browser_open: как у Instagram — не stop_profile, повторный залив
    на тот же профиль переиспользует CDP; YouTube может уйти вперёд,
    Instagram догоняет через pipeline.
    """
    keep_open = bool(keep_browser_open)
    _log(
        "Dolphin: Yt+Inst залив. "
        f"profile_id={profile_id!r}, headless={headless}, "
        f"keep_browser_open={keep_open}, video_path={video_path!r}"
    )
    api = DolphinAntyLocalAPI()
    endpoints: tuple[str, ...] = ()
    try:
        tok = (local_token or "").strip()
        if tok:
            _log("Dolphin: login_with_token…")
            api.login_with_token(tok)

        with _profile_launch_lock(profile_id):
            if keep_open:
                cached = _get_dolphin_keep_open_cdp(profile_id)
                if cached:
                    endpoints = cached
                    _log(
                        "Dolphin: Yt+Inst переиспользуем CDP keep-open "
                        f"profile_id={profile_id!r}, endpoints={endpoints!r}"
                    )
                else:
                    _log("Dolphin: start_profile…")
                    conn = api.start_profile(profile_id, headless=headless)
                    endpoints = (conn.ws_url(), conn.http_url())
                    _cache_dolphin_keep_open_cdp(profile_id, endpoints)
                    _log(
                        "Dolphin: профиль запущен (keep-open). "
                        f"ws_url={conn.ws_url()!r}, http_url={conn.http_url()!r}"
                    )
            else:
                _log("Dolphin: start_profile…")
                conn = api.start_profile(profile_id, headless=headless)
                endpoints = (conn.ws_url(), conn.http_url())
                _log(
                    "Dolphin: профиль запущен. "
                    f"ws_url={conn.ws_url()!r}, http_url={conn.http_url()!r}"
                )

        def _dolphin_yt_inst_job(pw):
            browser = None
            warmup_runner = None
            try:
                browser, context, page = _playwright_page_from_cdp(pw, endpoints)
                warmup_runner = _maybe_start_parallel_shorts_warmup(
                    enabled=warmup_during_schedule,
                    schedule_publish_at=schedule_publish_at,
                    scheduled_batch=scheduled_batch,
                    cdp_endpoints=endpoints,
                    login_credentials=login_credentials,
                    shorts_recommendations=warmup_shorts_recommendations,
                    search_query=warmup_search_query,
                    shorts_batch_count=warmup_shorts_batch_count,
                    like_probability_pct=warmup_like_probability_pct,
                    subscribe_probability_pct=warmup_subscribe_probability_pct,
                    shorts_watch_min_s=warmup_shorts_watch_min_s,
                    shorts_watch_max_s=warmup_shorts_watch_max_s,
                )
                if warmup_runner is not None:
                    _refocus_studio_tab_after_warmup_start(page)
                result = _run_youtube_then_instagram_session(
                    browser=browser,
                    context=context,
                    page=page,
                    cdp_endpoints=endpoints,
                    zaliver_db_path=zaliver_db_path,
                    video_path=video_path,
                    title=title,
                    description=description,
                    publish_before_checks=publish_before_checks,
                    keep_studio_title=keep_studio_title,
                    schedule_publish_at=schedule_publish_at,
                    scheduled_batch=scheduled_batch,
                    stats_server_username=stats_server_username,
                    studio_kw={
                        "profile_id": profile_id,
                        "login_credentials": login_credentials,
                        "yt_oldest_name": yt_oldest_name,
                        "search_oldest_channel": search_oldest_channel,
                    },
                    session_login=session_login,
                    session_password=session_password,
                    session_twofa=session_twofa,
                    profile_id=profile_id,
                    wait_for_instagram=not keep_open,
                    on_youtube_success=on_youtube_success,
                    on_instagram_success=on_instagram_success,
                    on_instagram_error=on_instagram_error,
                )
                if keep_open:
                    _log(
                        "Dolphin: Yt+Inst браузер оставлен открытым "
                        f"(profile_id={profile_id!r}) — следующий залив без stop."
                    )
                return result
            finally:
                _stop_parallel_shorts_warmup(warmup_runner)
                # keep-open + живой IG pipeline: browser.close() на shared CDP
                # гасит весь Chrome (YouTube). Отключаемся через pw.stop() в wrapper.
                if not keep_open:
                    _close_playwright_browser(browser, shared_cdp=True)

        return _with_sync_playwright(
            _dolphin_yt_inst_job,
            label=f"Yt+Inst-dolphin-{profile_id[:8]}",
            release_before_stop=keep_open,
        )
    except CombinedPlatformUploadError as e:
        if (video_path or "").strip():
            _log("Yt+Inst: fallback — отдельный залив Instagram…")
            try:
                try:
                    api.stop_profile(profile_id)
                except Exception:
                    pass
                clear_dolphin_keep_open_cdp(profile_id)
                ig_only = _fallback_instagram_only_upload(
                    profile_id=profile_id,
                    video_path=str(video_path),
                    title=str(title or ""),
                    description=str(description or ""),
                    headless=headless,
                    session_login=session_login,
                    session_password=session_password,
                    session_twofa=session_twofa,
                    local_token=local_token,
                    own_antidetect=False,
                )
                return {
                    "youtube": None,
                    "instagram": ig_only,
                    "youtube_error": (
                        f"{type(e.youtube_error).__name__}: {e.youtube_error}"
                        if e.youtube_error is not None
                        else str(e)
                    ),
                    "instagram_error": None,
                }
            except Exception as ie:
                raise CombinedPlatformUploadError(
                    f"Yt+Inst: обе площадки не залиты "
                    f"(YouTube: {e.youtube_error}; Instagram: {ie})",
                    youtube_error=e.youtube_error,
                    instagram_error=ie,
                ) from ie
        raise
    except YoutubeAllChannelsRemovedError:
        raise
    except Exception as e:
        _log(f"Yt+Inst ошибка: {type(e).__name__}: {e!r}")
        if (video_path or "").strip():
            _log("Yt+Inst: после сбоя сессии — fallback Instagram…")
            try:
                try:
                    api.stop_profile(profile_id)
                except Exception:
                    pass
                clear_dolphin_keep_open_cdp(profile_id)
                ig_res = _fallback_instagram_only_upload(
                    profile_id=profile_id,
                    video_path=str(video_path),
                    title=str(title or ""),
                    description=str(description or ""),
                    headless=headless,
                    session_login=session_login,
                    session_password=session_password,
                    session_twofa=session_twofa,
                    local_token=local_token,
                    own_antidetect=False,
                )
                return {
                    "youtube": None,
                    "instagram": ig_res,
                    "youtube_error": f"{type(e).__name__}: {e}",
                    "instagram_error": None,
                }
            except Exception as ie:
                raise CombinedPlatformUploadError(
                    f"Yt+Inst: обе площадки не залиты "
                    f"(YouTube: {e}; Instagram: {ie})",
                    youtube_error=e,
                    instagram_error=ie,
                ) from ie
        raise _wrap_exc(e) from e
    finally:
        if keep_open:
            _log(
                "Dolphin: stop_profile пропущен (keep_browser_open) "
                f"profile_id={profile_id!r}."
            )
        else:
            try:
                drain_yt_inst_ig_pipeline(profile_id)
            except Exception as de:
                _log(f"Dolphin: Yt+Inst drain IG: {de!r}")
            clear_dolphin_keep_open_cdp(profile_id)
            try:
                api.stop_profile(profile_id)
            except Exception as se:
                _log(f"Dolphin: stop_profile: {se!r}")
        api.close()


@with_log_profile
def upload_youtube_and_instagram_in_local_antidetect_profile(
    profile_id: str,
    *,
    base_url: str,
    headless: bool = True,
    zaliver_db_path: Path | None = None,
    video_path: str | None = None,
    title: str | None = None,
    description: str | None = None,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    search_oldest_channel: bool = True,
    remote_cdp=None,
    publish_before_checks: bool = False,
    keep_studio_title: bool = False,
    schedule_publish_at=None,
    scheduled_batch=None,
    stats_server_username: str | None = None,
    warmup_during_schedule: bool = False,
    warmup_shorts_recommendations: bool = True,
    warmup_search_query: str | None = None,
    warmup_shorts_batch_count: int = 5,
    warmup_like_probability_pct: float = 10.0,
    warmup_subscribe_probability_pct: float = 10.0,
    warmup_shorts_watch_min_s: float = 5.0,
    warmup_shorts_watch_max_s: float = 25.0,
    session_login: str = "",
    session_password: str = "",
    session_twofa: str = "",
    keep_browser_open: bool = False,
    on_youtube_success=None,
    on_instagram_success=None,
    on_instagram_error=None,
) -> dict:
    """
    Локальный антидетект: один профиль, 2 вкладки — YouTube затем Instagram.
    keep_browser_open: как у Instagram — сессия не stop'ится между заливами
    на тот же профиль; YouTube может уйти вперёд, Instagram догоняет.
    """
    from zaliver.antydetect.local_antidetect_api import (
        LocalAntidetectError,
        LocalAntidetectHttpAPI,
    )
    from zaliver.antydetect.local_active_sessions import (
        register_local_session,
        unregister_local_session,
    )

    keep_open = bool(keep_browser_open)
    _log(
        "Local antidetect: Yt+Inst залив. "
        f"profile_id={profile_id!r}, base_url={base_url!r}, headless={headless}, "
        f"keep_browser_open={keep_open}, video_path={video_path!r}"
    )
    api = LocalAntidetectHttpAPI(base_url)
    session_id: str | None = None
    started_at = time.perf_counter()
    login = (session_login or "").strip()
    pwd = (session_password or "").strip()
    twofa = (session_twofa or "").strip()
    bu = (base_url or "").strip() or "http://127.0.0.1:18765"
    try:
        if not pwd or not login:
            try:
                prof = api.get_profile(profile_id)
                loaded_login, loaded_pwd, loaded_twofa = (
                    _instagram_session_creds_from_profile_dict(prof)
                )
                if not login:
                    login = loaded_login
                if not pwd:
                    pwd = loaded_pwd
                if not twofa:
                    twofa = loaded_twofa
            except Exception as e:
                _log(f"Local antidetect: custom_data Instagram: {e!r}")

        ws_url = ""
        with _profile_launch_lock(profile_id):
            if keep_open:
                meta = _ig_meta_get(profile_id)
                if meta is None:
                    meta = {
                        "tabs_ready": threading.Event(),
                        "preopened": False,
                        "session_id": None,
                        "ws_url": None,
                    }
                    _ig_meta_set(profile_id, meta)
                ws_url = (meta.get("ws_url") or "").strip()
                session_id = (meta.get("session_id") or "").strip() or None
                if not ws_url or not session_id:
                    ws_existing, sid_existing, _msg = (
                        api.resolve_running_cdp_ws_url_for_profile(profile_id)
                    )
                    if (
                        isinstance(ws_existing, str)
                        and ws_existing.strip()
                        and isinstance(sid_existing, str)
                        and sid_existing.strip()
                    ):
                        session_id = sid_existing.strip()
                        ws_url = ws_existing.strip()
                        _log(
                            "Local antidetect: Yt+Inst переиспользуем сессию "
                            f"session_id={session_id!r}, cdp_ws_url={ws_url!r}"
                        )
                    else:
                        acc = api.launch_profile(
                            profile_id,
                            headless=headless,
                            expose_cdp=True,
                            remote_cdp=remote_cdp,
                        )
                        sid = acc.get("session_id")
                        if not isinstance(sid, str) or not sid.strip():
                            raise LocalAntidetectError(
                                f"Нет session_id в ответе launch: {acc!r}"
                            )
                        session_id = sid.strip()
                        ws_url = api.wait_for_cdp_ws_url(
                            session_id, timeout_s=120.0
                        )
                        _log(
                            "Local antidetect: профиль запущен (keep-open). "
                            f"cdp_ws_url={ws_url!r}"
                        )
                    meta["session_id"] = session_id
                    meta["ws_url"] = ws_url
                    register_local_session(
                        profile_id=profile_id, base_url=bu, session_id=session_id
                    )
                else:
                    _log(
                        "Local antidetect: Yt+Inst CDP keep-open из meta "
                        f"session_id={session_id!r}, cdp_ws_url={ws_url!r}"
                    )
            else:
                acc = api.launch_profile(
                    profile_id,
                    headless=headless,
                    expose_cdp=True,
                    remote_cdp=remote_cdp,
                )
                sid = acc.get("session_id")
                if not isinstance(sid, str) or not sid.strip():
                    raise LocalAntidetectError(
                        f"Нет session_id в ответе launch: {acc!r}"
                    )
                session_id = sid.strip()
                register_local_session(
                    profile_id=profile_id, base_url=bu, session_id=session_id
                )
                ws_url = api.wait_for_cdp_ws_url(session_id, timeout_s=120.0)
                _log(f"Local antidetect: cdp_ws_url={ws_url!r}")

        try:
            def _local_yt_inst_job(pw):
                browser = None
                warmup_runner = None
                try:
                    browser, context, page = _playwright_page_from_local_session_cdp(
                        pw, api, session_id, ws_url
                    )
                    warmup_runner = _maybe_start_parallel_shorts_warmup(
                        enabled=warmup_during_schedule,
                        schedule_publish_at=schedule_publish_at,
                        scheduled_batch=scheduled_batch,
                        cdp_endpoints=(ws_url,),
                        login_credentials=login_credentials,
                        shorts_recommendations=warmup_shorts_recommendations,
                        search_query=warmup_search_query,
                        shorts_batch_count=warmup_shorts_batch_count,
                        like_probability_pct=warmup_like_probability_pct,
                        subscribe_probability_pct=warmup_subscribe_probability_pct,
                        shorts_watch_min_s=warmup_shorts_watch_min_s,
                        shorts_watch_max_s=warmup_shorts_watch_max_s,
                    )
                    if warmup_runner is not None:
                        _refocus_studio_tab_after_warmup_start(page)
                    studio_kw = _local_studio_workflow_kwargs(
                        api,
                        profile_id,
                        login_credentials=login_credentials,
                        yt_oldest_name=yt_oldest_name,
                        search_oldest_channel=search_oldest_channel,
                    )
                    result = _run_youtube_then_instagram_session(
                        browser=browser,
                        context=context,
                        page=page,
                        cdp_endpoints=(ws_url,),
                        zaliver_db_path=zaliver_db_path,
                        video_path=video_path,
                        title=title,
                        description=description,
                        publish_before_checks=publish_before_checks,
                        keep_studio_title=keep_studio_title,
                        schedule_publish_at=schedule_publish_at,
                        scheduled_batch=scheduled_batch,
                        stats_server_username=stats_server_username,
                        studio_kw=studio_kw,
                        session_login=login,
                        session_password=pwd,
                        session_twofa=twofa,
                        profile_id=profile_id,
                        wait_for_instagram=not keep_open,
                        on_youtube_success=on_youtube_success,
                        on_instagram_success=on_instagram_success,
                        on_instagram_error=on_instagram_error,
                    )
                    if keep_open:
                        _log(
                            "Local antidetect: Yt+Inst браузер оставлен открытым "
                            f"(profile_id={profile_id!r}) — следующий залив без stop."
                        )
                    return result
                except YoutubeAllChannelsRemovedError as e:
                    _log(
                        "Local antidetect: все каналы удалены — "
                        "Instagram попробуем после остановки сессии."
                    )
                    raise CombinedPlatformUploadError(
                        str(e),
                        youtube_error=e,
                        instagram_error=RuntimeError(
                            "браузер закрыт после YouTube"
                        ),
                    ) from e
                finally:
                    _stop_parallel_shorts_warmup(warmup_runner)
                    if not keep_open:
                        _close_playwright_browser(browser, shared_cdp=True)

            return _with_sync_playwright(
                _local_yt_inst_job,
                label=f"Yt+Inst-local-{profile_id[:8]}",
                release_before_stop=keep_open,
            )
        finally:
            if not keep_open:
                unregister_local_session(profile_id=profile_id)
    except CombinedPlatformUploadError as e:
        if (video_path or "").strip():
            _log("Yt+Inst: fallback — отдельный залив Instagram…")
            if session_id:
                try:
                    api.stop_session(session_id)
                except Exception:
                    pass
                session_id = None
            close_instagram_keep_open_hub(profile_id)
            try:
                ig_only = _fallback_instagram_only_upload(
                    profile_id=profile_id,
                    video_path=str(video_path),
                    title=str(title or ""),
                    description=str(description or ""),
                    headless=headless,
                    session_login=login,
                    session_password=pwd,
                    session_twofa=twofa,
                    base_url=bu,
                    remote_cdp=remote_cdp,
                    own_antidetect=True,
                )
                return {
                    "youtube": None,
                    "instagram": ig_only,
                    "youtube_error": (
                        f"{type(e.youtube_error).__name__}: {e.youtube_error}"
                        if e.youtube_error is not None
                        else str(e)
                    ),
                    "instagram_error": None,
                }
            except Exception as ie:
                raise CombinedPlatformUploadError(
                    f"Yt+Inst: обе площадки не залиты "
                    f"(YouTube: {e.youtube_error}; Instagram: {ie})",
                    youtube_error=e.youtube_error,
                    instagram_error=ie,
                ) from ie
        raise
    except Exception as e:
        _log(f"Yt+Inst ошибка: {type(e).__name__}: {e!r}")
        raise LocalAntidetectError(f"Yt+Inst: ошибка профиля: {e}") from e
    finally:
        if keep_open:
            _log(
                "Local antidetect: stop_session пропущен (keep_browser_open) "
                f"profile_id={profile_id!r}."
            )
        else:
            try:
                drain_yt_inst_ig_pipeline(profile_id)
            except Exception as de:
                _log(f"Local antidetect: Yt+Inst drain IG: {de!r}")
            if session_id:
                try:
                    api.stop_session(session_id)
                except Exception:
                    pass
        try:
            _log(
                f"Local antidetect: Yt+Inst завершение. "
                f"elapsed_s={time.perf_counter() - started_at:.3f}"
            )
        except Exception:
            pass
        api.close()


@with_log_profile
def open_google_in_profile(
    profile_id: str,
    *,
    local_token: str | None = None,
    headless: bool = True,
    upload_latest_zaliver_video: bool = True,
    zaliver_db_path: Path | None = None,
    video_path: str | None = None,
    title: str | None = None,
    description: str | None = None,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    search_oldest_channel: bool = True,
    publish_before_checks: bool = False,
    keep_studio_title: bool = False,
    schedule_publish_at=None,
    scheduled_batch=None,
    stats_server_username: str | None = None,
    warmup_during_schedule: bool = False,
    warmup_shorts_recommendations: bool = True,
    warmup_search_query: str | None = None,
    warmup_shorts_batch_count: int = 5,
    warmup_like_probability_pct: float = 10.0,
    warmup_subscribe_probability_pct: float = 10.0,
    warmup_shorts_watch_min_s: float = 5.0,
    warmup_shorts_watch_max_s: float = 25.0,
) -> dict | None:
    """
    Запуск профиля через Dolphin Local API + Playwright CDP.

    Важно: логин Google должен быть уже в профиле антидетекта.
    """
    _log(
        "Dolphin: старт. "
        f"profile_id={profile_id!r}, headless={headless}, "
        f"upload_latest_zaliver_video={upload_latest_zaliver_video}, "
        f"local_token={'<set>' if (local_token or '').strip() else None}"
    )
    api = DolphinAntyLocalAPI()
    try:
        tok = (local_token or "").strip()
        if tok:
            _log("Dolphin: login_with_token…")
            api.login_with_token(tok)

        _log("Dolphin: start_profile…")
        conn = api.start_profile(profile_id, headless=headless)
        _log(
            "Dolphin: профиль запущен. "
            f"ws_url={conn.ws_url()!r}, http_url={conn.http_url()!r}"
        )

        with sync_playwright() as p:
            browser, context, page = _playwright_page_from_cdp(
                p, (conn.ws_url(), conn.http_url())
            )

            warmup_runner = _maybe_start_parallel_shorts_warmup(
                enabled=warmup_during_schedule,
                schedule_publish_at=schedule_publish_at,
                scheduled_batch=scheduled_batch,
                cdp_endpoints=(conn.ws_url(), conn.http_url()),
                login_credentials=login_credentials,
                shorts_recommendations=warmup_shorts_recommendations,
                search_query=warmup_search_query,
                shorts_batch_count=warmup_shorts_batch_count,
                like_probability_pct=warmup_like_probability_pct,
                subscribe_probability_pct=warmup_subscribe_probability_pct,
                shorts_watch_min_s=warmup_shorts_watch_min_s,
                shorts_watch_max_s=warmup_shorts_watch_max_s,
            )
            if warmup_runner is not None:
                _refocus_studio_tab_after_warmup_start(page)
            try:
                if upload_latest_zaliver_video:
                    res = _run_profile_studio_upload(
                        page=page,
                        browser=browser,
                        zaliver_db_path=zaliver_db_path,
                        video_path=video_path,
                        title=title,
                        description=description,
                        publish_before_checks=publish_before_checks,
                        keep_studio_title=keep_studio_title,
                        schedule_publish_at=schedule_publish_at,
                        scheduled_batch=scheduled_batch,
                        stats_server_username=stats_server_username,
                        studio_kw={
                            "profile_id": profile_id,
                            "login_credentials": login_credentials,
                            "yt_oldest_name": yt_oldest_name,
                            "search_oldest_channel": search_oldest_channel,
                        },
                    )
                    return res
                else:
                    # Ничего не делаем — открываем Studio напрямую.
                    _studio._studio_warmup_youtube_then_studio(
                        page, login_credentials=login_credentials
                    )
                    time.sleep(1)
            except YoutubeAllChannelsRemovedError as e:
                _log("Dolphin: все каналы удалены — закрываем профиль.")
                _close_playwright_browser(browser)
                try:
                    api.stop_profile(profile_id)
                except Exception as se:
                    _log(f"Dolphin: stop_profile: {se!r}")
                raise _wrap_exc(e) from e
            finally:
                _stop_parallel_shorts_warmup(warmup_runner)

            _close_playwright_browser(browser)
        return None
    except YoutubeAllChannelsRemovedError:
        raise
    except Exception as e:
        _log(f"Ошибка: {type(e).__name__}: {e!r}")
        raise _wrap_exc(e) from e
    finally:
        api.close()


@with_log_profile
def open_google_in_local_antidetect_profile(
    profile_id: str,
    *,
    base_url: str,
    headless: bool = True,
    upload_latest_zaliver_video: bool = True,
    zaliver_db_path: Path | None = None,
    video_path: str | None = None,
    title: str | None = None,
    description: str | None = None,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    search_oldest_channel: bool = True,
    remote_cdp=None,
    publish_before_checks: bool = False,
    keep_studio_title: bool = False,
    schedule_publish_at=None,
    scheduled_batch=None,
    stats_server_username: str | None = None,
    warmup_during_schedule: bool = False,
    warmup_shorts_recommendations: bool = True,
    warmup_search_query: str | None = None,
    warmup_shorts_batch_count: int = 5,
    warmup_like_probability_pct: float = 10.0,
    warmup_subscribe_probability_pct: float = 10.0,
    warmup_shorts_watch_min_s: float = 5.0,
    warmup_shorts_watch_max_s: float = 25.0,
) -> dict | None:
    """
    Запуск профиля через локальный HTTP API (см. OpenAPI антидетекта: launch + опрос сессии на cdp_ws_url),
    затем тот же сценарий YouTube Studio.
    """
    _log(
        "Local antidetect: вход в функцию. "
        f"profile_id={profile_id!r}, base_url={base_url!r}, headless={headless}, "
        f"upload_latest_zaliver_video={upload_latest_zaliver_video}"
    )
    try:
        from zaliver.antydetect.local_antidetect_api import (
            LocalAntidetectError,
            LocalAntidetectHttpAPI,
        )
    except Exception as e:
        _log(f"Local antidetect: import local_antidetect_api failed: {type(e).__name__}: {e!r}")
        raise

    api = LocalAntidetectHttpAPI(base_url)
    session_id: str | None = None
    try:
        started_at = time.perf_counter()
        _log("Local antidetect: клиент создан, запускаем профиль…")
        acc = api.launch_profile(
            profile_id, headless=headless, expose_cdp=True, remote_cdp=remote_cdp
        )
        _log(f"Local antidetect: launch_profile ответ: {acc!r}")
        sid = acc.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            raise LocalAntidetectError(f"Нет session_id в ответе launch: {acc!r}")
        session_id = sid.strip()
        _log(f"Local antidetect: session_id={session_id!r}. Ждём cdp_ws_url…")
        from zaliver.antydetect.local_active_sessions import (
            register_local_session,
            unregister_local_session,
        )

        bu = (base_url or "").strip() or "http://127.0.0.1:18765"
        register_local_session(profile_id=profile_id, base_url=bu, session_id=session_id)
        try:
            ws_url = api.wait_for_cdp_ws_url(session_id, timeout_s=120.0)
            _log(f"Local antidetect: получен cdp_ws_url: {ws_url!r}")

            with sync_playwright() as p:
                browser, context, page = _playwright_page_from_local_session_cdp(
                    p, api, session_id, ws_url
                )

                warmup_runner = _maybe_start_parallel_shorts_warmup(
                    enabled=warmup_during_schedule,
                    schedule_publish_at=schedule_publish_at,
                    scheduled_batch=scheduled_batch,
                    cdp_endpoints=(ws_url,),
                    login_credentials=login_credentials,
                    shorts_recommendations=warmup_shorts_recommendations,
                    search_query=warmup_search_query,
                    shorts_batch_count=warmup_shorts_batch_count,
                    like_probability_pct=warmup_like_probability_pct,
                    subscribe_probability_pct=warmup_subscribe_probability_pct,
                    shorts_watch_min_s=warmup_shorts_watch_min_s,
                    shorts_watch_max_s=warmup_shorts_watch_max_s,
                )
                if warmup_runner is not None:
                    _refocus_studio_tab_after_warmup_start(page)
                try:
                    if upload_latest_zaliver_video:
                        _log(
                            "Studio upload: запуск сценария загрузки. "
                            f"zaliver_db_path={str(zaliver_db_path) if zaliver_db_path else None!r}, "
                            f"video_path={video_path!r}, title={'<set>' if title else None}, "
                            f"description={'<set>' if description else None}"
                        )

                        studio_kw = _local_studio_workflow_kwargs(
                            api,
                            profile_id,
                            login_credentials=login_credentials,
                            yt_oldest_name=yt_oldest_name,
                            search_oldest_channel=search_oldest_channel,
                        )

                        res = _run_profile_studio_upload(
                            page=page,
                            browser=browser,
                            zaliver_db_path=zaliver_db_path,
                            video_path=video_path,
                            title=title,
                            description=description,
                            publish_before_checks=publish_before_checks,
                            keep_studio_title=keep_studio_title,
                            schedule_publish_at=schedule_publish_at,
                            scheduled_batch=scheduled_batch,
                            stats_server_username=stats_server_username,
                            studio_kw=studio_kw,
                        )
                        _log("Studio upload: сценарий завершён.")
                        return res
                    else:
                        _log("Studio: upload_latest_zaliver_video=False → открываем Studio…")
                        _studio._studio_warmup_youtube_then_studio(
                            page, login_credentials=login_credentials
                        )
                        time.sleep(1)
                        _log(f"Studio: открыт URL: {page.url!r}")
                except YoutubeAllChannelsRemovedError as e:
                    _log("Local antidetect: все каналы удалены — закрываем профиль.")
                    _close_playwright_browser(browser)
                    raise LocalAntidetectError(str(e)) from e
                finally:
                    _stop_parallel_shorts_warmup(warmup_runner)

                if not upload_latest_zaliver_video:
                    _close_playwright_browser(browser)
            return None
        finally:
            unregister_local_session(profile_id=profile_id)
    except Exception as e:
        _log(f"Ошибка: {type(e).__name__}: {e!r}")
        raise LocalAntidetectError(f"Ошибка открытия профиля локального антика: {e}")
    finally:
        if session_id:
            try:
                _log(f"Local antidetect: останавливаем сессию {session_id!r}…")
                api.stop_session(session_id)
                _log("Local antidetect: сессия остановлена.")
            except Exception:
                pass
        try:
            elapsed_s = time.perf_counter() - started_at
            _log(f"Local antidetect: завершение. elapsed_s={elapsed_s:.3f}")
        except Exception:
            pass
        api.close()


@with_log_profile
def farm_cookies_in_profile(
    profile_id: str,
    *,
    local_token: str | None = None,
    headless: bool = True,
    domains: list[str],
    sites_count: int,
    watch_min_s: float,
    watch_max_s: float,
) -> None:
    """Dolphin → последовательный обход сайтов с прокруткой для фарма Cookie."""
    _log(
        "Dolphin: фарм Cookie. "
        f"profile_id={profile_id!r}, headless={headless}, sites={sites_count}"
    )
    api = DolphinAntyLocalAPI()
    try:
        tok = (local_token or "").strip()
        if tok:
            _log("Dolphin: login_with_token…")
            api.login_with_token(tok)
        _log("Dolphin: start_profile…")
        conn = api.start_profile(profile_id, headless=headless)
        _log(
            "Dolphin: профиль запущен. "
            f"ws_url={conn.ws_url()!r}, http_url={conn.http_url()!r}"
        )
        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_cdp(
                p, (conn.ws_url(), conn.http_url())
            )
            try:
                run_cookie_farm(
                    page,
                    domains=domains,
                    sites_count=sites_count,
                    watch_min_s=watch_min_s,
                    watch_max_s=watch_max_s,
                )
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        _log(f"Ошибка фарма Cookie: {type(e).__name__}: {e!r}")
        raise _wrap_exc(e) from e
    finally:
        try:
            api.stop_profile(profile_id)
        except Exception as e:
            _log(f"Dolphin: stop_profile: {e!r}")
        api.close()


@with_log_profile
def farm_cookies_in_local_antidetect_profile(
    profile_id: str,
    *,
    base_url: str,
    headless: bool = True,
    domains: list[str],
    sites_count: int,
    watch_min_s: float,
    watch_max_s: float,
    remote_cdp=None,
) -> None:
    """Локальный антидетект → обход сайтов с прокруткой для фарма Cookie."""
    _log(
        "Local antidetect: фарм Cookie. "
        f"profile_id={profile_id!r}, base_url={base_url!r}, headless={headless}, "
        f"sites={sites_count}"
    )
    from zaliver.antydetect.local_antidetect_api import (
        LocalAntidetectError,
        LocalAntidetectHttpAPI,
    )
    from zaliver.antydetect.local_active_sessions import (
        register_local_session,
        unregister_local_session,
    )

    api = LocalAntidetectHttpAPI(base_url)
    session_id: str | None = None
    started_at = time.perf_counter()
    try:
        acc = api.launch_profile(
            profile_id, headless=headless, expose_cdp=True, remote_cdp=remote_cdp
        )
        sid = acc.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            raise LocalAntidetectError(f"Нет session_id в ответе launch: {acc!r}")
        session_id = sid.strip()
        bu = (base_url or "").strip() or "http://127.0.0.1:18765"
        register_local_session(profile_id=profile_id, base_url=bu, session_id=session_id)
        ws_url = api.wait_for_cdp_ws_url(session_id, timeout_s=120.0)
        _log(f"Local antidetect: cdp_ws_url={ws_url!r}")
        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_local_session_cdp(
                p, api, session_id, ws_url
            )
            try:
                run_cookie_farm(
                    page,
                    domains=domains,
                    sites_count=sites_count,
                    watch_min_s=watch_min_s,
                    watch_max_s=watch_max_s,
                )
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        _log(f"Ошибка фарма Cookie: {type(e).__name__}: {e!r}")
        raise LocalAntidetectError(f"Ошибка фарма Cookie: {e}") from e
    finally:
        if session_id:
            unregister_local_session(profile_id=profile_id)
            try:
                api.stop_session(session_id)
            except Exception:
                pass
        try:
            _log(
                "Local antidetect: фарм Cookie завершён за "
                f"{time.perf_counter() - started_at:.1f} с."
            )
        except Exception:
            pass
        api.close()

