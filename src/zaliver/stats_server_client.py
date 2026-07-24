"""
Уведомление stats_server об успешной загрузке ролика (YouTube / Instagram).
"""

from __future__ import annotations

import logging
from typing import Any

import requests

STATS_SERVER_BASE_URL = "https://feh1kc.site"
STATS_SERVER_UPLOADED_VIDEO_PATH = "/api/zaliver/uploaded-video"

PLATFORM_YOUTUBE = "youtube"
PLATFORM_INSTAGRAM = "instagram"

_LOG = logging.getLogger(__name__)


def _normalize_platform(value: str | None) -> str:
    v = (value or "").strip().lower()
    return PLATFORM_INSTAGRAM if v == PLATFORM_INSTAGRAM else PLATFORM_YOUTUBE


def notify_uploaded_video(
    *,
    video_id: str,
    username: str,
    profile_id: str = "",
    scheduled: int | None = None,
    platform: str = PLATFORM_YOUTUBE,
    timeout_s: float = 25.0,
) -> bool:
    """
    POST JSON ``{ "username", "video_id", "profile_id", "platform", "scheduled"? }``
    на stats_server.
    ``platform`` — ``youtube`` или ``instagram``.
    ``profile_id`` — id профиля антидетект-браузера или пустая строка.
    ``scheduled`` — unix-время отложенной публикации (только для schedule).
    Не бросает исключения наружу (ошибки только в лог).
    """
    vid = (video_id or "").strip()
    user = (username or "").strip()
    plat = _normalize_platform(platform)
    if not vid or not user:
        return False
    url = STATS_SERVER_BASE_URL.rstrip("/") + STATS_SERVER_UPLOADED_VIDEO_PATH
    try:
        payload: dict[str, Any] = {
            "username": user,
            "video_id": vid,
            "profile_id": (profile_id or "").strip(),
            "platform": plat,
        }
        if scheduled is not None:
            payload["scheduled"] = int(scheduled)
        resp = requests.post(url, json=payload, timeout=timeout_s)
        code = int(resp.status_code)
        ok = 200 <= code < 300
        if not ok:
            _LOG.warning(
                "stats_server notify bad status %s: %s",
                code,
                (resp.text or "")[:500],
            )
        else:
            _LOG.info(
                "stats_server notify ok: platform=%s video_id=%s username=%s "
                "profile_id=%s scheduled=%s",
                plat,
                vid,
                user,
                (profile_id or "").strip(),
                scheduled,
            )
        return ok
    except requests.RequestException as e:
        _LOG.warning("stats_server notify request failed: %s", e)
        return False
    except Exception as e:
        _LOG.warning("stats_server notify failed: %s", e)
        return False
