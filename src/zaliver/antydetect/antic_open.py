from __future__ import annotations

import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from zaliver.antydetect.api import DolphinAntyError, DolphinAntyLocalAPI
from zaliver.youtube_upload.studio import (
    YoutubeStudioError,
    run_upload_latest_ready_video,
    set_log_sink,
)

from zaliver.youtube_upload import studio as _studio


def _log(message: str) -> None:
    # Пишем в тот же sink, что и `youtube_upload.studio`,
    # чтобы UI показывал логи единым потоком.
    _studio._log(f"[antic_open] {message}")


def _wrap_exc(e: Exception) -> DolphinAntyError:
    # UI в приложении ловит DolphinAntyError и показывает аккуратный текст.
    if isinstance(e, DolphinAntyError):
        return e
    if isinstance(e, YoutubeStudioError):
        return DolphinAntyError(str(e))
    return DolphinAntyError(repr(e))


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
) -> None:
    """
    Запуск профиля через Dolphin Local API + Playwright CDP.

    Важно: логин Google должен быть уже в профиле антидетекта.
    """
    api = DolphinAntyLocalAPI()
    try:
        tok = (local_token or "").strip()
        if tok:
            api.login_with_token(tok)

        conn = api.start_profile(profile_id, headless=headless)

        with sync_playwright() as p:
            browser = None
            last_err: Exception | None = None
            for endpoint in (conn.ws_url(), conn.http_url()):
                try:
                    browser = p.chromium.connect_over_cdp(endpoint)
                    last_err = None
                    break
                except PlaywrightError as e:
                    last_err = e

            if browser is None:
                raise DolphinAntyError(
                    f"CDP connect failed for both endpoints. Last error: {last_err!r}"
                )

            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()

            if upload_latest_zaliver_video:
                run_upload_latest_ready_video(
                    page=page,
                    browser=browser,
                    zaliver_db_path=zaliver_db_path,
                    video_path=video_path,
                    title=title,
                    description=description,
                )
            else:
                # Ничего не делаем — просто открываем Studio, чтобы пользователь мог работать вручную.
                page.goto("https://studio.youtube.com/", wait_until="domcontentloaded")
                time.sleep(1)

            try:
                browser.close()
            except Exception:
                pass
    except Exception as e:
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
) -> None:
    """
    Запуск профиля через локальный HTTP API (см. OpenAPI антидетекта: launch + опрос сессии на cdp_ws_url),
    затем тот же сценарий YouTube Studio.
    """
    from zaliver.antydetect.local_antidetect_api import (
        LocalAntidetectError,
        LocalAntidetectHttpAPI,
    )

    api = LocalAntidetectHttpAPI(base_url)
    session_id: str | None = None
    try:
        started_at = time.perf_counter()
        _log(
            "Local antidetect: старт. "
            f"profile_id={profile_id!r}, base_url={base_url!r}, headless={headless}, "
            f"upload_latest_zaliver_video={upload_latest_zaliver_video}"
        )
        acc = api.launch_profile(profile_id, headless=headless, expose_cdp=True)
        _log(f"Local antidetect: launch_profile ответ: {acc!r}")
        sid = acc.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            raise LocalAntidetectError(f"Нет session_id в ответе launch: {acc!r}")
        session_id = sid.strip()
        _log(f"Local antidetect: session_id={session_id!r}. Ждём cdp_ws_url…")
        ws_url = api.wait_for_cdp_ws_url(session_id, timeout_s=120.0)
        _log(f"Local antidetect: получен cdp_ws_url: {ws_url!r}")

        with sync_playwright() as p:
            browser = None
            last_err: Exception | None = None
            try:
                _log("Playwright: connect_over_cdp…")
                browser = p.chromium.connect_over_cdp(ws_url)
                last_err = None
            except PlaywrightError as e:
                last_err = e
            if browser is None:
                raise LocalAntidetectError(f"CDP connect failed: {last_err!r}")
            _log(
                "Playwright: CDP подключение успешно. "
                f"contexts={len(browser.contexts)}"
            )

            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            _log(
                "Playwright: выбраны объекты. "
                f"context_pages={len(context.pages)}, page_url={page.url!r}"
            )

            if upload_latest_zaliver_video:
                _log(
                    "Studio upload: запуск сценария загрузки. "
                    f"zaliver_db_path={str(zaliver_db_path) if zaliver_db_path else None!r}, "
                    f"video_path={video_path!r}, title={'<set>' if title else None}, "
                    f"description={'<set>' if description else None}"
                )
                run_upload_latest_ready_video(
                    page=page,
                    browser=browser,
                    zaliver_db_path=zaliver_db_path,
                    video_path=video_path,
                    title=title,
                    description=description,
                )
                _log("Studio upload: сценарий завершён.")
            else:
                _log("Studio: upload_latest_zaliver_video=False → просто открываем Studio…")
                page.goto("https://studio.youtube.com/", wait_until="domcontentloaded")
                time.sleep(1)
                _log(f"Studio: открыт URL: {page.url!r}")

            try:
                _log("Playwright: закрываем browser…")
                browser.close()
                _log("Playwright: browser закрыт.")
            except Exception:
                pass
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

