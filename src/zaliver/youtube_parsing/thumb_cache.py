"""Локальный файловый кэш превью YouTube (mqdefault) для списков в UI."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def zaliver_app_data_dir() -> Path:
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
        if root:
            return Path(root) / "Zaliver"
    return Path.home() / ".zaliver"


def youtube_mq_thumbnail_url(video_id: str) -> str:
    vid = (video_id or "").strip()
    return f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"


def youtube_thumb_cache_dir() -> Path:
    return zaliver_app_data_dir() / "cache" / "youtube_thumbnails"


def _safe_thumb_stem(video_id: str) -> str:
    s = "".join(
        c if (c.isalnum() or c in "_-") else "_" for c in (video_id or "").strip()
    )
    return (s or "x")[:200]


def thumb_cache_path(video_id: str) -> Path:
    return youtube_thumb_cache_dir() / f"{_safe_thumb_stem(video_id)}.jpg"


def _looks_like_jpeg(data: bytes) -> bool:
    return len(data) >= 64 and data[:2] == b"\xff\xd8"


def read_youtube_thumb_cache(video_id: str) -> bytes | None:
    p = thumb_cache_path(video_id)
    try:
        if not p.is_file():
            return None
        data = p.read_bytes()
        if _looks_like_jpeg(data):
            return data
        try:
            p.unlink()
        except OSError:
            pass
    except OSError:
        pass
    return None


def write_youtube_thumb_cache(video_id: str, data: bytes) -> None:
    if not _looks_like_jpeg(data):
        return
    p = thumb_cache_path(video_id)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(p)
    except OSError:
        try:
            if tmp.is_file():
                tmp.unlink(missing_ok=True)  # type: ignore[arg-type]
        except OSError:
            pass
