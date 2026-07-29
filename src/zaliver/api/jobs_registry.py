"""In-memory job registry with cancel + bounded logs."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

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


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


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

    def append_log(self, line: str, *, max_lines: int) -> None:
        with self._lock:
            self.logs.append(line)
            if len(self.logs) > max_lines:
                overflow = len(self.logs) - max_lines
                del self.logs[:overflow]

    def snapshot(self, *, log_tail: int = 100) -> dict[str, Any]:
        with self._lock:
            logs = list(self.logs[-max(0, log_tail) :]) if log_tail else []
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
                "logs": logs,
            }


# run(sink, register_cancel) — register_cancel(fn) stores cancel callback
JobRunner = Callable[[JobProgressSink, Callable[[Callable[[], None]], None]], None]


class JobRegistry:
    def __init__(
        self,
        *,
        max_concurrent: int = 2,
        max_log_lines: int = 2000,
    ) -> None:
        self._max_concurrent = max(1, int(max_concurrent))
        self._max_log_lines = max(100, int(max_log_lines))
        self._jobs: dict[str, JobRecord] = {}
        self._lock = threading.Lock()

    def _active_count(self) -> int:
        return sum(
            1
            for j in self._jobs.values()
            if j.status in (JobStatus.QUEUED, JobStatus.RUNNING)
        )

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            items = sorted(
                self._jobs.values(), key=lambda j: j.created_at, reverse=True
            )[: max(1, min(200, limit))]
            return [j.snapshot(log_tail=0) for j in items]

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
            job.status = JobStatus.CANCELLED
            job.message = "Cancel requested."
            cancel = job._cancel
        if cancel is not None:
            try:
                cancel()
            except Exception:
                pass
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

        def on_progress(cur: int, total: int, msg: str) -> None:
            with job._lock:
                job.progress_current = int(cur)
                job.progress_total = int(total)
                job.progress_message = str(msg or "")

        def on_log(msg: str) -> None:
            job.append_log(str(msg), max_lines=self._max_log_lines)

        def on_output(path: str, _skip_upload: bool) -> None:
            with job._lock:
                job.outputs.append(str(path))

        def on_finished(ok: bool, message: str) -> None:
            with job._lock:
                job._finished_signaled = True
                job.finished_at = time.time()
                job.message = message or job.message
                if job.status == JobStatus.CANCELLED:
                    return
                if ok:
                    job.status = JobStatus.SUCCEEDED
                else:
                    job.status = JobStatus.FAILED
                    job.error = message or "failed"

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
            try:
                runner(sink, register_cancel)
            except Exception as e:
                with job._lock:
                    if job.status not in (JobStatus.CANCELLED, JobStatus.SUCCEEDED):
                        job.status = JobStatus.FAILED
                        job.error = str(e)
                        job.message = str(e)
                        job.finished_at = time.time()
                on_log(f"job exception: {e!r}")
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

        threading.Thread(
            target=thread_main, daemon=True, name=f"zaliver-job-{job_id}"
        ).start()
        return job
