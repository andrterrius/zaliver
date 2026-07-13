from __future__ import annotations

import threading
import time
from pathlib import Path

from patchright.sync_api import sync_playwright

from zaliver.antydetect.api import DolphinAntyError, DolphinAntyLocalAPI
from zaliver.antydetect.cookie_farm import run_cookie_farm
from zaliver.log_format import with_log_profile
from zaliver.youtube_upload.studio import (
    YoutubeAllChannelsRemovedError,
    YoutubeStudioError,
    run_studio_channel_description_and_link,
    run_studio_channel_profile_customization,
    run_studio_channel_profile_picture,
    run_studio_upload_default_title,
    run_upload_latest_ready_video,
    run_youtube_interface_language_to_russian,
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


def _close_playwright_browser(browser) -> None:
    try:
        if browser is not None:
            browser.close()
    except Exception:
        pass


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
                    _close_playwright_browser(browser)
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
    search_oldest_channel: bool = True,
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
    for pg in context.pages:
        try:
            if "studio.youtube.com" in (pg.url or ""):
                page = pg
                break
        except Exception:
            continue
    if page is None:
        page = context.pages[0] if context.pages else context.new_page()
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
    search_oldest_channel: bool = True,
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
def check_studio_availability_in_local_antidetect_profile(
    profile_id: str,
    *,
    base_url: str,
    headless: bool = True,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    search_oldest_channel: bool = True,
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


def _channel_setup_work_flags(
    *,
    description: str | None,
    link_title: str | None,
    link_url: str | None,
    video_default_title: str | None,
    avatar_path: str | Path | None,
    channel_name: str | None,
    skip_name_change: bool,
) -> tuple[bool, bool, bool, bool]:
    d = (description or "").strip()
    lt = (link_title or "").strip()
    lu = (link_url or "").strip()
    has_text = bool(d) or bool(lt and lu)
    has_video_title = bool((video_default_title or "").strip())
    has_avatar = bool(avatar_path)
    has_name = bool((channel_name or "").strip()) and not skip_name_change
    return has_text, has_video_title, has_avatar, has_name


@with_log_profile
def setup_channel_in_profile(
    profile_id: str,
    *,
    description: str | None = None,
    link_title: str | None = None,
    link_url: str | None = None,
    video_default_title: str | None = None,
    avatar_path: str | Path | None = None,
    channel_name: str | None = None,
    skip_name_change: bool = False,
    local_token: str | None = None,
    headless: bool = True,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    search_oldest_channel: bool = True,
) -> None:
    """Dolphin → Studio → «Настройка канала» (один запуск профиля на все шаги)."""
    has_text, has_video_title, has_avatar, has_name = _channel_setup_work_flags(
        description=description,
        link_title=link_title,
        link_url=link_url,
        video_default_title=video_default_title,
        avatar_path=avatar_path,
        channel_name=channel_name,
        skip_name_change=skip_name_change,
    )
    if not has_text and not has_video_title and not has_avatar and not has_name:
        raise DolphinAntyError("Не заданы параметры настройки канала.")
    parts: list[str] = []
    if has_text:
        parts.append("описание/ссылка")
    if has_video_title:
        parts.append("название для видео")
    if has_avatar:
        parts.append("аватарка")
    if has_name:
        parts.append("название")
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
                if has_text:
                    run_studio_channel_description_and_link(
                        page,
                        description=description,
                        link_title=link_title,
                        link_url=link_url,
                        **studio_kw,
                    )
                if has_video_title:
                    if has_text:
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
                    if has_text or has_video_title:
                        _log(
                            "Dolphin: повторный переход в Studio "
                            "для аватарки/названия (без перезапуска профиля)…"
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
    video_default_title: str | None = None,
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
    """Локальный антидетект → Studio → «Настройка канала» (один запуск профиля)."""
    from zaliver.antydetect.local_antidetect_api import (
        LocalAntidetectError,
        LocalAntidetectHttpAPI,
    )
    from zaliver.antydetect.local_active_sessions import (
        register_local_session,
        unregister_local_session,
    )

    has_text, has_video_title, has_avatar, has_name = _channel_setup_work_flags(
        description=description,
        link_title=link_title,
        link_url=link_url,
        video_default_title=video_default_title,
        avatar_path=avatar_path,
        channel_name=channel_name,
        skip_name_change=skip_name_change,
    )
    if not has_text and not has_video_title and not has_avatar and not has_name:
        raise LocalAntidetectError("Не заданы параметры настройки канала.")
    parts: list[str] = []
    if has_text:
        parts.append("описание/ссылка")
    if has_video_title:
        parts.append("название для видео")
    if has_avatar:
        parts.append("аватарка")
    if has_name:
        parts.append("название")
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
                if has_text:
                    run_studio_channel_description_and_link(
                        page,
                        description=description,
                        link_title=link_title,
                        link_url=link_url,
                        **studio_kw,
                    )
                if has_video_title:
                    if has_text:
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
                    if has_text or has_video_title:
                        _log(
                            "Local antidetect: повторный переход в Studio "
                            "для аватарки/названия (без перезапуска профиля)…"
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

