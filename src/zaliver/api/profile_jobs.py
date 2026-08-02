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
from zaliver.api.user_limits import assert_browser_budget
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


def _resolve_token_base(
    state: AppState,
    body: ProfileJobBaseRequest,
    *,
    username: str,
    session_token: str = "",
) -> tuple[str, str, str]:
    from zaliver.api.antydetect_resolve import (
        resolve_antidetect_kind,
        resolve_local_base_url,
        resolve_local_api_token_setting,
    )

    settings = state.user_settings(username)
    kind = resolve_antidetect_kind(settings, body.kind)
    token = (body.token or "").strip()
    if not token:
        token = (session_token or "").strip()
    if not token:
        token = resolve_local_api_token_setting(settings)
    if not token:
        token = str(settings.value("antydetect/dolphin_token", "") or "").strip()
    base_url = resolve_local_base_url(settings, body.base_url)
    return kind, token, base_url


def _coerce_setting_bool(raw: Any, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    s = str(raw).strip().lower()
    if not s:
        return default
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return default


def _resolve_search_oldest_channel(
    state: AppState, body: ProfileJobBaseRequest, *, username: str
) -> bool:
    fields_set = getattr(body, "model_fields_set", None) or set()
    if "search_oldest_channel" in fields_set and body.search_oldest_channel is not None:
        return bool(body.search_oldest_channel)
    return _coerce_setting_bool(
        state.user_settings(username).value("youtube/search_oldest_channel", False),
        False,
    )


def _base_request(
    state: AppState,
    body: ProfileJobBaseRequest,
    *,
    kind: str,
    username: str,
    session_token: str = "",
) -> ProfileJobRequest:
    antidetect_kind, token, base_url = _resolve_token_base(
        state, body, username=username, session_token=session_token
    )
    profile_ids = [p.strip() for p in body.profile_ids if (p or "").strip()]
    if not profile_ids:
        raise HTTPException(status_code=400, detail="profile_ids required")
    try:
        max_c = assert_browser_budget(
            state.jobs,
            username,
            requested_slots=int(body.max_concurrent),
        )
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return ProfileJobRequest(
        kind=kind,  # type: ignore[arg-type]
        profile_ids=profile_ids,
        platform=state.platform_for_user(username),
        antidetect_kind=antidetect_kind,
        token=token,
        base_url=base_url,
        headless=True,
        max_concurrent=max_c,
        profiles_custom_data=dict(body.profiles_custom_data or {}),
        yt_oldest_names=dict(body.yt_oldest_names or {}),
        search_oldest_channel=_resolve_search_oldest_channel(
            state, body, username=username
        ),
    )


def _start(
    state: AppState,
    *,
    job_kind: str,
    request: ProfileJobRequest,
    owner: str,
) -> Any:
    _require_browser_jobs(state)
    registry_kind = _KIND_MAP[job_kind]
    service = ProfileJobsService()

    def runner(sink: JobProgressSink, register_cancel, job_id: str = "") -> None:
        del job_id
        service.run(request, sink, register_cancel=register_cancel)

    try:
        job = state.jobs.start(
            kind=registry_kind,
            runner=runner,
            owner=owner,
            browser_slots=int(request.max_concurrent or 0),
        )
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return job


def start_availability(
    state: AppState, body: AvailabilityJobRequest, *, username: str, session_token: str = ""
):
    req = _base_request(state, body, kind="availability", username=username, session_token=session_token)
    return _start(state, job_kind="availability", request=req, owner=username)


def start_instagram_register(
    state: AppState, body: InstagramRegisterJobRequest, *, username: str, session_token: str = ""
):
    _require_browser_jobs(state)
    if state.platform_for_user(username) != "instagram":
        raise HTTPException(
            status_code=400, detail="instagram_register requires platform=instagram"
        )
    req = _base_request(
        state, body, kind="instagram_register", username=username, session_token=session_token
    )
    return _start(state, job_kind="instagram_register", request=req, owner=username)


def start_instagram_2fa(
    state: AppState, body: Instagram2FAJobRequest, *, username: str, session_token: str = ""
):
    _require_browser_jobs(state)
    if state.platform_for_user(username) != "instagram":
        raise HTTPException(
            status_code=400, detail="instagram_2fa requires platform=instagram"
        )
    req = _base_request(
        state, body, kind="instagram_2fa", username=username, session_token=session_token
    )
    return _start(state, job_kind="instagram_2fa", request=req, owner=username)


def start_warmup(
    state: AppState, body: WarmupJobRequest, *, username: str, session_token: str = ""
):
    req = _base_request(
        state, body, kind="warmup", username=username, session_token=session_token
    )
    req.warmup_shorts = ShortsWarmupSettings(**body.shorts.model_dump())
    req.warmup_reels = ReelsWarmupSettings(**body.reels.model_dump())
    return _start(state, job_kind="warmup", request=req, owner=username)


def start_promote(
    state: AppState, body: PromoteJobRequest, *, username: str, session_token: str = ""
):
    from zaliver.api.recent_values import remember_promote_comments

    req = _base_request(
        state, body, kind="promote", username=username, session_token=session_token
    )
    settings_data = body.settings.model_dump(exclude={"comments_field"})
    req.promote = PromoteSettings(**settings_data)
    req.promote_videos = [
        PromoteTargetVideo(
            profile_id=v.profile_id,
            video_id=v.video_id,
            url=v.url,
            title=v.title,
        )
        for v in body.videos
    ]
    plat = state.platform_for_user(username)
    remember_promote_comments(state.core().uploads, body, platform=plat)
    return _start(state, job_kind="promote", request=req, owner=username)


def start_cookie_farm(
    state: AppState, body: CookieFarmJobRequest, *, username: str, session_token: str = ""
):
    req = _base_request(
        state, body, kind="cookie_farm", username=username, session_token=session_token
    )
    req.cookie_farm = CookieFarmSettings(**body.settings.model_dump())
    return _start(state, job_kind="cookie_farm", request=req, owner=username)


def start_channel_setup(
    state: AppState, body: ChannelSetupJobRequest, *, username: str, session_token: str = ""
):
    from zaliver.api.recent_values import remember_channel_setup

    req = _base_request(
        state, body, kind="channel_setup", username=username, session_token=session_token
    )
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
    plat = state.platform_for_user(username)
    remember_channel_setup(state.core().uploads, body, platform=plat)
    return _start(state, job_kind="channel_setup", request=req, owner=username)
