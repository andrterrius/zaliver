from __future__ import annotations

import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from queue import Empty, Queue
from typing import Callable, Dict, Iterable, Optional


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass(slots=True)
class VideoTask:
    video_path: str
    title: str
    description: str
    attempts_by_profile: Dict[str, int] = field(default_factory=dict)
    last_failed_profile: str = ""


class MultiProfileUploader:
    """
    Queue-based multi-threaded uploader.

    - One profile = one thread.
    - Round-robin assignment via dispatcher thread.
    - Per-profile cooldown: wait at least `cooldown_s` from *start time* of previous upload.
    - Errors re-queue the same video to another profile (never the same one immediately),
      max `max_attempts_per_profile` attempts per video per profile.
    - stop() requests graceful shutdown; workers finish current upload and exit.
    """

    def __init__(
        self,
        *,
        profile_ids: list[str],
        cooldown_s: float = 3600.0,
        max_attempts_per_profile: int = 2,
        log_sink: Callable[[str], None],
        upload_one: Callable[[str, VideoTask], None],
    ) -> None:
        self._profiles = [p.strip() for p in (profile_ids or []) if (p or "").strip()]
        self._cooldown_s = float(cooldown_s)
        self._max_attempts = int(max(1, max_attempts_per_profile))
        self._log = log_sink
        self._upload_one = upload_one

        self._stop = threading.Event()

        self._global_q: Queue[VideoTask] = Queue()
        self._per_profile_q: dict[str, Queue[VideoTask]] = {
            pid: Queue(maxsize=1) for pid in self._profiles
        }

        self._last_start_monotonic: dict[str, float] = {pid: 0.0 for pid in self._profiles}
        self._workers: list[threading.Thread] = []
        self._dispatcher: threading.Thread | None = None

        self._total = 0
        self._done_ok = 0
        self._done_failed = 0
        self._done_lock = threading.Lock()

        self._orig_sigint = None

    @property
    def total(self) -> int:
        return self._total

    @property
    def done_ok(self) -> int:
        with self._done_lock:
            return self._done_ok

    @property
    def done_failed(self) -> int:
        with self._done_lock:
            return self._done_failed

    def is_finished(self) -> bool:
        return self._is_all_done()

    def stop_requested(self) -> bool:
        return self._stop.is_set()

    def enqueue_videos(
        self,
        *,
        video_paths: Iterable[str],
        title: str,
        description: str,
    ) -> None:
        for p in (video_paths or []):
            path = (p or "").strip()
            if not path:
                continue
            self._global_q.put(
                VideoTask(video_path=path, title=title or "", description=description or "")
            )
            self._total += 1

    def start(self) -> None:
        if not self._profiles:
            raise ValueError("No profiles selected.")
        if self._dispatcher is not None:
            return

        self._install_sigint_handler()

        self._dispatcher = threading.Thread(target=self._dispatch_loop, daemon=True)
        self._dispatcher.start()

        for pid in self._profiles:
            t = threading.Thread(target=self._worker_loop, args=(pid,), daemon=True)
            self._workers.append(t)
            t.start()

        self._log(f"[{_ts()}] [upload] started: profiles={len(self._profiles)}, total={self._total}")

    def stop(self) -> None:
        self._stop.set()
        self._restore_sigint_handler()
        self._log(f"[{_ts()}] [upload] stop requested")

    def join(self, timeout_s: float | None = None) -> None:
        deadline = None if timeout_s is None else (time.monotonic() + float(timeout_s))
        if self._dispatcher is not None:
            while self._dispatcher.is_alive():
                if deadline is not None and time.monotonic() >= deadline:
                    break
                self._dispatcher.join(timeout=0.2)
        for t in self._workers:
            while t.is_alive():
                if deadline is not None and time.monotonic() >= deadline:
                    return
                t.join(timeout=0.2)

    # ---------------- internal ----------------

    def _install_sigint_handler(self) -> None:
        try:
            self._orig_sigint = signal.getsignal(signal.SIGINT)
        except Exception:
            self._orig_sigint = None

        def _handler(_sig, _frame) -> None:
            self._log(f"[{_ts()}] [upload] SIGINT received → stopping…")
            self.stop()

        try:
            signal.signal(signal.SIGINT, _handler)
        except Exception:
            # In some contexts (e.g. non-main thread / embedded), signal may be unavailable.
            pass

    def _restore_sigint_handler(self) -> None:
        try:
            if self._orig_sigint is not None:
                signal.signal(signal.SIGINT, self._orig_sigint)
        except Exception:
            pass

    def _dispatch_loop(self) -> None:
        idx = 0
        n = len(self._profiles)
        while not self._stop.is_set():
            try:
                task = self._global_q.get(timeout=0.25)
            except Empty:
                # No new work; if all per-profile queues are empty too, we can exit once
                # all tasks are accounted for.
                if self._is_all_done():
                    return
                continue

            if self._stop.is_set():
                return

            # Pick next eligible profile in round-robin.
            chosen = ""
            for _ in range(max(1, n)):
                pid = self._profiles[idx % n]
                idx += 1
                if pid == task.last_failed_profile:
                    continue
                if int(task.attempts_by_profile.get(pid, 0)) >= self._max_attempts:
                    continue
                chosen = pid
                break

            if not chosen:
                # All profiles exhausted for this video.
                self._log(
                    f"[{_ts()}] [upload] [FAILED] video={task.video_path!r} "
                    f"reason=all_profiles_exhausted attempts={task.attempts_by_profile!r}"
                )
                with self._done_lock:
                    self._done_failed += 1
                self._global_q.task_done()
                continue

            # This blocks if the profile already has a queued task, which effectively
            # means "no free profiles" right now → we wait without blocking UI thread.
            self._per_profile_q[chosen].put(task)
            self._log(
                f"[{_ts()}] [upload] [QUEUED] profile={chosen} video={task.video_path!r} "
                f"attempts={int(task.attempts_by_profile.get(chosen, 0)) + 1}"
            )
            self._global_q.task_done()

    def _worker_loop(self, profile_id: str) -> None:
        q = self._per_profile_q[profile_id]
        while not self._stop.is_set():
            try:
                task = q.get(timeout=0.25)
            except Empty:
                if self._is_all_done():
                    return
                continue

            if self._stop.is_set():
                return

            # Cooldown based on start time of previous upload for this profile.
            last_start = float(self._last_start_monotonic.get(profile_id, 0.0))
            now = time.monotonic()
            elapsed = now - last_start if last_start > 0 else self._cooldown_s
            remaining = max(0.0, self._cooldown_s - elapsed)
            if remaining > 0:
                self._log(
                    f"[{_ts()}] [upload] [WAIT] profile={profile_id} "
                    f"sleep_s={remaining:.1f} video={task.video_path!r}"
                )
                # Wait with stop awareness.
                self._stop.wait(timeout=remaining)
                if self._stop.is_set():
                    return

            # Mark start time immediately (requirement: track *start*).
            self._last_start_monotonic[profile_id] = time.monotonic()

            self._log(
                f"[{_ts()}] [upload] [START] profile={profile_id} video={task.video_path!r}"
            )
            ok = False
            err_text = ""
            try:
                self._upload_one(profile_id, task)
                ok = True
            except Exception as e:
                ok = False
                err_text = str(e) or repr(e)

            if ok:
                self._log(
                    f"[{_ts()}] [upload] [OK] profile={profile_id} video={task.video_path!r}"
                )
                with self._done_lock:
                    self._done_ok += 1
            else:
                # Record attempt on this profile and requeue to another one.
                task.attempts_by_profile[profile_id] = int(
                    task.attempts_by_profile.get(profile_id, 0)
                ) + 1
                task.last_failed_profile = profile_id
                self._log(
                    f"[{_ts()}] [upload] [ERROR] profile={profile_id} video={task.video_path!r} "
                    f"attempt={task.attempts_by_profile[profile_id]}/{self._max_attempts} err={err_text!r}"
                )
                self._global_q.put(task)

            q.task_done()

    def _is_all_done(self) -> bool:
        # We consider "all done" when we have accounted for every initial task as OK/FAILED
        # and all queues are drained. This avoids busy loops when there is no work.
        with self._done_lock:
            finished = (self._done_ok + self._done_failed) >= self._total and self._total > 0
        if not finished:
            return False
        if not self._global_q.empty():
            return False
        for q in self._per_profile_q.values():
            if not q.empty():
                return False
        return True

