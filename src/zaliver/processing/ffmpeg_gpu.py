"""GPU-assisted uniquify: hwaccel decode + GPU scale/color where ffmpeg allows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from zaliver.processing import ffmpeg_vf as vf
from zaliver.processing.ffmpeg_merge import ffmpeg_filters_list_text
from zaliver.processing.pipeline import UniquifySettings
from zaliver.processing.text_overlay import ScaledTextOverlay, build_text_overlay_filters


@dataclass(frozen=True)
class GpuPipeline:
    """How to feed ffmpeg for GPU decode / filters before CPU-only steps."""

    name: str
    global_args: Tuple[str, ...]
    input_args: Tuple[str, ...]
    gpu_eq: bool


def resolve_gpu_pipeline(*, prefer_gpu: bool, encoder: str) -> Optional[GpuPipeline]:
    if not prefer_gpu:
        return None
    enc = str(encoder).strip().lower()
    flt = ffmpeg_filters_list_text().lower()
    if enc == "h264_videotoolbox":
        return GpuPipeline(
            name="videotoolbox",
            global_args=(),
            input_args=("-hwaccel", "videotoolbox"),
            gpu_eq=False,
        )
    if enc == "h264_nvenc" and "scale_cuda" in flt:
        return GpuPipeline(
            name="cuda",
            global_args=("-init_hw_device", "cuda=cu:0", "-filter_hw_device", "cu"),
            input_args=("-hwaccel", "cuda", "-hwaccel_output_format", "cuda"),
            gpu_eq=False,
        )
    if enc == "h264_qsv" and ("scale_qsv" in flt or "vpp_qsv" in flt):
        return GpuPipeline(
            name="qsv",
            global_args=("-init_hw_device", "qsv=hw", "-filter_hw_device", "hw"),
            input_args=("-hwaccel", "qsv", "-hwaccel_output_format", "qsv"),
            gpu_eq="vpp_qsv" in flt,
        )
    if enc == "h264_amf":
        return GpuPipeline(
            name="d3d11va",
            global_args=(),
            input_args=("-hwaccel", "d3d11va", "-hwaccel_output_format", "d3d11"),
            gpu_eq=False,
        )
    return None


def gpu_pipeline_label(pipeline: Optional[GpuPipeline]) -> str:
    if pipeline is None:
        return "CPU (фильтры и декод)"
    labels = {
        "cuda": "NVIDIA CUDA (декод + scale_cuda, eq/noise на CPU)",
        "qsv": "Intel QSV (декод + scale/vpp_qsv, шум на CPU)",
        "d3d11va": "AMD/Windows D3D11VA (декод на GPU, фильтры на CPU)",
        "videotoolbox": "Apple VideoToolbox (HW-декод, фильтры на CPU)",
    }
    return labels.get(pipeline.name, pipeline.name)


def _vpp_qsv_block(settings: UniquifySettings) -> str:
    """Map UniquifySettings to vpp_qsv procamp (0 = neutral)."""
    b = max(-100.0, min(100.0, float(settings.brightness_delta) / 255.0 * 100.0))
    c = max(-100.0, min(100.0, (float(settings.contrast) - 1.0) * 100.0))
    s = max(-100.0, min(100.0, (float(settings.saturation_scale) - 1.0) * 100.0))
    return f"vpp_qsv=brightness={b:.3f}:contrast={c:.3f}:saturation={s:.3f}"


def _scale_pct_cuda(w: int, h: int, scale_pct: float) -> Tuple[str, bool]:
    """GPU scale (+ crop if zoom-in). Returns (filter_chain, needs_cpu_pad)."""
    f = float(scale_pct) / 100.0
    if abs(f - 1.0) < 1e-6:
        return "", False
    nw = max(2, vf._even_dim(int(round(w * f))))
    nh = max(2, vf._even_dim(int(round(h * f))))
    if nw >= w and nh >= h:
        x0 = max(0, (nw - w) // 2)
        y0 = max(0, (nh - h) // 2)
        return f"scale_cuda={nw}:{nh},crop={w}:{h}:{x0}:{y0}", False
    return f"scale_cuda={nw}:{nh}", True


def _scale_pct_qsv(w: int, h: int, scale_pct: float) -> Tuple[str, bool]:
    f = float(scale_pct) / 100.0
    if abs(f - 1.0) < 1e-6:
        return "", False
    nw = max(2, vf._even_dim(int(round(w * f))))
    nh = max(2, vf._even_dim(int(round(h * f))))
    if nw >= w and nh >= h:
        x0 = max(0, (nw - w) // 2)
        y0 = max(0, (nh - h) // 2)
        return f"scale_qsv={nw}:{nh},crop={w}:{h}:{x0}:{y0}", False
    return f"scale_qsv={nw}:{nh}", True


def _cpu_pad_after_zoom(w: int, h: int, scale_pct: float) -> str:
    f = float(scale_pct) / 100.0
    if abs(f - 1.0) < 1e-6:
        return ""
    nw = max(2, vf._even_dim(int(round(w * f))))
    nh = max(2, vf._even_dim(int(round(h * f))))
    if nw >= w and nh >= h:
        return ""
    x0 = max(0, (w - nw) // 2)
    y0 = max(0, (h - nh) // 2)
    return f"pad={w}:{h}:{x0}:{y0}:black"


def _noise_block(settings: UniquifySettings) -> str:
    ns = float(settings.noise_sigma)
    if ns <= 1e-6:
        return ""
    amt = int(min(90, max(1, round(ns * 6.0))))
    return f"noise=alls={amt}:allf=t+u"


def _cpu_tail_after_download(
    *,
    settings: UniquifySettings,
    crop: Optional[Tuple[int, int, int, int]],
    w: int,
    h: int,
    w_out: int,
    h_out: int,
    pipeline: GpuPipeline,
    needs_cpu_pad: bool,
    scale_pct: float,
) -> List[str]:
    tail: List[str] = []
    if needs_cpu_pad:
        pad = _cpu_pad_after_zoom(w, h, scale_pct)
        if pad:
            tail.append(pad)
    cj = vf._crop_jitter_block(w, h, crop)
    if cj:
        tail.append(cj)
    if pipeline.gpu_eq:
        nb = _noise_block(settings)
        if nb:
            tail.append(nb)
    else:
        tail.append(vf._eq_block(settings))
    if pipeline.name not in ("cuda", "qsv"):
        sp = vf._scale_pct_block(w, h, scale_pct)
        if sp:
            tail.insert(0, sp)
    tail.append(f"format=yuv420p,{vf._final_scale_block(w_out, h_out)}")
    return tail


def build_gpu_uniquify_filtergraph(
    *,
    pipeline: GpuPipeline,
    start_frame: int,
    frame_count: int,
    settings: UniquifySettings,
    crop: Optional[Tuple[int, int, int, int]],
    w: int,
    h: int,
    w_out: int,
    h_out: int,
    text_overlay: Optional[ScaledTextOverlay] = None,
    total_frames: int = 0,
    fps: float = 30.0,
) -> str:
    s = int(start_frame)
    fc = int(frame_count)
    e = s + fc
    scale_pct = float(settings.scale_pct)

    gpu_parts: List[str] = [
        f"trim=start_frame={s}:end_frame={e},setpts=PTS-STARTPTS,{vf._normalize_sar_block()}"
    ]
    needs_cpu_pad = False

    if pipeline.name == "cuda":
        sc, needs_cpu_pad = _scale_pct_cuda(w, h, scale_pct)
        if sc:
            gpu_parts.append(sc)
    elif pipeline.name == "qsv":
        sc, needs_cpu_pad = _scale_pct_qsv(w, h, scale_pct)
        if sc:
            gpu_parts.append(sc)
        if pipeline.gpu_eq:
            gpu_parts.append(_vpp_qsv_block(settings))

    cpu_tail = _cpu_tail_after_download(
        settings=settings,
        crop=crop,
        w=w,
        h=h,
        w_out=w_out,
        h_out=h_out,
        pipeline=pipeline,
        needs_cpu_pad=needs_cpu_pad,
        scale_pct=scale_pct,
    )

    chain = (
        [*gpu_parts, *cpu_tail]
        if pipeline.name == "videotoolbox"
        else [*gpu_parts, "hwdownload,format=nv12", *cpu_tail]
    )
    tail_s = ",".join(chain)
    base = f"[0:v]{tail_s}[v0]"
    if text_overlay and text_overlay.lines:
        return (
            f"{base};{build_text_overlay_filters(text_overlay, 'v0', start_frame=s, frame_count=fc, total_frames=int(total_frames), fps=float(fps))}"
        )
    return f"{base};[v0]null[outv]"


def is_gpu_filter_fallback_error(stderr_lines: List[str]) -> bool:
    low = "\n".join(stderr_lines).lower()
    markers = (
        "scale_cuda",
        "scale_qsv",
        "vpp_qsv",
        "hwdownload",
        "hwupload",
        "hwaccel",
        "cuda",
        "qsv",
        "d3d11",
        "videotoolbox",
        "no device available",
        "cannot load",
        "error reinitializing filters",
        "error initializing filter",
        "unsupported pixel format",
        "impossible to convert",
        "generic error in an external library",
    )
    return any(m in low for m in markers)
