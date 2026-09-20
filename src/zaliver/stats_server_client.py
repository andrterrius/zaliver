"""
Уведомление stats_server об успешной загрузке ролика (YouTube / Instagram / TikTok).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

STATS_SERVER_BASE_URL = "https://feh1kc.site"
STATS_SERVER_UPLOADED_VIDEO_PATH = "/api/zaliver/uploaded-video"

PLATFORM_YOUTUBE = "youtube"
PLATFORM_INSTAGRAM = "instagram"
PLATFORM_TIKTOK = "tiktok"

_LOG = logging.getLogger(__name__)


def _normalize_platform(value: str | None) -> str:
    v = (value or "").strip().lower()
    if v == PLATFORM_INSTAGRAM:
        return PLATFORM_INSTAGRAM
    if v == PLATFORM_TIKTOK:
        return PLATFORM_TIKTOK
    return PLATFORM_YOUTUBE


def notify_uploaded_video(
    *,
    video_id: str,
    username: str,
    profile_id: str = "",
    scheduled: int | None = None,
    platform: str = PLATFORM_YOUTUBE,
    timeout_s: float = 25.0,
    verify_youtube: bool = False,
) -> bool:
    """
    POST JSON ``{ "username", "video_id", "profile_id", "platform", "scheduled"? }``
    на stats_server.
    ``platform`` — ``youtube``, ``instagram`` или ``tiktok``.
    ``profile_id`` — id профиля антидетект-браузера или пустая строка.
    ``scheduled`` — unix-время отложенной публикации (только для schedule).
    ``verify_youtube`` — oEmbed на сервере; для залива из Studio выкл.:
    ролик только что опубликован, oEmbed часто 404 → 422.
    Не бросает исключения наружу (ошибки только в лог).
    """
    vid = (video_id or "").strip()
    user = (username or "").strip()
    plat = _normalize_platform(platform)
    if not vid or not user:
        return False
    url = STATS_SERVER_BASE_URL.rstrip("/") + STATS_SERVER_UPLOADED_VIDEO_PATH
    payload: dict[str, Any] = {
        "username": user,
        "video_id": vid,
        "profile_id": (profile_id or "").strip(),
        "platform": plat,
        "verify_youtube": bool(verify_youtube),
    }
    if scheduled is not None:
        payload["scheduled"] = int(scheduled)
    attempts = 3 if plat == PLATFORM_YOUTUBE else 1
    last_code = 0
    last_body = ""
    try:
        for attempt in range(1, attempts + 1):
            resp = requests.post(url, json=payload, timeout=timeout_s)
            last_code = int(resp.status_code)
            last_body = (resp.text or "")[:500]
            if 200 <= last_code < 300:
                _LOG.info(
                    "stats_server notify ok: platform=%s video_id=%s username=%s "
                    "profile_id=%s scheduled=%s",
                    plat,
                    vid,
                    user,
                    (profile_id or "").strip(),
                    scheduled,
                )
                return True
            retryable = last_code in (422, 502, 503, 504)
            if retryable and attempt < attempts:
                _LOG.warning(
                    "stats_server notify retry %s/%s status %s: %s",
                    attempt,
                    attempts,
                    last_code,
                    last_body,
                )
                time.sleep(1.5 * attempt)
                continue
            break
        _LOG.warning(
            "stats_server notify bad status %s: %s",
            last_code,
            last_body,
        )
        return False
    except requests.RequestException as e:
        _LOG.warning("stats_server notify request failed: %s", e)
        return False
    except Exception as e:
        _LOG.warning("stats_server notify failed: %s", e)
        return False
