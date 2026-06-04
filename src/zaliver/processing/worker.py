"""Process pool workers: ffmpeg-only trim + filters + encode."""

from __future__ import annotations

import multiprocessing
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
    pick_best_h264_encoder,
    resolve_ffmpeg_executable,
)
from zaliver.processing.ffmpeg_vf import build_uniquify_filtergraph
from zaliver.processing.pipeline import UniquifySettings, pick_chunk_crop_offsets
from zaliver.processing.text_overlay import ScaledTextOverlay

_progress_queue: Optional[multiprocessing.Queue] = None
_cancel_event: Optional[multiprocessing.synchronize.Event] = None

_FRAME_RE = re.compile(r"frame=\s*(\d+)")
_MAX_FFMPEG_STDERR_LINES = 48


def _ffmpeg_error_message(code: int, stderr_lines: list[str]) -> str:
    err_lines = [ln for ln in stderr_lines if ln and not _FRAME_RE.search(ln)]
    if not err_lines:
        err_lines = [ln for ln in stderr_lines if ln][-8:]
    detail = "\n".join(err_lines[-12:]).strip()
    if detail:
        if len(detail) > 700:
            detail = detail[-700:]
        return f"ffmpeg exited with code {code}: {detail}"
    return f"ffmpeg exited with code {code}"


def init_worker(
    progress_queue: multiprocessing.Queue,
    cancel_event: multiprocessing.synchronize.Event,
) -> None:
    global _progress_queue, _cancel_event
    raise_fd_limit_soft()
    _progress_queue = progress_queue
    _cancel_event = cancel_event


def _report(job_id: str, chunk_index: int, done: int, total: int) -> None:
    if _progress_queue is not None:
        _progress_queue.put((job_id, chunk_index, done, total))


def _cancelled() -> bool:
    return _cancel_event is not None and _cancel_event.is_set()


def _popen_flags() -> int:
    if sys.platform == "win32":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def _filter_complex_argv(graph: str) -> tuple[list[str], Path | None]:
    """Windows command line is ~32K; long neon drawtext graphs need a script file."""
    use_script = sys.platform == "win32" or len(graph) > 7000
    if not use_script:
        return ["-filter_complex", graph], None
    fd, name = tempfile.mkstemp(suffix=".txt", prefix="zv_fc_")
    os.close(fd)
    script = Path(name)
    script.write_text(graph, encoding="utf-8")
    return ["-filter_complex_script", str(script)], script


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
    graph = build_uniquify_filtergraph(
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
    )
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

    filter_argv, filter_script = _filter_complex_argv(graph)
    cmd = [
        exe,
        "-hide_banner",
        "-loglevel",
        "info",
        "-stats",
        "-y",
        "-i",
        path,
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
        enc,
        *enc_args,
        part_path,
    ]

    committed = False
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
        if _cancelled():
            return {"ok": False, "chunk_index": chunk_index, "error": "cancelled"}
        if code != 0:
            return {
                "ok": False,
                "chunk_index": chunk_index,
                "error": _ffmpeg_error_message(code, stderr_lines),
            }
        _report(job_id, chunk_index, count, count)
        try:
            os.replace(part_path, out_path)
        except OSError as e:
            return {"ok": False, "chunk_index": chunk_index, "error": f"rename: {e}"}
        committed = True
        return {"ok": True, "chunk_index": chunk_index, "error": None}
    except subprocess.TimeoutExpired:
        if proc is not None:
            try:
                proc.kill()
            except OSError:
                pass
        return {"ok": False, "chunk_index": chunk_index, "error": "ffmpeg timeout"}
    except Exception as e:
        return {"ok": False, "chunk_index": chunk_index, "error": str(e)}
    finally:
        if filter_script is not None:
            try:
                filter_script.unlink(missing_ok=True)
            except OSError:
                pass
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        if not committed:
            try:
                Path(part_path).unlink(missing_ok=True)
            except OSError:
                pass
