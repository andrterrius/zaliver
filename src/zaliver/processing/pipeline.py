"""Stateless per-frame uniquification (light visual transforms)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, Dict, Optional

import cv2
import numpy as np

from zaliver.processing.color_grade import apply_auto_color_grade


@dataclass
class UniquifySettings:
    brightness_delta: float = 0.0  # additive on uint8 after clip
    contrast: float = 1.0  # multiply around 128
    saturation_scale: float = 1.0
    crop_jitter_px: int = 0  # max pixels crop from each side (random per chunk in worker)
    scale_pct: float = 100.0  # 99.5–100.5 style; 100 = no resize
    noise_sigma: float = 0.0
    seed_base: int = 0
    auto_color_grade: bool = False
    auto_color_strength: float = 0.85  # 0..1 blend graded vs original

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "UniquifySettings":
        allowed = {f.name for f in fields(UniquifySettings)}
        return UniquifySettings(**{k: v for k, v in d.items() if k in allowed})


def random_uniquify_settings(
    *,
    auto_color_grade: bool = False,
    auto_color_strength: float = 0.85,
) -> UniquifySettings:
    """Новый набор лёгких эффектов для каждого запуска / каждого файла."""
    r = np.random.default_rng()
    return UniquifySettings(
        brightness_delta=float(r.uniform(-22.0, 22.0)),
        contrast=float(r.uniform(0.88, 1.14)),
        saturation_scale=float(r.uniform(0.88, 1.12)),
        crop_jitter_px=int(r.integers(0, 4)),
        scale_pct=float(r.uniform(99.4, 100.6)),
        noise_sigma=float(r.uniform(0.15, 4.0)),
        seed_base=int(r.integers(0, 99_999_999)),
        auto_color_grade=bool(auto_color_grade),
        auto_color_strength=float(auto_color_strength),
    )


def _rng(job_id: str, frame_index: int, seed_base: int) -> np.random.Generator:
    h = hash((job_id, seed_base, frame_index)) & 0xFFFFFFFF
    return np.random.default_rng(h)


def apply_frame(
    frame_bgr: np.ndarray,
    frame_index: int,
    job_id: str,
    settings: UniquifySettings,
    crop_offsets: tuple[int, int, int, int] | None = None,
    color_grade_params: Optional[Dict[str, Any]] = None,
) -> np.ndarray:
    """crop_offsets: (top, bottom, left, right) fixed for whole chunk when set."""
    out = frame_bgr
    if (
        settings.auto_color_grade
        and color_grade_params is not None
    ):
        out = apply_auto_color_grade(
            out,
            color_grade_params,
            strength=settings.auto_color_strength,
        )
    if settings.scale_pct != 100.0 and settings.scale_pct > 0:
        f = settings.scale_pct / 100.0
        h, w = out.shape[:2]
        nh, nw = max(1, int(h * f)), max(1, int(w * f))
        out = cv2.resize(out, (nw, nh), interpolation=cv2.INTER_LINEAR)
        if nh != h or nw != w:
            if nh >= h and nw >= w:
                y0 = (nh - h) // 2
                x0 = (nw - w) // 2
                out = out[y0 : y0 + h, x0 : x0 + w]
            else:
                pad = np.zeros_like(frame_bgr)
                y0 = (h - nh) // 2
                x0 = (w - nw) // 2
                pad[y0 : y0 + nh, x0 : x0 + nw] = out
                out = pad

    jmax = max(0, int(settings.crop_jitter_px))
    if jmax > 0 and crop_offsets is None:
        rng = _rng(job_id, frame_index, settings.seed_base)
        t = int(rng.integers(0, jmax + 1))
        b = int(rng.integers(0, jmax + 1))
        l = int(rng.integers(0, jmax + 1))
        r = int(rng.integers(0, jmax + 1))
        crop_offsets = (t, b, l, r)

    if crop_offsets is not None:
        t, b, l, r = crop_offsets
        h, w = out.shape[:2]
        if h - t - b > 2 and w - l - r > 2:
            out = out[t : h - b, l : w - r]
            out = cv2.resize(out, (w, h), interpolation=cv2.INTER_LINEAR)

    if settings.contrast != 1.0 or settings.brightness_delta != 0.0:
        x = out.astype(np.float32)
        x = (x - 128.0) * settings.contrast + 128.0 + settings.brightness_delta
        out = np.clip(x, 0, 255).astype(np.uint8)

    if settings.saturation_scale != 1.0:
        hsv = cv2.cvtColor(out, cv2.COLOR_BGR2HSV).astype(np.float32)
        hsv[:, :, 1] = np.clip(hsv[:, :, 1] * settings.saturation_scale, 0, 255)
        out = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)

    if settings.noise_sigma > 0:
        rng = _rng(job_id, frame_index, settings.seed_base + 17)
        noise = rng.normal(0, settings.noise_sigma, out.shape).astype(np.float32)
        out = np.clip(out.astype(np.float32) + noise, 0, 255).astype(np.uint8)

    return out


def pick_chunk_crop_offsets(
    job_id: str, chunk_index: int, settings: UniquifySettings
) -> tuple[int, int, int, int] | None:
    jmax = max(0, int(settings.crop_jitter_px))
    if jmax == 0:
        return None
    rng = np.random.default_rng(hash((job_id, settings.seed_base, chunk_index)) & 0xFFFFFFFF)
    t = int(rng.integers(0, jmax + 1))
    b = int(rng.integers(0, jmax + 1))
    l = int(rng.integers(0, jmax + 1))
    r = int(rng.integers(0, jmax + 1))
    return (t, b, l, r)
