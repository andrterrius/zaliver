"""Единый формат строк лога: время и profile id (если известен)."""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import datetime
from functools import wraps
from typing import Callable, Iterator, TypeVar

_LOG_PROFILE_ID: ContextVar[str | None] = ContextVar("log_profile_id", default=None)

_TS_RE = re.compile(
    r"\[(?:\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}|\d{2}:\d{2}:\d{2})\]"
)

F = TypeVar("F", bound=Callable[..., object])


def log_timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def set_log_profile_id(profile_id: str | None) -> Token:
    pid = (profile_id or "").strip() or None
    return _LOG_PROFILE_ID.set(pid)


def reset_log_profile_id(token: Token) -> None:
    _LOG_PROFILE_ID.reset(token)


def get_log_profile_id() -> str | None:
    return _LOG_PROFILE_ID.get()


@contextmanager
def log_profile_context(profile_id: str | None) -> Iterator[None]:
    token = set_log_profile_id(profile_id)
    from zaliver.youtube_upload.studio import studio_profile_context

    with studio_profile_context(profile_id):
        try:
            yield
        finally:
            reset_log_profile_id(token)


def format_log_line(message: str, *, profile_id: str | None = None) -> str:
    msg = (message or "").rstrip("\n")
    if not msg:
        return msg

    pid = (profile_id or get_log_profile_id() or "").strip() or None

    prefix_parts: list[str] = []
    if not _TS_RE.search(msg):
        prefix_parts.append(f"[{log_timestamp()}]")
    # Всегда префикс [pid], даже если в тексте уже есть profile_id=...
    if pid and f"[{pid}]" not in msg:
        prefix_parts.append(f"[{pid}]")

    if prefix_parts:
        return " ".join(prefix_parts) + " " + msg
    return msg


def with_log_profile(fn: F) -> F:
    """Декоратор для функций с profile_id первым аргументом."""

    @wraps(fn)
    def wrapped(profile_id: str, /, *args, **kwargs):
        with log_profile_context(profile_id):
            return fn(profile_id, *args, **kwargs)

    return wrapped  # type: ignore[return-value]
