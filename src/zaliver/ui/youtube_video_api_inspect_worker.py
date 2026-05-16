"""Фоновый запрос YouTube Data API `videos.list` для вкладки «Готовые» (кнопка «Ответ API»)."""

from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal

from zaliver.youtube_parsing.video_stats import fetch_youtube_videos_list_full_text


class YoutubeVideoApiInspectWorker(QObject):
    finished = pyqtSignal(str)

    def __init__(self, url_or_id: str, api_key: str) -> None:
        super().__init__()
        self._url_or_id = (url_or_id or "").strip()
        self._api_key = (api_key or "").strip()

    def run(self) -> None:
        text = fetch_youtube_videos_list_full_text(
            self._url_or_id,
            api_key=self._api_key or None,
        )
        self.finished.emit(text or "")
