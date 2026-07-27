"""Helpers to start gated profile jobs via JobRegistry."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from zaliver.api.jobs_registry import JobKind
from zaliver.api.sandbox import PathNotAllowedError, resolve_existing_file
from zaliver.api.schemas import (
    AvailabilityJobRequest,
    ChannelSetupJobRequest,
    CookieFarmJobRequest,
    Instagram2FAJobRequest,
    InstagramRegisterJobRequest,
    ProfileJobBaseRequest,
    PromoteJobRequest,
    WarmupJobRequest,
)
from zaliver.api.state import AppState
from zaliver.core.profiles import (
    ChannelAssignment,
    CookieFarmSettings,
    ProfileJobRequest,
    ProfileJobsService,
    PromoteSettings,
    PromoteTargetVideo,
    ReelsWarmupSettings,
    ShortsWarmupSettings,
)
from zaliver.core.sinks import JobProgressSink


_KIND_MAP: dict[str, JobKind] = {
    "availability": JobKind.AVAILABILITY,
    "instagram_register": JobKind.INSTAGRAM_REGISTER,
    "instagram_2fa": JobKind.INSTAGRAM_2FA,
    "channel_setup": JobKind.CHANNEL_SETUP,
    "warmup": JobKind.WARMUP,
    "promote": JobKind.PROMOTE,
    "cookie_farm": JobKind.COOKIE_FARM,
}


def _require_browser_jobs(state: AppState) -> None:
    if not state.config.allow_browser_jobs:
        raise HTTPException(
            status_code=403,
            detail=(
                "Browser/profile jobs are disabled. "
                "Set ZALIVER_API_ALLOW_BROWSER_JOBS=1 to enable."
            ),
        )


def _resolve_token_base(state: AppState, body: ProfileJobBaseRequest) -> tuple[str, str, str]:
    from zaliver.api.antydetect_resolve import (
        resolve_antidetect_kind,
        resolve_local_base_url,
    )

    settings = state.core().settings
    kind = resolve_antidetect_kind(settings, body.kind)
    token = (body.token or "").strip()
    if not token:
        token = str(settings.value("antydetect/dolphin_token", "") or "").strip()
    base_url = resolve_local_base_url(settings, body.base_url)
    return kind, token, base_url


def _base_request(
    state: AppState,
    body: ProfileJobBaseRequest,
    *,
    kind: str,
) -> ProfileJobRequest:
    antidetect_kind, token, base_url = _resolve_token_base(state, body)
    profile_ids = [p.strip() for p in body.profile_ids if (p or "").strip()]
    if not profile_ids:
        raise HTTPException(status_code=400, detail="profile_ids required")
    return ProfileJobRequest(
        kind=kind,  # type: ignore[arg-type]
        profile_ids=profile_ids,
        platform=state.platform,
        antidetect_kind=antidetect_kind,
        token=token,
        base_url=base_url,
        headless=bool(body.headless),
        max_concurrent=int(body.max_concurrent),
        profiles_custom_data=dict(body.profiles_custom_data or {}),
        yt_oldest_names=dict(body.yt_oldest_names or {}),
        search_oldest_channel=bool(body.search_oldest_channel),
    )


def _start(
    state: AppState,
    *,
    job_kind: str,
    request: ProfileJobRequest,
) -> Any:
    _require_browser_jobs(state)
    registry_kind = _KIND_MAP[job_kind]
    service = ProfileJobsService()

    def runner(sink: JobProgressSink, register_cancel) -> None:
        service.run(request, sink, register_cancel=register_cancel)

    try:
        job = state.jobs.start(kind=registry_kind, runner=runner)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return job


def start_availability(state: AppState, body: AvailabilityJobRequest):
    req = _base_request(state, body, kind="availability")
    return _start(state, job_kind="availability", request=req)


def start_instagram_register(state: AppState, body: InstagramRegisterJobRequest):
    _require_browser_jobs(state)
    if state.platform != "instagram":
        raise HTTPException(
            status_code=400, detail="instagram_register requires platform=instagram"
        )
    req = _base_request(state, body, kind="instagram_register")
    return _start(state, job_kind="instagram_register", request=req)


def start_instagram_2fa(state: AppState, body: Instagram2FAJobRequest):
    _require_browser_jobs(state)
    if state.platform != "instagram":
        raise HTTPException(
            status_code=400, detail="instagram_2fa requires platform=instagram"
        )
    req = _base_request(state, body, kind="instagram_2fa")
    return _start(state, job_kind="instagram_2fa", request=req)


def start_warmup(state: AppState, body: WarmupJobRequest):
    req = _base_request(state, body, kind="warmup")
    req.warmup_shorts = ShortsWarmupSettings(**body.shorts.model_dump())
    req.warmup_reels = ReelsWarmupSettings(**body.reels.model_dump())
    return _start(state, job_kind="warmup", request=req)


def start_promote(state: AppState, body: PromoteJobRequest):
    req = _base_request(state, body, kind="promote")
    req.promote = PromoteSettings(**body.settings.model_dump())
    req.promote_videos = [
        PromoteTargetVideo(
            profile_id=v.profile_id,
            video_id=v.video_id,
            url=v.url,
            title=v.title,
        )
        for v in body.videos
    ]
    return _start(state, job_kind="promote", request=req)


def start_cookie_farm(state: AppState, body: CookieFarmJobRequest):
    req = _base_request(state, body, kind="cookie_farm")
    req.cookie_farm = CookieFarmSettings(**body.settings.model_dump())
    return _start(state, job_kind="cookie_farm", request=req)


def start_channel_setup(state: AppState, body: ChannelSetupJobRequest):
    req = _base_request(state, body, kind="channel_setup")
    req.headless = False
    req.channel_description = body.description
    req.channel_description_lines = list(body.description_lines or [])
    req.link_title = body.link_title
    req.link_url = body.link_url
    req.change_language = bool(body.change_language)
    links: list[tuple[str, str]] = []
    for pair in body.channel_links or []:
        if len(pair) >= 2:
            links.append((str(pair[0]), str(pair[1])))
    req.channel_links = links
    assignments: list[ChannelAssignment] = []
    for a in body.assignments or []:
        avatar = (a.avatar_path or "").strip()
        if avatar:
            try:
                avatar = str(resolve_existing_file(avatar, state.config.allowed_roots))
            except PathNotAllowedError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
        assignments.append(
            ChannelAssignment(
                profile_id=a.profile_id,
                profile_name=a.profile_name,
                channel_name=a.channel_name,
                channel_description=a.channel_description,
                skip_name_change=a.skip_name_change,
                video_default_title=a.video_default_title,
                avatar_path=avatar,
            )
        )
    req.channel_assignments = assignments
    return _start(state, job_kind="channel_setup", request=req)
