"""Process pool workers: ffmpeg-only trim + filters + encode."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from zaliver.processing.fd_limit import raise_fd_limit_soft
from zaliver.processing.ffmpeg_merge import (
    libx264_encode_args_for_target,
    pick_best_h264_encoder,
    resolve_ffmpeg_executable,
)
from zaliver.processing.ffmpeg_gpu import (
    build_gpu_uniquify_filtergraph,
    is_gpu_encoder_oom_error,
    is_gpu_filter_fallback_error,
    resolve_gpu_pipeline,
)
from zaliver.processing.ffmpeg_vf import build_uniquify_filtergraph
from zaliver.processing.pipeline import UniquifySettings, pick_chunk_crop_offsets
from zaliver.processing.text_overlay import ScaledTextOverlay

# Per-worker (process or thread) progress/cancel — thread-local so concurrent
# ThreadPool jobs in the API process do not clobber each other.
_tls = threading.local()
# ProcessPool fallback (one worker thread per process).
_progress_queue: Any = None
_cancel_event: Any = None

_FRAME_RE = re.compile(r"frame=\s*(\d+)")
_MAX_FFMPEG_STDERR_LINES = 48


def _ffmpeg_error_message(code: int, stderr_lines: list[str]) -> str:
    err_lines = [ln for ln in stderr_lines if ln and not _FRAME_RE.search(ln)]
    if not err_lines:
        err_lines = [ln for ln in stderr_lines if ln][-8:]
    detail = "\n".join(err_lines[-12:]).strip()
    if detail:
        low = detail.lower()
        if "no such filter" in low and "drawtext" in low:
            from zaliver.processing.ffmpeg_merge import ffmpeg_drawtext_missing_user_message

            return ffmpeg_drawtext_missing_user_message()
        if len(detail) > 700:
            detail = detail[-700:]
        return f"ffmpeg exited with code {code}: {detail}"
    return f"ffmpeg exited with code {code}"


def init_worker(progress_queue: Any, cancel_event: Any) -> None:
    global _progress_queue, _cancel_event
    raise_fd_limit_soft()
    _tls.progress_queue = progress_queue
    _tls.cancel_event = cancel_event
    _progress_queue = progress_queue
    _cancel_event = cancel_event


def _report(job_id: str, chunk_index: int, done: int, total: int) -> None:
    q = getattr(_tls, "progress_queue", None) or _progress_queue
    if q is not None:
        q.put((job_id, chunk_index, done, total))


def _cancelled() -> bool:
    ev = getattr(_tls, "cancel_event", None) or _cancel_event
    return ev is not None and ev.is_set()


def _popen_flags() -> int:
    from zaliver.processing.subprocess_flags import popen_creationflags

    return popen_creationflags()


_ffmpeg_major_by_exe: Dict[str, int] = {}


def _ffmpeg_major_version() -> int:
    """Major version of the resolved ffmpeg binary (0 if unknown)."""
    exe = resolve_ffmpeg_executable() or "ffmpeg"
    cached = _ffmpeg_major_by_exe.get(exe)
    if cached is not None:
        return cached
    major = 0
    try:
        out = subprocess.run(
            [exe, "-version"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=_popen_flags(),
        )
        m = re.search(r"ffmpeg version\s+n?(\d+)", out.stdout or "", re.I)
        if m:
            major = int(m.group(1))
    except (OSError, subprocess.SubprocessError, ValueError):
        major = 0
    _ffmpeg_major_by_exe[exe] = major
    return major


def _filter_complex_argv(graph: str) -> tuple[list[str], Path | None]:
    """Windows command line is ~32K; long neon drawtext graphs need a script file."""
    use_script = sys.platform == "win32" or len(graph) > 7000
    if not use_script:
        return ["-filter_complex", graph], None
    fd, name = tempfile.mkstemp(suffix=".txt", prefix="zv_fc_")
    os.close(fd)
    script = Path(name)
    script.write_text(graph, encoding="utf-8")
    # FFmpeg 8+: -/filter_complex. Older (Ubuntu apt 4–7): -filter_complex_script.
    if _ffmpeg_major_version() >= 8:
        return ["-/filter_complex", str(script)], script
    return ["-filter_complex_script", str(script)], script


def _run_ffmpeg_cmd(
    cmd: list[str],
    *,
    job_id: str,
    chunk_index: int,
    count: int,
) -> tuple[int, list[str]]:
    proc: Optional[subprocess.Popen] = None
    done_holder = [0]
    stderr_lines: list[str] = []

    def _stderr_reader(p: subprocess.Popen) -> None:
        if p.stderr is None:
            return
        for line in iter(p.stderr.readline, ""):
            if _cancelled():
                break
            if not line:
                break
            stderr_lines.append(line.rstrip("\n\r"))
            if len(stderr_lines) > _MAX_FFMPEG_STDERR_LINES:
                del stderr_lines[: len(stderr_lines) - _MAX_FFMPEG_STDERR_LINES]
            m = _FRAME_RE.search(line)
            if not m:
                continue
            fr = min(int(m.group(1)), count)
            if fr > done_holder[0]:
                done_holder[0] = fr
                _report(job_id, chunk_index, fr, count)

    try:
        proc = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_popen_flags(),
        )
        t = threading.Thread(target=_stderr_reader, args=(proc,), daemon=True)
        t.start()
        code = int(proc.wait(timeout=7200) or 0)
        t.join(timeout=5.0)
        if proc.stderr is not None:
            try:
                tail = proc.stderr.read()
                if tail:
                    for line in tail.splitlines():
                        stderr_lines.append(line.rstrip("\n\r"))
                    if len(stderr_lines) > _MAX_FFMPEG_STDERR_LINES:
                        stderr_lines[:] = stderr_lines[-_MAX_FFMPEG_STDERR_LINES :]
                proc.stderr.close()
            except OSError:
                pass
        return code, stderr_lines
    except subprocess.TimeoutExpired:
        if proc is not None:
            try:
                proc.kill()
            except OSError:
                pass
        raise
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass


def process_chunk_disk(task: Dict[str, Any]) -> Dict[str, Any]:
    """Trim + uniquify filter graph + encode (single ffmpeg child)."""
    path = str(task["video_path"])
    start = int(task["start_frame"])
    count = int(task["frame_count"])
    chunk_index = int(task["chunk_index"])
    job_id = str(task["job_id"])
    settings = UniquifySettings.from_dict(task["settings"])
    w = int(task["width"])
    h = int(task["height"])
    fps = float(task["fps"])
    use_gpu = bool(task.get("use_gpu", False))
    w_out = max(2, w - (w % 2))
    h_out = max(2, h - (h % 2))

    out_p = Path(task["output_path"]).expanduser()
    try:
        out_p = out_p.resolve()
    except OSError:
        pass
    try:
        out_p.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"ok": False, "chunk_index": chunk_index, "error": f"mkdir: {e}"}
    out_path = str(out_p)
    part_p = out_p.with_name(f"{out_p.stem}._zaliver_tmp{out_p.suffix}")
    part_path = str(part_p)
    try:
        part_p.unlink(missing_ok=True)
    except OSError:
        pass

    exe = resolve_ffmpeg_executable()
    if not exe:
        return {"ok": False, "chunk_index": chunk_index, "error": "ffmpeg not found"}

    crop = pick_chunk_crop_offsets(job_id, chunk_index, settings)
    raw_overlay = task.get("text_overlay")
    text_overlay: Optional[ScaledTextOverlay] = None
    if isinstance(raw_overlay, dict) and raw_overlay.get("lines"):
        text_overlay = ScaledTextOverlay.from_dict(raw_overlay)
    try:
        total_frames = int(task.get("total_frames") or 0)
    except (TypeError, ValueError):
        total_frames = 0
    if total_frames <= 0:
        total_frames = count
    tb = task.get("target_video_bps")
    tb_i: Optional[int]
    if tb is None:
        tb_i = None
    else:
        try:
            tb_i = int(tb)
        except (TypeError, ValueError):
            tb_i = None
    if tb_i is not None and tb_i <= 0:
        tb_i = None
    enc, enc_args = pick_best_h264_encoder(
        prefer_gpu=use_gpu, target_video_bps=tb_i
    )

    graph_common = dict(
        start_frame=start,
        frame_count=count,
        settings=settings,
        crop=crop,
        w=w,
        h=h,
        w_out=w_out,
        h_out=h_out,
        text_overlay=text_overlay,
        total_frames=total_frames,
        fps=float(fps),
    )

    attempts: list[tuple[str, Optional[object], list[str], str, list[str]]] = []
    if use_gpu:
        gpu_pipeline = resolve_gpu_pipeline(prefer_gpu=True, encoder=enc)
        if gpu_pipeline is not None:
            attempts.append(
                (
                    "gpu",
                    gpu_pipeline,
                    [
                        *gpu_pipeline.global_args,
                        *gpu_pipeline.input_args,
                    ],
                    enc,
                    list(enc_args),
                )
            )
    attempts.append(("cpu", None, [], enc, list(enc_args)))
    if use_gpu and enc != "libx264":
        # Last resort if GPU encoder OOMs (common with AMF + heavy overlays).
        attempts.append(
            ("cpu", None, [], "libx264", libx264_encode_args_for_target(tb_i))
        )

    filter_script: Path | None = None
    last_stderr: list[str] = []
    code = 1

    try:
        for mode, pipeline, hw_args, enc_name, enc_name_args in attempts:
            if mode == "gpu" and pipeline is not None:
                graph, emoji_argv = build_gpu_uniquify_filtergraph(
                    pipeline=pipeline,  # type: ignore[arg-type]
                    **graph_common,
                )
            else:
                graph, emoji_argv = build_uniquify_filtergraph(**graph_common)

            if filter_script is not None:
                try:
                    filter_script.unlink(missing_ok=True)
                except OSError:
                    pass
                filter_script = None

            filter_argv, filter_script = _filter_complex_argv(graph)
            cmd = [
                exe,
                "-hide_banner",
                "-loglevel",
                "info",
                "-stats",
                "-y",
                *hw_args,
                "-i",
                path,
                *emoji_argv,
                *filter_argv,
                "-map",
                "[outv]",
                "-an",
                "-r",
                f"{fps:.6f}",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-c:v",
                enc_name,
                *enc_name_args,
            ]
            if emoji_argv:
                cmd.append("-shortest")
            cmd.append(part_path)

            try:
                code, last_stderr = _run_ffmpeg_cmd(
                    cmd,
                    job_id=job_id,
                    chunk_index=chunk_index,
                    count=count,
                )
            except subprocess.TimeoutExpired:
                return {"ok": False, "chunk_index": chunk_index, "error": "ffmpeg timeout"}

            if code == 0:
                break
            if use_gpu and mode == "gpu" and is_gpu_filter_fallback_error(last_stderr):
                continue
            if (
                use_gpu
                and enc_name != "libx264"
                and is_gpu_encoder_oom_error(last_stderr)
            ):
                continue
            break

        if _cancelled():
            return {"ok": False, "chunk_index": chunk_index, "error": "cancelled"}
        if code != 0:
            return {
                "ok": False,
                "chunk_index": chunk_index,
                "error": _ffmpeg_error_message(code, last_stderr),
            }
        _report(job_id, chunk_index, count, count)
        try:
            os.replace(part_path, out_path)
        except OSError as e:
            return {"ok": False, "chunk_index": chunk_index, "error": f"rename: {e}"}
        return {"ok": True, "chunk_index": chunk_index, "error": None}
    except subprocess.TimeoutExpired:
        return {"ok": False, "chunk_index": chunk_index, "error": "ffmpeg timeout"}
    except Exception as e:
        return {"ok": False, "chunk_index": chunk_index, "error": str(e)}
    finally:
        if filter_script is not None:
            try:
                filter_script.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            if not Path(out_path).is_file():
                Path(part_path).unlink(missing_ok=True)
        except OSError:
            pass
