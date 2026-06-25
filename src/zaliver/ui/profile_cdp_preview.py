"""CDP screencast preview for remote antidetect profiles (Page.startScreencast)."""

from __future__ import annotations

import base64
import threading
from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal
from patchright.sync_api import sync_playwright

from zaliver.antydetect.local_antidetect_api import LocalAntidetectError, LocalAntidetectHttpAPI


class ProfileCdpPreviewBridge(QObject):
    status = pyqtSignal(str)
    frame_ready = pyqtSignal(bytes)
    failed = pyqtSignal(str)
    remote_stop_done = pyqtSignal(bool, str)


def _pick_page(browser):
    for ctx in browser.contexts:
        for page in ctx.pages:
            if not page.is_closed():
                return ctx, page
    for ctx in browser.contexts:
        try:
            return ctx, ctx.new_page()
        except Exception:
            continue
    raise RuntimeError(
        "Нет контекста/страницы: подключитесь к уже запущенному браузеру с открытой вкладкой."
    )


def _cdp_cleanup(cdp, cdp_holder: list) -> None:
    if cdp is None:
        return
    try:
        cdp.send("Page.stopScreencast")
    except Exception:
        pass
    try:
        cdp.detach()
    except Exception:
        pass
    cdp_holder[0] = None


def run_profile_cdp_preview_worker(
    *,
    profile_id: str,
    base_url: str,
    cancel_event: threading.Event,
    bridge: ProfileCdpPreviewBridge,
    cdp_ws_url: str | None = None,
) -> None:
    """Подключение к уже запущенной сессии профиля и трансляция JPEG-кадров."""
    pid = (profile_id or "").strip()
    bu = (base_url or "").strip()
    if not pid or not bu:
        bridge.failed.emit("profile_id или base_url пуст.")
        return

    api = LocalAntidetectHttpAPI(bu)
    cdp_holder: list[Any] = [None]

    try:
        ws_url = (cdp_ws_url or "").strip()
        if not ws_url:
            bridge.status.emit("Поиск запущенной сессии…")
            if cancel_event.is_set():
                return
            ws_url = api.find_running_cdp_ws_url_for_profile(
                pid,
                timeout_s=15.0,
                cancel_check=cancel_event.is_set,
            )

        bridge.status.emit("Подключение CDP…")
        if cancel_event.is_set():
            return

        def on_screencast_frame(msg: dict) -> None:
            if cancel_event.is_set():
                return
            sid_ack = msg.get("sessionId") if isinstance(msg, dict) else None
            data_b64 = msg.get("data") if isinstance(msg, dict) else None
            if isinstance(data_b64, str):
                try:
                    bridge.frame_ready.emit(base64.b64decode(data_b64))
                except Exception:
                    pass
            if sid_ack is not None and cdp_holder[0] is not None:
                try:
                    cdp_holder[0].send("Page.screencastFrameAck", {"sessionId": sid_ack})
                except Exception:
                    pass

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(ws_url, timeout=60_000)
            try:
                ctx, page = _pick_page(browser)
                cdp = ctx.new_cdp_session(page)
                cdp_holder[0] = cdp
                cdp.on("Page.screencastFrame", on_screencast_frame)
                cdp.send("Page.enable")
                cdp.send(
                    "Page.startScreencast",
                    {
                        "format": "jpeg",
                        "quality": 72,
                        "maxWidth": 1280,
                        "maxHeight": 720,
                        "everyNthFrame": 1,
                    },
                )
                bridge.status.emit(f"Трансляция: {page.url!r}")
                while not cancel_event.is_set():
                    try:
                        page.wait_for_timeout(400)
                    except Exception:
                        break
            finally:
                _cdp_cleanup(cdp_holder[0], cdp_holder)
    except Exception as e:
        if not cancel_event.is_set():
            bridge.failed.emit(str(e))
    finally:
        try:
            api.close()
        except Exception:
            pass
