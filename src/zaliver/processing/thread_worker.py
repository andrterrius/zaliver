"""Qt-friendly orchestration: process pool, progress queue, cancel."""

from __future__ import annotations

import queue
import random
import secrets
import shutil
import sys
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
from zaliver.processing.ffmpeg_probe import estimate_target_video_bps
from zaliver.processing.ffmpeg_merge import (
    BackgroundMusicUnavailableError,
    bgm_alternate_paths,
    check_ffmpeg,
    check_ffmpeg_tools,
    encoder_runtime_error,
    ffmpeg_encoder_list_text,
    is_background_music_failure,
    merge_segments_with_source_audio,
    mux_video_audio,
    mux_video_background_music,
    pick_best_h264_encoder,
)
from zaliver.processing.gpu_detect import detect_gpus, format_gpu_list
from zaliver.processing.pipeline import RandomUniquifyBounds, random_uniquify_settings
from zaliver.processing.text_overlay import TextOverlaySettings, compute_scaled_overlay
from zaliver.processing.worker import init_worker, process_chunk_disk


LogCallback = Callable[[str], None]


def _unique_output_filename(stem: str) -> str:
    """Случайное имя выходного файла (не счётчик), расширение .mp4."""
    return f"{stem}_u_{secrets.token_hex(10)}.mp4"


def _job_bgm_alternates(j: OutputJob, music_pool: List[str]) -> List[str]:
    if j.background_music_paths:
        return list(j.background_music_paths)
    if j.background_music_path:
        return bgm_alternate_paths(str(j.background_music_path), music_pool)
    return []


def _mux_job_final_audio(
    j: OutputJob,
    *,
    video_only: str,
    av_tmp: str,
    log: LogCallback,
    n_jobs: int,
    music_pool: List[str],
    cancel_check: Optional[Callable[[], bool]] = None,
) -> None:
    """Приклеить звук к видео; при сбое фона — исходник и skip_youtube_upload."""
    spd = _job_playback_speed(j.settings)
    chorus = bool(j.settings.get("audio_chorus", False))
    if j.background_music_path:
        try:
            mux_video_background_music(
                video_only,
                str(j.background_music_path),
                av_tmp,
                frame_count=int(j.info.frame_count),
                fps=float(j.info.fps),
                playback_speed=spd,
                log=log,
                target_video_bps=j.target_video_bps,
                mix_with_source=bool(j.background_music_mix),
                source_video_path=str(j.p),
                audio_chorus=chorus,
                music_volume_pct=float(j.background_music_volume_pct),
                music_path_alternates=_job_bgm_alternates(j, music_pool),
                cancel_check=cancel_check,
            )
            return
        except RuntimeError as e:
            if str(e) == "cancelled":
                raise
            if not is_background_music_failure(e):
                raise
            log(f"{j.tag(n_jobs)}: {e}")
            log(
                f"{j.tag(n_jobs)}: сохраняем со звуком исходника (без фона), "
                "видео исключено из залива в YouTube."
            )
            j.skip_youtube_upload = True
        except BackgroundMusicUnavailableError as e:
            log(f"{j.tag(n_jobs)}: {e}")
            log(
                f"{j.tag(n_jobs)}: сохраняем со звуком исходника (без фона), "
                "видео исключено из залива в YouTube."
            )
            j.skip_youtube_upload = True
    mux_video_audio(
        video_only,
        str(j.p),
        av_tmp,
        playback_speed=spd,
        audio_chorus=chorus,
        log=log,
        target_video_bps=j.target_video_bps,
    )


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
    target_video_bps: Optional[int] = None
    background_music_path: Optional[str] = None
    background_music_paths: List[str] = field(default_factory=list)
    background_music_mix: bool = False
    background_music_volume_pct: float = 35.0
    text_overlay: Optional[Dict[str, Any]] = None
    skip_youtube_upload: bool = False
    finalize_error: Optional[str] = None
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


