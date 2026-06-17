"""Apply neon text overlay to a finished slice video."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Optional

from zaliver.processing.ffmpeg_merge import pick_best_h264_encoder, run_ffmpeg
from zaliver.processing.slicing import (
    SLICE_ENCODE_CRF,
    SLICE_ENCODE_GPU_CQ,
    SLICE_ENCODE_VIDEOTOOLBOX_Q,
)
from zaliver.processing.ffmpeg_probe import probe_media_duration_seconds, probe_video_stream
from zaliver.processing.text_overlay import ScaledTextOverlay, build_text_overlay_filters
from zaliver.processing.worker import _filter_complex_argv

LogCallback = Callable[[str], None]


def apply_text_overlay_to_video(
    input_path: str,
    output_path: str,
    overlay: ScaledTextOverlay,
    *,
    log: Optional[LogCallback] = None,
    prefer_gpu: bool = False,
) -> None:
    """Re-encode video with drawtext overlay; audio copied from input."""
    _w, _h, fps, frame_count, _fourcc = probe_video_stream(input_path)
    if frame_count <= 0:
        raise RuntimeError(f"Не удалось определить число кадров: {input_path}")
    fc = int(frame_count)
    duration_sec = probe_media_duration_seconds(input_path)

    overlay_part = build_text_overlay_filters(
        overlay,
        "v0",
        start_frame=0,
        frame_count=fc,
        total_frames=fc,
        fps=float(fps),
        total_duration_sec=duration_sec,
    )
    if "drawtext" not in overlay_part:
        msg = "Текст на видео: макет пуст или все кадры пропущены — сохраняем без текста."
        if log:
            log(msg)
        shutil.copy2(input_path, output_path)
        return

    # Как во вкладке уникализации: отдельная метка v0, не [0:v] напрямую в цепочке drawtext.
    graph = f"[0:v]null[v0];{overlay_part}"

    out_p = Path(output_path)
    tmp = out_p.with_name(f"{out_p.stem}._zaliver_overlay{out_p.suffix}")
    enc, enc_args = pick_best_h264_encoder(
        prefer_gpu=bool(prefer_gpu),
        crf=SLICE_ENCODE_CRF,
        gpu_cq=SLICE_ENCODE_GPU_CQ,
        videotoolbox_q=SLICE_ENCODE_VIDEOTOOLBOX_Q,
    )
    filter_script: Path | None = None
    try:
        filter_argv, filter_script = _filter_complex_argv(graph)
        run_ffmpeg(
            [
                "-i",
                input_path,
                *filter_argv,
                "-map",
                "[outv]",
                "-map",
                "0:a?",
                "-c:v",
                enc,
                *enc_args,
                "-pix_fmt",
                "yuv420p",
                "-r",
                f"{fps:.6f}",
                "-c:a",
                "copy",
                "-movflags",
                "+faststart",
                str(tmp),
            ],
            log=log,
        )
        os.replace(str(tmp), str(out_p))
    except Exception:
        try:
            if tmp.is_file():
                tmp.unlink()
        except OSError:
            pass
        raise
    finally:
        if filter_script is not None:
            try:
                filter_script.unlink(missing_ok=True)
            except OSError:
                pass
