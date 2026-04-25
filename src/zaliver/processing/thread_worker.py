"""Qt-friendly orchestration: process pool, progress queue, cancel."""

from __future__ import annotations

import queue
import secrets
import shutil
import time
import uuid
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import multiprocessing

from PyQt6.QtCore import QObject, pyqtSignal

from zaliver.processing.batch_paths import list_video_files
from zaliver.processing.chunking import VideoInfo, build_n_even_chunks, probe_video
from zaliver.processing.ffmpeg_merge import (
    check_ffmpeg,
    check_ffmpeg_tools,
    concat_segments,
    encoder_runtime_error,
    ffmpeg_encoder_list_text,
    mux_video_audio,
    merge_segments_with_source_audio,
    pick_best_h264_encoder,
)
from zaliver.processing.gpu_detect import detect_gpus, format_gpu_list
from zaliver.processing.pipeline import RandomUniquifyBounds, random_uniquify_settings
from zaliver.processing.worker import init_worker, process_chunk_disk


LogCallback = Callable[[str], None]


def _unique_output_filename(stem: str) -> str:
    """Случайное имя выходного файла (не счётчик), расширение .mp4."""
    return f"{stem}_u_{secrets.token_hex(10)}.mp4"


def _job_playback_speed(settings: Dict[str, Any]) -> float:
    """Совместимость: раньше поле называлось audio_speed_factor."""
    v = settings.get("playback_speed_factor", settings.get("audio_speed_factor", 1.0))
    try:
        return float(v)
    except (TypeError, ValueError):
        return 1.0


# Один чанк не короче стольки кадров (иначе накладные расходы > выгоды).
_MIN_FRAMES_PER_CHUNK = 360
# Не дробить ролик на больше стольки частей (склейка и диск).
# При большом числе логических CPU (24/32/64) фиксированного лимита 24
# недостаточно, чтобы загрузить машину на одном видео.
_MAX_CHUNKS_PER_VIDEO = 64


@dataclass
class OutputJob:
    """Один выходной MP4: либо целый файл в одном процессе, либо части + ffmpeg concat."""

    file_idx: int
    copy_index: int
    copies_per_file: int
    p: Path
    outp: Path
    info: VideoInfo
    job_id: str
    settings: Dict[str, Any]
    done_frames: int = 0
    finished: bool = False
    chunk_mode: bool = False
    chunk_work_dir: Optional[Path] = None
    # (start_frame, frame_count, segment_path) по возрастанию start
    chunks: List[Tuple[int, int, Path]] = field(default_factory=list)
    chunk_progress: Dict[int, int] = field(default_factory=dict)
    chunks_finished: Set[int] = field(default_factory=set)

    def tag(self, n_jobs: int) -> str:
        if self.copies_per_file == 1:
            return f"[{self.file_idx}/{n_jobs}] {self.p.name}"
        return (
            f"[{self.file_idx}/{n_jobs}] {self.p.name} "
            f"(копия {self.copy_index}/{self.copies_per_file})"
        )

    def estimated_done_frames(self) -> int:
        if self.finished:
            return self.info.frame_count
        if not self.chunk_mode:
            return self.done_frames
        s = 0
        for i, (_, cnt, _) in enumerate(self.chunks):
            s += min(self.chunk_progress.get(i, 0), cnt)
        return s


