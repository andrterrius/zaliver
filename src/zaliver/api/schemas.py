"""Pydantic request/response models for the Zaliver API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    platform: str
    browser_jobs_enabled: bool
    docs_enabled: bool


class PlatformResponse(BaseModel):
    platform: str


class PlatformUpdate(BaseModel):
    platform: Literal["youtube", "instagram", "yt_inst"]


class SettingsGetResponse(BaseModel):
    platform: str
    values: dict[str, Any]


class SettingsPatchRequest(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)

    @field_validator("values")
    @classmethod
    def _non_empty_keys(cls, v: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, val in (v or {}).items():
            key = str(k).strip()
            if not key:
                continue
            out[key] = val
        return out


class UniquifySettingsModel(BaseModel):
    brightness_delta: float = 0.0
    contrast: float = 1.0
    saturation_scale: float = 1.0
    crop_jitter_px: int = 0
    scale_pct: float = 100.0
    noise_sigma: float = 0.0
    seed_base: int = 0
    playback_speed_factor: float = 1.0
    audio_chorus: bool = False


class TextOverlayModel(BaseModel):
    enabled: bool = False
    text: str = ""
    font_size: int = 95
    glow_enabled: bool = True
    glow_color: str = "#00FFFF"
    text_color: str = "#FFFFFF"
    letter_spacing: int = 0
    custom_font_path: str = ""
    font_bold: bool = True
    orientation: str = "vertical"
    from_middle: bool = True
    after_frame_change: bool = False
    anchor_x: float = 0.5
    anchor_y: float = 0.15
    wave_amp_frac: float = 0.0
    wave_frame_speed: float = 0.0
    wave_amp_frac_min: float | None = None
    wave_amp_frac_max: float | None = None
    wave_frame_speed_min: float | None = None
    wave_frame_speed_max: float | None = None


class UploadAfterFollowup(BaseModel):
    """Attach upload to a processing job (server-driven; supports upload-as-ready)."""

    profile_ids: list[str] = Field(min_length=1)
    title: str = ""
    description: str = ""
    platform: str = ""
    kind: str = "local"
    headless: bool = True
    max_concurrent_browsers: int = Field(default=5, ge=1, le=5)
    publish_before_checks: bool = True
    keep_studio_title: bool = False
    schedule_publish: bool = False
    schedule_times_iso: list[str] = Field(default_factory=list)
    schedule_warmup_shorts: bool = False
    schedule_warmup_shorts_recommendations: bool = True
    schedule_warmup_search_query: str = ""
    delete_after_upload: bool = False
    upload_as_ready: bool = False
    planned_videos: int = Field(default=0, ge=0, le=100_000)


class UniquifyJobRequest(BaseModel):
    # Optional; must be under managed output/<platform>/uniquify (or empty = that folder).
    output_dir: str = ""
    # UI platform — overrides in-memory AppState (multi-worker safe).
    platform: str = ""
    input_files: list[str] = Field(min_length=1)
    num_workers: int = Field(default=1, ge=1, le=1)
    copies_per_file: int = Field(default=1, ge=1)
    use_gpu: bool = False
    use_gpu_finalize: bool = False
    randomize_uniquify: bool = True
    one_copy_no_effects: bool = False
    brightness_enabled: bool = True
    contrast_enabled: bool = True
    saturation_enabled: bool = True
    crop_jitter_enabled: bool = False
    scale_enabled: bool = True
    noise_enabled: bool = True
    seed_enabled: bool = False
    playback_speed_enabled: bool = True
    audio_chorus_enabled: bool = False
    background_music_enabled: bool = False
    background_music_mix_with_source: bool = False
    background_music_volume_pct: int = Field(default=35, ge=0, le=100)
    background_music_volume_pct_min: int | None = Field(default=None, ge=0, le=100)
    background_music_volume_pct_max: int | None = Field(default=None, ge=0, le=100)
    background_music_files: list[str] = Field(default_factory=list)
    settings: UniquifySettingsModel = Field(default_factory=UniquifySettingsModel)
    random_bounds: dict[str, Any] = Field(default_factory=dict)
    text_overlay: TextOverlayModel = Field(default_factory=TextOverlayModel)
    youtube_upload_after_processing: bool = False
    upload_after: UploadAfterFollowup | None = None


class SlicingJobRequest(BaseModel):
    # Optional; must be under managed output/<platform>/slicing (or empty = that folder).
    output_dir: str = ""
    platform: str = ""
    clip_files: list[str] = Field(min_length=1)
    music_files: list[str] = Field(min_length=1)
    num_workers: int = Field(default=1, ge=1, le=1)
    copies_per_track: int = Field(default=1, ge=1)
    use_gpu: bool = False
    use_gpu_finalize: bool = False
    use_suggested_durations: bool = False
    min_scene_duration: float = Field(default=1.0, ge=0.1, le=120.0)
    max_scene_duration: float = Field(default=4.0, ge=0.1, le=120.0)
    min_scenes: int = Field(default=3, ge=1, le=100)
    max_scenes: int = Field(default=8, ge=1, le=100)
    slice_fps_mode: str = "auto"
    text_overlay: TextOverlayModel = Field(default_factory=TextOverlayModel)
    youtube_upload_after_processing: bool = False
    upload_after: UploadAfterFollowup | None = None


class StitchingJobRequest(BaseModel):
    # Optional; must be under managed output/<platform>/gluing (or empty = that folder).
    output_dir: str = ""
    platform: str = ""
    part1_files: list[str] = Field(min_length=1)
    part2_files: list[str] = Field(min_length=1)
    music_files: list[str] = Field(min_length=1)
    num_workers: int = Field(default=1, ge=1, le=1)
    copies_per_track: int = Field(default=1, ge=1)
    use_gpu: bool = False
    use_gpu_finalize: bool = False
    min_part_duration: float = Field(default=2.0, ge=0.3, le=120.0)
    max_part_duration: float = Field(default=6.0, ge=0.3, le=120.0)
    slice_fps_mode: str = "auto"
    transition: str = "cut"
    transition_duration: float = Field(default=0.4, ge=0.05, le=2.0)
    transition_random: bool = False
    text_overlay: TextOverlayModel = Field(default_factory=TextOverlayModel)
    youtube_upload_after_processing: bool = False
    upload_after: UploadAfterFollowup | None = None


class UploadJobRequest(BaseModel):
    """Gated browser upload (requires ZALIVER_API_ALLOW_BROWSER_JOBS=1)."""

    profile_ids: list[str] = Field(min_length=1)
    video_paths: list[str] = Field(default_factory=list)
    title: str = ""
    description: str = ""
    # If set — override in-memory AppState.platform for this job (UI source of truth).
    platform: str = ""
    kind: str = "local"
    token: str = ""
    base_url: str = ""
    headless: bool = True
    max_concurrent_browsers: int = Field(default=5, ge=1, le=5)
    cooldown_s: float = Field(default=0.0, ge=0.0, le=86400.0)
    publish_before_checks: bool = True
    keep_studio_title: bool = False
    schedule_publish: bool = False
    schedule_times_iso: list[str] = Field(default_factory=list)
    schedule_warmup_shorts: bool = False
    schedule_warmup_shorts_recommendations: bool = True
    schedule_warmup_search_query: str = ""
    delete_after_upload: bool = False
    # Streaming: keep accepting enqueue until producer-done (upload-as-ready).
    await_more_videos: bool = False
    planned_videos: int = Field(default=0, ge=0, le=100_000)


class UploadEnqueueRequest(BaseModel):
    """Add more videos to a streaming upload job."""

    video_paths: list[str] = Field(min_length=1)
    title: str = ""
    description: str = ""


class ProfileJobBaseRequest(BaseModel):
    """Shared fields for gated profile browser jobs."""

    profile_ids: list[str] = Field(min_length=1)
    kind: str = "local"
    token: str = ""
    base_url: str = ""
    headless: bool = True
    max_concurrent: int = Field(default=5, ge=1, le=5)
    # profile_id -> antidetect custom_data (credentials)
    profiles_custom_data: dict[str, dict[str, Any]] = Field(default_factory=dict)
    yt_oldest_names: dict[str, str] = Field(default_factory=dict)
    search_oldest_channel: bool | None = None


class AvailabilityJobRequest(ProfileJobBaseRequest):
    pass


class InstagramRegisterJobRequest(ProfileJobBaseRequest):
    pass


class Instagram2FAJobRequest(ProfileJobBaseRequest):
    pass


class ShortsWarmupModel(BaseModel):
    shorts_count: int = Field(default=10, ge=1, le=9999)
    like_probability_pct: float = Field(default=10.0, ge=0.0, le=100.0)
    subscribe_probability_pct: float = Field(default=10.0, ge=0.0, le=100.0)
    shorts_watch_min_s: int = Field(default=5, ge=1, le=600)
    shorts_watch_max_s: int = Field(default=25, ge=1, le=600)
    watch_full_video: bool = False
    shorts_recommendations: bool = True
    shorts_search_query: str = ""
    watch_horizontal_videos: bool = False
    horizontal_search_query: str = ""
    horizontal_videos_count: int = Field(default=3, ge=1, le=100)


class ReelsWarmupModel(BaseModel):
    reels_count: int = Field(default=10, ge=1, le=9999)
    like_probability_pct: float = Field(default=10.0, ge=0.0, le=100.0)
    follow_probability_pct: float = Field(default=10.0, ge=0.0, le=100.0)
    watch_min_s: int = Field(default=5, ge=1, le=600)
    watch_max_s: int = Field(default=25, ge=1, le=600)
    watch_full: bool = False
    reels_recommendations: bool = True
    reels_search_query: str = ""


class WarmupJobRequest(ProfileJobBaseRequest):
    shorts: ShortsWarmupModel = Field(default_factory=ShortsWarmupModel)
    reels: ReelsWarmupModel = Field(default_factory=ReelsWarmupModel)


class PromoteSettingsModel(BaseModel):
    subscribe_to_channels: bool = False
    shorts_count: int = Field(default=10, ge=1, le=9999)
    like_probability_pct: float = Field(default=10.0, ge=0.0, le=100.0)
    shorts_watch_min_s: int = Field(default=5, ge=1, le=600)
    shorts_watch_max_s: int = Field(default=25, ge=1, le=600)
    watch_full_video: bool = False
    enable_comments: bool = False
    comments: list[str] = Field(default_factory=list)
    comment_probability_pct: float = Field(default=50.0, ge=0.0, le=100.0)
    # Full textarea contents for recent-value history (web UI).
    comments_field: str = ""


class PromoteVideoModel(BaseModel):
    profile_id: str
    video_id: str
    url: str = ""
    title: str = ""


class PromoteJobRequest(ProfileJobBaseRequest):
    settings: PromoteSettingsModel = Field(default_factory=PromoteSettingsModel)
    videos: list[PromoteVideoModel] = Field(default_factory=list)


class CookieFarmSettingsModel(BaseModel):
    use_preset_domains: bool = True
    preset_kind: str = "intl"
    domains: list[str] = Field(default_factory=list)
    sites_count: int = Field(default=10, ge=1, le=500)
    watch_min_s: int = Field(default=15, ge=1, le=600)
    watch_max_s: int = Field(default=45, ge=1, le=600)


class CookieFarmJobRequest(ProfileJobBaseRequest):
    settings: CookieFarmSettingsModel = Field(default_factory=CookieFarmSettingsModel)


class ChannelAssignmentModel(BaseModel):
    profile_id: str
    profile_name: str = ""
    channel_name: str = ""
    channel_description: str = ""
    skip_name_change: bool = False
    video_default_title: str = ""
    avatar_path: str = ""


class ChannelSetupJobRequest(ProfileJobBaseRequest):
    description: str = ""
    description_lines: list[str] = Field(default_factory=list)
    link_title: str = ""
    link_url: str = ""
    channel_links: list[list[str]] = Field(default_factory=list)
    assignments: list[ChannelAssignmentModel] = Field(default_factory=list)
    change_language: bool = False
    headless: bool = False
    # Full textarea contents for recent-value history (web UI).
    names_field: str = ""
    description_field: str = ""
    video_titles_field: str = ""
    link_titles_field: str = ""
    link_urls_field: str = ""


class JobCreatedResponse(BaseModel):
    id: str
    kind: str
    status: str
    output_dir: str = ""


class OutputDirsResponse(BaseModel):
    root: str
    platform: str
    dirs: dict[str, str]


class SourceEntry(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: int | None = None
    abs_path: str | None = None
    created_at: str | None = None


class SourceListResponse(BaseModel):
    root: str
    path: str
    parent: str | None = None
    entries: list[SourceEntry] = Field(default_factory=list)
    # Volume stats for the browsed path (bytes); None if unavailable.
    disk_total: int | None = None
    disk_used: int | None = None
    disk_free: int | None = None


class SourceUploadResponse(BaseModel):
    paths: list[str] = Field(default_factory=list)
    relative: list[str] = Field(default_factory=list)


class SourceDeleteRequest(BaseModel):
    paths: list[str] = Field(
        default_factory=list,
        description="Relative paths under sources root to delete",
    )


class SourceMkdirRequest(BaseModel):
    parent: str = Field(
        default="",
        description="Relative parent directory under sources root",
    )
    name: str = Field(min_length=1, description="New folder name (single segment)")


class SourceMkdirResponse(BaseModel):
    path: str


class JobListResponse(BaseModel):
    jobs: list[dict[str, Any]]


class VideoItem(BaseModel):
    id: int
    path: str
    created_at: str
    added_at: str
    thumb_path: str | None = None


class UploadedVideoItem(BaseModel):
    id: int
    platform: str
    title: str
    description: str
    url: str
    video_id: str
    profile_id: str
    uploaded_at: str
    session_id: int = 0
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    stats_updated_at: str | None = None
    stats_unavailable: bool = False
    stats_unavailable_data_api: bool = False
    age_restricted: bool | None = None


class IdsRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)


class DeleteUploadedRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)
    filter: Literal["", "unavailable", "age_restricted"] = ""


class DeleteResult(BaseModel):
    deleted: int


class UploadSessionItem(BaseModel):
    id: int
    started_at: str
    planned_videos: int
    processed_videos: int
    uploaded_ok: int
    ended_at: str | None = None
    status: str = ""


class StatsRefreshRequest(BaseModel):
    """Refresh view/like/comment stats for uploaded videos."""

    video_ids: list[str] = Field(default_factory=list)
    # Prefer session scope: when set and video_ids empty, only that session.
    session_id: int | None = Field(default=None, ge=1)
    limit: int = Field(default=500, ge=1, le=2000)
    # Instagram: antidetect profile used as checker session.
    checker_profile_id: str = ""
    checker_custom_data: dict[str, Any] = Field(default_factory=dict)
    checker_proxy: str = ""


class AiPromptItem(BaseModel):
    id: str
    title: str
    text: str
    builtin: bool = False


class AiPromptsResponse(BaseModel):
    prompts: list[AiPromptItem]


class AiPromptsPutRequest(BaseModel):
    prompts: list[AiPromptItem] = Field(default_factory=list)


class AiPromptCreateRequest(BaseModel):
    title: str = ""
    text: str = ""


class AiGenerateRequest(BaseModel):
    prompt_id: str = ""
    prompt_text: str = ""
    reply_lines: int = Field(default=1, ge=1, le=500)


class AiGenerateResponse(BaseModel):
    text: str


class TitleVariableItem(BaseModel):
    token: str
    example: str
    description: str


class TitleVariablesResponse(BaseModel):
    variables: list[TitleVariableItem]
    example: str
    max_youtube_title_length: int


class RecentValuesResponse(BaseModel):
    """Recent text values for web dropdown pickers (per current platform)."""

    platform: str
    upload_titles: list[str] = Field(default_factory=list)
    channel_name_fields: list[str] = Field(default_factory=list)
    channel_descriptions: list[str] = Field(default_factory=list)
    channel_link_titles: list[str] = Field(default_factory=list)
    channel_link_urls: list[str] = Field(default_factory=list)
    video_default_title_fields: list[str] = Field(default_factory=list)
    promote_comment_fields: list[str] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    detail: str
