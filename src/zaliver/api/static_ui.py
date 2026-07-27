"""Resolve built React UI (web/dist) for FastAPI static serving."""

from __future__ import annotations

import os
from pathlib import Path


def resolve_web_dist() -> Path | None:
    """
    Find Vite build output.

    Order:
    1. ZALIVER_WEB_DIST
    2. zaliver/api/web_dist (bundled next to this package)
    3. <repo>/web/dist (dev checkout with src/ layout)
    """
    env = (os.environ.get("ZALIVER_WEB_DIST") or "").strip()
    if env:
        p = Path(env).expanduser()
        if (p / "index.html").is_file():
            return p.resolve()
        if p.is_file() and p.name == "index.html":
            return p.parent.resolve()

    here = Path(__file__).resolve().parent
    bundled = here / "web_dist"
    if (bundled / "index.html").is_file():
        return bundled

    # .../src/zaliver/api -> repo/web/dist
    repo_dist = here.parents[2] / "web" / "dist"
    if (repo_dist / "index.html").is_file():
        return repo_dist

    # .../zaliver/api (flat install) -> sibling web/dist unlikely; stop
    return None
