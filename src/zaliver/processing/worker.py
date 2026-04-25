"""Process pool workers: ffmpeg-only trim + filters + encode."""

from __future__ import annotations

import multiprocessing
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from zaliver.processing.ffmpeg_merge import (
    pick_best_h264_encoder,
    resolve_ffmpeg_executable,
)
from zaliver.processing.ffmpeg_vf import build_uniquify_filtergraph
from zaliver.processing.pipeline import UniquifySettings, pick_chunk_crop_offsets

_progress_queue: Optional[multiprocessing.Queue] = None
_cancel_event: Optional[multiprocessing.synchronize.Event] = None

_FRAME_RE = re.compile(r"frame=\s*(\d+)")


def init_worker(
    progress_queue: multiprocessing.Queue,
    cancel_event: multiprocessing.synchronize.Event,
) -> None:
    global _progress_queue, _cancel_event
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
    graph = build_uniquify_filtergraph(
        start_frame=start,
        frame_count=count,
        settings=settings,
        crop=crop,
        color_grade=task.get("color_grade"),
        w=w,
        h=h,
        w_out=w_out,
        h_out=h_out,
    )
    enc, enc_args = pick_best_h264_encoder(prefer_gpu=use_gpu)

    cmd = [
        exe,
        "-hide_banner",
        "-loglevel",
        "info",
        "-stats",
        "-y",
        "-i",
        path,
        "-filter_complex",
        graph,
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

    def _stderr_reader(p: subprocess.Popen) -> None:
        if p.stderr is None:
            return
        for line in iter(p.stderr.readline, ""):
            if _cancelled():
                break
            if not line:
                break
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
        try:
            if proc.stderr is not None:
                proc.stderr.close()
        except OSError:
            pass
        t.join(timeout=2.0)
        if _cancelled():
            return {"ok": False, "chunk_index": chunk_index, "error": "cancelled"}
        if code != 0:
            return {
                "ok": False,
                "chunk_index": chunk_index,
                "error": f"ffmpeg exited with code {code}",
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
