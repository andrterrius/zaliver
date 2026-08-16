"""Backpressure for «залив по мере готовности».

Limits how many processed-but-not-yet-consumed videos sit on disk while upload
is slower than uniquify/slice/stitch. Default size is profiles×2.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Callable, Optional

READY_PER_PROFILE = 2
_APPEND_LOCK = threading.Lock()


def compute_ready_buffer_limit(*, profile_count: int, planned: int) -> int:
    """Max ready-but-not-consumed videos: profiles×2, not more than planned."""
    n = max(0, int(profile_count))
    if n <= 0:
        return 1
    target = n * READY_PER_PROFILE
    planned_n = max(0, int(planned))
    if planned_n > 0:
        return max(1, min(target, planned_n))
    return max(1, target)


def default_consumed_path(job_id: str) -> str:
    jid = str(job_id or "").strip()
    if not jid:
        return ""
    raw = (os.environ.get("ZALIVER_JOB_LOG_DIR") or "").strip()
    if not raw:
        return ""
    return str(Path(raw) / f"{jid}.consumed.txt")


def append_consumed_path(consumed_file: str, video_path: str) -> None:
    """Record that a ready video left the buffer (uploaded / abandoned / deleted)."""
    dest = str(consumed_file or "").strip()
    path = str(video_path or "").strip()
    if not dest or not path:
        return
    line = path.replace("\r", " ").replace("\n", " ").strip() + "\n"
    parent = Path(dest).parent
    with _APPEND_LOCK:
        try:
            parent.mkdir(parents=True, exist_ok=True)
            with Path(dest).open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)
        except OSError:
            pass


def apply_ready_buffer_option(
    options: dict[str, Any],
    upload_after: dict[str, Any] | None,
    planned: int,
) -> None:
    """Set upload_ready_buffer_limit on processing options when streaming upload."""
    if not upload_after or not upload_after.get("upload_as_ready"):
        return
    n_prof = len(
        [p for p in (upload_after.get("profile_ids") or []) if str(p or "").strip()]
    )
    options["upload_ready_buffer_limit"] = compute_ready_buffer_limit(
        profile_count=n_prof, planned=planned
    )


def settle_ready_job(
    buf: "ReadyVideoBuffer | None",
    path: str,
    *,
    keep: bool,
) -> None:
    """Keep the slot for upload, or free it if the video will not be uploaded."""
    if buf is None:
        return
    if keep and str(path or "").strip():
        buf.note_produced(path)
    else:
        buf.release_inflight()


def buffer_from_options(options: dict[str, Any] | None) -> "ReadyVideoBuffer | None":
    if not isinstance(options, dict):
        return None
    try:
        limit = int(options.get("upload_ready_buffer_limit") or 0)
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0:
        return None
    consumed = str(options.get("upload_ready_consumed_path") or "").strip()
    if not consumed:
        consumed = default_consumed_path(str(options.get("job_id") or ""))
    return ReadyVideoBuffer(limit, consumed_path=consumed or None)


def _norm_path(path: str) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    try:
        return os.path.normcase(os.path.abspath(raw))
    except OSError:
        return os.path.normcase(raw)


class ReadyVideoBuffer:
    """Counting semaphore for videos that are encoding or waiting to be uploaded."""

    def __init__(self, limit: int, *, consumed_path: str | None = None) -> None:
        self.limit = max(1, int(limit))
        self._consumed_path = str(consumed_path or "").strip()
        self._cv = threading.Condition()
        self._held = 0
        self._inflight = 0
        self._pending: dict[str, None] = {}
        self._released: set[str] = set()
        self._consumed_seen = 0
        self._wait_logged = False
        self._closed = False

    def try_acquire(self) -> bool:
        self.reclaim()
        with self._cv:
            if self._closed or self._held >= self.limit:
                return False
            self._held += 1
            self._inflight += 1
            self._wait_logged = False
            return True

    def acquire(
        self,
        cancel_check: Optional[Callable[[], bool]] = None,
        *,
        log: Optional[Callable[[str], None]] = None,
        timeout: float = 0.25,
    ) -> bool:
        """Block until a slot is free, then take it. False if cancelled."""
        while True:
            if cancel_check is not None and cancel_check():
                return False
            self.reclaim()
            with self._cv:
                if self._closed:
                    return False
                if self._held < self.limit:
                    self._held += 1
                    self._inflight += 1
                    self._wait_logged = False
                    return True
                if log is not None and not self._wait_logged:
                    self._wait_logged = True
                    log(
                        f"Буфер готовых видео заполнен ({self._held}/{self.limit}, "
                        "профили×2) — ждём залив или удаление, затем делаем следующее."
                    )
                self._cv.wait(timeout=max(0.05, float(timeout)))

    def wait_has_room(
        self,
        cancel_check: Optional[Callable[[], bool]] = None,
        *,
        log: Optional[Callable[[str], None]] = None,
        timeout: float = 0.25,
    ) -> bool:
        """Wait until a slot is free without taking it. False if cancelled."""
        while True:
            if cancel_check is not None and cancel_check():
                return False
            self.reclaim()
            with self._cv:
                if self._closed:
                    return False
                if self._held < self.limit:
                    self._wait_logged = False
                    return True
                if log is not None and not self._wait_logged:
                    self._wait_logged = True
                    log(
                        f"Буфер готовых видео заполнен ({self._held}/{self.limit}, "
                        "профили×2) — ждём залив или удаление, затем делаем следующее."
                    )
                self._cv.wait(timeout=max(0.05, float(timeout)))

    def note_produced(self, path: str) -> None:
        """Encode finished: keep the slot until the video is consumed."""
        key = _norm_path(path)
        with self._cv:
            if self._inflight > 0:
                self._inflight -= 1
            if not key:
                return
            if key in self._released:
                self._held = max(0, self._held - 1)
                self._cv.notify_all()
                return
            self._pending[key] = None

    def release_inflight(self) -> None:
        """Encode failed / skipped upload — free the slot taken at start."""
        with self._cv:
            if self._inflight > 0:
                self._inflight -= 1
            self._held = max(0, self._held - 1)
            self._cv.notify_all()

    def release_path(self, path: str) -> bool:
        """Video left the buffer (uploaded, deleted, or abandoned)."""
        key = _norm_path(path)
        if not key:
            return False
        with self._cv:
            if key in self._released:
                return False
            self._released.add(key)
            if key in self._pending:
                del self._pending[key]
                self._held = max(0, self._held - 1)
                self._cv.notify_all()
                return True
            # Consumed before note_produced: next note_produced will drop held.
            return False

    def reclaim(self) -> int:
        """Free slots for deleted files and paths listed in the consumed sidecar."""
        n = 0
        for raw in self._read_consumed_file():
            if self.release_path(raw):
                n += 1
        gone: list[str] = []
        with self._cv:
            for key in list(self._pending):
                try:
                    if not Path(key).is_file():
                        gone.append(key)
                except OSError:
                    gone.append(key)
        for key in gone:
            if self.release_path(key):
                n += 1
        return n

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    def _read_consumed_file(self) -> list[str]:
        if not self._consumed_path:
            return []
        try:
            data = Path(self._consumed_path).read_text(encoding="utf-8")
        except OSError:
            return []
        lines = [ln.strip() for ln in data.splitlines() if ln.strip()]
        with self._cv:
            if self._consumed_seen >= len(lines):
                return []
            fresh = lines[self._consumed_seen :]
            self._consumed_seen = len(lines)
            return fresh
