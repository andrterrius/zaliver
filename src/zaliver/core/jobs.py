"""Background job helper (daemon thread) for profile/upload workers."""

from __future__ import annotations

import threading
from typing import Any, Callable


def start_daemon_job(
    target: Callable[..., Any],
    *,
    name: str | None = None,
    kwargs: dict[str, Any] | None = None,
) -> threading.Thread:
    """Start a daemon thread; same pattern MainWindow uses for profile jobs."""
    thread = threading.Thread(
        target=target,
        kwargs=kwargs or {},
        daemon=True,
        name=name,
    )
    thread.start()
    return thread
