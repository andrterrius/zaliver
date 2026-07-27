"""Profile job request DTOs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from zaliver.core.profiles.settings import (
    ChannelAssignment,
    CookieFarmSettings,
    PromoteSettings,
    PromoteTargetVideo,
    ReelsWarmupSettings,
    ShortsWarmupSettings,
)

ProfileJobKind = Literal[
    "availability",
    "instagram_register",
    "instagram_2fa",
    "channel_setup",
    "warmup",
    "promote",
    "cookie_farm",
    "tags_clear",
]


@dataclass(slots=True)
class ProfileJobRequest:
    kind: ProfileJobKind
    profile_ids: list[str]
    platform: str = "youtube"
    antidetect_kind: str = "local"
    token: str = ""
    base_url: str = ""
    headless: bool = True
    remote_cdp: Any | None = None
    max_concurrent: int = 5
    # profile_id -> custom_data dict (for credentials)
    profiles_custom_data: dict[str, dict[str, Any]] = field(default_factory=dict)
    # yt oldest channel name per profile (optional)
    yt_oldest_names: dict[str, str] = field(default_factory=dict)
    search_oldest_channel: bool = True
    # kind-specific
    warmup_shorts: ShortsWarmupSettings | None = None
    warmup_reels: ReelsWarmupSettings | None = None
    promote: PromoteSettings | None = None
    promote_videos: list[PromoteTargetVideo] = field(default_factory=list)
    cookie_farm: CookieFarmSettings | None = None
    channel_description: str = ""
    channel_description_lines: list[str] = field(default_factory=list)
    link_title: str = ""
    link_url: str = ""
    channel_links: list[tuple[str, str]] = field(default_factory=list)
    channel_assignments: list[ChannelAssignment] = field(default_factory=list)
    change_language: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProfileJobResult:
    ok: int = 0
    fail: int = 0
    failed_ids: list[str] = field(default_factory=list)
