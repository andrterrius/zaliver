"""Список видеофайлов во входной папке."""

from __future__ import annotations

from pathlib import Path
from typing import List

VIDEO_SUFFIXES = frozenset({".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".wmv"})


def list_video_files(folder: Path) -> List[Path]:
    if not folder.is_dir():
        return []
    found: List[Path] = []
    for p in folder.iterdir():
        if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES:
            found.append(p)
    return sorted(found, key=lambda x: x.name.lower())
