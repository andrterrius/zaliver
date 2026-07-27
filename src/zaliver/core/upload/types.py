"""Upload session request DTOs (built by UI or future HTTP API)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class AntidetectLaunchConfig:
    kind: str = "dolphin"
    token: str = ""
    base_url: str = ""
    headless: bool = True
    remote_cdp: Any | None = None


@dataclass(slots=True)
class UploadQueueRequest:
    """Parameters for MultiProfileUploader (platform-agnostic)."""

    platform: str
    profile_ids: list[str]
    video_paths: list[str]
    title: str = ""
    description: str = ""
    antidetect: AntidetectLaunchConfig = field(default_factory=AntidetectLaunchConfig)
    streaming: bool = False
    publish_before_checks: bool = True
    keep_studio_title: bool = False
    schedule_publish: bool = False
    schedule_times: list[datetime] = field(default_factory=list)
    schedule_warmup_shorts: bool = False
    schedule_warmup_shorts_recommendations: bool = True
    schedule_warmup_search_query: str = ""
    max_concurrent_browsers: int = 5
    instagram_tabs_per_profile: int = 1
    keep_browser_open: bool = False
    delete_after_upload: bool = False
    pending: dict[str, Any] = field(default_factory=dict)