def _try_enable_chunk_mode(
    job: OutputJob,
    num_workers: int,
    out_dir: Path,
    log: LogCallback,
    n_jobs: int,
) -> None:
    if num_workers < 2 or not check_ffmpeg():
        return
    fc = job.info.frame_count
    if fc < _MIN_FRAMES_PER_CHUNK * 2:
        return
    n_by_size = max(2, (fc + _MIN_FRAMES_PER_CHUNK - 1) // _MIN_FRAMES_PER_CHUNK)
    # Чанков делаем заметно больше, чем воркеров, чтобы пул не простаивал из‑за
    # неодинаковой сложности участков (сцены/шум и т.п.).
    # Для очень длинных роликов это обычно ускоряет обработку на многоядерных CPU.
    n_target = min(_MAX_CHUNKS_PER_VIDEO, max(num_workers * 3, 2), n_by_size)
    specs = build_n_even_chunks(fc, n_target)
    if len(specs) < 2:
        return
    wd = out_dir / ".zaliver_chunks" / job.job_id
    try:
        wd.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    chunks: List[Tuple[int, int, Path]] = []
    for spec in specs:
        seg = wd / f"part_{spec.index:04d}.mp4"
        chunks.append((spec.start_frame, spec.frame_count, seg))
    job.chunk_mode = True
    job.chunk_work_dir = wd
    job.chunks = chunks
    log(
        f"{job.tag(n_jobs)}: части ролика — {len(chunks)} фрагментов "
        f"(до {num_workers} параллельно), склейка ffmpeg"
    )


@dataclass(frozen=True)
class _PoolTaskMeta:
    """Метаданные future в пуле: целый файл или один чанк."""

    job_id: str
    chunk_idx: int  # -1 = целый ролик одним процессом


class ProcessingController(QObject):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(bool, str)
    log_line = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self._mp_cancel: Optional[multiprocessing.synchronize.Event] = None

    def cancel(self) -> None:
        if self._mp_cancel is not None:
            self._mp_cancel.set()

    def run(self, options: Dict[str, Any]) -> None:
        log: LogCallback = lambda m: self.log_line.emit(m)
        self._mp_cancel = None

        try:
            # Informational: list detected adapters (NVIDIA/AMD/Intel) early in the log.
            try:
                log(format_gpu_list(detect_gpus()))
            except Exception:
                pass

            out_dir = Path(options["output_dir"])
            raw_selected = options.get("input_files") or []
            selected: List[Path] = []
            try:
                for x in raw_selected:
                    p = Path(str(x))
                    if p.is_file():
                        selected.append(p)
            except Exception:
                selected = []

            if selected:
                # Только выбранные файлы (сохраняем порядок выбора).
                videos = selected
            else:
                inp_raw = str(options.get("input_dir") or "").strip()
                inp_dir = Path(inp_raw) if inp_raw else Path()
                if not inp_dir.is_dir():
                    self.finished.emit(
                        False,
                        "Выберите видеофайлы для обработки (кнопка «Выбрать файлы…»).",
                    )
                    return
                videos = list_video_files(inp_dir)
            if not videos:
                self.finished.emit(
                    False,
                    "Нет поддерживаемых видео (.mp4, .mkv, .mov, .avi, .webm…).",
                )
                return

            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                self.finished.emit(False, f"Не удалось создать выходную папку: {e}")
                return

            if not check_ffmpeg_tools():
                self.finished.emit(
                    False,
                    "Нужны ffmpeg и ffprobe в PATH (обработка только через ffmpeg, без OpenCV).",
                )
                return

            copies_per_file = max(1, int(options.get("copies_per_file", 1)))
            plan: List[Tuple[Path, Path, VideoInfo, int, int]] = []
            try:
                for p in videos:
                    inf = probe_video(str(p))
                    if inf.frame_count <= 0:
                        raise RuntimeError(
                            f"{p.name}: в файле нет кадров (frame_count=0)."
                        )
                    for ci in range(1, copies_per_file + 1):
                        outp = out_dir / _unique_output_filename(p.stem)
                        plan.append((p, outp, inf, ci, copies_per_file))
            except Exception as e:
                self.finished.emit(False, str(e))
                return

            total_all = max(1, sum(x[2].frame_count for x in plan))
            num_workers = max(1, int(options.get("num_workers", 1)))
            use_gpu = bool(options.get("use_gpu", False))
            randomize = bool(options.get("randomize_uniquify", True))
            ui_settings = dict(options.get("settings", {}))
            playback_speed_enabled = bool(
                options.get("playback_speed_enabled", options.get("audio_speed_enabled", True))
            )
            audio_chorus_enabled = bool(options.get("audio_chorus_enabled", True))

            ctx = multiprocessing.get_context("spawn")
            progress_q: multiprocessing.Queue = ctx.Queue()
            cancel_ev = ctx.Event()
            self._mp_cancel = cancel_ev

            def cancelled() -> bool:
                return cancel_ev.is_set()

            n_jobs = len(plan)
            n_sources = len(videos)

            def _fmt_job_tag(fi: int, pname: str, ci: int) -> str:
                if copies_per_file == 1:
                    return f"[{fi}/{n_jobs}] {pname}"
                return (
                    f"[{fi}/{n_jobs}] {pname} "
                    f"(копия {ci}/{copies_per_file})"
                )

            jobs: List[OutputJob] = []
            file_idx = 0
            try:
                for p, outp, info, copy_index, _ in plan:
                    file_idx += 1
                    if cancelled():
                        self.finished.emit(False, "Отменено.")
                        return

                    if randomize:
                        rb = RandomUniquifyBounds.from_options_dict(
                            options.get("random_bounds") or {}
                        )
                        st = random_uniquify_settings(rb)
                        settings = st.to_dict()
                        # Тумблеры отключают соответствующую случайность (значения из границ не используются).
                        if not playback_speed_enabled:
                            settings["playback_speed_factor"] = 1.0
                        if not audio_chorus_enabled:
                            settings["audio_chorus"] = False
                    else:
                        settings = dict(ui_settings)

                    job_id = str(uuid.uuid4())

                    job = OutputJob(
                        file_idx=file_idx,
                        copy_index=copy_index,
                        copies_per_file=copies_per_file,
                        p=p,
                        outp=outp,
                        info=info,
                        job_id=job_id,
                        settings=settings,
                    )
                    if randomize:
                        log(
                            f"{job.tag(n_jobs)} — случайно: "
                            f"ярк.{settings['brightness_delta']:.1f}, "
                            f"контр.{settings['contrast']:.3f}, "
                            f"насыщ.{settings['saturation_scale']:.3f}, "
                            f"шум σ={settings['noise_sigma']:.2f}"
                        )
                    _try_enable_chunk_mode(job, num_workers, out_dir, log, n_jobs)
                    if not job.chunk_mode:
                        log(
                            f"{job.tag(n_jobs)}: целый файл → один процесс пула "
                            f"(параллельно до {num_workers} роликов)"
                        )
                    jobs.append(job)
            except Exception as e:
                self.finished.emit(False, str(e))
                return

            jobs_by_id: Dict[str, OutputJob] = {j.job_id: j for j in jobs}
            if check_ffmpeg():
                try:
                    enc, _ = pick_best_h264_encoder(prefer_gpu=use_gpu)
                    if use_gpu and enc != "libx264":
                        log(f"GPU-энкодер ffmpeg: {enc}")
                    elif use_gpu and enc == "libx264":
                        # Explain why we didn't pick a GPU encoder (if ffmpeg reports any).
                        txt = ffmpeg_encoder_list_text().lower()
                        hints: List[str] = []
                        for cand in ("h264_nvenc", "h264_qsv", "h264_amf"):
                            if cand in txt:
                                err = encoder_runtime_error(cand)
                                if err:
                                    hints.append(f"{cand}: {err}")
                        if hints:
                            log(
                                "GPU включён, но GPU-энкодер не стартует. Будет CPU (libx264/mp4v).\n"
                                + "\n".join(hints)
                            )
                        else:
                            log("GPU включён, но доступного GPU-энкодера нет. Будет CPU (libx264/mp4v).")
                except Exception:
                    if use_gpu:
                        log("GPU-режим: не удалось определить/проверить энкодер ffmpeg, будет CPU.")

            def _cleanup_partial_outputs() -> None:
                for j in jobs:
                    wd = j.chunk_work_dir
                    if wd is not None:
                        try:
                            if wd.is_dir():
                                shutil.rmtree(wd, ignore_errors=True)
                        except OSError:
                            pass
                    outp = j.outp
                    try:
                        if outp.is_file():
                            outp.unlink()
                    except OSError:
                        pass
                    for part in (
                        outp.with_name(f"{outp.stem}._zaliver_tmp{outp.suffix}"),
                        Path(f"{outp}.part"),
                    ):
                        try:
                            if part.is_file():
                                part.unlink()
                        except OSError:
                            pass

            def finish_error(msg: str, fut_map: Dict[Future, _PoolTaskMeta]) -> None:
                cancel_ev.set()
                while fut_map:
                    done, _ = wait(
                        list(fut_map.keys()),
                        timeout=2.0,
                        return_when=FIRST_COMPLETED,
                    )
                    if not done:
                        break
                    for fut in done:
                        fut_map.pop(fut, None)
                        try:
                            fut.result(timeout=0.1)
                        except Exception:
                            pass
                for fut in list(fut_map.keys()):
                    fut_map.pop(fut, None)
                    try:
                        fut.result(timeout=0.1)
                    except Exception:
                        pass
                fut_map.clear()
                _cleanup_partial_outputs()
                self.finished.emit(False, msg)

            with ProcessPoolExecutor(
                max_workers=num_workers,
                mp_context=ctx,
                initializer=init_worker,
                initargs=(progress_q, cancel_ev),
            ) as pool:
                pending_tasks: deque[_PoolTaskMeta] = deque()
                chunked = [j for j in jobs if j.chunk_mode]
                max_c = max((len(j.chunks) for j in chunked), default=0)
                for ci in range(max_c):
                    for j in chunked:
                        if ci < len(j.chunks):
                            pending_tasks.append(_PoolTaskMeta(j.job_id, ci))
                for j in jobs:
                    if not j.chunk_mode:
                        pending_tasks.append(_PoolTaskMeta(j.job_id, -1))

                futures: Dict[Future, _PoolTaskMeta] = {}
                last_emit = 0.0

                def global_done_frames() -> int:
                    s = 0
                    for j in jobs:
                        if j.finished:
                            s += j.info.frame_count
                        else:
                            s += min(j.estimated_done_frames(), j.info.frame_count)
                    return s

                def emit_progress_global(msg: str = "") -> None:
                    nonlocal last_emit
                    now = time.monotonic()
                    cur = global_done_frames()
                    if now - last_emit < 0.05 and msg == "":
                        return
                    last_emit = now
                    inflight = len(futures)
                    hint = msg or (
                        f"Параллельно · задач в работе: {inflight} · "
                        f"кадров ~{min(cur, total_all)}/{total_all}"
                    )
                    self.progress.emit(min(cur, total_all), total_all, hint)

                def drain_progress_queue() -> None:
                    while True:
                        try:
                            jid, ci, d, t = progress_q.get_nowait()
                        except queue.Empty:
                            break
                        j = jobs_by_id.get(jid)
                        if j is None or t <= 0:
                            continue
                        if j.chunk_mode and ci >= 0:
                            j.chunk_progress[ci] = max(j.chunk_progress.get(ci, 0), d)
                            emit_progress_global(
                                f"{j.tag(n_jobs)}: часть {ci + 1}/{len(j.chunks)} "
                                f"кадры {d}/{t}"
                            )
                        else:
                            j.done_frames = max(j.done_frames, d)
                            emit_progress_global(
                                f"{j.tag(n_jobs)}: кадры {d}/{t}"
                            )

                def _submit_task(meta: _PoolTaskMeta) -> None:
                    j = jobs_by_id[meta.job_id]
                    if meta.chunk_idx < 0:
                        # Всегда считаем "видео без аудио" в temp-файл.
                        # Аудио добавим позже (mux из исходника) в одном потоке координатора.
                        temp_out = j.outp.with_name(
                            f"{j.outp.stem}._zaliver_video{j.outp.suffix}"
                        )
                        task = {
                            "video_path": str(j.p),
                            "start_frame": 0,
                            "frame_count": int(j.info.frame_count),
                            "output_path": str(temp_out),
                            "chunk_index": 0,
                            "job_id": j.job_id,
                            "settings": j.settings,
                            "width": j.info.width,
                            "height": j.info.height,
                            "fps": j.info.fps,
                            "use_gpu": use_gpu,
                        }
                    else:
                        start, cnt, seg = j.chunks[meta.chunk_idx]
                        task = {
                            "video_path": str(j.p),
                            "start_frame": start,
                            "frame_count": cnt,
                            "output_path": str(seg),
                            "chunk_index": meta.chunk_idx,
                            "job_id": j.job_id,
                            "settings": j.settings,
                            "width": j.info.width,
                            "height": j.info.height,
                            "fps": j.info.fps,
                            "use_gpu": use_gpu,
                        }
                    fut = pool.submit(process_chunk_disk, task)
                    futures[fut] = meta

                def fill_pool() -> None:
                    while len(futures) < num_workers and pending_tasks:
                        _submit_task(pending_tasks.popleft())

                fill_pool()
                while pending_tasks or futures:
                    if cancelled():
                        finish_error("Отменено.", futures)
                        return

                    drain_progress_queue()
                    emit_progress_global()

                    if not pending_tasks and not futures:
                        break

                    if futures:
                        done, _ = wait(
                            list(futures.keys()),
                            timeout=0.08,
                            return_when=FIRST_COMPLETED,
                        )
                    else:
                        done = []
                        fill_pool()
                        continue

                    drain_progress_queue()
                    for fut in done:
                        meta = futures.pop(fut, None)
                        if meta is None:
                            continue
                        try:
                            res = fut.result()
                        except Exception as e:
                            res = {"ok": False, "error": str(e)}
                        if not res.get("ok"):
                            err = res.get("error") or "unknown"
                            msg = (
                                "Отменено."
                                if err == "cancelled"
                                else f"Ошибка обработки: {err}"
                            )
                            finish_error(msg, futures)
                            return
                        j = jobs_by_id[meta.job_id]
                        if cancelled():
                            finish_error("Отменено.", futures)
                            return
                        if meta.chunk_idx < 0:
                            # После обработки: приклеиваем аудио из исходника (если есть ffmpeg).
                            # Если аудио нет или ffmpeg не найден, сохранится "только видео".
                            video_only = j.outp.with_name(
                                f"{j.outp.stem}._zaliver_video{j.outp.suffix}"
                            )
                            if check_ffmpeg() and video_only.is_file():
                                av_tmp = j.outp.with_name(
                                    f"{j.outp.stem}._zaliver_av{j.outp.suffix}"
                                )
                                try:
                                    mux_video_audio(
                                        str(video_only),
                                        str(j.p),
                                        str(av_tmp),
                                        playback_speed=_job_playback_speed(j.settings),
                                        audio_chorus=bool(j.settings.get("audio_chorus", False)),
                                        log=log,
                                    )
                                    try:
                                        av_tmp.replace(j.outp)
                                    except OSError:
                                        # If replace fails, keep whatever exists.
                                        pass
                                finally:
                                    try:
                                        if video_only.is_file():
                                            video_only.unlink()
                                    except OSError:
                                        pass
                            else:
                                try:
                                    if video_only.is_file():
                                        video_only.replace(j.outp)
                                except OSError:
                                    pass
                            j.finished = True
                            j.done_frames = j.info.frame_count
                            log(f"{j.tag(n_jobs)}: Сохранено: {j.outp.name}")
                        else:
                            j.chunks_finished.add(meta.chunk_idx)
                            if len(j.chunks_finished) >= len(j.chunks):
                                try:
                                    seg_paths = [str(t[2]) for t in j.chunks]
                                    # Склеиваем видео и добавляем аудио исходника.
                                    wd = str(j.chunk_work_dir or (out_dir / ".zaliver_chunks" / j.job_id))
                                    merge_segments_with_source_audio(
                                        seg_paths,
                                        str(j.p),
                                        str(j.outp),
                                        work_dir=wd,
                                        playback_speed=_job_playback_speed(j.settings),
                                        audio_chorus=bool(j.settings.get("audio_chorus", False)),
                                        log=log,
                                    )
                                except Exception as e:
                                    finish_error(
                                        f"Склейка ffmpeg: {e}",
                                        futures,
                                    )
                                    return
                                wd = j.chunk_work_dir
                                if wd is not None:
                                    try:
                                        shutil.rmtree(wd, ignore_errors=True)
                                    except OSError:
                                        pass
                                    j.chunk_work_dir = None
                                j.finished = True
                                j.done_frames = j.info.frame_count
                                j.chunk_progress.clear()
                                for i, (_, cnt, _) in enumerate(j.chunks):
                                    j.chunk_progress[i] = cnt
                                log(
                                    f"{j.tag(n_jobs)}: Сохранено: {j.outp.name} "
                                    f"(склеено из {len(j.chunks)} частей)"
                                )
                        emit_progress_global()
                    fill_pool()

            self.progress.emit(total_all, total_all, "Готово")
            done_msg = (
                f"Сохранено выходных файлов: {n_jobs}\n"
                f"Исходников: {n_sources}, копий на файл: {copies_per_file}\n"
                f"Папка: {out_dir}\n"
                "Формат: MP4 (H.264/AAC, если доступен ffmpeg)."
            )
            self.finished.emit(True, done_msg)
        except Exception as e:
            self.finished.emit(False, str(e))
        finally:
            self._mp_cancel = None
