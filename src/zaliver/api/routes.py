"""FastAPI route handlers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile, status

from zaliver.api.ai_service import (
    add_prompt,
    delete_prompt,
    generate_text,
    list_prompts,
    put_prompts,
)
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
from zaliver.api.sources import (
    delete_sources,
    list_sources,
    mkdir_sources,
    resolve_download_files,
)
from zaliver.api.sandbox import (
    PathNotAllowedError,
    resolve_dir,
    resolve_path_list,
    resolve_under_roots,
)
from zaliver.api.output_paths import (
    OUTPUT_KIND_GLUING,
    OUTPUT_KIND_SLICING,
    OUTPUT_KIND_UNIQUIFY,
    list_managed_output_dirs,
    resolve_job_output_dir,
    resolve_managed_output_dir,
)
from zaliver.api.schemas import (
    AiGenerateRequest,
    AiGenerateResponse,
    AiPromptCreateRequest,
    AiPromptItem,
    AiPromptsPutRequest,
    AiPromptsResponse,
    AvailabilityJobRequest,
    ChannelSetupJobRequest,
    CookieFarmJobRequest,
    DeleteResult,
    DeleteUploadedRequest,
    IdsRequest,
    Instagram2FAJobRequest,
    InstagramRegisterJobRequest,
    JobCreatedResponse,
    JobListResponse,
    OutputDirsResponse,
    PlatformResponse,
    PlatformUpdate,
    PromoteJobRequest,
    SettingsGetResponse,
    SettingsPatchRequest,
    SlicingJobRequest,
    SourceListResponse,
    SourceDeleteRequest,
    SourceMkdirRequest,
    SourceMkdirResponse,
    SourceUploadResponse,
    StatsRefreshRequest,
    StitchingJobRequest,
    TitleVariableItem,
    TitleVariablesResponse,
    UniquifyJobRequest,
    UploadJobRequest,
    UploadSessionItem,
    UploadedVideoItem,
    VideoItem,
    WarmupJobRequest,
)
from zaliver.api.settings_policy import (
    SETTINGS_OPTIONAL_FILE_KEYS,
    SETTINGS_PATH_LIST_KEYS,
    is_allowed_settings_key,
    public_settings_value,
)
from zaliver.api.state import AppState
from zaliver.api.upload_runner import run_upload_job
from zaliver.api.isolated_runner import (
    slicing_runner,
    stitching_runner,
    uniquify_runner,
)
from zaliver.title_variables import (
    MAX_YOUTUBE_TITLE_LENGTH,
    TITLE_VARIABLES,
    TITLE_VARIABLES_EXAMPLE,
)


def _state(request: Request) -> AppState:
    return request.app.state.zaliver  # type: ignore[attr-defined]


def _uploaded_item(platform: str, r: Any) -> UploadedVideoItem:
    return UploadedVideoItem(
        id=int(r.id),
        platform=platform,
        title=str(r.title or ""),
        description=str(r.description or ""),
        url=str(r.url or ""),
        video_id=str(r.video_id or ""),
        profile_id=str(r.profile_id or ""),
        uploaded_at=str(r.uploaded_at or ""),
        session_id=int(getattr(r, "session_id", 0) or 0),
        view_count=r.view_count,
        like_count=r.like_count,
        comment_count=r.comment_count,
        stats_updated_at=r.stats_updated_at,
        stats_unavailable=bool(getattr(r, "stats_unavailable", False)),
        stats_unavailable_data_api=bool(
            getattr(r, "stats_unavailable_data_api", False)
        ),
        age_restricted=getattr(r, "age_restricted", None),
    )


def _is_18_plus(r: Any) -> bool:
    if getattr(r, "age_restricted", None) is True:
        return True
    title = str(getattr(r, "title", "") or "").casefold()
    desc = str(getattr(r, "description", "") or "").casefold()
    blob = f"{title}\n{desc}"
    return "18+" in blob or "18 +" in blob


def _library_download_response(root: Path, paths: list[str]):
    """Return FileResponse for one file or a temporary zip for multiple."""
    import tempfile
    import zipfile

    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask

    rels = [str(p).strip() for p in (paths or []) if str(p).strip()]
    if not rels:
        raise HTTPException(status_code=400, detail="No paths provided")
    try:
        files = resolve_download_files(root, rels)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    if len(files) == 1:
        abs_path, rel = files[0]
        return FileResponse(
            path=str(abs_path),
            filename=Path(rel).name,
            media_type="application/octet-stream",
        )

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp_path = Path(tmp.name)
    tmp.close()
    try:
        with zipfile.ZipFile(
            tmp_path, "w", compression=zipfile.ZIP_STORED
        ) as zf:
            for abs_path, rel in files:
                zf.write(abs_path, arcname=rel)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    def _cleanup() -> None:
        tmp_path.unlink(missing_ok=True)

    return FileResponse(
        path=str(tmp_path),
        filename="zaliver-files.zip",
        media_type="application/zip",
        background=BackgroundTask(_cleanup),
    )


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
            if key in SETTINGS_PATH_LIST_KEYS and isinstance(value, list):
                # Persist under roots even if a file was deleted (drop escapes).
                kept: list[str] = []
                for raw in value:
                    try:
                        kept.append(
                            str(resolve_under_roots(str(raw), st.config.allowed_roots))
                        )
                    except PathNotAllowedError:
                        continue
                value = kept
            if key.endswith("output_folder") and value:
                try:
                    value = str(
                        resolve_dir(str(value), st.config.allowed_roots, create=True)
                    )
                except PathNotAllowedError as e:
                    raise HTTPException(status_code=400, detail=str(e)) from e
            if key in SETTINGS_OPTIONAL_FILE_KEYS and value:
                try:
                    value = str(
                        resolve_under_roots(str(value), st.config.allowed_roots)
                    )
                except PathNotAllowedError as e:
                    raise HTTPException(status_code=400, detail=str(e)) from e
            if key in {"ai/api_key", "youtube/api_key"} and (
                value is None or str(value).strip() == "" or "…" in str(value)
            ):
                continue
            if key == "antydetect/local_api_token" and (
                value is not None and "…" in str(value)
            ):
                continue
            core.settings.setValue(key, value)
        core.settings.sync()
        from zaliver.api.antydetect_resolve import apply_local_api_token_from_settings

        apply_local_api_token_from_settings(core.settings)
        return _read_settings(st, None)

    @router.get("/v1/library/output-dirs", response_model=OutputDirsResponse)
    def get_output_dirs(request: Request) -> OutputDirsResponse:
        st = _state(request)
        root = st.config.resolved_output_root()
        try:
            root.mkdir(parents=True, exist_ok=True)
            dirs = list_managed_output_dirs(root, platform=st.platform)
        except OSError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        return OutputDirsResponse(
            root=str(root.resolve()),
            platform=st.platform,
            dirs=dirs,
        )

    @router.get("/v1/library/sources", response_model=SourceListResponse)
    def browse_sources(
        request: Request,
        path: str = Query(default="", description="Relative path under sources root"),
        kind: str = Query(
            default="media",
            description="media | video | audio | all",
        ),
    ) -> SourceListResponse:
        st = _state(request)
        root = st.config.resolved_sources_root()
        try:
            data = list_sources(root, rel=path, kind=(kind or "media").strip().lower())
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except OSError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        return SourceListResponse(**data)

    @router.post(
        "/v1/library/sources/upload",
        response_model=SourceUploadResponse,
    )
    async def upload_sources(
        request: Request,
        files: list[UploadFile] = File(...),
        subdir: str = Query(default="uploads"),
    ) -> SourceUploadResponse:
        st = _state(request)
        root = st.config.resolved_sources_root()
        if not files:
            raise HTTPException(status_code=400, detail="No files uploaded")
        abs_paths: list[str] = []
        rels: list[str] = []
        try:
            from zaliver.api.sources import (
                MEDIA_EXTS,
                MAX_UPLOAD_BYTES,
                MAX_UPLOAD_FILES,
                assert_under_root,
                sanitize_filename,
                resolve_sources_rel,
            )
            import uuid as _uuid

            if len(files) > MAX_UPLOAD_FILES:
                raise HTTPException(
                    status_code=400,
                    detail=f"Too many files (max {MAX_UPLOAD_FILES})",
                )
            dest_dir = resolve_sources_rel(root, subdir or "uploads")
            if dest_dir.is_symlink():
                raise ValueError("Cannot upload into a symlink")
            dest_dir.mkdir(parents=True, exist_ok=True)
            for uf in files:
                name = sanitize_filename(uf.filename or "file")
                stem = Path(name).stem
                suffix = Path(name).suffix.lower()
                if suffix not in MEDIA_EXTS:
                    raise ValueError(f"File type not allowed: {suffix or '(none)'}")
                unique = f"{stem}_{_uuid.uuid4().hex[:10]}{suffix}"
                dest = assert_under_root(root, dest_dir / unique)
                size = 0
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_BINARY"):
                    flags |= os.O_BINARY
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                try:
                    fd = os.open(dest, flags, 0o644)
                except FileExistsError as e:
                    raise ValueError("Upload target already exists") from e
                try:
                    with os.fdopen(fd, "wb") as out:
                        while True:
                            chunk = await uf.read(1024 * 1024)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > MAX_UPLOAD_BYTES:
                                raise ValueError(
                                    f"File too large (max {MAX_UPLOAD_BYTES} bytes)"
                                )
                            out.write(chunk)
                except Exception:
                    dest.unlink(missing_ok=True)
                    raise
                if size <= 0:
                    dest.unlink(missing_ok=True)
                    continue
                # Final containment check after write.
                dest = assert_under_root(root, dest)
                abs_paths.append(str(dest.resolve()))
                rels.append(dest.relative_to(root.resolve()).as_posix())
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except OSError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        if not abs_paths:
            raise HTTPException(status_code=400, detail="Empty upload")
        return SourceUploadResponse(paths=abs_paths, relative=rels)

    @router.post("/v1/library/sources/delete", response_model=DeleteResult)
    def delete_source_entries(
        body: SourceDeleteRequest, request: Request
    ) -> DeleteResult:
        st = _state(request)
        root = st.config.resolved_sources_root()
        paths = [str(p).strip() for p in (body.paths or []) if str(p).strip()]
        if not paths:
            return DeleteResult(deleted=0)
        try:
            n = delete_sources(root, paths)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except OSError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        return DeleteResult(deleted=int(n))

    @router.post("/v1/library/sources/mkdir", response_model=SourceMkdirResponse)
    def mkdir_source_folder(
        body: SourceMkdirRequest, request: Request
    ) -> SourceMkdirResponse:
        st = _state(request)
        root = st.config.resolved_sources_root()
        try:
            rel = mkdir_sources(
                root,
                parent=str(body.parent or "").strip(),
                name=str(body.name or "").strip(),
            )
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except OSError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        return SourceMkdirResponse(path=rel)

    @router.post("/v1/library/sources/download")
    def download_sources(body: SourceDeleteRequest, request: Request):
        st = _state(request)
        root = st.config.resolved_sources_root()
        return _library_download_response(root, body.paths or [])

    @router.post("/v1/library/output/mkdir", response_model=SourceMkdirResponse)
    def mkdir_output_folder(
        body: SourceMkdirRequest, request: Request
    ) -> SourceMkdirResponse:
        st = _state(request)
        root = st.config.resolved_output_root()
        try:
            root.mkdir(parents=True, exist_ok=True)
            rel = mkdir_sources(
                root,
                parent=str(body.parent or "").strip(),
                name=str(body.name or "").strip(),
            )
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except OSError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        return SourceMkdirResponse(path=rel)

    @router.get("/v1/library/output", response_model=SourceListResponse)
    def browse_output(
        request: Request,
        path: str = Query(default="", description="Relative path under output root"),
        kind: str = Query(
            default="all",
            description="media | video | audio | all",
        ),
    ) -> SourceListResponse:
        st = _state(request)
        root = st.config.resolved_output_root()
        try:
            root.mkdir(parents=True, exist_ok=True)
            data = list_sources(root, rel=path, kind=(kind or "all").strip().lower())
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except OSError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        return SourceListResponse(**data)

    @router.post("/v1/library/output/delete", response_model=DeleteResult)
    def delete_output_entries(
        body: SourceDeleteRequest, request: Request
    ) -> DeleteResult:
        st = _state(request)
        root = st.config.resolved_output_root()
        paths = [str(p).strip() for p in (body.paths or []) if str(p).strip()]
        if not paths:
            return DeleteResult(deleted=0)
        try:
            n = delete_sources(root, paths)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except OSError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        return DeleteResult(deleted=int(n))

    @router.post("/v1/library/output/download")
    def download_output(body: SourceDeleteRequest, request: Request):
        st = _state(request)
        root = st.config.resolved_output_root()
        return _library_download_response(root, body.paths or [])

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

    @router.post("/v1/library/videos/delete", response_model=DeleteResult)
    def delete_videos(body: IdsRequest, request: Request) -> DeleteResult:
        ids = [int(x) for x in body.ids if int(x) > 0]
        if not ids:
            return DeleteResult(deleted=0)
        n = _state(request).core().videos.remove_video_records(ids)
        return DeleteResult(deleted=int(n))

    @router.get("/v1/library/uploaded", response_model=list[UploadedVideoItem])
    def list_uploaded(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        session_id: int | None = Query(default=None),
    ) -> list[UploadedVideoItem]:
        st = _state(request)
        if session_id is not None and int(session_id) > 0:
            by_sess = st.core().uploads.list_uploaded_videos_for_sessions(
                [int(session_id)], platform=st.platform
            )
            rows = by_sess.get(int(session_id), []) or []
        else:
            rows = st.core().uploads.list_uploaded_videos(
                limit=limit, platform=st.platform
            )
        return [_uploaded_item(st.platform, r) for r in rows]

    @router.get("/v1/library/sessions", response_model=list[UploadSessionItem])
    def list_sessions(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[UploadSessionItem]:
        st = _state(request)
        rows = st.core().uploads.list_sessions(limit=limit, platform=st.platform)
        return [
            UploadSessionItem(
                id=int(r.id),
                started_at=str(r.started_at or ""),
                planned_videos=int(r.planned_videos or 0),
                processed_videos=int(r.processed_videos or 0),
                uploaded_ok=int(r.uploaded_ok or 0),
                ended_at=str(r.ended_at) if r.ended_at else None,
                status=str(r.status or ""),
            )
            for r in rows
        ]

    @router.post("/v1/library/uploaded/delete", response_model=DeleteResult)
    def delete_uploaded(body: DeleteUploadedRequest, request: Request) -> DeleteResult:
        st = _state(request)
        store = st.core().uploads
        ids = [int(x) for x in body.ids if int(x) > 0]
        filt = (body.filter or "").strip()
        if filt in {"unavailable", "age_restricted"}:
            rows = store.list_uploaded_videos(limit=2000, platform=st.platform)
            if filt == "unavailable":
                ids = [int(r.id) for r in rows if r.stats_unavailable]
            else:
                ids = [int(r.id) for r in rows if _is_18_plus(r)]
        if not ids:
            return DeleteResult(deleted=0)
        n = store.delete_uploaded_videos_by_ids(ids)
        return DeleteResult(deleted=int(n))

    @router.post(
        "/v1/library/uploaded/refresh-stats",
        response_model=JobCreatedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def refresh_uploaded_stats(
        body: StatsRefreshRequest, request: Request
    ) -> JobCreatedResponse:
        st = _state(request)
        store = st.core().uploads
        settings = st.core().settings
        platform = st.platform
        vids = [v.strip() for v in body.video_ids if (v or "").strip()]
        if not vids:
            sid = int(body.session_id or 0)
            if sid > 0:
                by_sess = store.list_uploaded_videos_for_sessions(
                    [sid], platform=platform
                )
                rows = by_sess.get(sid, []) or []
            else:
                raise HTTPException(
                    status_code=400,
                    detail="Укажите session_id или video_ids для проверки статистики",
                )
            vids = [
                str(r.video_id).strip()
                for r in rows
                if str(r.video_id or "").strip()
            ]
        if not vids:
            raise HTTPException(status_code=400, detail="Нет video_id для проверки")

        from zaliver.config.platform_settings import is_instagram_platform

        use_ig = is_instagram_platform(platform)

        def runner(sink, register_cancel, job_id: str = "") -> None:
            del job_id
            cancelled = {"v": False}

            def _cancel() -> None:
                cancelled["v"] = True

            register_cancel(_cancel)
            total = len(vids)
            sink.on_progress(0, total, "stats")
            if use_ig:
                _run_ig_stats_refresh(
                    sink=sink,
                    store=store,
                    platform=platform,
                    video_ids=vids,
                    body=body,
                    settings=settings,
                    cancelled=cancelled,
                )
            else:
                _run_yt_stats_refresh(
                    sink=sink,
                    store=store,
                    platform=platform,
                    video_ids=vids,
                    settings=settings,
                    cancelled=cancelled,
                )

        try:
            job = st.jobs.start(kind=JobKind.STATS_REFRESH, runner=runner)
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        return JobCreatedResponse(
            id=job.id, kind=job.kind.value, status=job.status.value
        )

    @router.get("/v1/title-variables", response_model=TitleVariablesResponse)
    def title_variables() -> TitleVariablesResponse:
        return TitleVariablesResponse(
            variables=[
                TitleVariableItem(
                    token=v.token, example=v.example, description=v.description
                )
                for v in TITLE_VARIABLES
            ],
            example=TITLE_VARIABLES_EXAMPLE,
            max_youtube_title_length=MAX_YOUTUBE_TITLE_LENGTH,
        )

    @router.get("/v1/ai/prompts", response_model=AiPromptsResponse)
    def get_ai_prompts(request: Request) -> AiPromptsResponse:
        st = _state(request)
        items = list_prompts(st.core().settings, platform=st.platform)
        return AiPromptsResponse(
            prompts=[
                AiPromptItem(id=p.id, title=p.title, text=p.text, builtin=p.builtin)
                for p in items
            ]
        )

    @router.put("/v1/ai/prompts", response_model=AiPromptsResponse)
    def put_ai_prompts(
        body: AiPromptsPutRequest, request: Request
    ) -> AiPromptsResponse:
        st = _state(request)
        items = put_prompts(
            st.core().settings,
            platform=st.platform,
            prompts=[p.model_dump() for p in body.prompts],
        )
        return AiPromptsResponse(
            prompts=[
                AiPromptItem(id=p.id, title=p.title, text=p.text, builtin=p.builtin)
                for p in items
            ]
        )

    @router.post("/v1/ai/prompts", response_model=AiPromptItem)
    def create_ai_prompt(
        body: AiPromptCreateRequest, request: Request
    ) -> AiPromptItem:
        st = _state(request)
        p = add_prompt(
            st.core().settings,
            platform=st.platform,
            title=body.title,
            text=body.text,
        )
        return AiPromptItem(id=p.id, title=p.title, text=p.text, builtin=p.builtin)

    @router.delete("/v1/ai/prompts/{prompt_id}", response_model=DeleteResult)
    def remove_ai_prompt(prompt_id: str, request: Request) -> DeleteResult:
        st = _state(request)
        ok = delete_prompt(
            st.core().settings, platform=st.platform, prompt_id=prompt_id
        )
        if not ok:
            raise HTTPException(
                status_code=400,
                detail="Нельзя удалить встроенный или неизвестный промпт",
            )
        return DeleteResult(deleted=1)

    @router.post("/v1/ai/generate", response_model=AiGenerateResponse)
    def ai_generate(body: AiGenerateRequest, request: Request) -> AiGenerateResponse:
        st = _state(request)
        try:
            text = generate_text(
                st.core().settings,
                platform=st.platform,
                prompt_id=body.prompt_id,
                prompt_text=body.prompt_text,
                reply_lines=body.reply_lines,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
        return AiGenerateResponse(text=text)

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
        return _state(request).jobs.snapshot(job, log_tail=log_tail)

    @router.post("/v1/jobs/{job_id}/cancel")
    def cancel_job(job_id: str, request: Request) -> dict[str, Any]:
        job = _state(request).jobs.cancel(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return _state(request).jobs.snapshot(job, log_tail=20)

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
                resolve_job_output_dir(
                    st.config.resolved_output_root(),
                    platform=st.platform,
                    kind=OUTPUT_KIND_UNIQUIFY,
                    requested=body.output_dir,
                    create=True,
                )
            )
            inputs = resolve_path_list(body.input_files, st.config.allowed_roots)
            music = resolve_path_list(
                body.background_music_files, st.config.allowed_roots
            )
            overlay = body.text_overlay.model_dump(exclude_none=True)
            if "orientation" in overlay:
                overlay["preview_orientation"] = overlay.pop("orientation")
            if overlay.get("custom_font_path"):
                overlay["custom_font_path"] = resolve_path_list(
                    [overlay["custom_font_path"]], st.config.allowed_roots
                )[0]
        except PathNotAllowedError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except OSError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e

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
            "brightness_enabled": bool(body.brightness_enabled),
            "contrast_enabled": bool(body.contrast_enabled),
            "saturation_enabled": bool(body.saturation_enabled),
            "crop_jitter_enabled": False,
            "scale_enabled": bool(body.scale_enabled),
            "noise_enabled": bool(body.noise_enabled),
            "seed_enabled": False,
            "playback_speed_enabled": bool(body.playback_speed_enabled),
            "audio_chorus_enabled": False,
            "background_music_enabled": bool(body.background_music_enabled),
            "background_music_mix_with_source": bool(
                body.background_music_mix_with_source
            ),
            "background_music_volume_pct": int(body.background_music_volume_pct),
            "background_music_volume_pct_min": int(
                body.background_music_volume_pct_min
                if body.background_music_volume_pct_min is not None
                else body.background_music_volume_pct
            ),
            "background_music_volume_pct_max": int(
                body.background_music_volume_pct_max
                if body.background_music_volume_pct_max is not None
                else body.background_music_volume_pct
            ),
            "background_music_files": music,
            "random_bounds": dict(body.random_bounds or {}),
            "text_overlay": overlay,
            "youtube_upload_after_processing": bool(
                body.youtube_upload_after_processing
            ),
        }

        try:
            job = st.jobs.start(
                kind=JobKind.UNIQUIFY, runner=uniquify_runner(options)
            )
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        return JobCreatedResponse(
            id=job.id,
            kind=job.kind.value,
            status=job.status.value,
            output_dir=out_dir,
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
                resolve_job_output_dir(
                    st.config.resolved_output_root(),
                    platform=st.platform,
                    kind=OUTPUT_KIND_SLICING,
                    requested=body.output_dir,
                    create=True,
                )
            )
            clips = resolve_path_list(body.clip_files, st.config.allowed_roots)
            music = resolve_path_list(body.music_files, st.config.allowed_roots)
            overlay = body.text_overlay.model_dump(exclude_none=True)
            if "orientation" in overlay:
                overlay["preview_orientation"] = overlay.pop("orientation")
            if overlay.get("custom_font_path"):
                overlay["custom_font_path"] = resolve_path_list(
                    [overlay["custom_font_path"]], st.config.allowed_roots
                )[0]
        except PathNotAllowedError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except OSError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e

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

        try:
            job = st.jobs.start(
                kind=JobKind.SLICING, runner=slicing_runner(options)
            )
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        return JobCreatedResponse(
            id=job.id,
            kind=job.kind.value,
            status=job.status.value,
            output_dir=out_dir,
        )

    @router.post(
        "/v1/jobs/stitching",
        response_model=JobCreatedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_stitching(
        body: StitchingJobRequest, request: Request
    ) -> JobCreatedResponse:
        st = _state(request)
        if body.min_part_duration > body.max_part_duration:
            raise HTTPException(
                status_code=400,
                detail="min_part_duration cannot exceed max_part_duration",
            )
        try:
            out_dir = str(
                resolve_job_output_dir(
                    st.config.resolved_output_root(),
                    platform=st.platform,
                    kind=OUTPUT_KIND_GLUING,
                    requested=body.output_dir,
                    create=True,
                )
            )
            part1 = resolve_path_list(body.part1_files, st.config.allowed_roots)
            part2 = resolve_path_list(body.part2_files, st.config.allowed_roots)
            music = resolve_path_list(body.music_files, st.config.allowed_roots)
            overlay = body.text_overlay.model_dump(exclude_none=True)
            if "orientation" in overlay:
                overlay["preview_orientation"] = overlay.pop("orientation")
            if overlay.get("custom_font_path"):
                overlay["custom_font_path"] = resolve_path_list(
                    [overlay["custom_font_path"]], st.config.allowed_roots
                )[0]
        except PathNotAllowedError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except OSError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e

        workers = min(int(body.num_workers), st.config.max_workers_per_job)
        options: dict[str, Any] = {
            "output_dir": out_dir,
            "part1_files": part1,
            "part2_files": part2,
            "music_files": music,
            "num_workers": workers,
            "copies_per_track": int(body.copies_per_track),
            "text_overlay": overlay,
            "min_part_duration": float(body.min_part_duration),
            "max_part_duration": float(body.max_part_duration),
            "use_gpu": bool(body.use_gpu),
            "use_gpu_finalize": bool(body.use_gpu_finalize),
            "slice_fps_mode": str(body.slice_fps_mode or "auto"),
            "transition": str(body.transition or "cut"),
            "transition_duration": float(body.transition_duration),
            "transition_random": bool(body.transition_random),
            "youtube_upload_after_processing": bool(
                body.youtube_upload_after_processing
            ),
        }

        try:
            job = st.jobs.start(
                kind=JobKind.STITCHING, runner=stitching_runner(options)
            )
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        return JobCreatedResponse(
            id=job.id,
            kind=job.kind.value,
            status=job.status.value,
            output_dir=out_dir,
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
        from zaliver.api.antydetect_resolve import (
            resolve_antidetect_kind,
            resolve_local_base_url,
        )

        settings = st.core().settings
        kind = resolve_antidetect_kind(settings, body.kind)
        base_url = resolve_local_base_url(settings, body.base_url)
        headless = bool(body.headless)
        max_b = int(body.max_concurrent_browsers)
        cooldown = float(body.cooldown_s)
        title = body.title
        description = body.description
        publish_before_checks = bool(body.publish_before_checks)
        keep_studio_title = bool(body.keep_studio_title)
        schedule_times_raw = [
            str(x).strip() for x in (body.schedule_times_iso or []) if str(x).strip()
        ]
        if body.schedule_publish and schedule_times_raw:
            schedule_times = schedule_times_raw
        else:
            schedule_times = []
        schedule_warmup_shorts = bool(body.schedule_warmup_shorts)
        schedule_warmup_reco = bool(body.schedule_warmup_shorts_recommendations)
        schedule_warmup_q = str(body.schedule_warmup_search_query or "").strip()
        delete_after_upload = bool(body.delete_after_upload)
        search_oldest_channel = bool(
            settings.value("youtube/search_oldest_channel", False)
        )

        def runner(sink, register_cancel, job_id: str = "") -> None:
            del job_id
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
                publish_before_checks=publish_before_checks,
                keep_studio_title=keep_studio_title,
                schedule_times=schedule_times,
                schedule_warmup_shorts=schedule_warmup_shorts,
                schedule_warmup_shorts_recommendations=schedule_warmup_reco,
                schedule_warmup_search_query=schedule_warmup_q,
                delete_after_upload=delete_after_upload,
                search_oldest_channel=search_oldest_channel,
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
            description="local | remote (default from settings, usually local)",
        ),
    ) -> dict[str, Any]:
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


def _run_yt_stats_refresh(
    *,
    sink: Any,
    store: Any,
    platform: str,
    video_ids: list[str],
    settings: Any,
    cancelled: dict[str, bool],
) -> None:
    import requests

    from zaliver.youtube_parsing.video_stats import (
        YOUTUBE_DATA_API_VIDEOS_LIST_MAX_IDS,
        YoutubeDataApiError,
        fetch_video_stats_batch,
    )

    api_key = str(settings.value("youtube/api_key", "", type=str) or "").strip()
    total = len(video_ids)
    done = 0
    ok_n = 0
    fail_n = 0
    http = requests.Session()
    try:
        step = YOUTUBE_DATA_API_VIDEOS_LIST_MAX_IDS
        for start in range(0, total, step):
            if cancelled["v"]:
                sink.on_finished(False, "Cancelled")
                return
            chunk = video_ids[start : start + step]
            try:
                batch_ok, batch_fail = fetch_video_stats_batch(
                    chunk, api_key=api_key or None, session=http
                )
            except Exception as e:
                is_api = isinstance(e, YoutubeDataApiError)
                for vid in chunk:
                    store.mark_video_stats_unavailable(
                        video_id=vid,
                        youtube_data_api_error=is_api,
                        platform=platform,
                    )
                    fail_n += 1
                done += len(chunk)
                sink.on_progress(done, total, chunk[-1])
                continue
            for st_row in batch_ok:
                store.update_video_stats(
                    video_id=st_row.video_id,
                    view_count=int(st_row.view_count),
                    like_count=st_row.like_count,
                    comment_count=st_row.comment_count,
                    age_restricted=bool(st_row.age_restricted),
                    platform=platform,
                )
                ok_n += 1
            for vid_f, msg_f in batch_fail:
                is_api = "Invalid video id" not in msg_f
                store.mark_video_stats_unavailable(
                    video_id=vid_f,
                    youtube_data_api_error=is_api,
                    platform=platform,
                )
                fail_n += 1
                sink.on_log(f"{vid_f}: {msg_f}")
            done += len(chunk)
            sink.on_progress(done, total, chunk[-1])
    finally:
        http.close()
    sink.on_finished(True, f"OK={ok_n}, fail={fail_n}")


def _run_ig_stats_refresh(
    *,
    sink: Any,
    store: Any,
    platform: str,
    video_ids: list[str],
    body: StatsRefreshRequest,
    settings: Any,
    cancelled: dict[str, bool],
) -> None:
    from zaliver.instagram_upload.instagrapi_session import ensure_instagrapi_client
    from zaliver.instagram_upload.reel_stats import fetch_reel_stats

    pid = (body.checker_profile_id or "").strip() or str(
        settings.value("instagram/stats_checker_profile_id", "", type=str) or ""
    ).strip()
    if not pid:
        sink.on_finished(False, "Не задан checker_profile_id для Instagram")
        return
    cd = dict(body.checker_custom_data or {})
    username = str(cd.get("username") or cd.get("login") or "").strip()
    password = str(cd.get("password") or "").strip()
    twofa = str(cd.get("twofa_secret") or cd.get("2fa") or "").strip()
    proxy = (body.checker_proxy or "").strip()
    try:
        client, _ = ensure_instagrapi_client(
            pid,
            username=username,
            password=password,
            twofa_secret=twofa,
            allow_dump=True,
            proxy=proxy,
        )
    except Exception as e:
        sink.on_finished(False, f"Не удалось открыть IG-сессию: {e}")
        return

    total = len(video_ids)
    ok_n = 0
    fail_n = 0
    for i, vid in enumerate(video_ids):
        if cancelled["v"]:
            sink.on_finished(False, "Cancelled")
            return
        try:
            st_row = fetch_reel_stats(client, vid)
            store.update_video_stats(
                video_id=vid,
                view_count=int(getattr(st_row, "view_count", 0) or 0),
                like_count=getattr(st_row, "like_count", None),
                comment_count=getattr(st_row, "comment_count", None),
                age_restricted=False,
                platform=platform,
            )
            ok_n += 1
        except Exception as e:
            store.mark_video_stats_unavailable(video_id=vid, platform=platform)
            fail_n += 1
            sink.on_log(f"{vid}: {e}")
        sink.on_progress(i + 1, total, vid)
    sink.on_finished(True, f"OK={ok_n}, fail={fail_n}")
