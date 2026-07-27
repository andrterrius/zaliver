"""Progress callbacks for headless jobs (Qt / web adapters bind these)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


def _noop(*_args: object, **_kwargs: object) -> None:
    return None


@dataclass
class JobProgressSink:
    """UI-agnostic progress surface for processing / slicing / long jobs."""

    on_progress: Callable[[int, int, str], None] = field(default=_noop)
    on_finished: Callable[[bool, str], None] = field(default=_noop)
    on_log: Callable[[str], None] = field(default=_noop)
    on_output_saved: Callable[[str, bool], None] = field(default=_noop)
