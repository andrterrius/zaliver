"""Stateless uniquification settings (effects applied via ffmpeg, not OpenCV)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, Dict, Optional, Tuple

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
    playback_speed_factor: float = 1.0  # 1.0 = без изменений; >1 быстрее видео+аудио
    audio_chorus: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "UniquifySettings":
        allowed = {f.name for f in fields(UniquifySettings)}
        return UniquifySettings(**{k: v for k, v in d.items() if k in allowed})


def _ordered_pair_f(lo: float, hi: float) -> Tuple[float, float]:
    if lo > hi:
        return hi, lo
    return lo, hi


def _ordered_pair_i(lo: int, hi: int) -> Tuple[int, int]:
    if lo > hi:
        return hi, lo
    return lo, hi


def sample_range_value(
    lo: float,
    hi: float,
    *,
    integer: bool = False,
    rng: Optional[np.random.Generator] = None,
) -> float:
    """Случайное значение в [lo, hi]; при lo==hi возвращает точное значение."""
    a, b = _ordered_pair_f(float(lo), float(hi))
    r = rng or np.random.default_rng()
    if integer:
        ia, ib = int(round(a)), int(round(b))
        if ib < ia:
            ia, ib = ib, ia
        return float(int(r.integers(ia, ib + 1)))
    if abs(b - a) < 1e-12:
        return float(a)
    return float(r.uniform(a, b))


def materialize_text_overlay_ranges(
    raw: Optional[Dict[str, Any]],
    *,
    rng: Optional[np.random.Generator] = None,
) -> Optional[Dict[str, Any]]:
    """Выбрать wave amp/speed из min/max (если заданы) для одного ролика."""
    if not isinstance(raw, dict):
        return None
    d = dict(raw)
    r = rng or np.random.default_rng()
    amp_lo = d.pop("wave_amp_frac_min", None)
    amp_hi = d.pop("wave_amp_frac_max", None)
    spd_lo = d.pop("wave_frame_speed_min", None)
    spd_hi = d.pop("wave_frame_speed_max", None)
    if amp_lo is not None or amp_hi is not None:
        base = float(d.get("wave_amp_frac", 0.0) or 0.0)
        d["wave_amp_frac"] = sample_range_value(
            float(base if amp_lo is None else amp_lo),
            float(base if amp_hi is None else amp_hi),
            rng=r,
        )
    if spd_lo is not None or spd_hi is not None:
        base = float(d.get("wave_frame_speed", 0.0) or 0.0)
        d["wave_frame_speed"] = sample_range_value(
            float(base if spd_lo is None else spd_lo),
            float(base if spd_hi is None else spd_hi),
            rng=r,
        )
    # Color emoji PNGs: render/download on the orchestrator process (has Qt on macOS).
    text = str(d.get("text") or "")
    if text.strip():
        try:
            from zaliver.processing.text_overlay import prefetch_color_emoji_for_text

            prefetch_color_emoji_for_text(text)
        except Exception:
            pass
    return d


@dataclass
class RandomUniquifyBounds:
    """Границы для случайной уникализации (min/max)."""

    brightness_min: float = -22.0
    brightness_max: float = 22.0
    contrast_min: float = 0.88
    contrast_max: float = 1.14
    saturation_min: float = 0.88
    saturation_max: float = 1.12
    crop_jitter_min: int = 0
    crop_jitter_max: int = 3  # включительно (как раньше integers(0, 4))
    scale_pct_min: float = 99.4
    scale_pct_max: float = 100.6
    noise_sigma_min: float = 0.15
    noise_sigma_max: float = 4.0
    seed_min: int = 0
    seed_max: int = 99_999_999  # включительно
    playback_speed_min: float = 1.0
    playback_speed_max: float = 1.1
    audio_chorus_prob: float = 0.45
    audio_chorus_prob_min: float = 0.45
    audio_chorus_prob_max: float = 0.45

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def normalized(self) -> "RandomUniquifyBounds":
        """Поджимает экстремальные значения, чтобы ffmpeg/numpy не ломались."""
        plo, phi = _ordered_pair_f(self.playback_speed_min, self.playback_speed_max)
        plo = max(0.25, float(plo))
        phi = max(plo, min(4.0, float(phi)))
        ch_lo, ch_hi = _ordered_pair_f(
            float(self.audio_chorus_prob_min), float(self.audio_chorus_prob_max)
        )
        ch_lo = max(0.0, min(1.0, ch_lo))
        ch_hi = max(ch_lo, min(1.0, ch_hi))
        ch = max(0.0, min(1.0, float(self.audio_chorus_prob)))
        jlo, jhi = _ordered_pair_i(int(self.crop_jitter_min), int(self.crop_jitter_max))
        jlo = max(0, jlo)
        jhi = max(jlo, min(jhi, 1_000_000))
        zlo, zhi = _ordered_pair_i(int(self.seed_min), int(self.seed_max))
        zlo = max(0, zlo)
        zhi = min(max(zlo, zhi), 2_147_483_646)
        blo, bhi = _ordered_pair_f(self.brightness_min, self.brightness_max)
        clo, chi = _ordered_pair_f(self.contrast_min, self.contrast_max)
        slo, shi = _ordered_pair_f(self.saturation_min, self.saturation_max)
        xlo, xhi = _ordered_pair_f(self.scale_pct_min, self.scale_pct_max)
        nlo, nhi = _ordered_pair_f(self.noise_sigma_min, self.noise_sigma_max)
        return RandomUniquifyBounds(
            brightness_min=blo,
            brightness_max=bhi,
            contrast_min=clo,
            contrast_max=chi,
            saturation_min=slo,
            saturation_max=shi,
            crop_jitter_min=jlo,
            crop_jitter_max=jhi,
            scale_pct_min=xlo,
            scale_pct_max=xhi,
            noise_sigma_min=nlo,
            noise_sigma_max=nhi,
            seed_min=zlo,
            seed_max=zhi,
            playback_speed_min=plo,
            playback_speed_max=phi,
            audio_chorus_prob=ch,
            audio_chorus_prob_min=ch_lo,
            audio_chorus_prob_max=ch_hi,
        )

    @classmethod
    def from_options_dict(cls, d: Optional[Dict[str, Any]]) -> "RandomUniquifyBounds":
        base = cls()
        if not d:
            return base.normalized()
        names = {f.name for f in fields(cls)}
        merged = {**asdict(base), **{k: v for k, v in d.items() if k in names}}
        # Старые сохранения: одно число вероятности хора → обе границы.
        if "audio_chorus_prob" in (d or {}) and (
            "audio_chorus_prob_min" not in (d or {})
            and "audio_chorus_prob_max" not in (d or {})
        ):
            ch = float(d["audio_chorus_prob"])  # type: ignore[index]
            merged["audio_chorus_prob_min"] = ch
            merged["audio_chorus_prob_max"] = ch
        return cls(**merged).normalized()  # type: ignore[arg-type]


def neutral_uniquify_settings() -> UniquifySettings:
    """Без визуальных/аудио-эффектов уникализации (трек и текст — отдельно)."""
    return UniquifySettings()


# Флаги «эффект включён» в options → поле UniquifySettings и нейтральное значение.
UNIQUIFY_EFFECT_ENABLE_FIELDS: tuple[tuple[str, str, Any], ...] = (
    ("brightness_enabled", "brightness_delta", 0.0),
    ("contrast_enabled", "contrast", 1.0),
    ("saturation_enabled", "saturation_scale", 1.0),
    ("crop_jitter_enabled", "crop_jitter_px", 0),
    ("scale_enabled", "scale_pct", 100.0),
    ("noise_enabled", "noise_sigma", 0.0),
    ("seed_enabled", "seed_base", 0),
    ("playback_speed_enabled", "playback_speed_factor", 1.0),
    ("audio_chorus_enabled", "audio_chorus", False),
)


def apply_uniquify_effect_enables(
    settings: Dict[str, Any],
    options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Обнуляет эффекты, для которых в options стоит enabled=False (по умолчанию вкл.)."""
    out = dict(settings or {})
    opts = options or {}
    for flag, field, neutral in UNIQUIFY_EFFECT_ENABLE_FIELDS:
        # Совместимость: раньше audio_speed_enabled = playback_speed_enabled.
        if flag == "playback_speed_enabled":
            enabled = bool(
                opts.get(
                    "playback_speed_enabled",
                    opts.get("audio_speed_enabled", True),
                )
            )
        else:
            enabled = bool(opts.get(flag, True))
        if not enabled:
            out[field] = neutral
    return out


