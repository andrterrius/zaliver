"""Фоновое обновление статистики залитых видео (YouTube Data API)."""

from __future__ import annotations

import requests
from PyQt6.QtCore import QObject, pyqtSignal

from zaliver.youtube_parsing.video_stats import (
    YoutubeDataApiError,
    YOUTUBE_DATA_API_VIDEOS_LIST_MAX_IDS,
    fetch_video_stats_batch,
)


class UploadedStatsRefreshWorker(QObject):
    """
    Запросы к API выполняются в отдельном потоке Qt.
    Результат: успехи и неудачи; в каждой неудаче — id, текст и флаг YoutubeDataApiError.
    """

    progress = pyqtSignal(int, int, str)
    batch_done = pyqtSignal(object, object)
    finished = pyqtSignal(object, object)

    def __init__(self, video_ids: list[str], api_key: str) -> None:
        super().__init__()
        self._video_ids = list(video_ids)
        self._api_key = (api_key or "").strip()

    def run(self) -> None:
        successes: list[tuple[str, int, int | None, int | None, bool]] = []
        failures: list[tuple[str, str, bool]] = []
        key = self._api_key or None
        ids = [(vid or "").strip() for vid in self._video_ids if (vid or "").strip()]
        total = len(ids)
        if total <= 0:
            self.finished.emit(successes, failures)
            return
        self.progress.emit(0, total, ids[0])
        http = requests.Session()
        done = 0
        step = YOUTUBE_DATA_API_VIDEOS_LIST_MAX_IDS
        for batch_start in range(0, total, step):
            chunk = ids[batch_start : batch_start + step]
            last_in_chunk = chunk[-1]
            try:
                batch_ok, batch_fail = fetch_video_stats_batch(
                    chunk, api_key=key, session=http
                )
            except Exception as e:
                is_data_api = isinstance(e, YoutubeDataApiError)
                batch_fail_out: list[tuple[str, str, bool]] = []
                for v in chunk:
                    row = (v, str(e), is_data_api)
                    failures.append(row)
                    batch_fail_out.append(row)
                done += len(chunk)
                if batch_fail_out:
                    self.batch_done.emit([], batch_fail_out)
                self.progress.emit(done, total, last_in_chunk)
                continue
            batch_succ: list[tuple[str, int, int | None, int | None, bool]] = []
            for st in batch_ok:
                row = (
                    st.video_id,
                    int(st.view_count),
                    st.like_count,
                    st.comment_count,
                    bool(st.age_restricted),
                )
                successes.append(row)
                batch_succ.append(row)
            batch_fail_out = []
            for vid_f, msg_f in batch_fail:
                is_data_api = "Invalid video id" not in msg_f
                row = (vid_f, msg_f, is_data_api)
                failures.append(row)
                batch_fail_out.append(row)
            done += len(chunk)
            if batch_succ or batch_fail_out:
                self.batch_done.emit(batch_succ, batch_fail_out)
            self.progress.emit(done, total, last_in_chunk)
        self.finished.emit(successes, failures)
