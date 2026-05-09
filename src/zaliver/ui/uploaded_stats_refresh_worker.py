"""Фоновое обновление статистики залитых видео (YouTube Data API)."""

from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal

from zaliver.youtube_parsing.video_stats import fetch_video_stats_by_id


class UploadedStatsRefreshWorker(QObject):
    """
    Запросы к API выполняются в отдельном потоке Qt.
    Результат: список успешных обновлений и список текстов ошибок по отдельным id.
    """

    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(object, object)

    def __init__(self, video_ids: list[str], api_key: str) -> None:
        super().__init__()
        self._video_ids = list(video_ids)
        self._api_key = (api_key or "").strip()

    def run(self) -> None:
        successes: list[tuple[str, int, int | None, int | None]] = []
        errors: list[str] = []
        key = self._api_key or None
        ids = [(vid or "").strip() for vid in self._video_ids if (vid or "").strip()]
        total = len(ids)
        if total <= 0:
            self.finished.emit(successes, errors)
            return
        self.progress.emit(0, total, ids[0])
        for i, v in enumerate(ids, start=1):
            try:
                st = fetch_video_stats_by_id(v, api_key=key)
                successes.append(
                    (st.video_id, int(st.view_count), st.like_count, st.comment_count)
                )
            except Exception as e:
                errors.append(f"{v}: {e}")
            self.progress.emit(i, total, v)
        self.finished.emit(successes, errors)
