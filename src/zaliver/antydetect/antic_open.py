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
        acc = api.launch_profile(profile_id, headless=headless, expose_cdp=True)
        sid = acc.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            raise LocalAntidetectError(f"Нет session_id в ответе launch: {acc!r}")
        session_id = sid.strip()
        ws_url = api.wait_for_cdp_ws_url(session_id, timeout_s=120.0)

        with sync_playwright() as p:
            browser = None
            last_err: Exception | None = None
            try:
                browser = p.chromium.connect_over_cdp(ws_url)
                last_err = None
            except PlaywrightError as e:
                last_err = e
            if browser is None:
                raise LocalAntidetectError(f"CDP connect failed: {last_err!r}")

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
                page.goto("https://studio.youtube.com/", wait_until="domcontentloaded")
                time.sleep(1)

            try:
                browser.close()
            except Exception:
                pass
    except Exception as e:
        raise _wrap_exc(e) from e
    finally:
        if session_id:
            try:
                api.stop_session(session_id)
            except Exception:
                pass
        api.close()

