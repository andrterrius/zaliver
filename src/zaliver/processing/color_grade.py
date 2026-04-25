"""Automatic color correction: sample frames via ffmpeg, estimate BGR gains (gray-world)."""

from __future__ import annotations

import subprocess
import sys
from typing import Any, Dict, Tuple

import numpy as np

from zaliver.processing.ffmpeg_merge import resolve_ffmpeg_executable
from zaliver.processing.ffmpeg_probe import probe_video_stream


def _popen_flags() -> int:
    if sys.platform == "win32":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def estimate_grade_params(
    video_path: str,
    sample_frames: int = 48,
    gain_clip: Tuple[float, float] = (0.65, 1.55),
) -> Dict[str, Any]:
    """
    Sample frames evenly across the file and estimate BGR gains (gray-world).
    Same params applied to the whole clip in ffmpeg (colorchannelmixer).
    """
    exe = resolve_ffmpeg_executable()
    if not exe:
        return _neutral_params()
    try:
        _w, _h, _fps, fc, _fourcc = probe_video_stream(video_path)
    except Exception:
        return _neutral_params()
    if fc <= 0:
        return _neutral_params()

    step = max(1, fc // max(1, int(sample_frames)))
    sw, sh = 640, 360
    vf = (
        f"select=not(mod(n\\,{step})),"
        f"scale={sw}:{sh}:force_original_aspect_ratio=decrease,"
        f"pad={sw}:{sh}:(ow-iw)/2:(oh-ih)/2:black,format=bgr24"
    )
    k = max(2, min(int(sample_frames), fc))
    cmd = [
        exe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        video_path,
        "-vf",
        vf,
        "-frames:v",
        str(k),
        "-pix_fmt",
        "bgr24",
        "-f",
        "rawvideo",
        "-",
    ]
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            timeout=180,
            creationflags=_popen_flags(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return _neutral_params()
    if p.returncode != 0 or not p.stdout:
        return _neutral_params()
    raw = p.stdout
    fs = sw * sh * 3
    if len(raw) < fs * 2:
        return _neutral_params()
    nfr = len(raw) // fs
    if nfr < 2:
        return _neutral_params()
    try:
        buf = np.frombuffer(raw[: nfr * fs], dtype=np.uint8).reshape((nfr, sh, sw, 3))
    except ValueError:
        return _neutral_params()
    stack = buf.astype(np.float32)
    mb = float(stack[:, :, :, 0].mean())
    mg = float(stack[:, :, :, 1].mean())
    mr = float(stack[:, :, :, 2].mean())
    m = (mb + mg + mr) / 3.0
    eps = 1e-3
    lo, hi = gain_clip
    gb = float(np.clip(m / (mb + eps), lo, hi))
    gg = float(np.clip(m / (mg + eps), lo, hi))
    gr = float(np.clip(m / (mr + eps), lo, hi))
    return {
        "bgr_gains": (gb, gg, gr),
        "clahe_clip": 2.2,
        "clahe_tile": 8,
    }


def _neutral_params() -> Dict[str, Any]:
    return {"bgr_gains": (1.0, 1.0, 1.0), "clahe_clip": 2.2, "clahe_tile": 8}
