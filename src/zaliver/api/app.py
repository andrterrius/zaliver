"""FastAPI application factory."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from zaliver import __version__
from zaliver.api.auth import make_auth_dependency
from zaliver.api.config import ApiConfig, load_api_config
from zaliver.api.routes import build_router
from zaliver.api.schemas import HealthResponse
from zaliver.api.state import build_app_state
from zaliver.api.static_ui import resolve_web_dist
from zaliver.api.antydetect_resolve import ensure_antidetect_defaults


_API_PREFIXES = (
    "v1/",
    "health",
    "docs",
    "redoc",
    "openapi.json",
    "assets/",
)


def _mount_web_ui(app: FastAPI, dist: Path) -> None:
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="web-assets")

    index = dist / "index.html"

    @app.get("/")
    def web_index() -> FileResponse:
        return FileResponse(index)

    @app.get("/{full_path:path}")
    def web_spa(full_path: str) -> FileResponse:
        # Let FastAPI 404 real API/docs paths if somehow unmatched.
        if full_path.startswith(_API_PREFIXES) or full_path in {
            "health",
            "docs",
            "redoc",
            "openapi.json",
        }:
            raise HTTPException(status_code=404, detail="Not found")
        candidate = (dist / full_path).resolve()
        try:
            candidate.relative_to(dist.resolve())
        except ValueError as e:
            raise HTTPException(status_code=404, detail="Not found") from e
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)


def create_app(config: ApiConfig | None = None) -> FastAPI:
    # Also set when launched via `uvicorn …:create_app` (not only `python -m zaliver.api`).
    os.environ.setdefault("ZALIVER_API_SERVER", "1")
    try:
        from zaliver.processing.win_console import install_permanent_ctrl_break_guard

        install_permanent_ctrl_break_guard()
    except Exception:
        pass
    cfg = config or load_api_config()
    cfg.validate_startup()
    os.environ["ZALIVER_JOB_LOG_DIR"] = str((cfg.data_dir / "job_logs").resolve())
    state = build_app_state(cfg)
    auth = make_auth_dependency(state)
    ensure_antidetect_defaults(state.core().settings)
    web_dist = resolve_web_dist()

    docs_url = "/docs" if cfg.enable_docs else None
    redoc_url = "/redoc" if cfg.enable_docs else None
    openapi_url = "/openapi.json" if cfg.enable_docs else None

    app = FastAPI(
        title="Zaliver API",
        description=(
            "Safe headless control plane for Zaliver. "
            "Requires Bearer token; file paths are sandboxed to ZALIVER_ALLOWED_ROOTS; "
            "browser/upload jobs require ZALIVER_API_ALLOW_BROWSER_JOBS=1."
        ),
        version=__version__,
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
    )
    app.state.zaliver = state  # type: ignore[attr-defined]
    app.state.web_dist = web_dist  # type: ignore[attr-defined]

    if cfg.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cfg.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "PATCH"],
            allow_headers=["Authorization", "Content-Type"],
        )

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            version=__version__,
            platform=state.platform,
            browser_jobs_enabled=state.config.allow_browser_jobs,
            docs_enabled=state.config.enable_docs,
        )

    app.include_router(build_router(), dependencies=[Depends(auth)])

    if web_dist is not None:
        _mount_web_ui(app, web_dist)

    return app
