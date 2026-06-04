"""RLIMIT_NOFILE / EMFILE: лимит дескрипторов (типично macOS soft=256)."""

from __future__ import annotations

import sys

_DEFAULT_TARGET = 4096
_LIMIT_TARGETS = (65536, 16384, 4096, 2048, 1024)
# Запас под concat, ffprobe, SQLite, HTTP, UI.
_BASELINE_FDS = 80
_FDS_PER_WORKER = 28
_DARWIN_WORKER_CAP = 6


def soft_fd_limit() -> int:
    if sys.platform == "win32":
        return 8192
    try:
        import resource

        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        return max(64, int(soft))
    except Exception:
        return 256


def raise_fd_limit_soft(*, target: int = _DEFAULT_TARGET) -> int:
    """
    Поднять soft ulimit -n (на macOS часто 256 → EMFILE при ffmpeg + HTTP).
    Без прав суперпользователя поднимается только до hard.
    """
    if sys.platform == "win32":
        return soft_fd_limit()
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        tried = {int(target), *_LIMIT_TARGETS}
        for t in sorted(tried, reverse=True):
            want = min(max(int(t), int(soft)), int(hard))
            if want <= soft:
                continue
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
                soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            except OSError:
                continue
        return int(soft)
    except Exception:
        return soft_fd_limit()


def bootstrap_fd_limits() -> str | None:
    """
    Вызвать при старте UI: поднять лимит и вернуть строку для лога (или None).
    """
    if sys.platform == "win32":
        raise_fd_limit_soft()
        return None
    before = soft_fd_limit()
    after = raise_fd_limit_soft()
    if after > before:
        return f"Лимит открытых файлов: {after} (было {before}) — выставлено автоматически."
    if after < 1024:
        return (
            f"Лимит открытых файлов низкий ({after}); потоки обработки ограничены автоматически. "
            "При Errno 24 уменьшите параллельные заливы/браузеры."
        )
    return None


def cap_workers_for_fd_limit(requested: int) -> int:
    """Ограничить число параллельных ffmpeg-воркеров под текущий ulimit."""
    req = max(1, int(requested))
    soft = soft_fd_limit()
    budget = max(32, soft - _BASELINE_FDS)
    cap = max(1, budget // _FDS_PER_WORKER)
    if sys.platform == "darwin":
        cap = min(cap, _DARWIN_WORKER_CAP)
    return max(1, min(req, cap))
