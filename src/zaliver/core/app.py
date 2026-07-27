"""ZaliverCore — headless façade for desktop UI and future web API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from zaliver.config.platform_settings import (
    PlatformSettings,
    normalize_platform,
)
from zaliver.config.store import SettingsStore, ensure_settings_store
from zaliver.core.sinks import JobProgressSink
from zaliver.db.upload_store import UploadStore
from zaliver.db.video_store import VideoStore
from zaliver.processing.slicing_worker import SlicingService
from zaliver.processing.thread_worker import ProcessingService


@dataclass
class ZaliverCore:
    """
    Application service: settings + stores + job factories.

    Desktop PyQt and future HTTP backends should call this instead of
    embedding orchestration inside widgets.
    """

    platform: str
    settings: PlatformSettings
    videos: VideoStore
    uploads: UploadStore

    @classmethod
    def create(
        cls,
        platform: str = "youtube",
        *,
        settings_store: SettingsStore | Any | None = None,
        video_store: VideoStore | None = None,
        upload_store: UploadStore | None = None,
    ) -> ZaliverCore:
        store = ensure_settings_store(settings_store)
        plat = normalize_platform(platform)
        videos = video_store or VideoStore()
        uploads = upload_store or UploadStore(db_path=videos.db_path)
        return cls(
            platform=plat,
            settings=PlatformSettings(store, plat),
            videos=videos,
            uploads=uploads,
        )

    def processing_service(
        self, sink: JobProgressSink | None = None
    ) -> ProcessingService:
        return ProcessingService(sink)

    def slicing_service(self, sink: JobProgressSink | None = None) -> SlicingService:
        return SlicingService(sink)

    def run_uniquify(
        self,
        options: dict[str, Any],
        sink: JobProgressSink | None = None,
    ) -> None:
        self.processing_service(sink).run(options)

    def run_slicing(
        self,
        options: dict[str, Any],
        sink: JobProgressSink | None = None,
    ) -> None:
        self.slicing_service(sink).run(options)
