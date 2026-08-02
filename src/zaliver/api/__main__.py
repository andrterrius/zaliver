"""Run: python -m zaliver.api"""

from __future__ import annotations

import os
import sys


def main() -> None:
    try:
        import uvicorn
    except ImportError as e:
        print(
            "uvicorn/fastapi not installed. Install with:\n"
            "  pip install 'zaliver[api]'\n"
            "or:\n"
            "  pip install fastapi uvicorn",
            file=sys.stderr,
        )
        raise SystemExit(1) from e

    from zaliver.api.config import load_api_config

    # Mark API process so ProcessPool is avoided on Windows (spawn can kill uvicorn).
    os.environ["ZALIVER_API_SERVER"] = "1"
    try:
        from zaliver.processing.win_console import install_permanent_ctrl_break_guard

        install_permanent_ctrl_break_guard()
    except Exception:
        pass

    cfg = load_api_config()
    try:
        cfg.validate_startup()
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(2) from e

    print(
        f"Zaliver API on http://{cfg.host}:{cfg.port} "
        f"(docs={'on' if cfg.enable_docs else 'off'}, "
        f"browser_jobs={'on' if cfg.allow_browser_jobs else 'off'})",
        file=sys.stderr,
    )
    print(
        f"Auth: login/password (private dir: {cfg.resolved_private_dir()})",
        file=sys.stderr,
    )
    if cfg.api_token:
        print(
            "Legacy ZALIVER_API_TOKEN is set (automation Bearer still accepted).",
            file=sys.stderr,
        )
    from zaliver.api.static_ui import resolve_web_dist

    dist = resolve_web_dist()
    if dist is not None:
        print(f"Web UI: {dist}", file=sys.stderr)
    else:
        print(
            "Web UI: not found (run `cd web && npm run build`, "
            "or set ZALIVER_WEB_DIST)",
            file=sys.stderr,
        )
    print(
        "Allowed roots:\n  " + "\n  ".join(str(r) for r in cfg.allowed_roots),
        file=sys.stderr,
    )

    uvicorn.run(
        "zaliver.api.app:create_app",
        factory=True,
        host=cfg.host,
        port=cfg.port,
        log_level="info",
    )


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    main()
