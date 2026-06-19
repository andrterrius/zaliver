from __future__ import annotations

import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from patchright.sync_api import sync_playwright

from zaliver.antydetect.api import DolphinAntyError, DolphinAntyLocalAPI
from zaliver.youtube_upload.studio import (
    YoutubeAllChannelsRemovedError,
    YoutubeStudioError,
    run_studio_channel_description_and_link,
    run_upload_latest_ready_video,
    run_youtube_shorts_warmup,
    set_log_sink,
    verify_studio_upload_dialog_available,
    _studio_dismiss_upload_dialog,
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


def _local_studio_workflow_kwargs(
    api,
    profile_id: str,
    *,
    login_credentials=None,
    yt_oldest_name: str | None = None,
) -> dict:
    return {
        "login_credentials": login_credentials,
        "yt_oldest_name": (yt_oldest_name or "").strip() or None,
        "on_oldest_channel_name": _make_save_yt_oldest_name_handler(api, profile_id),
    }


def _playwright_page_from_cdp(p, endpoint_candidates: tuple[str, ...]):
    """Подключение к браузеру по CDP; возвращает (browser, context, page)."""
    browser = None
    last_err: Exception | None = None
    for endpoint in endpoint_candidates:
        if not (endpoint or "").strip():
            continue
        try:
            _log(f"Playwright: connect_over_cdp endpoint={endpoint!r}…")
            browser = p.chromium.connect_over_cdp(endpoint)
            last_err = None
            break
        except PlaywrightError as e:
            last_err = e
    if browser is None:
        raise DolphinAntyError(
            f"CDP connect failed for all endpoints. Last error: {last_err!r}"
        )
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


def check_studio_availability_in_profile(
    profile_id: str,
    *,
    local_token: str | None = None,
    headless: bool = True,
    login_credentials=None,
    yt_oldest_name: str | None = None,
) -> None:
    """
    Запуск профиля Dolphin → Studio → окно «Добавить видео» (без загрузки файла).
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
                    login_credentials=login_credentials,
                    yt_oldest_name=yt_oldest_name,
                )
                _studio_dismiss_upload_dialog(page)
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


def check_studio_availability_in_local_antidetect_profile(
    profile_id: str,
    *,
    base_url: str,
    headless: bool = True,
    login_credentials=None,
    yt_oldest_name: str | None = None,
) -> None:
    """Локальный антидетект → Studio → окно загрузки (без файла)."""
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
        acc = api.launch_profile(profile_id, headless=headless, expose_cdp=True)
        sid = acc.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            raise LocalAntidetectError(f"Нет session_id в ответе launch: {acc!r}")
        session_id = sid.strip()
        bu = (base_url or "").strip() or "http://127.0.0.1:18765"
        register_local_session(profile_id=profile_id, base_url=bu, session_id=session_id)
        ws_url = api.wait_for_cdp_ws_url(session_id, timeout_s=120.0)
        _log(f"Local antidetect: cdp_ws_url={ws_url!r}")

        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_cdp(p, (ws_url,))
            try:
                studio_kw = _local_studio_workflow_kwargs(
                    api,
                    profile_id,
                    login_credentials=login_credentials,
                    yt_oldest_name=yt_oldest_name,
                )
                verify_studio_upload_dialog_available(page, **studio_kw)
                _studio_dismiss_upload_dialog(page)
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
                    description=description,
                    link_title=link_title,
                    link_url=link_url,
                    login_credentials=login_credentials,
                    yt_oldest_name=yt_oldest_name,
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
        acc = api.launch_profile(profile_id, headless=headless, expose_cdp=True)
        sid = acc.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            raise LocalAntidetectError(f"Нет session_id в ответе launch: {acc!r}")
        session_id = sid.strip()
        bu = (base_url or "").strip() or "http://127.0.0.1:18765"
        register_local_session(profile_id=profile_id, base_url=bu, session_id=session_id)
        ws_url = api.wait_for_cdp_ws_url(session_id, timeout_s=120.0)
        _log(f"Local antidetect: cdp_ws_url={ws_url!r}")
        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_cdp(p, (ws_url,))
            try:
                studio_kw = _local_studio_workflow_kwargs(
                    api,
                    profile_id,
                    login_credentials=login_credentials,
                    yt_oldest_name=yt_oldest_name,
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


def warmup_youtube_shorts_in_profile(
    profile_id: str,
    *,
    local_token: str | None = None,
    headless: bool = True,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    shorts_count: int | None = None,
    like_probability_pct: float | None = None,
    subscribe_probability_pct: float | None = None,
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
                    "login_credentials": login_credentials,
                    "yt_oldest_name": yt_oldest_name,
                }
                if shorts_count is not None:
                    kw["shorts_count"] = shorts_count
                if like_probability_pct is not None:
                    kw["like_probability_pct"] = like_probability_pct
                if subscribe_probability_pct is not None:
                    kw["subscribe_probability_pct"] = subscribe_probability_pct
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


def warmup_youtube_shorts_in_local_antidetect_profile(
    profile_id: str,
    *,
    base_url: str,
    headless: bool = True,
    login_credentials=None,
    yt_oldest_name: str | None = None,
    shorts_count: int | None = None,
    like_probability_pct: float | None = None,
    subscribe_probability_pct: float | None = None,
    watch_horizontal_videos: bool = False,
    horizontal_search_query: str | None = None,
    horizontal_videos_count: int | None = None,
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
        acc = api.launch_profile(profile_id, headless=headless, expose_cdp=True)
        sid = acc.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            raise LocalAntidetectError(f"Нет session_id в ответе launch: {acc!r}")
        session_id = sid.strip()
        bu = (base_url or "").strip() or "http://127.0.0.1:18765"
        register_local_session(profile_id=profile_id, base_url=bu, session_id=session_id)
        ws_url = api.wait_for_cdp_ws_url(session_id, timeout_s=120.0)
        _log(f"Local antidetect: cdp_ws_url={ws_url!r}")
        with sync_playwright() as p:
            browser, _context, page = _playwright_page_from_cdp(p, (ws_url,))
            try:
                studio_kw = _local_studio_workflow_kwargs(
                    api,
                    profile_id,
                    login_credentials=login_credentials,
                    yt_oldest_name=yt_oldest_name,
                )
                if shorts_count is not None:
                    studio_kw["shorts_count"] = shorts_count
                if like_probability_pct is not None:
                    studio_kw["like_probability_pct"] = like_probability_pct
                if subscribe_probability_pct is not None:
                    studio_kw["subscribe_probability_pct"] = subscribe_probability_pct
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

            try:
                if upload_latest_zaliver_video:
                    res = run_upload_latest_ready_video(
                        page=page,
                        browser=browser,
                        zaliver_db_path=zaliver_db_path,
                        video_path=video_path,
                        title=title,
                        description=description,
                        login_credentials=login_credentials,
                        yt_oldest_name=yt_oldest_name,
                    )
                    return res
                else:
                    # Ничего не делаем — просто открываем Studio, чтобы пользователь мог работать вручную.
                    page.goto(
                        "https://studio.youtube.com/",
                        wait_until="domcontentloaded",
                        timeout=120_000,
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

            _close_playwright_browser(browser)
        return None
    except YoutubeAllChannelsRemovedError:
        raise
    except Exception as e:
        _log(f"Ошибка: {type(e).__name__}: {e!r}")
        raise _wrap_exc(e) from e
    finally:
        api.close()


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
        acc = api.launch_profile(profile_id, headless=headless, expose_cdp=True)
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
                browser, context, page = _playwright_page_from_cdp(p, (ws_url,))

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
                        )

                        def _run_upload():
                            return run_upload_latest_ready_video(
                                page=page,
                                browser=browser,
                                zaliver_db_path=zaliver_db_path,
                                video_path=video_path,
                                title=title,
                                description=description,
                                **studio_kw,
                            )

                        res = _run_upload()
                        _log("Studio upload: сценарий завершён.")
                        return res
                    else:
                        _log("Studio: upload_latest_zaliver_video=False → просто открываем Studio…")
                        page.goto(
                            "https://studio.youtube.com/",
                            wait_until="domcontentloaded",
                            timeout=120_000,
                        )
                        time.sleep(1)
                        _log(f"Studio: открыт URL: {page.url!r}")
                except YoutubeAllChannelsRemovedError as e:
                    _log("Local antidetect: все каналы удалены — закрываем профиль.")
                    _close_playwright_browser(browser)
                    raise LocalAntidetectError(str(e)) from e

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

