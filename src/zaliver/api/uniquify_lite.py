"""Lightweight uniquify orchestration for Windows API workers.

Avoids importing ``thread_worker`` / ``multiprocessing``, which can trigger
access violations (0xC0000005) in CREATE_NO_WINDOW child processes under uvicorn.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

from zaliver.processing.chunking import probe_video
from zaliver.processing.ffmpeg_merge import mux_video_audio, resolve_ffmpeg_executable
from zaliver.processing.ffmpeg_probe import estimate_target_video_bps
from zaliver.processing.pipeline import (
    RandomUniquifyBounds,
    apply_uniquify_effect_enables,
    random_uniquify_settings,
)
from zaliver.processing.subprocess_flags import (
    popen_creationflags,
    resolve_python_executable,
)

LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]


def _unique_name(stem: str) -> str:
    return f"{stem}_u_{secrets.token_hex(10)}.mp4"


def _run_encode_one(task: dict[str, Any], work_dir: Path) -> dict[str, Any]:
    work_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:12]
    task_path = work_dir / f"task_{token}.json"
    result_path = work_dir / f"task_{token}_out.json"
    stderr_path = work_dir / f"task_{token}.stderr.txt"
    task_path.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
    try:
        result_path.unlink(missing_ok=True)
    except OSError:
        pass

    video = str(task.get("video_path") or "")
    if sys.platform == "win32" and video:
        try:
            import ctypes

            buf = ctypes.create_unicode_buffer(32768)
            got = ctypes.windll.kernel32.GetShortPathNameW(video, buf, len(buf))
            if got and buf.value:
                task = dict(task)
                task["video_path"] = buf.value
                task_path.write_text(
                    json.dumps(task, ensure_ascii=False), encoding="utf-8"
                )
        except Exception:
            pass

    cmd = [
        resolve_python_executable(),
        "-m",
        "zaliver.api.encode_one",
        "--task-file",
        str(task_path),
        "--result-file",
        str(result_path),
    ]
    env = os.environ.copy()
    env["ZALIVER_API_SERVER"] = "1"
    try:
        with stderr_path.open("wb") as err_f:
            proc = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=err_f,
                env=env,
                creationflags=popen_creationflags(),
                timeout=7200,
                check=False,
            )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "encode subprocess timeout"}
    except OSError as e:
        return {"ok": False, "error": f"encode subprocess failed: {e}"}

    if result_path.is_file():
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError) as e:
            return {"ok": False, "error": f"bad encode result: {e}"}

    code = int(proc.returncode or 0)
    u = code & 0xFFFFFFFF
    if u == 0xC0000005:
        return {
            "ok": False,
            "error": "encode process crashed (access violation 0xC0000005)",
        }
    return {"ok": False, "error": f"encode process exited {code} without result"}


def run_uniquify_lite(
    options: dict[str, Any],
    *,
    log: LogFn,
    on_output: Callable[[str], None],
    cancel_check: CancelFn,
) -> tuple[bool, str]:
    if not resolve_ffmpeg_executable():
        return False, "ffmpeg not found"

    out_dir = Path(str(options.get("output_dir") or "")).expanduser()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, f"Cannot create output dir: {e}"

    inputs = [Path(str(p)) for p in (options.get("input_files") or []) if str(p).strip()]
    inputs = [p for p in inputs if p.is_file()]
    if not inputs:
        return False, "No input video files"

    use_gpu = bool(options.get("use_gpu", False))
    use_gpu_finalize = bool(options.get("use_gpu_finalize", False))
    randomize = bool(options.get("randomize_uniquify", True))
    copies = max(1, int(options.get("copies_per_file", 1) or 1))
    rb = RandomUniquifyBounds.from_options_dict(options.get("random_bounds") or {})

    log("uniquify_lite: без multiprocessing (стабильный Windows API).")
    if use_gpu:
        log("GPU обработка кадров: включена.")
    else:
        log("GPU обработка кадров: выключена (CPU, libx264).")
    if use_gpu_finalize:
        log("GPU склейка/mux: включена.")
    else:
        log("GPU склейка/mux: выключена (CPU, libx264).")

    saved = 0
    n_jobs = len(inputs) * copies
    file_idx = 0
    encode_dir = out_dir / ".zaliver_encode"
    encode_dir.mkdir(parents=True, exist_ok=True)

    for src in inputs:
        if cancel_check():
            return False, "Отменено."
        try:
            info = probe_video(str(src))
        except Exception as e:
            return False, f"{src.name}: probe failed: {e}"
        tvb = estimate_target_video_bps(str(src))

        for copy_i in range(1, copies + 1):
            if cancel_check():
                return False, "Отменено."
            file_idx += 1
            tag = f"[{file_idx}/{n_jobs}] {src.name}"

            if randomize:
                settings = random_uniquify_settings(rb).to_dict()
            else:
                settings = dict(options.get("settings") or {})
            settings = apply_uniquify_effect_enables(settings, options)

            outp = out_dir / _unique_name(src.stem)
            video_only = outp.with_name(f"{outp.stem}._zaliver_video{outp.suffix}")
            log(
                f"{tag} — ярк.{float(settings.get('brightness_delta', 0)):.1f}, "
                f"контр.{float(settings.get('contrast', 1)):.3f}, "
                f"насыщ.{float(settings.get('saturation_scale', 1)):.3f}"
            )
            if tvb:
                log(f"{tag}: видео ~{tvb / 1_000_000:.2f} Мбит/с")

            task = {
                "video_path": str(src.resolve()),
                "start_frame": 0,
                "frame_count": int(info.frame_count),
                "output_path": str(video_only),
                "chunk_index": 0,
                "job_id": uuid.uuid4().hex,
                "settings": settings,
                "width": int(info.width),
                "height": int(info.height),
                "fps": float(info.fps),
                "use_gpu": use_gpu,
                "target_video_bps": tvb,
                "text_overlay": None,
                "total_frames": int(info.frame_count),
            }
            log(f"{tag}: кодирование (subprocess)…")
            res = _run_encode_one(task, encode_dir)
            if not res.get("ok"):
                err = res.get("error") or "encode failed"
                try:
                    video_only.unlink(missing_ok=True)
                except OSError:
                    pass
                return False, f"Ошибка обработки: {err}"

            if cancel_check():
                try:
                    video_only.unlink(missing_ok=True)
                except OSError:
                    pass
                return False, "Отменено."

            spd = float(settings.get("playback_speed_factor") or 1.0)
            chorus = bool(settings.get("audio_chorus"))
            try:
                log(f"{tag}: mux audio…")
                mux_video_audio(
                    str(video_only),
                    str(src.resolve()),
                    str(outp),
                    playback_speed=spd,
                    audio_chorus=chorus,
                    log=log,
                    target_video_bps=tvb,
                    prefer_gpu=use_gpu_finalize,
                )
            except Exception as e:
                try:
                    video_only.unlink(missing_ok=True)
                except OSError:
                    pass
                return False, f"mux failed: {e}"
            finally:
                try:
                    video_only.unlink(missing_ok=True)
                except OSError:
                    pass

            if outp.is_file():
                saved += 1
                on_output(str(outp))
                log(f"{tag}: Сохранено: {outp.name}")
            else:
                return False, f"{tag}: output missing after mux"

    msg = (
        f"Сохранено выходных файлов: {saved} из {n_jobs}\n"
        f"Исходников: {len(inputs)}, копий на файл: {copies}\n"
        f"Папка: {out_dir}\n"
        "Формат: MP4 (H.264/AAC, если доступен ffmpeg)."
    )
    return saved > 0, msg
