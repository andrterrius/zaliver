"""Automatic color correction from video sample (gray-world + CLAHE on L)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np


def estimate_grade_params(
    video_path: str,
    sample_frames: int = 48,
    gain_clip: Tuple[float, float] = (0.65, 1.55),
) -> Dict[str, Any]:
    """
    Sample frames evenly across the file and estimate BGR gains (gray-world)
    plus CLAHE settings. Same params applied to all frames for temporal stability.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return _neutral_params()
    try:
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        if n <= 0:
            frames = _read_sequential(cap, min(sample_frames, 200))
        else:
            k = min(sample_frames, n)
            indices = np.linspace(0, n - 1, k, dtype=np.int64)
            frames = []
            for idx in indices:
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
                ok, fr = cap.read()
                if ok and fr is not None:
                    frames.append(fr)
        if not frames:
            return _neutral_params()
        stack = np.stack([f.astype(np.float32) for f in frames], axis=0)
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
    finally:
        cap.release()


def _neutral_params() -> Dict[str, Any]:
    return {"bgr_gains": (1.0, 1.0, 1.0), "clahe_clip": 2.2, "clahe_tile": 8}


def _read_sequential(cap: cv2.VideoCapture, max_frames: int) -> List[np.ndarray]:
    out: List[np.ndarray] = []
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    for _ in range(max_frames):
        ok, fr = cap.read()
        if not ok:
            break
        out.append(fr)
    return out


def apply_auto_color_grade(
    bgr: np.ndarray,
    params: Dict[str, Any],
    strength: float = 1.0,
) -> np.ndarray:
    """
    strength in [0, 1]: blend between input and fully graded result.
    """
    strength = float(np.clip(strength, 0.0, 1.0))
    if strength <= 0:
        return bgr

    gb, gg, gr = params.get("bgr_gains", (1.0, 1.0, 1.0))
    clip = float(params.get("clahe_clip", 2.2))
    tile = int(params.get("clahe_tile", 8))
    tile = max(2, tile)

    x = bgr.astype(np.float32)
    gains = np.array([[[gb, gg, gr]]], dtype=np.float32)
    graded = x * gains
    graded = np.clip(graded, 0, 255).astype(np.uint8)

    lab = cv2.cvtColor(graded, cv2.COLOR_BGR2LAB)
    clahe = cv2.createCLAHE(clipLimit=clip, tileGridSize=(tile, tile))
    l, a, b = cv2.split(lab)
    l2 = clahe.apply(l)
    lab2 = cv2.merge([l2, a, b])
    graded = cv2.cvtColor(lab2, cv2.COLOR_LAB2BGR)

    if strength >= 0.999:
        return graded
    a_f = bgr.astype(np.float32)
    g_f = graded.astype(np.float32)
    out = np.clip(a_f * (1.0 - strength) + g_f * strength, 0, 255).astype(np.uint8)
    return out
