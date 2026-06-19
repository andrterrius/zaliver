from __future__ import annotations

import signal
import sys
import threading
import time
from collections import deque
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


# Глобальный лимит одновременных upload_one (остальные ждут семафор).
_MAX_CONCURRENT_UPLOADS = 3 if sys.platform == "darwin" else 5
# Последние N завершённых загрузок — не назначаем им новое видео, пока есть другие свободные очереди.
_RECENT_COMPLETED_MAX = 5
# Если «свободны» только недавно отработавшие профили — пауза диспетчера перед повторным назначением.
_RECENT_BATCH_WAIT_S = 10800.0


class MultiProfileUploader:
    """
    Queue-based multi-threaded uploader.

    - One profile = one thread.
    - At most `_MAX_CONCURRENT_UPLOADS` profiles run `upload_one` at the same time (RAM);
      others wait on a semaphore until a slot frees.
    - Round-robin assignment via dispatcher thread; среди профилей с пустой per-profile
      очередью сначала выбираются те, кто не входит в последние `_RECENT_COMPLETED_MAX`
      завершённых загрузок (чтобы не гонять одни и те же 5, если другие свободны).
    - Если подходят только «недавно отработавшие», диспетчер ждёт `_RECENT_BATCH_WAIT_S` (3 ч),
      затем сбрасывает список недавних и назначает снова (лог [WAIT]).
    - Per-profile cooldown: wait at least `cooldown_s` from *start time* of previous upload
      in this run, and optionally `profile_upload_pause_remaining_s` (e.g. DB «Пауза 3 ч»).
    - Errors re-queue the same video to another profile (never the same one immediately),
      max `max_attempts_per_profile` attempts per video per profile.
    - stop() requests graceful shutdown; workers finish current upload and exit.
    - Waiting for a concurrency slot uses short acquire timeouts so stop() is honored
      (plain Semaphore.acquire() would ignore threading.Event).
    """

    def __init__(
        self,
        *,
        profile_ids: list[str],
        cooldown_s: float = 10800.0,
        max_attempts_per_profile: int = 2,
        max_concurrent_uploads: int = _MAX_CONCURRENT_UPLOADS,
        profile_upload_pause_remaining_s: Callable[[str], float] | None = None,
        log_sink: Callable[[str], None],
        upload_one: Callable[[str, VideoTask], None],
        on_profile_attempt: Callable[[str, bool, str], None] | None = None,
    ) -> None:
        self._profiles = [p.strip() for p in (profile_ids or []) if (p or "").strip()]
        self._cooldown_s = float(cooldown_s)
        self._max_attempts = int(max(1, max_attempts_per_profile))
        n_prof = max(1, len(self._profiles))
        cap = max(1, min(int(max_concurrent_uploads), _MAX_CONCURRENT_UPLOADS, n_prof))
        self._max_parallel = cap
        self._upload_slots = threading.Semaphore(cap)
        self._log = log_sink
        self._upload_one = upload_one
        self._on_profile_attempt = on_profile_attempt
        self._profile_upload_pause_remaining_s = profile_upload_pause_remaining_s

        self._stop = threading.Event()
        self._stop_reason = ""

        self._global_q: Queue[VideoTask] = Queue()
        self._per_profile_q: dict[str, Queue[VideoTask]] = {
            pid: Queue(maxsize=1) for pid in self._profiles
        }

        self._last_start_monotonic: dict[str, float] = {pid: 0.0 for pid in self._profiles}
        self._recent_completed: deque[str] = deque(maxlen=_RECENT_COMPLETED_MAX)
        self._recent_lock = threading.Lock()
        self._workers: list[threading.Thread] = []
        self._dispatcher: threading.Thread | None = None

        self._total = 0
        self._done_ok = 0
        self._done_failed = 0
        self._abandoned = 0
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

    @property
    def stop_reason(self) -> str:
        return self._stop_reason if self._stop.is_set() else ""

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

        self._log(
            f"[{_ts()}] [upload] started: profiles={len(self._profiles)}, "
            f"max_parallel={self._max_parallel}, total={self._total}"
        )

    def stop(self, *, reason: str = "user") -> None:
        self._stop_reason = (reason or "user").strip() or "user"
        self._stop.set()
        self._restore_sigint_handler()
        self._log(f"[{_ts()}] [upload] stop requested (reason={self._stop_reason})")

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
            self.stop(reason="sigint")

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

    def _task_exhausted_on_all_profiles(self, task: VideoTask) -> bool:
        for pid in self._profiles:
            if pid == task.last_failed_profile:
                continue
            if int(task.attempts_by_profile.get(pid, 0)) >= self._max_attempts:
                continue
            return False
        return True

    def _eligible_profiles_for_dispatch(self, task: VideoTask) -> list[str]:
        """Профили, которым можно поставить задачу (очередь пуста, лимиты попыток)."""
        out: list[str] = []
        for pid in self._profiles:
            if pid == task.last_failed_profile:
                continue
            if int(task.attempts_by_profile.get(pid, 0)) >= self._max_attempts:
                continue
            if not self._per_profile_q[pid].empty():
                continue
            out.append(pid)
        return out

    def _pick_round_robin(self, candidates: list[str], start_idx: int) -> str:
        if not candidates:
            raise ValueError("candidates must be non-empty")
        want = set(candidates)
        n = len(self._profiles)
        for i in range(n):
            pid = self._profiles[(start_idx + i) % n]
            if pid in want:
                return pid
        return candidates[0]

    def _wait_recent_batch_cooldown(self) -> None:
        total = float(_RECENT_BATCH_WAIT_S)
        self._log(
            f"[{_ts()}] [upload] [WAIT] reason=recent_parallel_profiles_only "
            f"sleep_s={total:.0f}"
        )
        remaining = total
        while remaining > 0 and not self._stop.is_set():
            chunk = min(30.0, remaining)
            if self._stop.wait(timeout=chunk):
                break
            remaining -= chunk
        with self._recent_lock:
            self._recent_completed.clear()

    def _dispatch_loop(self) -> None:
        idx = 0
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

            while not self._stop.is_set():
                if self._task_exhausted_on_all_profiles(task):
                    self._log(
                        f"[{_ts()}] [upload] [FAILED] video={task.video_path!r} "
                        f"reason=all_profiles_exhausted attempts={task.attempts_by_profile!r}"
                    )
                    with self._done_lock:
                        self._done_failed += 1
                    self._global_q.task_done()
                    break

                eligible = self._eligible_profiles_for_dispatch(task)
                if not eligible:
                    time.sleep(0.05)
                    continue

                with self._recent_lock:
                    recent = set(self._recent_completed)
                preferred = [p for p in eligible if p not in recent]
                if not preferred:
                    self._wait_recent_batch_cooldown()
                    if self._stop.is_set():
                        try:
                            self._global_q.put(task)
                        except Exception:
                            pass
                        return
                    continue

                chosen = self._pick_round_robin(preferred, idx)
                try:
                    pos = self._profiles.index(chosen)
                except ValueError:
                    pos = 0
                idx = (pos + 1) % max(1, len(self._profiles))

                self._per_profile_q[chosen].put(task)
                self._log(
                    f"[{_ts()}] [upload] [QUEUED] profile={chosen} video={task.video_path!r} "
                    f"attempts={int(task.attempts_by_profile.get(chosen, 0)) + 1}"
                )
                self._global_q.task_done()
                break

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
                try:
                    self._global_q.put(task)
                except Exception:
                    pass
                q.task_done()
                return

            # Cooldown: min interval between starts in this run + optional wall-clock pause (DB).
            pause_fn = self._profile_upload_pause_remaining_s
            while True:
                if self._stop.is_set():
                    try:
                        self._global_q.put(task)
                    except Exception:
                        pass
                    q.task_done()
                    return

                last_start = float(self._last_start_monotonic.get(profile_id, 0.0))
                now_m = time.monotonic()
                elapsed = now_m - last_start if last_start > 0 else self._cooldown_s
                rem_internal = max(0.0, self._cooldown_s - elapsed)
                db_rem = 0.0
                if pause_fn is not None:
                    try:
                        db_rem = max(0.0, float(pause_fn(profile_id)))
                    except Exception:
                        db_rem = 0.0
                remaining = max(rem_internal, db_rem)
                if remaining <= 0:
                    break

                parts: list[str] = []
                if rem_internal > 0:
                    parts.append(f"между_стартами≈{rem_internal:.0f}с")
                if db_rem > 0:
                    parts.append(f"пауза_1ч≈{db_rem:.0f}с")
                hint = "+".join(parts) if parts else "cooldown"
                self._log(
                    f"[{_ts()}] [upload] [WAIT] profile={profile_id} "
                    f"sleep_s={remaining:.1f} ({hint}) video={task.video_path!r}"
                )
                chunk = min(remaining, 30.0)
                self._stop.wait(timeout=chunk)
                if self._stop.is_set():
                    try:
                        self._global_q.put(task)
                    except Exception:
                        pass
                    q.task_done()
                    return

            # Semaphore.acquire() без таймаута не прерывается при stop() — поток «висит» и не
            # отпускает очередь; отмена с главного окна не доводит сессию до конца.
            got_slot = False
            while True:
                if self._stop.is_set():
                    break
                if self._upload_slots.acquire(timeout=0.35):
                    got_slot = True
                    break
            if not got_slot:
                self._log(
                    f"[{_ts()}] [upload] [ABANDON] profile={profile_id} "
                    f"reason=stop_waiting_slot video={task.video_path!r}"
                )
                with self._done_lock:
                    self._abandoned += 1
                q.task_done()
                continue

            upload_ran = False
            try:
                if self._stop.is_set():
                    try:
                        self._global_q.put(task)
                    except Exception:
                        pass
                    q.task_done()
                    continue

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
                upload_ran = True

                cb_attempt = self._on_profile_attempt
                if cb_attempt is not None:
                    try:
                        cb_attempt(profile_id, ok, err_text)
                    except Exception:
                        # Callback errors must not break upload flow.
                        pass

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
            finally:
                self._upload_slots.release()

            if upload_ran:
                with self._recent_lock:
                    self._recent_completed.append(profile_id)

            q.task_done()

    def _is_all_done(self) -> bool:
        # We consider "all done" when we have accounted for every initial task as OK/FAILED
        # and all queues are drained. This avoids busy loops when there is no work.
        with self._done_lock:
            finished = (
                (self._done_ok + self._done_failed + self._abandoned) >= self._total
                and self._total > 0
            )
        if not finished:
            return False
        if not self._global_q.empty():
            return False
        for q in self._per_profile_q.values():
            if not q.empty():
                return False
        return True

