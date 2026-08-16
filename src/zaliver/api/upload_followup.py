"""Server-side «залив после обработки» / «по мере готовности».

Runs inside the processing job callbacks so upload does not depend on the web UI poller.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from zaliver.api.jobs_registry import JobKind, JobRecord, JobRegistry
from zaliver.processing.ready_buffer import (
    compute_ready_buffer_limit,
    default_consumed_path,
)


STREAMING_UPLOAD_WORKERS = 1


def compute_min_ready(*, profile_count: int, planned: int) -> int:
    return compute_ready_buffer_limit(profile_count=profile_count, planned=planned)


def build_upload_runner(
    st: Any,
    *,
    cfg: dict[str, Any],
    video_paths: list[str],
    await_more: bool,
    planned_videos: int,
) -> Callable:
    """Build the same runner used by POST /v1/jobs/upload."""
    from zaliver.api.antydetect_resolve import (
        resolve_antidetect_kind,
        resolve_local_base_url,
    )
    from zaliver.api.upload_runner import run_upload_job

    settings = (
        st.user_settings(str(cfg.get("owner") or ""))
        if cfg.get("owner")
        else st.core().settings
    )
    platform = str(cfg.get("platform") or st.platform or "").strip()
    if platform:
        from zaliver.config.platform_settings import normalize_platform

        platform = normalize_platform(platform)
    else:
        platform = st.platform

    kind = resolve_antidetect_kind(settings, str(cfg.get("kind") or "local"))
    base_url = resolve_local_base_url(settings, str(cfg.get("base_url") or ""))
    token = str(cfg.get("token") or "").strip()
    if not token:
        token = str(
            settings.value("antydetect/dolphin_token", "") or ""
        ).strip()

    profile_ids = [
        str(p).strip() for p in (cfg.get("profile_ids") or []) if str(p).strip()
    ]
    title = str(cfg.get("title") or "")
    description = str(cfg.get("description") or "")
    headless = True
    from zaliver.api.user_limits import clamp_browsers_per_user

    max_b = clamp_browsers_per_user(cfg.get("max_concurrent_browsers") or 5)
    publish_before_checks = bool(cfg.get("publish_before_checks", True))
    keep_studio_title = bool(cfg.get("keep_studio_title", False))
    schedule_times_raw = [
        str(x).strip()
        for x in (cfg.get("schedule_times_iso") or [])
        if str(x).strip()
    ]
    schedule_times = (
        schedule_times_raw if cfg.get("schedule_publish") and schedule_times_raw else []
    )
    schedule_warmup_shorts = bool(cfg.get("schedule_warmup_shorts", False))
    schedule_warmup_reco = bool(
        cfg.get("schedule_warmup_shorts_recommendations", True)
    )
    schedule_warmup_q = str(cfg.get("schedule_warmup_search_query") or "").strip()
    schedule_warmup_htag = str(cfg.get("schedule_warmup_hashtag") or "").strip()
    delete_after_upload = bool(cfg.get("delete_after_upload", False))
    search_oldest_channel = bool(
        settings.value("youtube/search_oldest_channel", False)
    )
    # Web: stats notifications use the account login (job owner) as username.
    stats_username = str(cfg.get("owner") or "").strip()
    upload_store = st.core().uploads
    paths = list(video_paths)
    planned = max(0, int(planned_videos or 0))
    consumed = str(cfg.get("ready_consumed_path") or "").strip()

    def runner(sink, register_cancel, job_id: str = "") -> None:
        run_upload_job(
            platform=platform,
            profile_ids=profile_ids,
            video_paths=paths,
            title=title,
            description=description,
            kind=kind,
            token=token,
            base_url=base_url,
            headless=headless,
            max_concurrent=max_b,
            cooldown_s=0.0,
            sink=sink,
            register_cancel=register_cancel,
            publish_before_checks=publish_before_checks,
            keep_studio_title=keep_studio_title,
            schedule_times=schedule_times,
            schedule_warmup_shorts=schedule_warmup_shorts,
            schedule_warmup_shorts_recommendations=schedule_warmup_reco,
            schedule_warmup_search_query=schedule_warmup_q,
            schedule_warmup_hashtag=schedule_warmup_htag,
            delete_after_upload=delete_after_upload,
            search_oldest_channel=search_oldest_channel,
            upload_store=upload_store,
            stats_server_username=stats_username,
            settings=settings,
            await_more_videos=bool(await_more),
            planned_videos=planned,
            job_id=job_id,
            ready_consumed_path=consumed,
        )

    return runner


class UploadFollowup:
    """Attached to a processing JobRecord; driven by output/finished callbacks."""

    def __init__(
        self,
        *,
        st: Any,
        jobs: JobRegistry,
        cfg: dict[str, Any],
        planned_videos: int,
    ) -> None:
        self._st = st
        self._jobs = jobs
        self._cfg = dict(cfg)
        self._planned = max(0, int(planned_videos or 0))
        self._streaming = bool(cfg.get("upload_as_ready"))
        n_prof = len(
            [p for p in (cfg.get("profile_ids") or []) if str(p or "").strip()]
        )
        self._min_ready = compute_min_ready(
            profile_count=n_prof, planned=self._planned
        )
        self._lock = threading.Lock()
        self._upload_job_id: str | None = None
        self._enqueued: set[str] = set()
        self._producer_done = False
        self._batch_done = False
        self._starting = False

    def bind_job(self, job: JobRecord) -> None:
        job.linked_upload_job_id = ""
        job.upload_followup_active = self._streaming
        job.upload_followup_min_ready = self._min_ready

    def on_outputs(self, job: JobRecord, paths: list[str]) -> None:
        fresh = [p for p in paths if p and p not in self._enqueued]
        if not fresh and self._upload_job_id:
            return
        with self._lock:
            outputs = list(job.outputs)
        if self._streaming:
            self._handle_streaming(job, outputs)
        # non-streaming waits for on_finished

    def on_finished(self, job: JobRecord, ok: bool) -> None:
        with self._lock:
            outputs = list(job.outputs)
            upload_id = self._upload_job_id
        if not ok:
            # Streaming upload may already be RUNNING and holding browser_slots.
            # Abort it so the per-user budget is released even if no browser
            # windows are open (await_more wait loop).
            if upload_id:
                job.append_log(
                    "[upload] Обработка завершилась с ошибкой — "
                    "останавливаем залив, чтобы освободить слоты браузеров.",
                    max_lines=2000,
                )
                self.abort_linked_upload(job, reason="processing_failed")
            else:
                job.append_log(
                    "[upload] Обработка завершилась с ошибкой — залив не стартуем.",
                    max_lines=2000,
                )
            return
        if self._streaming:
            self._handle_streaming(job, outputs)
            self._mark_producer_done(job)
            return
        self._start_batch(job, outputs)

    def abort_linked_upload(
        self, job: JobRecord, *, reason: str = "aborted"
    ) -> None:
        """Cancel an already-started followup upload and free browser slots."""
        with self._lock:
            upload_id = self._upload_job_id
            self._producer_done = True
            self._batch_done = True
        if not upload_id:
            linked = str(job.linked_upload_job_id or "").strip()
            upload_id = linked or None
        if not upload_id or upload_id == job.id:
            return
        try:
            from zaliver.api.upload_runner import mark_streaming_producer_done

            mark_streaming_producer_done(upload_id)
        except Exception:
            pass
        try:
            cancelled = self._jobs.cancel(upload_id)
            if cancelled is not None:
                job.append_log(
                    f"[upload] Followup-залив отменён ({reason}): {upload_id[:8]}…",
                    max_lines=2000,
                )
        except Exception as e:
            job.append_log(
                f"[upload] Не удалось отменить followup-залив: {e!r}",
                max_lines=2000,
            )

    def _handle_streaming(self, job: JobRecord, outputs: list[str]) -> None:
        with self._lock:
            if self._batch_done:
                return
            if self._upload_job_id:
                upload_id = self._upload_job_id
                fresh = [p for p in outputs if p not in self._enqueued]
            else:
                upload_id = None
                fresh = []
                if self._starting:
                    return
                if len(outputs) < self._min_ready:
                    return
                self._starting = True

        if upload_id is None:
            try:
                self._start_streaming(job, outputs)
            finally:
                with self._lock:
                    self._starting = False
            with self._lock:
                upload_id = self._upload_job_id
                fresh = [
                    p
                    for p in outputs
                    if p not in self._enqueued and upload_id
                ]
            # Initial start already enqueued current outputs.
            return

        if not fresh or not upload_id:
            return
        try:
            from zaliver.api.upload_runner import enqueue_streaming_upload

            n = enqueue_streaming_upload(
                upload_id,
                video_paths=fresh,
                title=str(self._cfg.get("title") or ""),
                description=str(self._cfg.get("description") or ""),
            )
            if n:
                with self._lock:
                    for p in fresh:
                        self._enqueued.add(p)
                job.append_log(
                    f"[upload] В очередь по мере готовности: +{n}",
                    max_lines=2000,
                )
        except Exception as e:
            job.append_log(
                f"[upload] enqueue failed: {e!r}",
                max_lines=2000,
            )

    def _browser_jobs_allowed(self) -> bool:
        try:
            return bool(self._st.config.allow_browser_jobs)
        except Exception:
            return False

    def _start_streaming(self, job: JobRecord, outputs: list[str]) -> None:
        paths = list(outputs)
        if not paths:
            return
        if not self._browser_jobs_allowed():
            job.append_log(
                "[upload] Browser jobs disabled (ZALIVER_API_ALLOW_BROWSER_JOBS).",
                max_lines=2000,
            )
            return
        job.append_log(
            f"[upload] Залив по мере готовности: старт с запасом "
            f"{len(paths)} (порог {self._min_ready}, буфер профили×2)",
            max_lines=2000,
        )
        runner = build_upload_runner(
            self._st,
            cfg=self._cfg,
            video_paths=paths,
            await_more=True,
            planned_videos=max(self._planned, len(paths), self._min_ready),
        )
        try:
            upload_job = self._jobs.start(
                kind=JobKind.UPLOAD,
                runner=runner,
                bypass_limit=True,
                owner=str(job.owner or self._cfg.get("owner") or ""),
                browser_slots=int(
                    self._cfg.get("max_concurrent_browsers")
                    or job.browser_slots
                    or 0
                ),
            )
        except Exception as e:
            job.append_log(f"[upload] Не удалось стартовать залив: {e!r}", max_lines=2000)
            return
        with self._lock:
            self._upload_job_id = upload_job.id
            for p in paths:
                self._enqueued.add(p)
        job.linked_upload_job_id = upload_job.id
        self._jobs._persist_meta(job)  # noqa: SLF001 — keep UI in sync

    def _start_batch(self, job: JobRecord, outputs: list[str]) -> None:
        with self._lock:
            if self._batch_done or self._upload_job_id or self._starting:
                return
            self._starting = True
        try:
            paths = [p for p in outputs if p]
            if not paths:
                job.append_log(
                    "[upload] Обработка завершена, но нет видео для залива.",
                    max_lines=2000,
                )
                return
            if not self._browser_jobs_allowed():
                job.append_log(
                    "[upload] Browser jobs disabled (ZALIVER_API_ALLOW_BROWSER_JOBS).",
                    max_lines=2000,
                )
                return
            job.append_log(
                f"[upload] Старт залива после обработки: {len(paths)} видео",
                max_lines=2000,
            )
            runner = build_upload_runner(
                self._st,
                cfg=self._cfg,
                video_paths=paths,
                await_more=False,
                planned_videos=max(self._planned, len(paths)),
            )
            try:
                upload_job = self._jobs.start(
                    kind=JobKind.UPLOAD,
                    runner=runner,
                    bypass_limit=True,
                    owner=str(job.owner or self._cfg.get("owner") or ""),
                    browser_slots=int(
                        self._cfg.get("max_concurrent_browsers")
                        or job.browser_slots
                        or 0
                    ),
                )
            except Exception as e:
                job.append_log(
                    f"[upload] Не удалось стартовать залив: {e!r}",
                    max_lines=2000,
                )
                return
            with self._lock:
                self._upload_job_id = upload_job.id
                self._batch_done = True
                for p in paths:
                    self._enqueued.add(p)
            job.linked_upload_job_id = upload_job.id
            self._jobs._persist_meta(job)  # noqa: SLF001
        finally:
            with self._lock:
                self._starting = False

    def _mark_producer_done(self, job: JobRecord) -> None:
        with self._lock:
            if self._producer_done:
                return
            upload_id = self._upload_job_id
            if not upload_id:
                # Buffer never filled — fall back to batch with whatever we have.
                outputs = list(job.outputs)
                self._producer_done = True
            else:
                self._producer_done = True
                outputs = None
        if outputs is not None:
            if outputs:
                self._start_batch(job, outputs)
            return
        try:
            from zaliver.api.upload_runner import mark_streaming_producer_done

            mark_streaming_producer_done(upload_id)
            job.append_log(
                "[upload] Обработка закончена — producer-done для залива",
                max_lines=2000,
            )
        except Exception as e:
            job.append_log(f"[upload] producer-done: {e!r}", max_lines=2000)


def attach_upload_followup(
    st: Any,
    job: JobRecord,
    cfg: dict[str, Any] | None,
    *,
    planned_videos: int,
) -> UploadFollowup | None:
    if not cfg:
        return None
    profile_ids = [
        str(p).strip() for p in (cfg.get("profile_ids") or []) if str(p).strip()
    ]
    if not profile_ids:
        return None
    payload = dict(cfg)
    payload["profile_ids"] = profile_ids
    followup = UploadFollowup(
        st=st,
        jobs=st.jobs,
        cfg=payload,
        planned_videos=planned_videos,
    )
    followup.bind_job(job)
    job._upload_followup = followup  # type: ignore[attr-defined]
    if followup._streaming:
        consumed = default_consumed_path(job.id)
        if consumed:
            followup._cfg["ready_consumed_path"] = consumed
        job.append_log(
            "Залив по мере готовности: обработка на сервере, "
            f"залив — после запаса {followup._min_ready} готовых видео "
            f"({len(profile_ids)} профилей×2). "
            "Буфер: максимум столько же сделанных, но ещё не залитых.",
            max_lines=2000,
        )
    else:
        job.append_log(
            "После обработки будет запущен залив выбранных профилей.",
            max_lines=2000,
        )
    return followup
