"""Helpers to build UploadQueueRequest from pending UI/API payloads."""

from __future__ import annotations

from typing import Any

from zaliver.config.platform_settings import normalize_platform
from zaliver.core.upload.types import AntidetectLaunchConfig, UploadQueueRequest
from zaliver.youtube_upload.schedule_publish import parse_msk_datetime


def build_upload_queue_request(
    *,
    platform: str,
    pending: dict[str, Any],
    video_paths: list[str],
    antidetect: AntidetectLaunchConfig,
    streaming: bool = False,
    max_concurrent_browsers: int = 5,
    instagram_tabs_per_profile: int = 1,
    keep_browser_open: bool = False,
    delete_after_upload: bool = False,
) -> UploadQueueRequest:
    """Normalize pending dialog dict + launch config into a typed request."""
    plat = normalize_platform(platform)
    raw_ids = (pending.get("profile_ids", "") or "").strip()
    profile_ids = [p.strip() for p in raw_ids.split(",") if p.strip()]
    schedule_times = []
    if pending.get("schedule_publish") and plat != "instagram":
        for raw in pending.get("schedule_times_iso") or []:
            dt = parse_msk_datetime(raw)
            if dt is not None:
                schedule_times.append(dt)
        schedule_times = sorted(schedule_times)
    return UploadQueueRequest(
        platform=plat,
        profile_ids=profile_ids,
        video_paths=list(video_paths),
        title=str(pending.get("title") or ""),
        description=str(pending.get("description") or ""),
        antidetect=antidetect,
        streaming=bool(streaming),
        publish_before_checks=bool(pending.get("publish_before_checks", True)),
        keep_studio_title=bool(pending.get("keep_studio_title", False)),
        schedule_publish=bool(pending.get("schedule_publish")),
        schedule_times=schedule_times,
        schedule_warmup_shorts=bool(pending.get("schedule_warmup_shorts")),
        schedule_warmup_shorts_recommendations=bool(
            pending.get("schedule_warmup_shorts_recommendations", True)
        ),
        schedule_warmup_search_query=(
            pending.get("schedule_warmup_search_query") or ""
        ).strip(),
        max_concurrent_browsers=int(max_concurrent_browsers),
        instagram_tabs_per_profile=int(instagram_tabs_per_profile),
        keep_browser_open=bool(keep_browser_open),
        delete_after_upload=bool(delete_after_upload),
        pending=dict(pending),
    )
