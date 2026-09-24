"""TikTok browser workflows — copy of Instagram wrappers; logic not rewritten yet."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from patchright.sync_api import sync_playwright

from zaliver.antydetect.api import DolphinAntyError, DolphinAntyLocalAPI
from zaliver.antydetect.antic_open import (
    _cache_dolphin_keep_open_cdp,
    _get_dolphin_keep_open_cdp,
    _log,
    _playwright_page_from_cdp,
    _playwright_page_from_local_session_cdp,
    _profile_launch_lock,
    _try_enlarge_browser_os_window,
    _wrap_exc,
    clear_dolphin_keep_open_cdp,
)
from zaliver.log_format import with_log_profile
from zaliver.tiktok_upload.reels_upload import (
    DEFAULT_TIKTOK_CROP_ASPECT,
    normalize_tiktok_crop_aspect,
)
from zaliver.youtube_upload.studio import PromotionTargetVideo

_TT_KEEP_OPEN_META: dict[str, dict] = {}
_TT_KEEP_OPEN_META_GUARD = threading.Lock()

def _tt_meta_get(profile_id: str) -> dict | None:
    pid = (profile_id or "").strip()
    if not pid:
        return None
    with _TT_KEEP_OPEN_META_GUARD:
        return _TT_KEEP_OPEN_META.get(pid)


def _tt_meta_set(profile_id: str, meta: dict) -> None:
    pid = (profile_id or "").strip()
    if not pid:
        return
    with _TT_KEEP_OPEN_META_GUARD:
        _TT_KEEP_OPEN_META[pid] = meta


def close_tiktok_keep_open_hub(profile_id: str) -> None:
    """Сбросить keep-open метаданные профиля (браузер гасит вызывающий код)."""
    pid = (profile_id or "").strip()
    if not pid:
        return
    with _TT_KEEP_OPEN_META_GUARD:
        meta = _TT_KEEP_OPEN_META.pop(pid, None)
    if not meta:
        return
    ready = meta.get("tabs_ready")
    if isinstance(ready, threading.Event):
        ready.set()
    _log(f"TikToks: keep-open meta сброшена profile_id={pid!r}.")


def _tt_hub_page_alive(page) -> bool:
    if page is None:
        return False
    try:
        return not page.is_closed()
    except Exception:
        return False


def _tt_hub_navigate_home_quick(page) -> None:
    """Быстро открыть главную на новой вкладке (без полной verify-сессии)."""
    try:
        from zaliver.tiktok_upload.register import TIKTOK_URL

        page.goto(TIKTOK_URL, wait_until="domcontentloaded", timeout=45_000)
    except Exception as e:
        _log(f"TikToks: goto главной на новой вкладке: {e!r}")
    # Уникальная метка вкладки (для claim между connect'ами).
    try:
        _tt_page_target_id(page)
    except Exception:
        pass


def _tt_alive_context_pages(context) -> list:
    pages: list = []
    for pg in list(getattr(context, "pages", None) or []):
        if _tt_hub_page_alive(pg):
            pages.append(pg)
    return pages


def _tt_page_url_lower(page) -> str:
    try:
        return (page.url or "").strip().lower()
    except Exception:
        return ""


def _tt_tiktok_pages(context) -> list:
    """Только вкладки TikTok — служебные Dolphin/chrome в лимит не считаем."""
    out: list = []
    for pg in _tt_alive_context_pages(context):
        if "tiktok.com" in _tt_page_url_lower(pg):
            out.append(pg)
    return out


def _tt_reusable_blank_pages(context) -> list:
    """about:blank / пустой URL — можно превратить в IG вместо new_page."""
    out: list = []
    for pg in _tt_alive_context_pages(context):
        url = _tt_page_url_lower(pg)
        if url in ("about:blank", "about:srcdoc", ""):
            out.append(pg)
    return out


def _tt_new_page_background(context, *, seed_page=None, url: str = "about:blank"):
    """
    Новая вкладка БЕЗ переключения на неё (CDP Target.createTarget background=true).
    Fallback: context.new_page() — в Chrome обычно активирует вкладку.
    Если createTarget создал target, но Playwright его не увидел — закрываем orphan,
    иначе остаётся лишняя about:blank рядом с вкладкой от new_page().
    """
    seed = seed_page
    if seed is None:
        alive = _tt_alive_context_pages(context)
        seed = alive[0] if alive else None
    if seed is None:
        return context.new_page()

    # Уже есть TikTok — вторую не открываем.
    existing_ig = _tt_tiktok_pages(context)
    if existing_ig:
        return existing_ig[0]

    before_ids = {id(p) for p in _tt_alive_context_pages(context)}
    want_url = (url or "about:blank").strip() or "about:blank"
    cdp = None
    target_id: str | None = None

    def _find_new_page():
        for p in _tt_alive_context_pages(context):
            if id(p) not in before_ids:
                return p
        return None

    def _close_orphan_target(session, tid: str | None) -> None:
        if session is None or not tid:
            return
        try:
            session.send("Target.closeTarget", {"targetId": tid})
            _log(
                "TikToks: закрыт orphan createTarget "
                f"(targetId={tid!r})."
            )
        except Exception as e:
            _log(
                f"TikToks: не удалось закрыть orphan createTarget: {e!r}"
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
        # НЕ закрываем targetId (это и была живая вкладка TikTok).
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            found = _find_new_page()
            if found is not None:
                _log("TikToks: вкладка открыта в фоне (CDP background).")
                return found
            ig_now = _tt_tiktok_pages(context)
            if ig_now:
                return ig_now[0]
            time.sleep(0.1)

        _log(
            "TikToks: CDP createTarget ещё не в context.pages "
            f"(targetId={target_id!r}) — fallback new_page() без closeTarget."
        )
        # closeTarget здесь нельзя: гасит единственную IG-вкладку, потом
        # pipeline видит только Studio и browser.close() убивал YouTube.
        found = _find_new_page()
        if found is not None:
            return found
        ig_now = _tt_tiktok_pages(context)
        if ig_now:
            return ig_now[0]
    except Exception as e:
        _log(
            f"TikToks: фоновое createTarget не удалось ({e!r}) — "
            "fallback new_page() только если IG ещё нет."
        )
        found = _find_new_page()
        if found is not None:
            return found
        ig_now = _tt_tiktok_pages(context)
        if ig_now:
            return ig_now[0]
    finally:
        if cdp is not None:
            try:
                cdp.detach()
            except Exception:
                pass

    ig_now = _tt_tiktok_pages(context)
    if ig_now:
        return ig_now[0]
    return context.new_page()


def _tt_preopen_sibling_tabs(context, tabs_per_profile: int, meta: dict | None) -> None:
    """
    После «Новая публикация» добрать вкладки до лимита tabs_per_profile.
    Лимит считаем только по TikTok-вкладкам (не по всем context.pages:
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

    ig_have = len(_tt_tiktok_pages(context))
    all_have = len(_tt_alive_context_pages(context))
    if meta is not None and meta.get("preopened") and ig_have >= want:
        ready = meta.get("tabs_ready")
        if isinstance(ready, threading.Event):
            ready.set()
        _log(
            f"TikToks: вкладки уже готовы "
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
            f"TikToks: лимит IG-вкладок уже достигнут "
            f"(ig={ig_have}/{want}, all={all_have}) — new_page пропущен."
        )
        return

    alive = _tt_alive_context_pages(context)
    seed = (_tt_tiktok_pages(context) or alive or [None])[0]
    from zaliver.tiktok_upload.register import TIKTOK_URL

    blanks = _tt_reusable_blank_pages(context)
    # Primary IG не трогаем как blank (её нет в blanks).
    opened = 0
    _log(
        f"TikToks: preopen — нужно ещё {need} "
        f"(сейчас ig={ig_have}/{want}, all={all_have}, blank={len(blanks)})."
    )
    for _ in range(need):
        ig_now = len(_tt_tiktok_pages(context))
        if ig_now >= want:
            break
        page = None
        reused_blank = False
        if blanks:
            page = blanks.pop(0)
            reused_blank = True
            try:
                _tt_hub_navigate_home_quick(page)
            except Exception as e:
                _log(f"TikToks: blank→IG не удалось: {e!r}")
                page = None
                reused_blank = False
        if page is None:
            try:
                page = _tt_new_page_background(
                    context, seed_page=seed, url=TIKTOK_URL
                )
            except Exception as e:
                _log(f"TikToks: не удалось заранее открыть вкладку: {e!r}")
                break
        # Если createTarget уже с url — только метка; иначе goto (без activate).
        try:
            cur = (page.url or "").strip().lower()
        except Exception:
            cur = ""
        if "tiktok.com" not in cur:
            _tt_hub_navigate_home_quick(page)
        else:
            try:
                _tt_page_target_id(page)
            except Exception:
                pass
        opened += 1
        how = "blank→IG" if reused_blank else "new"
        _log(
            f"TikToks: заранее открыта вкладка +{opened} "
            f"({how}, ig→{len(_tt_tiktok_pages(context))}/{want}, фон)."
        )

    if meta is not None:
        ready = meta.get("tabs_ready")
        if isinstance(ready, threading.Event):
            ready.set()
        meta["preopened"] = True
        total_ig = len(_tt_tiktok_pages(context))
        _log(
            f"TikToks: соседние вкладки готовы "
            f"(ig={total_ig}/{want}, all={len(_tt_alive_context_pages(context))}) "
            "— можно стартовать параллельный залив."
        )


def _tt_page_target_id(page) -> str:
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


def _tt_claim_page_for_tab(meta: dict | None, page, tab_index: int) -> bool:
    """
    Занять вкладку за tab_index. False если её уже держит другой tab.
    """
    if meta is None or page is None:
        return True
    tid = _tt_page_target_id(page)
    with _TT_KEEP_OPEN_META_GUARD:
        busy: dict = meta.setdefault("busy_targets", {})
        owner = busy.get(tid)
        if owner is not None and int(owner) != int(tab_index):
            return False
        busy[tid] = int(tab_index)
        meta.setdefault("tab_targets", {})[int(tab_index)] = tid
    return True


def _tt_release_page_for_tab(meta: dict | None, tab_index: int) -> None:
    if meta is None:
        return
    with _TT_KEEP_OPEN_META_GUARD:
        tab_targets: dict = meta.get("tab_targets") or {}
        tid = tab_targets.pop(int(tab_index), None)
        busy: dict = meta.get("busy_targets") or {}
        if tid is not None and busy.get(tid) == int(tab_index):
            busy.pop(tid, None)


def _tt_pick_page_for_tab(
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
    pages = _tt_alive_context_pages(context)

    if tab_i == 0 and not dedicated_tab:
        primary = _pick_primary_tiktok_page(context, fallback_page)
        chosen = primary if primary is not None else fallback_page
        if chosen is not None and not _tt_claim_page_for_tab(meta, chosen, tab_i):
            _log(
                f"TikToks: tab={tab_i} primary уже занята другим воркером."
            )
        return chosen

    # tab>=1: только отдельные вкладки, primary никогда не трогаем.
    ig_pages: list = []
    primary = _pick_primary_tiktok_page(context, None)
    for pg in pages:
        if primary is not None and pg is primary:
            continue
        try:
            url = (pg.url or "").strip().lower()
        except Exception:
            url = ""
        if "tiktok.com" in url or url in ("about:blank", "about:srcdoc", ""):
            ig_pages.append(pg)

    def _try_claim(candidates: list):
        for pg in candidates:
            if pg is None:
                continue
            if primary is not None and pg is primary:
                continue
            if _tt_claim_page_for_tab(meta, pg, tab_i):
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
            _log(f"TikToks: взяли заранее открытую вкладку tab={tab_i}.")
            return chosen

    have = len(_tt_tiktok_pages(context))
    if have < want:
        page, _own = _open_dedicated_tiktok_tab(
            context, fallback_page=None
        )
        if page is not None and page is not fallback_page and page is not primary:
            _tt_hub_navigate_home_quick(page)
            if _tt_claim_page_for_tab(meta, page, tab_i):
                _log(
                    f"TikToks: открыта новая вкладка tab={tab_i} "
                    f"(ig={have + 1}/{want})."
                )
                return page

    # Свободная доп. вкладка (не primary).
    chosen = _try_claim(ig_pages)
    if chosen is not None:
        _log(
            f"TikToks: tab={tab_i} взял свободную доп. вкладку "
            f"(лимит ig={want})."
        )
        return chosen

    _log(
        f"TikToks: tab={tab_i} нет свободной вкладки "
        f"(не используем primary, чтобы не сбить залив tab0)."
    )
    return None

def _open_dedicated_tiktok_tab(context, *, fallback_page=None):
    """
    Новая вкладка для параллельного залива Reels (отдельное UI-состояние).
    Стараемся открыть в фоне, без переключения активной вкладки.
    """
    try:
        from zaliver.tiktok_upload.register import TIKTOK_URL

        page = _tt_new_page_background(
            context, seed_page=fallback_page, url=TIKTOK_URL
        )
        try:
            cur = (page.url or "").strip().lower()
        except Exception:
            cur = ""
        if "tiktok.com" not in cur:
            _tt_hub_navigate_home_quick(page)
        else:
            try:
                _tt_page_target_id(page)
            except Exception:
                pass
        _log("TikToks: открыта отдельная вкладка для залива (фон).")
        return page, True
    except Exception as e:
        _log(f"TikToks: new_page не удался ({e!r}) — используем fallback.")
        if fallback_page is not None:
            return fallback_page, False
        return None, False


def _pick_primary_tiktok_page(context, fallback_page=None):
    """
    Стартовая вкладка TikTok: первая незакрытая с tiktok.com
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
        if "tiktok.com" in url and first_ig is None:
            first_ig = pg
            break
    chosen = first_ig or first_any or fallback_page
    if chosen is not None:
        try:
            _log(
                "TikToks: используем первичную вкладку "
                f"url={(chosen.url or '')!r}"
            )
        except Exception:
            _log("TikToks: используем первичную вкладку профиля.")
    return chosen

def _save_tiktok_credentials_to_profile(api, profile_id: str, credentials) -> None:
    """Сохранить tt_login / tt_password в custom_data (yt_* и tt_2fa не трогаем)."""
    from zaliver.core.profiles.account_data import TT_LOGIN_KEY, TT_PASSWORD_KEY

    if credentials is None:
        return
    email = str(getattr(credentials, "email", "") or "").strip()
    password = str(getattr(credentials, "password", "") or "")
    if not email and not password:
        return
    # tt_2fa не пишем при регистрации — не затираем вручную введённый секрет.
    payload = {
        TT_LOGIN_KEY: email,
        TT_PASSWORD_KEY: password,
    }
    try:
        api.merge_profile_custom_data(profile_id, payload)
        _log(
            "Local antidetect: в custom_data сохранены TikTok-данные "
            f"({TT_LOGIN_KEY}={email!r}) для profile_id={profile_id!r}."
        )
    except Exception as e:
        _log(
            "Local antidetect: не удалось сохранить TikTok-данные "
            f"для profile_id={profile_id!r}: {e!r}"
        )

def _tiktok_session_creds_from_profile_dict(
    profile: dict | None,
) -> tuple[str, str, str]:
    """(login, password, twofa) из custom_data для re-login вне регистрации."""
    from zaliver.tiktok_upload.tiktok_availability import (
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
def check_tiktok_availability_in_profile(
    profile_id: str,
    *,
    local_token: str | None = None,
    headless: bool = True,
    session_login: str = "",
    session_password: str = "",
    session_twofa: str = "",
    login_credentials=None,
) -> None:
    """Dolphin → tiktok.com → проверка входа в аккаунт → закрытие профиля."""
    from zaliver.tiktok_upload.tiktok_availability import (
        verify_tiktok_home_available,
    )

    _log(
        "Dolphin: проверка доступности TikTok. "
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
                verify_tiktok_home_available(
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
        _log(f"Ошибка проверки TikTok: {type(e).__name__}: {e!r}")
        raise _wrap_exc(e) from e
    finally:
        try:
            api.stop_profile(profile_id)
        except Exception as e:
            _log(f"Dolphin: stop_profile: {e!r}")
        api.close()


@with_log_profile
def check_tiktok_availability_in_local_antidetect_profile(
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
    """Локальный антидетект → tiktok.com → проверка входа → закрытие профиля."""
    from zaliver.tiktok_upload.tiktok_availability import (
        verify_tiktok_home_available,
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
        "Local antidetect: проверка доступности TikTok. "
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
                    _tiktok_session_creds_from_profile_dict(prof)
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

        _log("Local antidetect: launch TikTok check (desktop, без mobile preset)")
        acc = api.launch_profile(
            profile_id,
            headless=headless,
            expose_cdp=True,
            remote_cdp=remote_cdp,
            start_url="https://www.tiktok.com/",
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
                _try_enlarge_browser_os_window(page)
                verify_tiktok_home_available(
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
        _log(f"Ошибка проверки TikTok: {type(e).__name__}: {e!r}")
        raise LocalAntidetectError(
            f"Ошибка проверки доступности TikTok: {e}"
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
                f"Local antidetect: проверка TikTok завершена за "
                f"{time.perf_counter() - started_at:.1f} с."
            )
        except Exception:
            pass
        api.close()


@with_log_profile
def warmup_tiktok_reels_in_profile(
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
    """Dolphin → TikTok → /reels/ или keyword search → прогрев."""
    from zaliver.tiktok_upload.reels_warmup import run_tiktok_reels_warmup

    _log(
        "Dolphin: прогрев TikToks. "
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
                run_tiktok_reels_warmup(page, **kw)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        _log(f"Ошибка прогрева Тиктоков: {type(e).__name__}: {e!r}")
        raise _wrap_exc(e) from e
    finally:
        try:
            api.stop_profile(profile_id)
        except Exception as e:
            _log(f"Dolphin: stop_profile: {e!r}")
        api.close()


@with_log_profile
def warmup_tiktok_reels_in_local_antidetect_profile(
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
    """Локальный антидетект → TikTok → /reels/ или keyword search → прогрев."""
    from zaliver.tiktok_upload.reels_warmup import run_tiktok_reels_warmup
    from zaliver.antydetect.local_antidetect_api import (
        LocalAntidetectError,
        LocalAntidetectHttpAPI,
    )
    from zaliver.antydetect.local_active_sessions import (
        register_local_session,
        unregister_local_session,
    )

    _log(
        "Local antidetect: прогрев TikToks. "
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
                    _tiktok_session_creds_from_profile_dict(prof)
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
                run_tiktok_reels_warmup(page, **kw)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        _log(f"Ошибка прогрева Тиктоков: {type(e).__name__}: {e!r}")
        raise LocalAntidetectError(f"Ошибка прогрева Тиктоков: {e}") from e
    finally:
        if session_id:
            unregister_local_session(profile_id=profile_id)
            try:
                api.stop_session(session_id)
            except Exception:
                pass
        try:
            _log(
                "Local antidetect: прогрев Тиктоков завершён за "
                f"{time.perf_counter() - started_at:.1f} с."
            )
        except Exception:
            pass
        api.close()


@with_log_profile
def upload_tiktok_reel_in_profile(
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
    crop_aspect: str = DEFAULT_TIKTOK_CROP_ASPECT,
    schedule_publish_at=None,
    scheduled_batch=None,
) -> dict:
    """Dolphin → TikTok → «Новая публикация» → файл → Share (Reels)."""
    from zaliver.tiktok_upload.reels_upload import run_tiktok_reels_upload

    keep_open = bool(keep_browser_open)
    use_tab = bool(dedicated_tab)
    scan_n = max(1, int(top_reels_scan or 1))
    tab_i = max(0, int(tab_index or 0))
    tabs_n = max(1, int(tabs_per_profile or 1))
    crop = normalize_tiktok_crop_aspect(crop_aspect)
    _log(
        "Dolphin: залив TikToks. "
        f"profile_id={profile_id!r}, headless={headless}, "
        f"keep_browser_open={keep_open}, dedicated_tab={use_tab}, "
        f"tab={tab_i}/{tabs_n}, top_reels_scan={scan_n}, "
        f"crop={crop}, video_path={video_path!r}"
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
                    upload_page, own_page = _open_dedicated_tiktok_tab(
                        context, fallback_page=page
                    )
                else:
                    upload_page = _pick_primary_tiktok_page(context, page)
                    own_page = False
                try:
                    return run_tiktok_reels_upload(
                        upload_page,
                        video_path=video_path,
                        title=title,
                        description=description,
                        session_login=session_login,
                        session_password=session_password,
                        session_twofa=session_twofa,
                        profile_id=profile_id,
                        top_reels_scan=scan_n,
                        crop_aspect=crop,
                        schedule_publish_at=schedule_publish_at,
                        scheduled_batch=scheduled_batch,
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
        meta = _tt_meta_get(profile_id)
        if meta is None:
            meta = {"tabs_ready": threading.Event(), "preopened": False}
            _tt_meta_set(profile_id, meta)
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
            upload_page = _tt_pick_page_for_tab(
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
                _tt_preopen_sibling_tabs(context, tabs_n, meta)

        try:
            result = run_tiktok_reels_upload(
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
                crop_aspect=crop,
                schedule_publish_at=schedule_publish_at,
                scheduled_batch=scheduled_batch,
            )
            _log(
                "Dolphin: браузер оставлен открытым "
                f"(profile_id={profile_id!r}) — следующий залив без stop."
            )
            return result
        finally:
            _tt_release_page_for_tab(meta, tab_i)
            if pw_cm is not None:
                try:
                    pw_cm.__exit__(None, None, None)
                except Exception:
                    pass
                pw_cm = None
    except Exception as e:
        _log(f"Ошибка залива Тиктоков: {type(e).__name__}: {e!r}")
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


def _local_launch_or_reuse_tiktok_session(
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
            start_url="https://www.tiktok.com/",
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
            start_url="https://www.tiktok.com/",
        )

    sid = acc.get("session_id")
    if not isinstance(sid, str) or not sid.strip():
        raise LocalAntidetectError(f"Нет session_id в ответе launch: {acc!r}")
    session_id = sid.strip()
    ws_url = api.wait_for_cdp_ws_url(session_id, timeout_s=120.0)
    return _register(session_id, ws_url)


@with_log_profile
def upload_tiktok_reel_in_local_antidetect_profile(
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
    crop_aspect: str = DEFAULT_TIKTOK_CROP_ASPECT,
    schedule_publish_at=None,
    scheduled_batch=None,
) -> dict:
    """Локальный антидетект → TikTok → «Новая публикация» → файл → Share (Reels)."""
    from zaliver.tiktok_upload.reels_upload import run_tiktok_reels_upload
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
    crop = normalize_tiktok_crop_aspect(crop_aspect)
    _log(
        "Local antidetect: залив TikToks. "
        f"profile_id={profile_id!r}, base_url={base_url!r}, headless={headless}, "
        f"keep_browser_open={keep_open}, dedicated_tab={use_tab}, "
        f"tab={tab_i}/{tabs_n}, top_reels_scan={scan_n}, "
        f"crop={crop}, video_path={video_path!r}"
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
                    _tiktok_session_creds_from_profile_dict(prof)
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
                session_id, ws_url = _local_launch_or_reuse_tiktok_session(
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
                    upload_page, own_page = _open_dedicated_tiktok_tab(
                        context, fallback_page=page
                    )
                else:
                    upload_page = _pick_primary_tiktok_page(context, page)
                    own_page = False
                try:
                    return run_tiktok_reels_upload(
                        upload_page,
                        video_path=video_path,
                        title=title,
                        description=description,
                        session_login=login,
                        session_password=pwd,
                        session_twofa=twofa,
                        profile_id=profile_id,
                        top_reels_scan=scan_n,
                        crop_aspect=crop,
                        schedule_publish_at=schedule_publish_at,
                        scheduled_batch=scheduled_batch,
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

        meta = _tt_meta_get(profile_id)
        if meta is None:
            meta = {
                "tabs_ready": threading.Event(),
                "preopened": False,
                "session_id": None,
                "ws_url": None,
            }
            _tt_meta_set(profile_id, meta)
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
                sid, ws = _local_launch_or_reuse_tiktok_session(
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
                    session_id, ws_url = _local_launch_or_reuse_tiktok_session(
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
            upload_page = _tt_pick_page_for_tab(
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
                _tt_preopen_sibling_tabs(context, tabs_n, meta)

        try:
            result = run_tiktok_reels_upload(
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
                crop_aspect=crop,
                schedule_publish_at=schedule_publish_at,
                scheduled_batch=scheduled_batch,
            )
            _log(
                "Local antidetect: браузер оставлен открытым "
                f"(profile_id={profile_id!r}) — следующий залив без stop."
            )
            return result
        finally:
            _tt_release_page_for_tab(meta, tab_i)
            if pw_cm is not None:
                try:
                    pw_cm.__exit__(None, None, None)
                except Exception:
                    pass
                pw_cm = None
    except Exception as e:
        _log(f"Ошибка залива Тиктоков: {type(e).__name__}: {e!r}")
        raise LocalAntidetectError(f"Ошибка залива Тиктоков: {e}") from e
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
                "Local antidetect: залив Тиктоков завершён за "
                f"{time.perf_counter() - started_at:.1f} с."
            )
        except Exception:
            pass
        api.close()


def _tiktok_sessionid_from_page(page) -> str:
    """Cookie sessionid с tiktok.com (пусто, если нет рабочей сессии)."""
    from zaliver.tiktok_upload.instagrapi_session import normalize_tiktok_sessionid

    cookies = []
    try:
        try:
            cookies = page.context.cookies(
                ["https://www.tiktok.com", "https://www.tiktok.com"]
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
        if domain and "tiktok" not in domain:
            continue
        val = normalize_tiktok_sessionid(c.get("value") or "")
        if val and val not in ("0", '""', "null") and len(val) > len(best):
            best = val
    return best


def _page_url_looks_like_tiktok_home(url: str) -> bool:
    u = (url or "").strip().lower()
    if not u:
        return False
    if "tiktok.com" not in u:
        return False
    # Google OAuth / login walls — не считаем «уже на TikTok».
    bad = (
        "accounts.google.com",
        "/accounts/login",
        "/accounts/emailsignup",
        "/challenge/",
        "/accounts/suspended",
        "flowName=GlifWebSignIn",
    )
    return not any(b.lower() in u for b in bad)


def _ensure_tiktok_page_for_cookies(page) -> None:
    """
    Сначала открыть TikTok, потом читать sessionid.

    Иначе вкладка может висеть на Google chooser / blank, а старый
    cookie sessionid всё равно найдётся и чекер возьмёт мёртвую сессию.
    Не ждём networkidle — только commit + короткая пауза.
    """
    from zaliver.tiktok_upload.register import TIKTOK_URL

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
    if _page_url_looks_like_tiktok_home(cur):
        _log(f"TikTok cookies: уже на TikTok ({cur[:120]}), goto не нужен.")
    else:
        _log(
            "TikTok cookies: короткий goto tiktok.com "
            f"(было: {(cur or '—')[:160]})…"
        )
        try:
            # commit = первый байт ответа, не ждём DOM/networkidle (часто зависает).
            page.goto(TIKTOK_URL, wait_until="commit", timeout=25_000)
        except Exception as e:
            _log(f"TikTok cookies: goto commit failed: {e!r}")
            try:
                page.goto(TIKTOK_URL, wait_until="domcontentloaded", timeout=20_000)
            except Exception as e2:
                _log(f"TikTok cookies: goto domcontentloaded failed: {e2!r}")
        try:
            page.wait_for_timeout(1500)
        except Exception:
            time.sleep(1.5)

    try:
        after = (page.url or "").strip()
    except Exception:
        after = ""
    if after:
        _log(f"TikTok cookies: URL после перехода: {after[:180]}")
    if not _page_url_looks_like_tiktok_home(after):
        _log(
            "TikTok cookies: после goto всё ещё не лента TikTok "
            "(login/Google/challenge) — sessionid может быть мёртвым."
        )


@with_log_profile
def extract_tiktok_sessionid_from_profile(
    profile_id: str,
    *,
    local_token: str | None = None,
    headless: bool = True,
) -> str:
    """Dolphin: sessionid TikTok из cookies профиля."""
    # Для чекера всегда headless: видимое окно часто зависает на blank.
    use_headless = True
    _log(
        "Dolphin: извлечение TikTok sessionid. "
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
                _ensure_tiktok_page_for_cookies(page)
                sid = _tiktok_sessionid_from_page(page)
                if not sid:
                    raise DolphinAntyError(
                        "В профиле нет cookie sessionid TikTok "
                        "(войдите в аккаунт в этом профиле)."
                    )
                _log("Dolphin: TikTok sessionid получен.")
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
def extract_tiktok_sessionid_from_local_antidetect_profile(
    profile_id: str,
    *,
    base_url: str,
    headless: bool = True,
    remote_cdp=None,
) -> str:
    """Локальный/удалённый антидетект: sessionid TikTok из cookies профиля."""
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
        "Local antidetect: извлечение TikTok sessionid. "
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
                _ensure_tiktok_page_for_cookies(page)
                cookie_sid = _tiktok_sessionid_from_page(page)
                if not cookie_sid:
                    raise LocalAntidetectError(
                        "В профиле нет cookie sessionid TikTok "
                        "(войдите в аккаунт в этом профиле)."
                    )
                _log("Local antidetect: TikTok sessionid получен.")
                return cookie_sid
            finally:
                _close_playwright_browser(browser)
    except Exception as e:
        _log(f"Ошибка извлечения sessionid: {type(e).__name__}: {e!r}")
        if isinstance(e, LocalAntidetectError):
            raise
        raise LocalAntidetectError(f"Ошибка извлечения TikTok sessionid: {e}") from e
    finally:
        if session_id:
            unregister_local_session(profile_id=profile_id)
            try:
                api.stop_session(session_id)
            except Exception:
                pass
        api.close()


@with_log_profile
def register_tiktok_account_in_profile(
    profile_id: str,
    *,
    local_token: str | None = None,
    headless: bool = True,
    login_credentials=None,
    on_manual_captcha=None,
) -> None:
    """Dolphin → Gmail inbox → TikTok signup → капча → код из почты."""
    from zaliver.tiktok_upload.gmail_availability import verify_gmail_inbox_available
    from zaliver.tiktok_upload.register import (
        KEEP_PROFILE_OPEN_AFTER_TT_REGISTER,
        TikTokRegistrationFailedError,
        TikTokSmsCaptchaError,
        run_tiktok_registration_after_gmail,
    )

    _log(
        "Dolphin: регистрация TikTok. "
        f"profile_id={profile_id!r}, headless={headless}"
    )
    api = DolphinAntyLocalAPI()
    keep_open_on_error = bool(KEEP_PROFILE_OPEN_AFTER_TT_REGISTER)
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
                    username = run_tiktok_registration_after_gmail(
                        page,
                        login_credentials,
                        on_manual_captcha=on_manual_captcha,
                        profile_id=profile_id,
                    )
                    succeeded = True
                    _log(f"Dolphin: TikTok зарегистрирован, username={username!r}")
                except (TikTokSmsCaptchaError, TikTokRegistrationFailedError):
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
            isinstance(e, (TikTokSmsCaptchaError, TikTokRegistrationFailedError))
            or TikTokSmsCaptchaError.matches(str(e))
            or TikTokRegistrationFailedError.matches(str(e))
        ):
            force_close = True
        if force_close:
            _log(
                "Dolphin: известная ошибка регистрации — закрываем профиль "
                f"(profile_id={profile_id!r})."
            )
        _log(f"Ошибка регистрации TikTok: {type(e).__name__}: {e!r}")
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
                "(KEEP_PROFILE_OPEN_AFTER_TT_REGISTER)."
            )
            api.close()


@with_log_profile
def register_tiktok_account_in_local_antidetect_profile(
    profile_id: str,
    *,
    base_url: str,
    headless: bool = True,
    login_credentials=None,
    remote_cdp=None,
    on_manual_captcha=None,
) -> None:
    """Локальный антидетект → Gmail → TikTok signup → капча → код из почты."""
    from zaliver.tiktok_upload.gmail_availability import verify_gmail_inbox_available
    from zaliver.tiktok_upload.register import (
        KEEP_PROFILE_OPEN_AFTER_TT_REGISTER,
        TikTokRegistrationFailedError,
        TikTokSmsCaptchaError,
        run_tiktok_registration_after_gmail,
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
        "Local antidetect: регистрация TikTok. "
        f"profile_id={profile_id!r}, base_url={base_url!r}, headless={headless}"
    )
    api = LocalAntidetectHttpAPI(base_url)
    session_id: str | None = None
    started_at = time.perf_counter()
    keep_open_on_error = bool(KEEP_PROFILE_OPEN_AFTER_TT_REGISTER)
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
                    username = run_tiktok_registration_after_gmail(
                        page,
                        login_credentials,
                        on_manual_captcha=on_manual_captcha,
                        profile_id=profile_id,
                    )
                    succeeded = True
                    _save_tiktok_credentials_to_profile(
                        api, profile_id, login_credentials
                    )
                    _log(
                        "Local antidetect: TikTok зарегистрирован, "
                        f"username={username!r}"
                    )
                except (TikTokSmsCaptchaError, TikTokRegistrationFailedError):
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
            isinstance(e, (TikTokSmsCaptchaError, TikTokRegistrationFailedError))
            or TikTokSmsCaptchaError.matches(str(e))
            or TikTokRegistrationFailedError.matches(str(e))
        ):
            force_close = True
        if force_close:
            _log(
                "Local antidetect: известная ошибка регистрации — закрываем профиль "
                f"(profile_id={profile_id!r})."
            )
        _log(f"Ошибка регистрации TikTok: {type(e).__name__}: {e!r}")
        raise LocalAntidetectError(f"Ошибка регистрации TikTok: {e}") from e
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
                "(KEEP_PROFILE_OPEN_AFTER_TT_REGISTER)."
            )
        try:
            _log(
                f"Local antidetect: регистрация TikTok завершена за "
                f"{time.perf_counter() - started_at:.1f} с."
            )
        except Exception:
            pass
        api.close()


def _save_tiktok_2fa_to_profile(api, profile_id: str, secret: str) -> None:
    """Сохранить tt_2fa в custom_data профиля."""
    from zaliver.core.profiles.account_data import TT_2FA_KEY

    s = (secret or "").strip()
    if not s:
        return
    try:
        api.merge_profile_custom_data(profile_id, {TT_2FA_KEY: s})
        _log(
            "Local antidetect: в custom_data сохранён "
            f"{TT_2FA_KEY} (len={len(s)}) для profile_id={profile_id!r}."
        )
    except Exception as e:
        _log(
            "Local antidetect: не удалось сохранить "
            f"{TT_2FA_KEY} для profile_id={profile_id!r}: {e!r}"
        )
        raise


@with_log_profile
def setup_tiktok_2fa_in_profile(
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
    from zaliver.tiktok_upload.setup_2fa import (
        KEEP_PROFILE_OPEN_AFTER_TT_2FA,
        setup_tiktok_totp_2fa,
    )

    _log(
        "Dolphin: подключение 2FA TikTok. "
        f"profile_id={profile_id!r}, headless={headless}"
    )
    api = DolphinAntyLocalAPI()
    if keep_open_on_error is None:
        keep_open_on_error = bool(KEEP_PROFILE_OPEN_AFTER_TT_2FA)
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
                secret = setup_tiktok_totp_2fa(
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
                    f"Dolphin: 2FA TikTok подключена (secret_len={len(secret)})."
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
        _log(f"Ошибка подключения 2FA TikTok: {type(e).__name__}: {e!r}")
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
def setup_tiktok_2fa_in_local_antidetect_profile(
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
    """Локальный антидетект → Accounts Center → TOTP 2FA → сохранить tt_2fa."""
    from zaliver.tiktok_upload.setup_2fa import (
        KEEP_PROFILE_OPEN_AFTER_TT_2FA,
        setup_tiktok_totp_2fa,
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
        "Local antidetect: подключение 2FA TikTok. "
        f"profile_id={profile_id!r}, base_url={base_url!r}, headless={headless}"
    )
    api = LocalAntidetectHttpAPI(base_url)
    session_id: str | None = None
    started_at = time.perf_counter()
    if keep_open_on_error is None:
        keep_open_on_error = bool(KEEP_PROFILE_OPEN_AFTER_TT_2FA)
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
                    _tiktok_session_creds_from_profile_dict(prof)
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
                    _save_tiktok_2fa_to_profile(api, profile_id, s)

                secret = setup_tiktok_totp_2fa(
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
                    "Local antidetect: 2FA TikTok подключена "
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
        _log(f"Ошибка подключения 2FA TikTok: {type(e).__name__}: {e!r}")
        raise LocalAntidetectError(f"Ошибка подключения 2FA TikTok: {e}") from e
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
                f"Local antidetect: подключение 2FA TikTok завершено за "
                f"{time.perf_counter() - started_at:.1f} с."
            )
        except Exception:
            pass
        api.close()
    return secret


@with_log_profile
def setup_tiktok_profile_in_profile(
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
    from zaliver.tiktok_upload.edit_profile import run_tiktok_edit_profile

    bio = (description or "").strip()
    has_avatar = bool(avatar_path)
    uname = (username or "").strip().lstrip("@")
    do_lang = bool(change_language)
    if not bio and not has_avatar and not uname and not do_lang:
        raise DolphinAntyError(
            "Не заданы смена языка, bio, аватарка или юзернейм для TikTok."
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
        "Dolphin: редактирование TikTok-профиля ("
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
                run_tiktok_edit_profile(
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
        _log(f"Ошибка редактирования TikTok-профиля: {type(e).__name__}: {e!r}")
        raise _wrap_exc(e) from e
    finally:
        try:
            api.stop_profile(profile_id)
        except Exception as e:
            _log(f"Dolphin: stop_profile: {e!r}")
        api.close()


@with_log_profile
def setup_tiktok_profile_in_local_antidetect_profile(
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
    from zaliver.tiktok_upload.edit_profile import run_tiktok_edit_profile
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
            "Не заданы смена языка, bio, аватарка или юзернейм для TikTok."
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
        "Local antidetect: редактирование TikTok-профиля ("
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
                    _tiktok_session_creds_from_profile_dict(prof)
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
                run_tiktok_edit_profile(
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
        _log(f"Ошибка редактирования TikTok-профиля: {type(e).__name__}: {e!r}")
        raise LocalAntidetectError(
            f"Ошибка редактирования TikTok-профиля: {e}"
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
                "Local antidetect: редактирование TikTok-профиля завершено за "
                f"{time.perf_counter() - started_at:.1f} с."
            )
        except Exception:
            pass
        api.close()


@with_log_profile
def promote_tiktok_reels_in_profile(
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
    """Dolphin → TikTok → рилсы по ссылкам: подписка/лайк/коммент на странице."""
    from zaliver.tiktok_upload.reels_promote import run_tiktok_profiles_promotion

    _log(
        "Dolphin: продвижение TikToks. "
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
                run_tiktok_profiles_promotion(page, **kw)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        _log(f"Ошибка продвижения Тиктоков: {type(e).__name__}: {e!r}")
        raise _wrap_exc(e) from e
    finally:
        try:
            api.stop_profile(profile_id)
        except Exception as e:
            _log(f"Dolphin: stop_profile: {e!r}")
        api.close()


@with_log_profile
def promote_tiktok_reels_in_local_antidetect_profile(
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
    """Локальный антидетект → TikTok → рилсы по ссылкам: подписка/лайк/коммент."""
    from zaliver.tiktok_upload.reels_promote import run_tiktok_profiles_promotion
    from zaliver.antydetect.local_antidetect_api import (
        LocalAntidetectError,
        LocalAntidetectHttpAPI,
    )
    from zaliver.antydetect.local_active_sessions import (
        register_local_session,
        unregister_local_session,
    )

    _log(
        "Local antidetect: продвижение TikToks. "
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
                    _tiktok_session_creds_from_profile_dict(prof)
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
                run_tiktok_profiles_promotion(page, **kw)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        _log(f"Ошибка продвижения Тиктоков: {type(e).__name__}: {e!r}")
        raise LocalAntidetectError(f"Ошибка продвижения Тиктоков: {e}") from e
    finally:
        if session_id:
            unregister_local_session(profile_id=profile_id)
            try:
                api.stop_session(session_id)
            except Exception:
                pass
        try:
            _log(
                "Local antidetect: продвижение Тиктоков завершено за "
                f"{time.perf_counter() - started_at:.1f} с."
            )
        except Exception:
            pass
        api.close()


def _run_tiktok_stats_in_open_page(
    page,
    items: list,
    *,
    profile_id: str,
    session_login: str = "",
    session_password: str = "",
    session_twofa: str = "",
    login_credentials=None,
    on_progress=None,
    on_item=None,
    should_cancel=None,
    request_pause_s: float = 0.0,
    parallel_tabs: int = 100,
):
    from zaliver.tiktok_upload.tiktok_availability import verify_tiktok_home_available
    from zaliver.tiktok_upload.reel_stats import fetch_reel_stats_many_on_page

    verify_tiktok_home_available(
        page,
        session_login=session_login,
        session_password=session_password,
        session_twofa=session_twofa,
        profile_id=profile_id,
        login_credentials=login_credentials,
    )
    return fetch_reel_stats_many_on_page(
        page,
        items,
        request_pause_s=request_pause_s,
        parallel_tabs=parallel_tabs,
        on_progress=on_progress,
        on_item=on_item,
        should_cancel=should_cancel,
    )


@with_log_profile
def refresh_tiktok_video_stats_in_profile(
    profile_id: str,
    items: list,
    *,
    local_token: str | None = None,
    headless: bool = True,
    session_login: str = "",
    session_password: str = "",
    session_twofa: str = "",
    login_credentials=None,
    on_progress=None,
    on_item=None,
    should_cancel=None,
    request_pause_s: float = 0.0,
    parallel_tabs: int = 100,
):
    """Dolphin: чекер открывает публичные URL роликов и читает метрики."""
    use_headless = True
    _log(
        "Dolphin: чек статистики TikTok. "
        f"profile_id={profile_id!r}, headless={use_headless} "
        f"(requested={headless}), items={len(items)}, tabs={parallel_tabs}"
    )
    api = DolphinAntyLocalAPI()
    try:
        tok = (local_token or "").strip()
        if tok:
            _log("Dolphin: login_with_token…")
            api.login_with_token(tok)
        _log("Dolphin: start_profile…")
        conn = api.start_profile(profile_id, headless=use_headless)
        _log(
            "Dolphin: профиль запущен. "
            f"ws_url={conn.ws_url()!r}, http_url={conn.http_url()!r}"
        )
        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_cdp(
                p, (conn.ws_url(), conn.http_url())
            )
            try:
                return _run_tiktok_stats_in_open_page(
                    page,
                    items,
                    profile_id=profile_id,
                    session_login=session_login,
                    session_password=session_password,
                    session_twofa=session_twofa,
                    login_credentials=login_credentials,
                    on_progress=on_progress,
                    on_item=on_item,
                    should_cancel=should_cancel,
                    request_pause_s=request_pause_s,
                    parallel_tabs=parallel_tabs,
                )
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        _log(f"Ошибка чека статистики TikTok: {type(e).__name__}: {e!r}")
        raise _wrap_exc(e) from e
    finally:
        try:
            api.stop_profile(profile_id)
        except Exception as e:
            _log(f"Dolphin: stop_profile: {e!r}")
        api.close()


@with_log_profile
def refresh_tiktok_video_stats_in_local_antidetect_profile(
    profile_id: str,
    items: list,
    *,
    base_url: str,
    headless: bool = True,
    remote_cdp=None,
    session_login: str = "",
    session_password: str = "",
    session_twofa: str = "",
    login_credentials=None,
    on_progress=None,
    on_item=None,
    should_cancel=None,
    request_pause_s: float = 0.0,
    parallel_tabs: int = 100,
):
    """Локальный антидетект: чекер читает метрики с публичных страниц роликов."""
    from zaliver.antydetect.local_antidetect_api import (
        LocalAntidetectError,
        LocalAntidetectHttpAPI,
    )
    from zaliver.antydetect.local_active_sessions import unregister_local_session
    from zaliver.youtube_upload.google_login import (
        gmail_or_yt_credentials_from_custom_data,
        has_login_credentials,
    )

    use_headless = True
    _log(
        "Local antidetect: чек статистики TikTok. "
        f"profile_id={profile_id!r}, base_url={base_url!r}, "
        f"headless={use_headless} (requested={headless}), "
        f"items={len(items)}, tabs={parallel_tabs}"
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
                    _tiktok_session_creds_from_profile_dict(prof)
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

        bu = (base_url or "").strip() or "http://127.0.0.1:18765"
        _log("Local antidetect: launch TikTok stats (headless)")
        with _profile_launch_lock(profile_id):
            session_id, ws_url = _local_launch_or_reuse_tiktok_session(
                api,
                profile_id=profile_id,
                base_url=bu,
                headless=use_headless,
                remote_cdp=remote_cdp,
            )

        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_local_session_cdp(
                p, api, session_id, ws_url
            )
            try:
                return _run_tiktok_stats_in_open_page(
                    page,
                    items,
                    profile_id=profile_id,
                    session_login=login,
                    session_password=pwd,
                    session_twofa=twofa,
                    login_credentials=google_creds,
                    on_progress=on_progress,
                    on_item=on_item,
                    should_cancel=should_cancel,
                    request_pause_s=request_pause_s,
                    parallel_tabs=parallel_tabs,
                )
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        _log(f"Ошибка чека статистики TikTok: {type(e).__name__}: {e!r}")
        raise LocalAntidetectError(f"Ошибка чека статистики TikTok: {e}") from e
    finally:
        if session_id:
            unregister_local_session(profile_id=profile_id)
            try:
                api.stop_session(session_id)
            except Exception:
                pass
        try:
            _log(
                "Local antidetect: чек статистики TikTok завершён за "
                f"{time.perf_counter() - started_at:.1f} с."
            )
        except Exception:
            pass
        api.close()

