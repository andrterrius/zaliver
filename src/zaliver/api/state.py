"""Shared application state for the FastAPI process."""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from zaliver.api.config import ApiConfig
from zaliver.api.job_log_store import JobLogStore
from zaliver.api.jobs_registry import JobRegistry
from zaliver.api.sandbox import register_denied_root
from zaliver.api.users import SessionStore, UsersStore
from zaliver.config.platform_settings import normalize_platform
from zaliver.config.store import JsonFileSettingsStore, SettingsStore
from zaliver.core import ZaliverCore


def _secure_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform != "win32":
            path.chmod(0o700)
    except OSError:
        pass


@dataclass
class AppState:
    config: ApiConfig
    settings_store: SettingsStore
    jobs: JobRegistry
    users: UsersStore
    sessions: SessionStore
    platform: str = "youtube"
    _cores: dict[str, ZaliverCore] = field(default_factory=dict)
    _user_settings: dict[str, SettingsStore] = field(default_factory=dict)

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

    def user_settings(self, username: str) -> SettingsStore:
        key = (username or "").strip().lower()
        if not key:
            raise ValueError("username required")
        existing = self._user_settings.get(key)
        if existing is not None:
            return existing
        user_dir = self.config.data_dir / "users" / key
        user_dir.mkdir(parents=True, exist_ok=True)
        path = user_dir / "settings.json"
        if not path.is_file():
            # Seed from legacy global settings once.
            legacy = self.config.settings_path or (self.config.data_dir / "settings.json")
            try:
                if legacy.is_file():
                    shutil.copy2(legacy, path)
            except OSError:
                pass
        store = JsonFileSettingsStore(path)
        self._user_settings[key] = store
        return store

    def set_platform(self, platform: str, *, username: str | None = None) -> str:
        self.platform = normalize_platform(platform)
        store = (
            self.user_settings(username)
            if username
            else self.settings_store
        )
        try:
            store.setValue("api/platform", self.platform)
            store.sync()
        except Exception:
            pass
        self.core(self.platform)
        return self.platform

    def platform_for_user(self, username: str) -> str:
        store = self.user_settings(username)
        try:
            saved = str(store.value("api/platform", "") or "").strip()
        except Exception:
            saved = ""
        if saved:
            return normalize_platform(saved)
        return normalize_platform(self.platform or self.config.platform_default)

    def refresh_platform_from_store(self) -> str:
        """Re-read persisted platform (multi-worker / after restart)."""
        try:
            saved = str(self.settings_store.value("api/platform", "") or "").strip()
        except Exception:
            saved = ""
        if not saved:
            return self.platform
        plat = normalize_platform(saved)
        if plat != self.platform:
            self.platform = plat
            self.core(plat)
        return self.platform


def build_app_state(config: ApiConfig) -> AppState:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    private_dir = config.resolved_private_dir()
    _secure_mkdir(private_dir)
    register_denied_root(private_dir)
    # Also deny per-user credential-adjacent tree if private were nested under data.
    register_denied_root(config.data_dir / "private")

    config.resolved_output_root().mkdir(parents=True, exist_ok=True)
    config.resolved_sources_root().mkdir(parents=True, exist_ok=True)
    (config.resolved_sources_root() / "uploads").mkdir(parents=True, exist_ok=True)
    (config.resolved_sources_root() / "video").mkdir(parents=True, exist_ok=True)
    (config.resolved_sources_root() / "audio").mkdir(parents=True, exist_ok=True)
    (config.data_dir / "users").mkdir(parents=True, exist_ok=True)

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
    users = UsersStore(private_dir / "users.json")
    sessions = SessionStore(
        private_dir / "sessions.json",
        ttl_seconds=config.session_ttl_seconds,
    )
    sessions.revoke_unknown_users({u.username for u in users.list_users()})

    bootstrap_pw = (config.bootstrap_admin_password or "").strip()
    if not users.list_users():
        if not bootstrap_pw:
            bootstrap_pw = "admin"
            print(
                "WARNING: created bootstrap admin with password 'admin'. "
                "Set ZALIVER_ADMIN_PASSWORD and change it immediately.",
                file=sys.stderr,
            )
        created = users.ensure_bootstrap_admin(
            username=config.bootstrap_admin_username or "admin",
            password=bootstrap_pw,
        )
        if created is not None:
            print(
                f"Bootstrap admin user '{created.username}' created "
                f"(credentials file: {users.path}).",
                file=sys.stderr,
            )

    saved_plat = str(store.value("api/platform", "") or "").strip()
    platform = normalize_platform(saved_plat or config.platform_default)
    state = AppState(
        config=config,
        settings_store=store,
        jobs=jobs,
        users=users,
        sessions=sessions,
        platform=platform,
    )
    state.core(state.platform)
    return state
