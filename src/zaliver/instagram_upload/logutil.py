"""Логи Instagram-алгоритмов: единый sink + profile id в каждой строке."""

from __future__ import annotations

from functools import wraps
from typing import Callable, TypeVar

from zaliver.log_format import format_log_line, get_log_profile_id, log_profile_context

F = TypeVar("F", bound=Callable[..., object])


def emit_instagram_log(message: str, *, tag: str = "[instagram]") -> None:
    """Пишет в тот же sink, что и youtube_upload.studio, с id профиля браузера."""
    from zaliver.youtube_upload.studio import _LOG_SINK, get_studio_profile_id

    t = (tag or "").strip() or "[instagram]"
    msg = (message or "").rstrip("\n")
    line = format_log_line(
        f"{t} {msg}" if msg else t,
        profile_id=get_studio_profile_id() or get_log_profile_id(),
    )
    sink = _LOG_SINK
    if sink is not None:
        try:
            sink(line)
        except Exception:
            pass
    else:
        print(line)


def instagram_entrypoint(fn: F) -> F:
    """Как studio._studio_entrypoint: выставляет profile id из kwargs внутри Playwright."""

    @wraps(fn)
    def wrapped(*args, **kwargs):
        with log_profile_context(kwargs.get("profile_id")):
            return fn(*args, **kwargs)

    return wrapped  # type: ignore[return-value]
