"""Stateless uniquification settings (effects applied via ffmpeg, not OpenCV)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, Dict

import numpy as np


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
    audio_speed_factor: float = 1.0  # 1.00..1.10 typical
    audio_chorus: bool = False

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
        audio_speed_factor=float(r.uniform(1.0, 1.1)),
        audio_chorus=bool(r.random() < 0.45),
    )


def pick_chunk_crop_offsets(
    job_id: str, chunk_index: int, settings: UniquifySettings
) -> tuple[int, int, int, int] | None:
    jmax = max(0, int(settings.crop_jitter_px))
    if jmax == 0:
        return None
    rng = np.random.default_rng(
        hash((job_id, settings.seed_base, chunk_index)) & 0xFFFFFFFF
    )
    t = int(rng.integers(0, jmax + 1))
    b = int(rng.integers(0, jmax + 1))
    l = int(rng.integers(0, jmax + 1))
    r = int(rng.integers(0, jmax + 1))
    return (t, b, l, r)
