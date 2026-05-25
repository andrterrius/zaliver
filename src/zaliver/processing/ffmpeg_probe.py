"""Video metadata via ffprobe (no OpenCV)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

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
        "-show_streams",
        "-show_format",
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


def _stream_rotation_degrees(st: Dict[str, Any]) -> int:
    tags = st.get("tags") or {}
    for key in ("rotate", "ROTATE"):
        raw = tags.get(key)
        if raw is None or str(raw).strip() in ("", "0", "N/A"):
            continue
        try:
            return int(round(float(str(raw).strip())))
        except ValueError:
            continue
    for sd in st.get("side_data_list") or []:
        if str(sd.get("side_data_type") or "") != "Display Matrix":
            continue
        rot = sd.get("rotation")
        if rot is None or str(rot).strip() in ("", "N/A", "0"):
            continue
        try:
            return int(round(float(str(rot).strip())))
        except ValueError:
            continue
    return 0


def _display_dimensions(w: int, h: int, rotation_deg: int) -> tuple[int, int]:
    """Размер кадра после autorotate ffmpeg (как в плеере), не сырой storage."""
    r = int(rotation_deg) % 360
    if r in (90, 270):
        return h, w
    return w, h


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
    w, h = _display_dimensions(w, h, _stream_rotation_degrees(st))
    fps = _parse_frame_rate(str(st.get("avg_frame_rate") or ""))
    if fps <= 0.01:
        fps = _parse_frame_rate(str(st.get("r_frame_rate") or ""))
    if fps <= 0.01:
        fps = 30.0
    fc = _frame_count_from_probe(st, fmt, fps)
    return w, h, fps, fc, 0


def _probe_positive_int(val: Any) -> Optional[int]:
    if val is None:
        return None
    s = str(val).strip()
    if not s or s in ("N/A", "0"):
        return None
    try:
        n = int(s)
        return n if n > 0 else None
    except ValueError:
        return None


def ffprobe_streams_and_format(path: str) -> Dict[str, Any]:
    """All streams (codec, bitrate) + format size/duration/bit_rate for bitrate heuristics."""
    probe = resolve_ffprobe_executable()
    if not probe:
        raise RuntimeError("ffprobe не найден (нужен рядом с ffmpeg или в PATH)")
    cmd = [
        probe,
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,bit_rate",
        "-show_entries",
        "format=duration,size,bit_rate",
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


def estimate_target_video_bps(path: str) -> Optional[int]:
    """
    Оценка битрейта основного видеопотока (бит/с) по метаданным контейнера,
    чтобы перекодирование давало размер файла близкий к исходнику.
    """
    try:
        data = ffprobe_streams_and_format(path)
    except (RuntimeError, json.JSONDecodeError, OSError):
        return None
    streams: List[Dict[str, Any]] = list(data.get("streams") or [])
    fmt = data.get("format") or {}

    video_br: Optional[int] = None
    n_audio = 0
    audio_sum = 0
    for st in streams:
        ct = str(st.get("codec_type") or "").lower()
        br = _probe_positive_int(st.get("bit_rate"))
        if ct == "video" and video_br is None:
            video_br = br
        elif ct == "audio":
            n_audio += 1
            if br is not None:
                audio_sum += br

    if video_br is not None:
        return video_br

    fmt_br = _probe_positive_int(fmt.get("bit_rate"))
    if fmt_br is not None:
        guess = fmt_br - audio_sum
        if n_audio > 0 and audio_sum == 0:
            guess -= 128_000 * n_audio
        if guess > 200_000:
            return guess

    size_b = _probe_positive_int(fmt.get("size"))
    dur_s = fmt.get("duration")
    try:
        dur = float(dur_s) if dur_s is not None and str(dur_s) not in ("N/A", "") else 0.0
    except ValueError:
        dur = 0.0
    if size_b is None or dur <= 0.05:
        return None
    total_bps = int((size_b * 8) / dur)
    overhead = audio_sum if audio_sum > 0 else (128_000 * n_audio if n_audio else 0)
    guess2 = total_bps - overhead - 64_000
    if guess2 > 200_000:
        return guess2
    return None


def probe_media_duration_seconds(path: str) -> Optional[float]:
    """
    Длительность файла в секундах (format.duration или сумма по потокам).
    Нужна для случайного отрезка фоновой музыки.
    """
    try:
        data = ffprobe_streams_and_format(path)
    except (RuntimeError, json.JSONDecodeError, OSError):
        return None
    fmt = data.get("format") or {}
    raw = fmt.get("duration")
    try:
        d = float(raw) if raw is not None and str(raw) not in ("N/A", "") else 0.0
    except ValueError:
        d = 0.0
    if d > 0.05:
        return d
    streams: List[Dict[str, Any]] = list(data.get("streams") or [])
    best = 0.0
    for st in streams:
        if str(st.get("codec_type") or "").lower() != "audio":
            continue
        sd = st.get("duration")
        try:
            x = float(sd) if sd is not None and str(sd) not in ("N/A", "") else 0.0
        except ValueError:
            x = 0.0
        if x > best:
            best = x
    return best if best > 0.05 else None
