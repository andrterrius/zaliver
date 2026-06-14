from __future__ import annotations

import threading
import time
from queue import Empty, Queue
from typing import Callable

from zaliver.youtube_upload.multi_uploader import _MAX_CONCURRENT_UPLOADS

# Проверка доступности Studio — отдельный лимит параллельных профилей.
_MAX_CONCURRENT_AVAILABILITY_CHECKS = 4


class MultiProfileAvailabilityChecker:
    """
    Параллельная проверка доступности Studio: до max_concurrent профилей одновременно.
    По умолчанию для заливки/прочих сценариев — лимит MultiProfileUploader;
    для проверки доступности передайте max_concurrent=_MAX_CONCURRENT_AVAILABILITY_CHECKS.
    """

    def __init__(
        self,
        *,
        profile_ids: list[str],
        check_one: Callable[[str], None],
        on_profile_done: Callable[[str, bool, str], None] | None = None,
        on_progress: Callable[[int, int, str], None] | None = None,
        log_sink: Callable[[str], None] | None = None,
        max_concurrent: int = _MAX_CONCURRENT_UPLOADS,
    ) -> None:
        self._profiles = [p.strip() for p in (profile_ids or []) if (p or "").strip()]
        n_prof = len(self._profiles)
        cap = max(1, min(int(max_concurrent), max(1, n_prof)))
        self._max_parallel = cap
        self._check_one = check_one
        self._on_profile_done = on_profile_done
        self._on_progress = on_progress
        self._log = log_sink or (lambda _m: None)

        self._queue: Queue[str] = Queue()
        for pid in self._profiles:
            self._queue.put(pid)

        self._total = n_prof
        self._completed = 0
        self._ok = 0
        self._fail = 0
        self._failed_ids: list[str] = []
        self._stats_lock = threading.Lock()
        self._slots = threading.Semaphore(cap)
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []

    @property
    def total(self) -> int:
        return self._total

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> tuple[int, int, list[str]]:
        """Блокирует до завершения всех профилей. Возвращает (ok, fail, failed_ids)."""
        if not self._profiles:
            return 0, 0, []

        self._log(
            f"[availability] Старт проверки: профилей={self._total}, "
            f"параллельно до {self._max_parallel}, headless."
        )

        for _ in range(self._max_parallel):
            t = threading.Thread(target=self._worker_loop, daemon=True)
            self._workers.append(t)
            t.start()

        for t in self._workers:
            t.join()

        with self._stats_lock:
            return self._ok, self._fail, list(self._failed_ids)

    def _is_all_done(self) -> bool:
        with self._stats_lock:
            return self._completed >= self._total

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            if self._is_all_done():
                return
            try:
                pid = self._queue.get(timeout=0.25)
            except Empty:
                continue

            if self._stop.is_set():
                self._queue.task_done()
                return

            got_slot = False
            while not self._stop.is_set():
                if self._slots.acquire(timeout=0.35):
                    got_slot = True
                    break
            if not got_slot:
                self._queue.put(pid)
                self._queue.task_done()
                return

            err = ""
            ok = False
            try:
                if not self._stop.is_set():
                    self._check_one(pid)
                    ok = True
            except Exception as e:
                err = str(e).strip() or repr(e)
            finally:
                self._slots.release()

            with self._stats_lock:
                self._completed += 1
                if ok:
                    self._ok += 1
                else:
                    self._fail += 1
                    self._failed_ids.append(pid)
                done = self._completed
                ok_n = self._ok
                fail_n = self._fail

            if self._on_profile_done is not None:
                try:
                    self._on_profile_done(pid, ok, err)
                except Exception:
                    pass

            if self._on_progress is not None:
                try:
                    self._on_progress(done, self._total, pid)
                except Exception:
                    pass

            total = self._total
            if ok:
                self._log(f"[availability] OK profile={pid} ({done}/{total})")
            else:
                self._log(f"[availability] ОШИБКА profile={pid} ({done}/{total}): {err}")

            self._queue.task_done()

            # Небольшая пауза между стартами в одном воркере (как при заливке — снижает пик RAM).
            if not self._stop.is_set():
                time.sleep(0.05)