def _skip_job_finalize_error(
    j: OutputJob,
    err: BaseException,
    *,
    log: LogCallback,
    n_jobs: int,
) -> None:
    """Пропустить один ролик после сбоя склейки/mux; остальные продолжают."""
    msg = str(err).strip() or repr(err)
    log(f"{j.tag(n_jobs)}: ошибка склейки/ffmpeg: {msg}")
    log(f"{j.tag(n_jobs)}: файл пропущен, обработка остальных продолжается.")
    j.finalize_error = msg
    j.skip_youtube_upload = True
    j.finished = True
    j.done_frames = j.info.frame_count
    for part in (
        j.outp,
        j.outp.with_name(f"{j.outp.stem}._zaliver_video{j.outp.suffix}"),
        j.outp.with_name(f"{j.outp.stem}._zaliver_av{j.outp.suffix}"),
        Path(f"{j.outp}.part"),
    ):
        try:
            if part.is_file():
                part.unlink()
        except OSError:
            pass
    wd = j.chunk_work_dir
    if wd is not None:
        try:
            shutil.rmtree(wd, ignore_errors=True)
        except OSError:
            pass
        j.chunk_work_dir = None


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
    # macOS: меньше частей и меньший коэффициент — ниже риск EMFILE при ffmpeg concat.
    if sys.platform == "darwin":
        max_chunks = min(_MAX_CHUNKS_PER_VIDEO, 48)
        worker_factor = 2
    else:
        max_chunks = _MAX_CHUNKS_PER_VIDEO
        worker_factor = 3
    n_target = min(max_chunks, max(num_workers * worker_factor, 2), n_by_size)
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
    output_saved = pyqtSignal(str, bool)

    def __init__(self) -> None:
        super().__init__()
        self._mp_cancel: Optional[multiprocessing.synchronize.Event] = None

    def cancel(self) -> None:
        if self._mp_cancel is not None:
            self._mp_cancel.set()

    def run(self, options: Dict[str, Any]) -> None:
        def safe_log(msg: str) -> None:
            try:
                self.log_line.emit(msg)
            except RuntimeError:
                pass

        log: LogCallback = safe_log
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
            plan: List[Tuple[Path, Path, VideoInfo, int, int, Optional[int]]] = []
            try:
                for p in videos:
                    inf = probe_video(str(p))
                    if inf.frame_count <= 0:
                        raise RuntimeError(
                            f"{p.name}: в файле нет кадров (frame_count=0)."
                        )
                    tvb = estimate_target_video_bps(str(p))
                    for ci in range(1, copies_per_file + 1):
                        outp = out_dir / _unique_output_filename(p.stem)
                        plan.append((p, outp, inf, ci, copies_per_file, tvb))
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
            bg_music_enabled = bool(options.get("background_music_enabled", False))
            raw_music = options.get("background_music_files") or []
            music_pool: List[str] = []
            try:
                for x in raw_music:
                    p = Path(str(x))
                    if p.is_file():
                        music_pool.append(str(p.resolve()))
            except Exception:
                music_pool = []

            bg_mix_opt = bool(options.get("background_music_mix_with_source", False))
            try:
                bg_vol_opt = float(options.get("background_music_volume_pct", 35.0))
            except (TypeError, ValueError):
                bg_vol_opt = 35.0
            bg_vol_opt = max(0.0, min(100.0, bg_vol_opt))

            text_overlay_cfg: Optional[Dict[str, Any]] = None
            raw_text_overlay = options.get("text_overlay")
            if isinstance(raw_text_overlay, dict):
                toc = TextOverlaySettings.from_dict(raw_text_overlay)
                if toc.enabled and (toc.text or "").strip():
                    text_overlay_cfg = toc.to_dict()

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
                for p, outp, info, copy_index, _, tvb in plan:
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

                    bg_track: Optional[str] = None
                    if bg_music_enabled and music_pool:
                        bg_track = random.choice(music_pool)
                    bg_mix = bool(bg_track) and bg_mix_opt

                    job = OutputJob(
                        file_idx=file_idx,
                        copy_index=copy_index,
                        copies_per_file=copies_per_file,
                        p=p,
                        outp=outp,
                        info=info,
                        job_id=job_id,
                        settings=settings,
                        target_video_bps=tvb,
                        background_music_path=bg_track,
                        background_music_paths=(
                            bgm_alternate_paths(bg_track, music_pool) if bg_track else []
                        ),
                        background_music_mix=bg_mix,
                        background_music_volume_pct=bg_vol_opt,
                        text_overlay=text_overlay_cfg,
                    )
                    if randomize:
                        log(
                            f"{job.tag(n_jobs)} — случайно: "
                            f"ярк.{settings['brightness_delta']:.1f}, "
                            f"контр.{settings['contrast']:.3f}, "
                            f"насыщ.{settings['saturation_scale']:.3f}, "
                            f"шум σ={settings['noise_sigma']:.2f}"
                        )
                    if tvb is not None:
                        log(
                            f"{job.tag(n_jobs)}: размер ≈ как у исходника — "
                            f"видео ~{tvb / 1_000_000:.2f} Мбит/с (оценка ffprobe)."
                        )
                    if bg_track:
                        mix_note = (
                            ", наложение на звук видео"
                            if bg_mix
                            else ", замена звука"
                        )
                        log(
                            f"{job.tag(n_jobs)}: фоновая музыка — {Path(bg_track).name}{mix_note} "
                            f"(из пула {len(music_pool)} треков; случайный отрезок по длительности ролика)"
                            + (
                                f", громкость музыки {bg_vol_opt:.0f}%"
                                if bg_mix
                                else ""
                            )
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

                def _scaled_text_overlay_for_job(j: OutputJob) -> Optional[Dict[str, Any]]:
                    if not j.text_overlay:
                        return None
                    toc = TextOverlaySettings.from_dict(j.text_overlay)
                    scaled = compute_scaled_overlay(toc, j.info.width, j.info.height)
                    return scaled.to_dict() if scaled else None

                def _submit_task(meta: _PoolTaskMeta) -> None:
                    j = jobs_by_id[meta.job_id]
                    scaled_overlay = _scaled_text_overlay_for_job(j)
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
                            "target_video_bps": j.target_video_bps,
                            "text_overlay": scaled_overlay,
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
                            "target_video_bps": j.target_video_bps,
                            "text_overlay": scaled_overlay,
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
                            video_only = j.outp.with_name(
                                f"{j.outp.stem}._zaliver_video{j.outp.suffix}"
                            )
                            finalize_ok = False
                            try:
                                if check_ffmpeg() and video_only.is_file():
                                    av_tmp = j.outp.with_name(
                                        f"{j.outp.stem}._zaliver_av{j.outp.suffix}"
                                    )
                                    try:
                                        _mux_job_final_audio(
                                            j,
                                            video_only=str(video_only),
                                            av_tmp=str(av_tmp),
                                            log=log,
                                            n_jobs=n_jobs,
                                            music_pool=music_pool,
                                            cancel_check=cancelled,
                                        )
                                        try:
                                            av_tmp.replace(j.outp)
                                        except OSError:
                                            pass
                                        finalize_ok = True
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
                                            finalize_ok = True
                                    except OSError:
                                        pass
                            except RuntimeError as e:
                                if str(e) == "cancelled":
                                    finish_error("Отменено.", futures)
                                    return
                                _skip_job_finalize_error(
                                    j, e, log=log, n_jobs=n_jobs
                                )
                            except Exception as e:
                                _skip_job_finalize_error(
                                    j, e, log=log, n_jobs=n_jobs
                                )
                            if finalize_ok and not j.finalize_error:
                                j.finished = True
                                j.done_frames = j.info.frame_count
                                log(f"{j.tag(n_jobs)}: Сохранено: {j.outp.name}")
                                try:
                                    self.output_saved.emit(
                                        str(j.outp), not j.skip_youtube_upload
                                    )
                                except RuntimeError:
                                    pass
                                log(
                                    f"{j.tag(n_jobs)}: Пауза 3 секунды после сохранения "
                                    f"(файл: {str(j.outp)!r})…"
                                )
                                time.sleep(3.0)
                        else:
                            j.chunks_finished.add(meta.chunk_idx)
                            if len(j.chunks_finished) >= len(j.chunks):
                                seg_paths = [str(t[2]) for t in j.chunks]
                                wd = str(
                                    j.chunk_work_dir
                                    or (out_dir / ".zaliver_chunks" / j.job_id)
                                )
                                finalize_ok = False
                                try:
                                    bg_ok = merge_segments_with_source_audio(
                                        seg_paths,
                                        str(j.p),
                                        str(j.outp),
                                        work_dir=wd,
                                        playback_speed=_job_playback_speed(j.settings),
                                        audio_chorus=bool(
                                            j.settings.get("audio_chorus", False)
                                        ),
                                        log=log,
                                        target_video_bps=j.target_video_bps,
                                        background_music_path=j.background_music_path,
                                        background_music_alternates=_job_bgm_alternates(
                                            j, music_pool
                                        ),
                                        music_video_meta=(
                                            int(j.info.frame_count),
                                            float(j.info.fps),
                                        ),
                                        background_music_mix=bool(j.background_music_mix),
                                        background_music_volume_pct=float(
                                            j.background_music_volume_pct
                                        ),
                                        cancel_check=cancelled,
                                    )
                                    if not bg_ok:
                                        j.skip_youtube_upload = True
                                    finalize_ok = True
                                except RuntimeError as e:
                                    if str(e) == "cancelled":
                                        finish_error("Отменено.", futures)
                                        return
                                    if is_background_music_failure(e):
                                        log(f"{j.tag(n_jobs)}: {e}")
                                        log(
                                            f"{j.tag(n_jobs)}: сохраняем со звуком исходника "
                                            "(без фона), видео исключено из залива в YouTube."
                                        )
                                        j.skip_youtube_upload = True
                                        try:
                                            merge_segments_with_source_audio(
                                                seg_paths,
                                                str(j.p),
                                                str(j.outp),
                                                work_dir=wd,
                                                playback_speed=_job_playback_speed(
                                                    j.settings
                                                ),
                                                audio_chorus=bool(
                                                    j.settings.get("audio_chorus", False)
                                                ),
                                                log=log,
                                                target_video_bps=j.target_video_bps,
                                                background_music_path=None,
                                                cancel_check=cancelled,
                                            )
                                            finalize_ok = True
                                        except RuntimeError as e2:
                                            if str(e2) == "cancelled":
                                                finish_error("Отменено.", futures)
                                                return
                                            _skip_job_finalize_error(
                                                j, e2, log=log, n_jobs=n_jobs
                                            )
                                        except Exception as e2:
                                            _skip_job_finalize_error(
                                                j, e2, log=log, n_jobs=n_jobs
                                            )
                                    else:
                                        _skip_job_finalize_error(
                                            j, e, log=log, n_jobs=n_jobs
                                        )
                                except Exception as e:
                                    if is_background_music_failure(e):
                                        log(f"{j.tag(n_jobs)}: {e}")
                                        log(
                                            f"{j.tag(n_jobs)}: сохраняем со звуком исходника "
                                            "(без фона), видео исключено из залива в YouTube."
                                        )
                                        j.skip_youtube_upload = True
                                        try:
                                            merge_segments_with_source_audio(
                                                seg_paths,
                                                str(j.p),
                                                str(j.outp),
                                                work_dir=wd,
                                                playback_speed=_job_playback_speed(
                                                    j.settings
                                                ),
                                                audio_chorus=bool(
                                                    j.settings.get("audio_chorus", False)
                                                ),
                                                log=log,
                                                target_video_bps=j.target_video_bps,
                                                background_music_path=None,
                                                cancel_check=cancelled,
                                            )
                                            finalize_ok = True
                                        except RuntimeError as e2:
                                            if str(e2) == "cancelled":
                                                finish_error("Отменено.", futures)
                                                return
                                            _skip_job_finalize_error(
                                                j, e2, log=log, n_jobs=n_jobs
                                            )
                                        except Exception as e2:
                                            _skip_job_finalize_error(
                                                j, e2, log=log, n_jobs=n_jobs
                                            )
                                    else:
                                        _skip_job_finalize_error(
                                            j, e, log=log, n_jobs=n_jobs
                                        )
                                if finalize_ok and not j.finalize_error:
                                    wd_path = j.chunk_work_dir
                                    if wd_path is not None:
                                        try:
                                            shutil.rmtree(wd_path, ignore_errors=True)
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
                                    try:
                                        self.output_saved.emit(
                                            str(j.outp), not j.skip_youtube_upload
                                        )
                                    except RuntimeError:
                                        pass
                                    log(
                                        f"{j.tag(n_jobs)}: Пауза 3 секунды после сохранения "
                                        f"(файл: {str(j.outp)!r})…"
                                    )
                                    time.sleep(3.0)
                        emit_progress_global()
                    fill_pool()

            if bool(options.get("youtube_upload_after_processing")) and total_all > 0:
                cur99 = (total_all * 99) // 100
                if cur99 >= total_all:
                    cur99 = max(0, total_all - 1)
                self.progress.emit(cur99, total_all, "YouTube: загрузка, прогресс 99%…")
            else:
                self.progress.emit(total_all, total_all, "Готово")
            saved_n = sum(
                1 for j in jobs if j.finished and not j.finalize_error
            )
            failed_finalize = [j for j in jobs if j.finalize_error]
            done_msg = (
                f"Сохранено выходных файлов: {saved_n} из {n_jobs}\n"
                f"Исходников: {n_sources}, копий на файл: {copies_per_file}\n"
                f"Папка: {out_dir}\n"
                "Формат: MP4 (H.264/AAC, если доступен ffmpeg)."
            )
            if failed_finalize:
                done_msg += (
                    f"\n\nНе удалось склеить/сохранить: {len(failed_finalize)} "
                    f"(подробности в логе, без остановки остальных)."
                )
            self.finished.emit(True, done_msg)
        except Exception as e:
            self.finished.emit(False, str(e))
        finally:
            self._mp_cancel = None
