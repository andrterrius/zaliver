"""Profile job settings (UI-agnostic)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ShortsWarmupSettings:
    shorts_count: int = 10
    like_probability_pct: float = 10.0
    subscribe_probability_pct: float = 10.0
    shorts_watch_min_s: int = 5
    shorts_watch_max_s: int = 25
    watch_full_video: bool = False
    shorts_recommendations: bool = True
    shorts_search_query: str = ""
    hashtag: str = ""
    watch_horizontal_videos: bool = False
    horizontal_search_query: str = ""
    horizontal_videos_count: int = 3


@dataclass(frozen=True)
class ReelsWarmupSettings:
    reels_count: int = 10
    like_probability_pct: float = 10.0
    follow_probability_pct: float = 10.0
    watch_min_s: int = 5
    watch_max_s: int = 25
    watch_full: bool = False
    reels_recommendations: bool = True
    reels_search_query: str = ""


@dataclass(frozen=True)
class PromoteSettings:
    subscribe_to_channels: bool = False
    shorts_count: int = 10
    like_probability_pct: float = 10.0
    shorts_watch_min_s: int = 5
    shorts_watch_max_s: int = 25
    watch_full_video: bool = False
    enable_comments: bool = False
    comments: list[str] = field(default_factory=list)
    comment_probability_pct: float = 50.0


@dataclass(frozen=True)
class CookieFarmSettings:
    use_preset_domains: bool = True
    preset_kind: str = "intl"
    domains: list[str] = field(default_factory=list)
    sites_count: int = 10
    watch_min_s: int = 15
    watch_max_s: int = 45


@dataclass(frozen=True)
class PromoteTargetVideo:
    profile_id: str
    video_id: str
    url: str = ""
    title: str = ""


@dataclass(frozen=True)
class ChannelAssignment:
    profile_id: str
    profile_name: str = ""
    channel_name: str = ""
    channel_description: str = ""
    skip_name_change: bool = False
    video_default_title: str = ""
    avatar_path: str = ""
