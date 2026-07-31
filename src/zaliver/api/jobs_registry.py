"""In-memory job registry with optional disk-backed logs/meta."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from zaliver.api.job_log_store import JobLogStore
from zaliver.core.sinks import JobProgressSink


class JobKind(str, Enum):
    UNIQUIFY = "uniquify"
    SLICING = "slicing"
    STITCHING = "stitching"
    UPLOAD = "upload"
    AVAILABILITY = "availability"
    INSTAGRAM_REGISTER = "instagram_register"
    INSTAGRAM_2FA = "instagram_2fa"
    CHANNEL_SETUP = "channel_setup"
    WARMUP = "warmup"
    PROMOTE = "promote"
    COOKIE_FARM = "cookie_farm"
    STATS_REFRESH = "stats_refresh"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _parse_kind(raw: object) -> JobKind:
    try:
        return JobKind(str(raw or ""))
    except ValueError:
        return JobKind.UNIQUIFY


def _parse_status(raw: object) -> JobStatus:
    try:
        return JobStatus(str(raw or ""))
    except ValueError:
        return JobStatus.FAILED


@dataclass
class JobRecord:
    id: str
    kind: JobKind
    status: JobStatus = JobStatus.QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    progress_current: int = 0
    progress_total: int = 0
    progress_message: str = ""
    message: str = ""
    outputs: list[str] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    error: str = ""
    _cancel: Callable[[], None] | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _finished_signaled: bool = field(default=False, repr=False)
    _from_disk: bool = field(default=False, repr=False)

    def append_log(self, line: str, *, max_lines: int) -> None:
        with self._lock:
            self.logs.append(line)
            if len(self.logs) > max_lines:
                overflow = len(self.logs) - max_lines
                del self.logs[:overflow]

    def meta_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind.value,
                "status": self.status.value,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "progress": {
                    "current": self.progress_current,
                    "total": self.progress_total,
                    "message": self.progress_message,
                },
                "message": self.message,
                "outputs": list(self.outputs),
                "error": self.error,
            }

    def snapshot(
        self,
        *,
        log_tail: int = 100,
        store: JobLogStore | None = None,
    ) -> dict[str, Any]:
        data = self.meta_dict()
        if store is not None:
            # Child isolated workers write progress into meta on disk — merge if ahead.
            disk_meta = store.load_meta(self.id)
            if isinstance(disk_meta, dict):
                dp = disk_meta.get("progress")
                if isinstance(dp, dict):
                    try:
                        disk_cur = int(dp.get("current") or 0)
                        disk_total = int(dp.get("total") or 0)
                    except (TypeError, ValueError):
                        disk_cur, disk_total = 0, 0
                    mem_cur = int(data.get("progress", {}).get("current") or 0)
                    if disk_cur > mem_cur or (
                        disk_cur == mem_cur
                        and disk_total
                        > int(data.get("progress", {}).get("total") or 0)
                    ):
                        data["progress"] = {
                            "current": disk_cur,
                            "total": disk_total,
                            "message": str(dp.get("message") or ""),
                        }
        if log_tail <= 0:
            data["logs"] = []
            return data
        if store is not None:
            disk_logs = store.read_tail(self.id, log_tail)
            if disk_logs:
                data["logs"] = disk_logs
                return data
        with self._lock:
            data["logs"] = list(self.logs[-log_tail:])
        return data

    @classmethod
    def from_meta(cls, meta: dict[str, Any]) -> JobRecord:
        progress = meta.get("progress") if isinstance(meta.get("progress"), dict) else {}
        outputs = meta.get("outputs")
        return cls(
            id=str(meta.get("id") or ""),
            kind=_parse_kind(meta.get("kind")),
            status=_parse_status(meta.get("status")),
            created_at=float(meta.get("created_at") or 0.0) or time.time(),
            started_at=(
                float(meta["started_at"])
                if meta.get("started_at") is not None
                else None
            ),
            finished_at=(
                float(meta["finished_at"])
                if meta.get("finished_at") is not None
                else None
            ),
            progress_current=int(progress.get("current") or 0),
            progress_total=int(progress.get("total") or 0),
            progress_message=str(progress.get("message") or ""),
            message=str(meta.get("message") or ""),
            outputs=[str(x) for x in outputs] if isinstance(outputs, list) else [],
            error=str(meta.get("error") or ""),
            _from_disk=True,
        )


# run(sink, register_cancel, job_id) — job_id is the registry id (for disk logs etc.)
JobRunner = Callable[
    [JobProgressSink, Callable[[Callable[[], None]], None], str], None
]


class JobRegistry:
    def __init__(
        self,
        *,
        max_concurrent: int = 2,
        max_log_lines: int = 2000,
        log_store: JobLogStore | None = None,
        cleanup_interval_sec: int = 3600,
    ) -> None:
        self._max_concurrent = max(1, int(max_concurrent))
        self._max_log_lines = max(100, int(max_log_lines))
        self._store = log_store
        self._jobs: dict[str, JobRecord] = {}
        self._lock = threading.Lock()
        self._cleanup_interval = max(300, int(cleanup_interval_sec))
        if self._store is not None:
            try:
                self._store.cleanup()
            except Exception:
                pass
            threading.Thread(
                target=self._cleanup_loop,
                daemon=True,
                name="zaliver-job-log-cleanup",
            ).start()

    def _cleanup_loop(self) -> None:
        while True:
            time.sleep(self._cleanup_interval)
            if self._store is None:
                return
            try:
                self._store.cleanup()
            except Exception:
                pass

    def _persist_meta(self, job: JobRecord) -> None:
        if self._store is None:
            return
        try:
            self._store.save_meta(job.id, job.meta_dict())
        except Exception:
            pass

    def _persist_log(self, job_id: str, line: str) -> None:
        if self._store is None:
            return
        try:
            self._store.append_log(job_id, line)
        except Exception:
            pass

    def _active_count(self) -> int:
        return sum(
            1
            for j in self._jobs.values()
            if j.status in (JobStatus.QUEUED, JobStatus.RUNNING)
        )

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            hit = self._jobs.get(job_id)
            if hit is not None:
                return hit
        if self._store is None:
            return None
        meta = self._store.load_meta(job_id)
        if meta is None:
            return None
        job = JobRecord.from_meta(meta)
        if not job.id:
            job.id = job_id
        # Process restart: unfinished jobs cannot resume.
        if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
            with job._lock:
                job.status = JobStatus.FAILED
                job.error = job.error or "Interrupted by server restart"
                job.message = job.message or "Interrupted by server restart"
                if job.finished_at is None:
                    job.finished_at = time.time()
            self._persist_meta(job)
        with self._lock:
            # Another thread may have inserted a live job.
            existing = self._jobs.get(job_id)
            if existing is not None:
                return existing
            self._jobs[job_id] = job
            return job

    def list_jobs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(200, limit))
        with self._lock:
            mem = {j.id: j.snapshot(log_tail=0, store=None) for j in self._jobs.values()}
        if self._store is not None:
            for meta in self._store.list_metas(limit=max(limit, 200)):
                jid = str(meta.get("id") or "")
                if not jid or jid in mem:
                    continue
                # Disk-only finished jobs
                try:
                    job = JobRecord.from_meta(meta)
                    if not job.id:
                        job.id = jid
                    mem[jid] = job.snapshot(log_tail=0, store=None)
                except Exception:
                    continue
        items = sorted(
            mem.values(),
            key=lambda j: float(j.get("created_at") or 0.0),
            reverse=True,
        )
        return items[:limit]

    def snapshot(self, job: JobRecord, *, log_tail: int = 100) -> dict[str, Any]:
        return job.snapshot(log_tail=log_tail, store=self._store)

    def cancel(self, job_id: str) -> JobRecord | None:
        job = self.get(job_id)
        if job is None:
            return None
        with job._lock:
            if job.status in (
                JobStatus.SUCCEEDED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            ):
                return job
            if job._from_disk and job._cancel is None:
                # Cannot cancel a historical record with no runner.
                return job
            job.status = JobStatus.CANCELLED
            job.message = "Cancel requested."
            cancel = job._cancel
        if cancel is not None:
            try:
                cancel()
            except Exception:
                pass
        self._persist_meta(job)
        return job

    def start(self, *, kind: JobKind, runner: JobRunner) -> JobRecord:
        with self._lock:
            if self._active_count() >= self._max_concurrent:
                raise RuntimeError(
                    f"Too many concurrent jobs (max={self._max_concurrent}). "
                    "Cancel or wait for an active job."
                )
            job_id = uuid.uuid4().hex
            job = JobRecord(id=job_id, kind=kind)
            self._jobs[job_id] = job

        self._persist_meta(job)

        def on_progress(cur: int, total: int, msg: str) -> None:
            with job._lock:
                job.progress_current = int(cur)
                job.progress_total = int(total)
                job.progress_message = str(msg or "")

        def on_log(msg: str) -> None:
            text = str(msg)
            job.append_log(text, max_lines=self._max_log_lines)
            self._persist_log(job_id, text)

        def on_output(path: str, _skip_upload: bool) -> None:
            with job._lock:
                job.outputs.append(str(path))
            self._persist_meta(job)

        def on_finished(ok: bool, message: str) -> None:
            with job._lock:
                job._finished_signaled = True
                job.finished_at = time.time()
                job.message = message or job.message
                if job.status != JobStatus.CANCELLED:
                    if ok:
                        job.status = JobStatus.SUCCEEDED
                    else:
                        job.status = JobStatus.FAILED
                        job.error = message or "failed"
            self._persist_meta(job)

        sink = JobProgressSink(
            on_progress=on_progress,
            on_finished=on_finished,
            on_log=on_log,
            on_output_saved=on_output,
        )

        def register_cancel(fn: Callable[[], None]) -> None:
            job._cancel = fn

        def thread_main() -> None:
            with job._lock:
                job.status = JobStatus.RUNNING
                job.started_at = time.time()
            self._persist_meta(job)
            try:
                runner(sink, register_cancel, job_id)
            except Exception as e:
                with job._lock:
                    if job.status not in (JobStatus.CANCELLED, JobStatus.SUCCEEDED):
                        job.status = JobStatus.FAILED
                        job.error = str(e)
                        job.message = str(e)
                        job.finished_at = time.time()
                on_log(f"job exception: {e!r}")
                self._persist_meta(job)
            finally:
                with job._lock:
                    if (
                        job.status == JobStatus.RUNNING
                        and not job._finished_signaled
                    ):
                        job.status = JobStatus.SUCCEEDED
                        job.finished_at = time.time()
                    elif job.status == JobStatus.CANCELLED and job.finished_at is None:
                        job.finished_at = time.time()
                self._persist_meta(job)

        threading.Thread(
            target=thread_main, daemon=True, name=f"zaliver-job-{job_id}"
        ).start()
        return job
