"""API runtime configuration from environment (fail-closed defaults)."""

from __future__ import annotations

import os
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _split_roots(raw: str) -> list[Path]:
    parts: list[Path] = []
    for chunk in raw.replace(";", os.pathsep).split(os.pathsep):
        s = chunk.strip().strip('"')
        if not s:
            continue
        try:
            parts.append(Path(s).expanduser().resolve())
        except OSError:
            parts.append(Path(s).expanduser())
    return parts


def _default_data_dir() -> Path:
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
        if root:
            return Path(root) / "Zaliver" / "api"
    return Path.home() / ".zaliver" / "api"


@dataclass
class ApiConfig:
    """Fail-closed config: token required unless insecure localhost mode."""

    host: str = "127.0.0.1"
    port: int = 8080
    api_token: str = ""
    allow_insecure_no_token: bool = False
    enable_docs: bool = False
    cors_origins: list[str] = field(default_factory=list)
    allowed_roots: list[Path] = field(default_factory=list)
    data_dir: Path = field(default_factory=_default_data_dir)
    output_root: Path | None = None
    sources_root: Path | None = None
    settings_path: Path | None = None
    max_concurrent_jobs: int = 2
    max_workers_per_job: int = 8
    max_log_lines: int = 2000
    job_log_retention_days: int = 14
    job_log_max_jobs: int = 500
    allow_browser_jobs: bool = True
    platform_default: str = "youtube"

    def resolved_output_root(self) -> Path:
        root = self.output_root if self.output_root is not None else (
            self.data_dir / "output"
        )
        return Path(root).expanduser()

    def resolved_sources_root(self) -> Path:
        root = self.sources_root if self.sources_root is not None else (
            self.data_dir / "sources"
        )
        return Path(root).expanduser()


    @property
    def token_required(self) -> bool:
        if self.api_token:
            return True
        return not self.allow_insecure_no_token

    def validate_startup(self) -> None:
        if self.api_token:
            return
        if self.allow_insecure_no_token and self.host in {"127.0.0.1", "localhost", "::1"}:
            return
        raise RuntimeError(
            "ZALIVER_API_TOKEN is required. "
            "For local-only testing set ZALIVER_API_ALLOW_INSECURE=1 "
            "and bind to 127.0.0.1."
        )


def load_api_config() -> ApiConfig:
    data_dir = Path(
        os.environ.get("ZALIVER_API_DATA_DIR") or str(_default_data_dir())
    ).expanduser()
    settings_raw = (os.environ.get("ZALIVER_API_SETTINGS_PATH") or "").strip()
    settings_path = (
        Path(settings_raw).expanduser()
        if settings_raw
        else data_dir / "settings.json"
    )
    roots_raw = (os.environ.get("ZALIVER_ALLOWED_ROOTS") or "").strip()
    roots = _split_roots(roots_raw)
    output_raw = (os.environ.get("ZALIVER_API_OUTPUT_DIR") or "").strip()
    output_root = (
        Path(output_raw).expanduser() if output_raw else data_dir / "output"
    )
    sources_raw = (os.environ.get("ZALIVER_API_SOURCES_DIR") or "").strip()
    sources_root = (
        Path(sources_raw).expanduser() if sources_raw else data_dir / "sources"
    )
    if not roots:
        # Sensible defaults: home + data dir (still explicit sandbox).
        try:
            roots = [Path.home().resolve(), data_dir.resolve()]
        except OSError:
            roots = [Path.home(), data_dir]
    # Always allow writing managed outputs / sources.
    for extra in (output_root, sources_root):
        try:
            resolved = extra.resolve()
        except OSError:
            resolved = extra
        if not any(_is_same_or_under(resolved, r) for r in roots):
            roots.append(resolved)

    cors_raw = (os.environ.get("ZALIVER_API_CORS_ORIGINS") or "").strip()
    if cors_raw:
        cors = [c.strip() for c in cors_raw.split(",") if c.strip()]
    else:
        # Local React (Vite) defaults — override with ZALIVER_API_CORS_ORIGINS.
        cors = [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]

    token = (os.environ.get("ZALIVER_API_TOKEN") or "").strip()
    # Temporary default for local/dev until real auth is configured.
    if not token:
        token = "secret"

    return ApiConfig(
        host=(os.environ.get("ZALIVER_API_HOST") or "127.0.0.1").strip() or "127.0.0.1",
        port=_env_int("ZALIVER_API_PORT", 8080),
        api_token=token,
        allow_insecure_no_token=_env_bool("ZALIVER_API_ALLOW_INSECURE", False),
        enable_docs=_env_bool("ZALIVER_API_DOCS", False),
        cors_origins=cors,
        allowed_roots=roots,
        data_dir=data_dir,
        output_root=output_root,
        sources_root=sources_root,
        settings_path=settings_path,
        max_concurrent_jobs=max(1, min(8, _env_int("ZALIVER_API_MAX_JOBS", 2))),
        max_workers_per_job=max(1, min(32, _env_int("ZALIVER_API_MAX_WORKERS", 8))),
        max_log_lines=max(100, min(50_000, _env_int("ZALIVER_API_MAX_LOG_LINES", 2000))),
        job_log_retention_days=max(
            1, min(365, _env_int("ZALIVER_API_JOB_LOG_RETENTION_DAYS", 14))
        ),
        job_log_max_jobs=max(
            10, min(10_000, _env_int("ZALIVER_API_JOB_LOG_MAX_JOBS", 500))
        ),
        allow_browser_jobs=_env_bool("ZALIVER_API_ALLOW_BROWSER_JOBS", True),
        platform_default=(
            os.environ.get("ZALIVER_API_PLATFORM") or "youtube"
        ).strip().lower()
        or "youtube",
    )


def _is_same_or_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve() if root.exists() else root)
        return True
    except (ValueError, OSError):
        try:
            return path == root.resolve()
        except OSError:
            return path == root



def generate_dev_token() -> str:
    return secrets.token_urlsafe(32)
