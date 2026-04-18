"""Process pool workers: encode video segments (disk or shared-memory input)."""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import cv2
import numpy as np

from zaliver.processing.pipeline import (
    UniquifySettings,
    apply_frame,
    pick_chunk_crop_offsets,
)
from zaliver.processing.shm_buffers import attach_shm_numpy, close_shm

_progress_queue: Optional[multiprocessing.Queue] = None
_cancel_event: Optional[multiprocessing.synchronize.Event] = None


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


def process_chunk_disk(task: Dict[str, Any]) -> Dict[str, Any]:
    """Read chunk from file, write processed video to task['output_path'] (video only, mp4v)."""
    path = str(task["video_path"])
    start = int(task["start_frame"])
    count = int(task["frame_count"])
    chunk_index = int(task["chunk_index"])
    job_id = str(task["job_id"])
    settings = UniquifySettings.from_dict(task["settings"])
    w = int(task["width"])
    h = int(task["height"])
    fps = float(task["fps"])
    # Many MP4 backends require even width/height.
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
    # Temp file must end in .mp4 — OpenCV often refuses ".part" / unknown suffix.
    part_p = out_p.with_name(f"{out_p.stem}._zaliver_tmp{out_p.suffix}")
    part_path = str(part_p)
    try:
        part_p.unlink(missing_ok=True)
    except OSError:
        pass

    crop = pick_chunk_crop_offsets(job_id, chunk_index, settings)

    cap = cv2.VideoCapture(path)
    writer: cv2.VideoWriter | None = None
    committed = False
    try:
        if not cap.isOpened():
            return {"ok": False, "chunk_index": chunk_index, "error": "open failed"}
        cap.set(cv2.CAP_PROP_POS_FRAMES, start)
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(part_path, fourcc, fps, (w_out, h_out))
        if not writer.isOpened():
            return {
                "ok": False,
                "chunk_index": chunk_index,
                "error": f"writer failed ({part_path}, {w_out}x{h_out})",
            }

        for i in range(count):
            if _cancelled():
                return {"ok": False, "chunk_index": chunk_index, "error": "cancelled"}
            ok, frame = cap.read()
            if not ok:
                break
            global_idx = start + i
            proc = apply_frame(
                frame,
                global_idx,
                job_id,
                settings,
                crop_offsets=crop,
                color_grade_params=task.get("color_grade"),
            )
            if proc.shape[0] != h or proc.shape[1] != w:
                proc = cv2.resize(proc, (w, h), interpolation=cv2.INTER_LINEAR)
            if proc.shape[1] != w_out or proc.shape[0] != h_out:
                proc = cv2.resize(proc, (w_out, h_out), interpolation=cv2.INTER_LINEAR)
            writer.write(proc)
            _report(job_id, chunk_index, i + 1, count)
        writer.release()
        writer = None
        if _cancelled():
            return {"ok": False, "chunk_index": chunk_index, "error": "cancelled"}
        try:
            os.replace(part_path, out_path)
        except OSError as e:
            return {"ok": False, "chunk_index": chunk_index, "error": f"rename: {e}"}
        committed = True
        return {"ok": True, "chunk_index": chunk_index, "error": None}
    except Exception as e:
        return {"ok": False, "chunk_index": chunk_index, "error": str(e)}
    finally:
        if writer is not None:
            try:
                writer.release()
            except Exception:
                pass
        cap.release()
        if not committed:
            try:
                Path(part_path).unlink(missing_ok=True)
            except OSError:
                pass


def process_chunk_shm(task: Dict[str, Any]) -> Dict[str, Any]:
    """Process frames already in shared memory; coordinator unlinks after result."""
    shm_name = task["shm_name"]
    shape = tuple(task["shape"])
    dtype = np.dtype(task["dtype"])
    out_path = task["output_path"]
    chunk_index = int(task["chunk_index"])
    job_id = str(task["job_id"])
    settings = UniquifySettings.from_dict(task["settings"])
    w = int(task["width"])
    h = int(task["height"])
    fps = float(task["fps"])
    start = int(task["start_frame"])

    crop = pick_chunk_crop_offsets(job_id, chunk_index, settings)
    shm = None
    try:
        shm, buf = attach_shm_numpy(shm_name, shape, dtype)
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(out_path, fourcc, fps, (w, h))
        if not writer.isOpened():
            return {"ok": False, "chunk_index": chunk_index, "error": "writer failed"}

        n = shape[0]
        for i in range(n):
            if _cancelled():
                writer.release()
                return {"ok": False, "chunk_index": chunk_index, "error": "cancelled"}
            frame = np.ascontiguousarray(buf[i])
            global_idx = start + i
            proc = apply_frame(
                frame,
                global_idx,
                job_id,
                settings,
                crop_offsets=crop,
                color_grade_params=task.get("color_grade"),
            )
            if proc.shape[0] != h or proc.shape[1] != w:
                proc = cv2.resize(proc, (w, h), interpolation=cv2.INTER_LINEAR)
            writer.write(proc)
            _report(job_id, chunk_index, i + 1, n)
        writer.release()
        if _cancelled():
            return {"ok": False, "chunk_index": chunk_index, "error": "cancelled"}
        return {"ok": True, "chunk_index": chunk_index, "error": None}
    except Exception as e:
        return {"ok": False, "chunk_index": chunk_index, "error": str(e)}
    finally:
        close_shm(shm, unlink=False)


def decode_chunk_to_shm(
    video_path: str,
    start_frame: int,
    frame_count: int,
    height: int,
    width: int,
) -> Tuple[Any, str, Tuple[int, ...], str]:
    """Create SHM (F,H,W,3), fill from video; returns (shm, name, shape, dtype str)."""
    from zaliver.processing.shm_buffers import create_shm_numpy

    shape = (frame_count, height, width, 3)
    shm, arr = create_shm_numpy(shape, np.uint8)
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        close_shm(shm, unlink=True)
        raise RuntimeError("Cannot open video for SHM decode")
    try:
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
        for i in range(frame_count):
            ok, frame = cap.read()
            if not ok:
                break
            if frame.shape[0] != height or frame.shape[1] != width:
                frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_LINEAR)
            arr[i] = frame
    finally:
        cap.release()
    return shm, shm.name, shape, str(np.dtype(np.uint8))
