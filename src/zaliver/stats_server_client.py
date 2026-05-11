"""
Уведомление stats_server об успешной загрузке ролика на YouTube.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

STATS_SERVER_BASE_URL = "https://feh1kc.site"
STATS_SERVER_UPLOADED_VIDEO_PATH = "/api/zaliver/uploaded-video"

_LOG = logging.getLogger(__name__)


def notify_uploaded_video(
    *, video_id: str, username: str, timeout_s: float = 25.0
) -> bool:
    """
    POST JSON ``{ "username", "video_id" }`` на stats_server.
    Не бросает исключения наружу (ошибки только в лог).
    """
    vid = (video_id or "").strip()
    user = (username or "").strip()
    if not vid or not user:
        return False
    url = STATS_SERVER_BASE_URL.rstrip("/") + STATS_SERVER_UPLOADED_VIDEO_PATH
    try:
        payload: dict[str, Any] = {"username": user, "video_id": vid}
        resp = requests.post(url, json=payload, timeout=timeout_s)
        code = int(resp.status_code)
        ok = 200 <= code < 300
        if not ok:
            _LOG.warning(
                "stats_server notify bad status %s: %s",
                code,
                (resp.text or "")[:500],
            )
        return ok
    except requests.RequestException as e:
        _LOG.warning("stats_server notify request failed: %s", e)
        return False
    except Exception as e:
        _LOG.warning("stats_server notify failed: %s", e)
        return False

