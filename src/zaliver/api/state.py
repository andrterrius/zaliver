"""Shared application state for the FastAPI process."""

from __future__ import annotations

from dataclasses import dataclass, field

from zaliver.api.config import ApiConfig
from zaliver.api.job_log_store import JobLogStore
from zaliver.api.jobs_registry import JobRegistry
from zaliver.config.platform_settings import normalize_platform
from zaliver.config.store import JsonFileSettingsStore, SettingsStore
from zaliver.core import ZaliverCore


@dataclass
class AppState:
    config: ApiConfig
    settings_store: SettingsStore
    jobs: JobRegistry
    platform: str = "youtube"
    _cores: dict[str, ZaliverCore] = field(default_factory=dict)

    def core(self, platform: str | None = None) -> ZaliverCore:
        plat = normalize_platform(platform or self.platform)
        existing = self._cores.get(plat)
        if existing is not None:
            return existing
        # Share one VideoStore/UploadStore across platforms (same DB file).
        if self._cores:
            any_core = next(iter(self._cores.values()))
            core = ZaliverCore.create(
                plat,
                settings_store=self.settings_store,
                video_store=any_core.videos,
                upload_store=any_core.uploads,
            )
        else:
            core = ZaliverCore.create(plat, settings_store=self.settings_store)
        self._cores[plat] = core
        return core

    def set_platform(self, platform: str) -> str:
        self.platform = normalize_platform(platform)
        self.core(self.platform)
        return self.platform


def build_app_state(config: ApiConfig) -> AppState:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.resolved_output_root().mkdir(parents=True, exist_ok=True)
    config.resolved_sources_root().mkdir(parents=True, exist_ok=True)
    (config.resolved_sources_root() / "uploads").mkdir(parents=True, exist_ok=True)
    settings_path = config.settings_path or (config.data_dir / "settings.json")
    store = JsonFileSettingsStore(settings_path)
    log_store = JobLogStore(
        config.data_dir / "job_logs",
        retention_days=config.job_log_retention_days,
        max_jobs=config.job_log_max_jobs,
    )
    jobs = JobRegistry(
        max_concurrent=config.max_concurrent_jobs,
        max_log_lines=config.max_log_lines,
        log_store=log_store,
    )
    state = AppState(
        config=config,
        settings_store=store,
        jobs=jobs,
        platform=normalize_platform(config.platform_default),
    )
    state.core(state.platform)
    return state
