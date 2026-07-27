"""FastAPI route handlers."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status

from zaliver.api.jobs_registry import JobKind
from zaliver.api.profile_jobs import (
    start_availability,
    start_channel_setup,
    start_cookie_farm,
    start_instagram_2fa,
    start_instagram_register,
    start_promote,
    start_warmup,
)
from zaliver.api.sandbox import PathNotAllowedError, resolve_dir, resolve_path_list
from zaliver.api.schemas import (
    AvailabilityJobRequest,
    ChannelSetupJobRequest,
    CookieFarmJobRequest,
    Instagram2FAJobRequest,
    InstagramRegisterJobRequest,
    JobCreatedResponse,
    JobListResponse,
    PlatformResponse,
    PlatformUpdate,
    PromoteJobRequest,
    SettingsGetResponse,
    SettingsPatchRequest,
    SlicingJobRequest,
    UniquifyJobRequest,
    UploadJobRequest,
    UploadedVideoItem,
    VideoItem,
    WarmupJobRequest,
)
from zaliver.api.settings_policy import (
    is_allowed_settings_key,
    public_settings_value,
)
from zaliver.api.state import AppState
from zaliver.api.upload_runner import run_upload_job
from zaliver.processing.slicing_worker import SlicingService
from zaliver.processing.thread_worker import ProcessingService


def _state(request: Request) -> AppState:
    return request.app.state.zaliver  # type: ignore[attr-defined]


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/v1/platform", response_model=PlatformResponse)
    def get_platform(request: Request) -> PlatformResponse:
        return PlatformResponse(platform=_state(request).platform)

    @router.put("/v1/platform", response_model=PlatformResponse)
    def set_platform(body: PlatformUpdate, request: Request) -> PlatformResponse:
        plat = _state(request).set_platform(body.platform)
        return PlatformResponse(platform=plat)

    @router.get("/v1/settings", response_model=SettingsGetResponse)
    def get_settings(
        request: Request,
        keys: str | None = Query(
            default=None,
            description="Comma-separated allowlisted keys; default = all allowlisted present",
        ),
    ) -> SettingsGetResponse:
        return _read_settings(_state(request), keys)

    def _read_settings(st: AppState, keys: str | None) -> SettingsGetResponse:
        core = st.core()
        if keys:
            wanted = [k.strip() for k in keys.split(",") if k.strip()]
        else:
            from zaliver.api.settings_policy import SETTINGS_ALLOWLIST

            wanted = sorted(SETTINGS_ALLOWLIST)
        values: dict[str, Any] = {}
        for key in wanted:
            if not is_allowed_settings_key(key):
                raise HTTPException(
                    status_code=400, detail=f"Settings key not allowlisted: {key}"
                )
            if not core.settings.contains(key):
                continue
            values[key] = public_settings_value(key, core.settings.value(key))
        return SettingsGetResponse(platform=st.platform, values=values)

    @router.patch("/v1/settings", response_model=SettingsGetResponse)
    def patch_settings(
        body: SettingsPatchRequest, request: Request
    ) -> SettingsGetResponse:
        st = _state(request)
        core = st.core()
        for key, value in body.values.items():
            if not is_allowed_settings_key(key):
                raise HTTPException(
                    status_code=400, detail=f"Settings key not allowlisted: {key}"
                )
            # Refuse path escapes via settings lists
            if key in {"input_files", "background_music_files"} and isinstance(
                value, list
            ):
                try:
                    value = resolve_path_list(
                        [str(x) for x in value], st.config.allowed_roots
                    )
                except PathNotAllowedError as e:
                    raise HTTPException(status_code=400, detail=str(e)) from e
            if key in {"output_folder", "text_overlay_font_path"} and value:
                try:
                    if key == "output_folder":
                        value = str(
                            resolve_dir(
                                str(value), st.config.allowed_roots, create=True
                            )
                        )
                    else:
                        value = str(
                            resolve_path_list([str(value)], st.config.allowed_roots)[0]
                        )
                except PathNotAllowedError as e:
                    raise HTTPException(status_code=400, detail=str(e)) from e
            core.settings.setValue(key, value)
        core.settings.sync()
        return _read_settings(st, None)

    @router.get("/v1/library/videos", response_model=list[VideoItem])
    def list_videos(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[VideoItem]:
        rows = _state(request).core().videos.list_videos(limit=limit)
        return [
            VideoItem(
                id=r.id,
                path=r.path,
                created_at=r.created_at,
                added_at=r.added_at,
                thumb_path=r.thumb_path,
            )
            for r in rows
        ]

    @router.get("/v1/library/uploaded", response_model=list[UploadedVideoItem])
    def list_uploaded(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[UploadedVideoItem]:
        st = _state(request)
        rows = st.core().uploads.list_uploaded_videos(
            limit=limit, platform=st.platform
        )
        out: list[UploadedVideoItem] = []
        for r in rows:
            out.append(
                UploadedVideoItem(
                    id=int(r.id),
                    platform=st.platform,
                    title=str(r.title or ""),
                    description=str(r.description or ""),
                    url=str(r.url or ""),
                    video_id=str(r.video_id or ""),
                    profile_id=str(r.profile_id or ""),
                    uploaded_at=str(r.uploaded_at or ""),
                )
            )
        return out

    @router.get("/v1/jobs", response_model=JobListResponse)
    def list_jobs(
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> JobListResponse:
        return JobListResponse(jobs=_state(request).jobs.list_jobs(limit=limit))

    @router.get("/v1/jobs/{job_id}")
    def get_job(
        job_id: str,
        request: Request,
        log_tail: int = Query(default=100, ge=0, le=5000),
    ) -> dict[str, Any]:
        job = _state(request).jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return job.snapshot(log_tail=log_tail)

    @router.post("/v1/jobs/{job_id}/cancel")
    def cancel_job(job_id: str, request: Request) -> dict[str, Any]:
        job = _state(request).jobs.cancel(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return job.snapshot(log_tail=20)

    @router.post(
        "/v1/jobs/uniquify",
        response_model=JobCreatedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_uniquify(
        body: UniquifyJobRequest, request: Request
    ) -> JobCreatedResponse:
        st = _state(request)
        try:
            out_dir = str(
                resolve_dir(body.output_dir, st.config.allowed_roots, create=True)
            )
            inputs = resolve_path_list(body.input_files, st.config.allowed_roots)
            music = resolve_path_list(
                body.background_music_files, st.config.allowed_roots
            )
            overlay = body.text_overlay.model_dump()
            if "orientation" in overlay:
                overlay["preview_orientation"] = overlay.pop("orientation")
            if overlay.get("custom_font_path"):
                overlay["custom_font_path"] = resolve_path_list(
                    [overlay["custom_font_path"]], st.config.allowed_roots
                )[0]
        except PathNotAllowedError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        workers = min(int(body.num_workers), st.config.max_workers_per_job)
        options: dict[str, Any] = {
            "input_dir": "",
            "output_dir": out_dir,
            "input_files": inputs,
            "num_workers": workers,
            "use_gpu": bool(body.use_gpu),
            "use_gpu_finalize": bool(body.use_gpu_finalize),
            "settings": body.settings.model_dump(),
            "randomize_uniquify": bool(body.randomize_uniquify),
            "copies_per_file": int(body.copies_per_file),
            "one_copy_no_effects": bool(body.one_copy_no_effects),
            "playback_speed_enabled": bool(body.playback_speed_enabled),
            "audio_chorus_enabled": bool(body.audio_chorus_enabled),
            "background_music_enabled": bool(body.background_music_enabled),
            "background_music_mix_with_source": bool(
                body.background_music_mix_with_source
            ),
            "background_music_volume_pct": int(body.background_music_volume_pct),
            "background_music_files": music,
            "random_bounds": dict(body.random_bounds or {}),
            "text_overlay": overlay,
            "youtube_upload_after_processing": bool(
                body.youtube_upload_after_processing
            ),
        }

        def runner(sink, register_cancel) -> None:
            svc = ProcessingService(sink)
            register_cancel(svc.cancel)
            svc.run(options)

        try:
            job = st.jobs.start(kind=JobKind.UNIQUIFY, runner=runner)
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        return JobCreatedResponse(
            id=job.id, kind=job.kind.value, status=job.status.value
        )

    @router.post(
        "/v1/jobs/slicing",
        response_model=JobCreatedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_slicing(body: SlicingJobRequest, request: Request) -> JobCreatedResponse:
        st = _state(request)
        if body.min_scenes > body.max_scenes:
            raise HTTPException(
                status_code=400, detail="min_scenes cannot exceed max_scenes"
            )
        if (
            not body.use_suggested_durations
            and body.min_scene_duration > body.max_scene_duration
        ):
            raise HTTPException(
                status_code=400,
                detail="min_scene_duration cannot exceed max_scene_duration",
            )
        try:
            out_dir = str(
                resolve_dir(body.output_dir, st.config.allowed_roots, create=True)
            )
            clips = resolve_path_list(body.clip_files, st.config.allowed_roots)
            music = resolve_path_list(body.music_files, st.config.allowed_roots)
            overlay = body.text_overlay.model_dump()
            if "orientation" in overlay:
                overlay["preview_orientation"] = overlay.pop("orientation")
            if overlay.get("custom_font_path"):
                overlay["custom_font_path"] = resolve_path_list(
                    [overlay["custom_font_path"]], st.config.allowed_roots
                )[0]
        except PathNotAllowedError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        workers = min(int(body.num_workers), st.config.max_workers_per_job)
        options: dict[str, Any] = {
            "output_dir": out_dir,
            "clip_files": clips,
            "music_files": music,
            "num_workers": workers,
            "copies_per_track": int(body.copies_per_track),
            "text_overlay": overlay,
            "use_suggested_durations": bool(body.use_suggested_durations),
            "min_scene_duration": float(body.min_scene_duration),
            "max_scene_duration": float(body.max_scene_duration),
            "min_scenes": int(body.min_scenes),
            "max_scenes": int(body.max_scenes),
            "use_gpu": bool(body.use_gpu),
            "use_gpu_finalize": bool(body.use_gpu_finalize),
            "slice_fps_mode": str(body.slice_fps_mode or "auto"),
            "youtube_upload_after_processing": bool(
                body.youtube_upload_after_processing
            ),
        }

        def runner(sink, register_cancel) -> None:
            svc = SlicingService(sink)
            register_cancel(svc.cancel)
            svc.run(options)

        try:
            job = st.jobs.start(kind=JobKind.SLICING, runner=runner)
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        return JobCreatedResponse(
            id=job.id, kind=job.kind.value, status=job.status.value
        )

    @router.post(
        "/v1/jobs/upload",
        response_model=JobCreatedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_upload(body: UploadJobRequest, request: Request) -> JobCreatedResponse:
        st = _state(request)
        if not st.config.allow_browser_jobs:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Browser upload jobs are disabled. "
                    "Set ZALIVER_API_ALLOW_BROWSER_JOBS=1 to enable."
                ),
            )
        try:
            videos = resolve_path_list(body.video_paths, st.config.allowed_roots)
        except PathNotAllowedError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        token = (body.token or "").strip()
        if not token:
            token = (
                str(
                    st.core().settings.value("antydetect/dolphin_token", "") or ""
                ).strip()
            )
        profile_ids = [p.strip() for p in body.profile_ids if (p or "").strip()]
        if not profile_ids:
            raise HTTPException(status_code=400, detail="profile_ids required")

        platform = st.platform
        kind = (body.kind or "dolphin").strip()
        base_url = (body.base_url or "").strip()
        headless = bool(body.headless)
        max_b = int(body.max_concurrent_browsers)
        cooldown = float(body.cooldown_s)
        title = body.title
        description = body.description

        def runner(sink, register_cancel) -> None:
            run_upload_job(
                platform=platform,
                profile_ids=profile_ids,
                video_paths=videos,
                title=title,
                description=description,
                kind=kind,
                token=token,
                base_url=base_url,
                headless=headless,
                max_concurrent=max_b,
                cooldown_s=cooldown,
                sink=sink,
                register_cancel=register_cancel,
            )

        try:
            job = st.jobs.start(kind=JobKind.UPLOAD, runner=runner)
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        return JobCreatedResponse(
            id=job.id, kind=job.kind.value, status=job.status.value
        )

    def _profile_job_response(job) -> JobCreatedResponse:
        return JobCreatedResponse(
            id=job.id, kind=job.kind.value, status=job.status.value
        )

    @router.post(
        "/v1/jobs/profiles/availability",
        response_model=JobCreatedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def job_availability(
        body: AvailabilityJobRequest, request: Request
    ) -> JobCreatedResponse:
        return _profile_job_response(start_availability(_state(request), body))

    @router.post(
        "/v1/jobs/profiles/instagram-register",
        response_model=JobCreatedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def job_ig_register(
        body: InstagramRegisterJobRequest, request: Request
    ) -> JobCreatedResponse:
        return _profile_job_response(start_instagram_register(_state(request), body))

    @router.post(
        "/v1/jobs/profiles/instagram-2fa",
        response_model=JobCreatedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def job_ig_2fa(
        body: Instagram2FAJobRequest, request: Request
    ) -> JobCreatedResponse:
        return _profile_job_response(start_instagram_2fa(_state(request), body))

    @router.post(
        "/v1/jobs/profiles/channel-setup",
        response_model=JobCreatedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def job_channel_setup(
        body: ChannelSetupJobRequest, request: Request
    ) -> JobCreatedResponse:
        return _profile_job_response(start_channel_setup(_state(request), body))

    @router.post(
        "/v1/jobs/profiles/warmup",
        response_model=JobCreatedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def job_warmup(body: WarmupJobRequest, request: Request) -> JobCreatedResponse:
        return _profile_job_response(start_warmup(_state(request), body))

    @router.post(
        "/v1/jobs/profiles/promote",
        response_model=JobCreatedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def job_promote(body: PromoteJobRequest, request: Request) -> JobCreatedResponse:
        return _profile_job_response(start_promote(_state(request), body))

    @router.post(
        "/v1/jobs/profiles/cookie-farm",
        response_model=JobCreatedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def job_cookie_farm(
        body: CookieFarmJobRequest, request: Request
    ) -> JobCreatedResponse:
        return _profile_job_response(start_cookie_farm(_state(request), body))

    @router.get("/v1/antidetect/profiles")
    def list_profiles(
        request: Request,
        kind: str | None = Query(
            default=None,
            description="dolphin | local | remote (default from settings, usually local)",
        ),
    ) -> dict[str, Any]:
        """List antidetect profiles. Local API is default (no Dolphin token)."""
        st = _state(request)
        if not st.config.allow_browser_jobs:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Antidetect endpoints require ZALIVER_API_ALLOW_BROWSER_JOBS=1."
                ),
            )
        from zaliver.api.antydetect_resolve import list_antidetect_profiles

        try:
            return list_antidetect_profiles(st.core().settings, kind=kind)
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e

    return router
