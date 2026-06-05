"""RLIMIT_NOFILE / EMFILE: лимит дескрипторов (типично macOS soft=256)."""

from __future__ import annotations

import os
import sys

_DEFAULT_TARGET = 4096
_LIMIT_TARGETS = (65536, 16384, 4096, 2048, 1024)
# CPython ProcessPoolExecutor на Windows: max_workers <= 60
_WIN_PROCESS_POOL_MAX_WORKERS = 60


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
            f"Лимит открытых файлов низкий ({after}). "
            "При Errno 24 уменьшите число потоков или параллельные заливы."
        )
    return None


def cap_workers_for_fd_limit(requested: int) -> int:
    """Число параллельных ffmpeg-воркеров (без искусственного потолка)."""
    return max(1, int(requested))


def max_process_pool_workers() -> int:
    """Верхняя граница ProcessPoolExecutor (для UI и run())."""
    if sys.platform == "win32":
        return _WIN_PROCESS_POOL_MAX_WORKERS
    n = max(1, os.cpu_count() or 2)
    return max(n * 4, 64)


def cap_process_pool_workers(requested: int) -> int:
    return max(1, min(int(requested), max_process_pool_workers()))
