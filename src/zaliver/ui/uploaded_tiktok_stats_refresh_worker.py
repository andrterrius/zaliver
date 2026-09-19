"""Фоновое обновление статистики залитых TikTok-роликов (HTTP, без профиля)."""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal

from zaliver.tiktok_upload.reel_stats import DEFAULT_STATS_WORKERS


class UploadedTikTokStatsRefreshWorker(QObject):
    """Публичные URL роликов: GET HTML и JSON stats/statsV2."""

    progress = pyqtSignal(int, int, str)
    batch_done = pyqtSignal(object, object)
    finished = pyqtSignal(object, object)
    log_line = pyqtSignal(str)

    def __init__(self, items: list[dict[str, str]] | list[str]) -> None:
        super().__init__()
        self._items = list(items)

    def _log(self, msg: str) -> None:
        line = f"[tt-stats] {msg}"
        try:
            self.log_line.emit(line)
        except Exception:
            pass

    def run(self) -> None:
        from zaliver.tiktok_upload.reel_stats import fetch_reel_stats_many

        successes: list[tuple[str, int, int | None, int | None, bool]] = []
        failures: list[tuple[str, str, bool]] = []
        items = list(self._items)
        total = len(items)
        if total <= 0:
            self.finished.emit(successes, failures)
            return
        first = ""
        if isinstance(items[0], dict):
            first = str(items[0].get("video_id") or "").strip()
        else:
            first = str(items[0]).strip()
        self.progress.emit(0, total, first)
        self._log(
            f"Старт: {total} роликов, HTTP workers={DEFAULT_STATS_WORKERS}"
        )

        def _on_progress(step: int, tot: int, vid: str) -> None:
            self.progress.emit(step, tot, vid)

        def _on_item(st: Any, vid: str, err: str | None) -> None:
            if err is None and st is not None:
                row = (
                    vid,
                    int(getattr(st, "view_count", 0) or 0),
                    getattr(st, "like_count", None),
                    getattr(st, "comment_count", None),
                    False,
                )
                successes.append(row)
                self._log(
                    f"{vid}: views={row[1]} likes={row[2]} comments={row[3]}"
                )
                self.batch_done.emit([row], [])
                return
            msg = err or "unknown error"
            self._log(f"{vid}: {msg}")
            fail = (vid, msg, False)
            failures.append(fail)
            self.batch_done.emit([], [fail])

        try:
            fetch_reel_stats_many(
                items,
                workers=DEFAULT_STATS_WORKERS,
                on_progress=_on_progress,
                on_item=_on_item,
            )
        except Exception as e:
            msg = str(e) or type(e).__name__
            self._log(f"FAIL: {msg}")
            if not successes and not failures:
                for it in items:
                    if isinstance(it, dict):
                        vid = str(it.get("video_id") or "").strip()
                    else:
                        vid = str(it).strip()
                    if vid:
                        failures.append((vid, msg, False))
                self.batch_done.emit([], list(failures))
        self.progress.emit(total, total, first)
        self.finished.emit(successes, failures)
