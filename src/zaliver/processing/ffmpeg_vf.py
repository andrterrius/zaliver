"""Build ffmpeg filter graphs for uniquification (no OpenCV)."""

from __future__ import annotations

from typing import Optional, Tuple

from zaliver.processing.pipeline import UniquifySettings


def _even_dim(x: int) -> int:
    return max(2, int(x) - (int(x) % 2))


def _scale_pct_block(w: int, h: int, scale_pct: float) -> str:
    f = float(scale_pct) / 100.0
    if abs(f - 1.0) < 1e-6:
        return ""
    nw = max(2, _even_dim(int(round(w * f))))
    nh = max(2, _even_dim(int(round(h * f))))
    sc = f"scale={nw}:{nh}:flags=bilinear"
    if nw >= w and nh >= h:
        x0 = max(0, (nw - w) // 2)
        y0 = max(0, (nh - h) // 2)
        return f"{sc},crop={w}:{h}:{x0}:{y0}"
    x0 = max(0, (w - nw) // 2)
    y0 = max(0, (h - nh) // 2)
    return f"{sc},pad={w}:{h}:{x0}:{y0}:black"


def _crop_jitter_block(
    w: int, h: int, crop: Optional[Tuple[int, int, int, int]]
) -> str:
    if crop is None:
        return ""
    t, b, l, r = (int(crop[0]), int(crop[1]), int(crop[2]), int(crop[3]))
    iw = w - l - r
    ih = h - t - b
    if iw <= 2 or ih <= 2:
        return ""
    return f"crop={iw}:{ih}:{l}:{t},scale={w}:{h}:flags=bilinear"


def _eq_block(settings: UniquifySettings) -> str:
    c = float(settings.contrast)
    b = max(-1.0, min(1.0, float(settings.brightness_delta) / 255.0))
    sat = float(settings.saturation_scale)
    parts = [f"eq=contrast={c:.6f}:brightness={b:.6f}:saturation={sat:.6f}"]
    ns = float(settings.noise_sigma)
    if ns > 1e-6:
        amt = int(min(90, max(1, round(ns * 6.0))))
        parts.append(f"noise=alls={amt}:allf=t+u")
    return ",".join(parts)


def build_uniquify_filtergraph(
    *,
    start_frame: int,
    frame_count: int,
    settings: UniquifySettings,
    crop: Optional[Tuple[int, int, int, int]],
    w: int,
    h: int,
    w_out: int,
    h_out: int,
) -> str:
    """
    Full -filter_complex graph: one video input [0:v] -> uniquified [outv].
    """
    s = int(start_frame)
    fc = int(frame_count)
    e = s + fc
    head = f"trim=start_frame={s}:end_frame={e},setpts=PTS-STARTPTS"

    tail: list[str] = []
    sp = _scale_pct_block(w, h, float(settings.scale_pct))
    if sp:
        tail.append(sp)
    cj = _crop_jitter_block(w, h, crop)
    if cj:
        tail.append(cj)
    tail.append(_eq_block(settings))
    tail.append(f"format=yuv420p,scale={w_out}:{h_out}:flags=bilinear")
    tail_s = ",".join(tail)

    return f"[0:v]{head},{tail_s}[outv]"