def random_uniquify_settings(
    bounds: Optional[RandomUniquifyBounds] = None,
) -> UniquifySettings:
    """Новый набор лёгких эффектов для каждого запуска / каждого файла."""
    b = bounds or RandomUniquifyBounds()
    r = np.random.default_rng()
    blo, bhi = _ordered_pair_f(b.brightness_min, b.brightness_max)
    clo, chi = _ordered_pair_f(b.contrast_min, b.contrast_max)
    slo, shi = _ordered_pair_f(b.saturation_min, b.saturation_max)
    jlo, jhi = _ordered_pair_i(int(b.crop_jitter_min), int(b.crop_jitter_max))
    jlo = max(0, jlo)
    jhi = max(jlo, jhi)
    xlo, xhi = _ordered_pair_f(b.scale_pct_min, b.scale_pct_max)
    nlo, nhi = _ordered_pair_f(b.noise_sigma_min, b.noise_sigma_max)
    zlo, zhi = _ordered_pair_i(int(b.seed_min), int(b.seed_max))
    plo, phi = _ordered_pair_f(b.playback_speed_min, b.playback_speed_max)
    ch_lo, ch_hi = _ordered_pair_f(
        float(getattr(b, "audio_chorus_prob_min", b.audio_chorus_prob)),
        float(getattr(b, "audio_chorus_prob_max", b.audio_chorus_prob)),
    )
    ch_p = max(0.0, min(1.0, float(sample_range_value(ch_lo, ch_hi, rng=r))))
    return UniquifySettings(
        brightness_delta=float(r.uniform(blo, bhi)),
        contrast=float(r.uniform(clo, chi)),
        saturation_scale=float(r.uniform(slo, shi)),
        crop_jitter_px=int(r.integers(jlo, jhi + 1)),
        scale_pct=float(r.uniform(xlo, xhi)),
        noise_sigma=float(r.uniform(nlo, nhi)),
        seed_base=int(r.integers(zlo, zhi + 1)),
        playback_speed_factor=float(r.uniform(plo, phi)),
        audio_chorus=bool(r.random() < ch_p),
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
