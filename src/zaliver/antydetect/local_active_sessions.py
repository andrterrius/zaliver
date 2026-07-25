"""Активные сессии локального антидетекта — чтобы при отмене залива вызвать stop_session по HTTP."""

from __future__ import annotations

import threading
from typing import List, Tuple

_lock = threading.RLock()
# profile_id -> (base_url, session_id)
_active: dict[str, Tuple[str, str]] = {}


def register_local_session(*, profile_id: str, base_url: str, session_id: str) -> None:
    pid = (profile_id or "").strip()
    sid = (session_id or "").strip()
    if not pid or not sid:
        return
    bu = (base_url or "").strip() or "http://127.0.0.1:18765"
    with _lock:
        _active[pid] = (bu, sid)


def unregister_local_session(*, profile_id: str) -> None:
    pid = (profile_id or "").strip()
    if not pid:
        return
    with _lock:
        _active.pop(pid, None)


def stop_registered_local_session_sync(profile_id: str) -> List[str]:
    """Остановить одну зарегистрированную сессию (освобождение слота keep_browser_open)."""
    pid = (profile_id or "").strip()
    if not pid:
        return []
    with _lock:
        item = _active.pop(pid, None)
    if item is None:
        return []
    bu, sid = item
    from zaliver.antydetect.local_antidetect_api import LocalAntidetectHttpAPI

    try:
        api = LocalAntidetectHttpAPI(bu)
        try:
            api.stop_session(sid)
            return [
                f"[upload] [STOP] local antidetect stop_session ok "
                f"profile={pid!r} session_id={sid!r}"
            ]
        finally:
            api.close()
    except Exception as e:
        return [
            f"[upload] [STOP] local antidetect stop_session failed "
            f"profile={pid!r} session_id={sid!r} err={e!r}"
        ]


def stop_all_registered_local_sessions_sync() -> List[str]:
    """
    Останавливает все зарегистрированные сессии (отмена залива).
    Возвращает строки для лога UI.
    """
    with _lock:
        items = list(_active.items())
        _active.clear()
    lines: list[str] = []
    if not items:
        lines.append("[upload] [STOP] local antidetect: нет зарегистрированных session_id")
        return lines

    from zaliver.antydetect.local_antidetect_api import LocalAntidetectHttpAPI

    for pid, (bu, sid) in items:
        try:
            api = LocalAntidetectHttpAPI(bu)
            try:
                api.stop_session(sid)
                lines.append(
                    f"[upload] [STOP] local antidetect stop_session ok "
                    f"profile={pid!r} session_id={sid!r}"
                )
            finally:
                api.close()
        except Exception as e:
            lines.append(
                f"[upload] [STOP] local antidetect stop_session failed "
                f"profile={pid!r} session_id={sid!r} err={e!r}"
            )
    return lines
