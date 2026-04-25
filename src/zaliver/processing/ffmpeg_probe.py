"""Video metadata via ffprobe (no OpenCV)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from zaliver.processing.ffmpeg_merge import resolve_ffmpeg_executable


def resolve_ffprobe_executable() -> Optional[str]:
    ff = resolve_ffmpeg_executable()
    if ff:
        p = Path(ff)
        name = "ffprobe.exe" if p.name.lower().endswith(".exe") else "ffprobe"
        sib = p.parent / name
        if sib.is_file():
            return str(sib.resolve())
    for cand in ("ffprobe", "ffprobe.exe"):
        w = shutil.which(cand)
        if w:
            return w
    return None


def _popen_flags() -> int:
    if sys.platform == "win32":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def _parse_frame_rate(s: str) -> float:
    s = (s or "").strip()
    if not s or s == "0/0":
        return 30.0
    if "/" in s:
        a, b = s.split("/", 1)
        try:
            x, y = float(a), float(b)
            return (x / y) if y else 30.0
        except ValueError:
            return 30.0
    try:
        return float(s)
    except ValueError:
        return 30.0


def ffprobe_json(path: str) -> Dict[str, Any]:
    probe = resolve_ffprobe_executable()
    if not probe:
        raise RuntimeError("ffprobe не найден (нужен рядом с ffmpeg или в PATH)")
    cmd = [
        probe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,avg_frame_rate,r_frame_rate,nb_frames,duration",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        path,
    ]
    p = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_popen_flags(),
        timeout=120,
    )
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip()
        raise RuntimeError(err or f"ffprobe failed ({p.returncode})")
    return json.loads(p.stdout or "{}")


def _frame_count_from_probe(
    st: Dict[str, Any], fmt: Dict[str, Any], fps: float
) -> int:
    nb = st.get("nb_frames")
    if nb is not None and str(nb).strip() and str(nb) not in ("N/A", "0"):
        try:
            n = int(str(nb).strip())
            if n > 0:
                return n
        except ValueError:
            pass
    dur = st.get("duration")
    if dur is None or str(dur) in ("N/A", ""):
        dur = fmt.get("duration")
    if dur is not None and str(dur) not in ("N/A", ""):
        try:
            d = float(dur)
            if d > 0 and fps > 0:
                return max(1, int(round(d * fps)))
        except ValueError:
            pass
    raise RuntimeError(
        "Не удалось определить число кадров (nb_frames/duration). "
        "Попробуйте другой контейнер или переупаковать в MP4."
    )


def probe_video_stream(path: str) -> tuple[int, int, float, int, int]:
    """
    Returns (width, height, fps, frame_count, fourcc_int).
    fourcc_int is kept for compatibility with VideoInfo; always 0 here.
    """
    data = ffprobe_json(path)
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError("ffprobe: нет видеопотока")
    st = streams[0]
    fmt = data.get("format") or {}
    w = int(st.get("width") or 0)
    h = int(st.get("height") or 0)
    if w <= 0 or h <= 0:
        raise RuntimeError("ffprobe: некорректный размер кадра")
    fps = _parse_frame_rate(str(st.get("avg_frame_rate") or ""))
    if fps <= 0.01:
        fps = _parse_frame_rate(str(st.get("r_frame_rate") or ""))
    if fps <= 0.01:
        fps = 30.0
    fc = _frame_count_from_probe(st, fmt, fps)
    return w, h, fps, fc, 0
