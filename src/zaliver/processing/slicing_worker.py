"""Qt-friendly slicing orchestration: parallel jobs, progress, cancel."""

from __future__ import annotations

import secrets
import shutil
import tempfile
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from zaliver.processing.ffmpeg_merge import (
    check_ffmpeg_tools,
    ffmpeg_drawtext_missing_user_message,
    ffmpeg_has_drawtext,
    pick_best_h264_encoder,
)
from zaliver.processing.gpu_detect import detect_gpus, format_gpu_list
from zaliver.processing.slicing import (
    DEFAULT_EDGE_EXCLUDE,
    DEFAULT_MAX_SCENE_DURATION,
    DEFAULT_MAX_SCENES,
    DEFAULT_MIN_SCENE_DURATION,
    DEFAULT_MIN_SCENES,
    DEFAULT_SLICE_FPS_MODE,
    find_segments_with_peaks,
    generate_video_from_segment,
    suggest_scene_durations,
)
from zaliver.processing.text_overlay import TextOverlaySettings

LogCallback = Callable[[str], None]

# Одновременных роликов при GPU-кодировании (AMF/NVENC/QSV делят один чип).
SLICE_GPU_MAX_CONCURRENT_JOBS = 2


def _max_concurrent_slice_jobs(
    num_workers: int,
    *,
    use_gpu: bool,
    use_gpu_finalize: bool,
) -> int:
    workers = max(1, int(num_workers))
    if use_gpu or use_gpu_finalize:
        return max(1, min(workers, SLICE_GPU_MAX_CONCURRENT_JOBS))
    return workers


def _unique_slice_filename(music_stem: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in music_stem)[:48]
    return f"{safe}_s_{secrets.token_hex(8)}.mp4"


@dataclass
class SliceJob:
    job_idx: int
    music_path: Path
    copy_index: int
    track_use_total: int
    output_path: Path
    finished: bool = False
    skip_youtube_upload: bool = False
    error: Optional[str] = None

    def tag(self, n_jobs: int) -> str:
        name = self.music_path.name
        if self.track_use_total == 1:
            return f"[{self.job_idx}/{n_jobs}] {name}"
        return (
            f"[{self.job_idx}/{n_jobs}] {name} "
            f"(повтор {self.copy_index}/{self.track_use_total})"
        )


def _run_slice_job(
    job: SliceJob,
    *,
    clip_pool: List[str],
    text_overlay_cfg: Optional[Dict[str, Any]],
    use_suggested_durations: bool,
    min_scene_duration: float,
    max_scene_duration: float,
    min_scenes: int,
    max_scenes: int,
    edge_exclude: float,
    use_gpu: bool,
    use_gpu_finalize: bool,
    slice_fps_mode: str,
    log: LogCallback,
    cancel_check: Callable[[], bool],
    n_jobs: int,
) -> SliceJob:
    if cancel_check():
        job.error = "Отменено."
        return job

    music = str(job.music_path.resolve())
    log(f"{job.tag(n_jobs)}: анализ аудио…")

    min_dur = float(min_scene_duration)
    max_dur = float(max_scene_duration)
    if use_suggested_durations:
        suggestion = suggest_scene_durations(
            music,
            edge_exclude=edge_exclude,
            min_scenes=min_scenes,
            max_scenes=max_scenes,
            verbose=False,
        )
        if suggestion:
            min_dur = float(suggestion["min_scene_duration"])
            max_dur = float(suggestion["max_scene_duration"])
            log(
                f"{job.tag(n_jobs)}: рекомендованные сцены "
                f"{min_dur}–{max_dur} с (BPM≈{suggestion.get('estimated_bpm')})"
            )

    if cancel_check():
        job.error = "Отменено."
        return job

    segments = find_segments_with_peaks(
        music,
        min_scene_duration=min_dur,
        max_scene_duration=max_dur,
        min_scenes=min_scenes,
        max_scenes=max_scenes,
        edge_exclude=edge_exclude,
    )
    if not segments:
        job.error = "Не найден подходящий сегмент аудио для нарезки."
        job.skip_youtube_upload = True
        return job

    if cancel_check():
        job.error = "Отменено."
        return job

    segment = segments[0]
    try:
        result = generate_video_from_segment(
            music,
            segment,
            str(job.output_path),
            clip_pool=clip_pool,
            fps=slice_fps_mode,
            log=log,
            use_gpu=bool(use_gpu),
            use_gpu_finalize=bool(use_gpu_finalize),
            text_overlay_cfg=text_overlay_cfg,
        )
        if not result or not job.output_path.is_file():
            job.error = "Не удалось собрать видео из клипов."
            job.skip_youtube_upload = True
            return job

        job.finished = True
        log(f"{job.tag(n_jobs)}: готово → {job.output_path.name}")
    except Exception as e:
        job.error = str(e).strip() or repr(e)
        job.skip_youtube_upload = True
        log(f"{job.tag(n_jobs)}: ошибка — {job.error}")
    return job


class SlicingController(QObject):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(bool, str)
    log_line = pyqtSignal(str)
    output_saved = pyqtSignal(str, bool)

    def __init__(self) -> None:
        super().__init__()
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self, options: Dict[str, Any]) -> None:
        def safe_log(msg: str) -> None:
            try:
                self.log_line.emit(msg)
            except RuntimeError:
                pass

        log: LogCallback = safe_log
        self._cancelled = False

        try:
            out_dir = Path(options["output_dir"])
            clip_pool: List[str] = []
            for x in options.get("clip_files") or []:
                p = Path(str(x))
                if p.is_file():
                    clip_pool.append(str(p.resolve()))
            music_pool: List[str] = []
            for x in options.get("music_files") or []:
                p = Path(str(x))
                if p.is_file():
                    music_pool.append(str(p.resolve()))

            if not clip_pool:
                self.finished.emit(False, "Выберите хотя бы один видеофайл для сцен.")
                return
            if not music_pool:
                self.finished.emit(False, "Добавьте хотя бы один аудиотрек для нарезки.")
                return
            if not check_ffmpeg_tools():
                self.finished.emit(
                    False,
                    "Нужны ffmpeg и ffprobe в PATH.",
                )
                return

            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                self.finished.emit(False, f"Не удалось создать выходную папку: {e}")
                return

            text_overlay_cfg: Optional[Dict[str, Any]] = None
            raw_text_overlay = options.get("text_overlay")
            if isinstance(raw_text_overlay, dict):
                toc = TextOverlaySettings.from_dict(raw_text_overlay)
                if toc.enabled and (toc.text or "").strip():
                    text_overlay_cfg = toc.to_dict()
            if text_overlay_cfg and not ffmpeg_has_drawtext():
                self.finished.emit(False, ffmpeg_drawtext_missing_user_message())
                return

            output_count = max(1, int(options.get("copies_per_track", 1)))
            num_workers = max(1, int(options.get("num_workers", 1)))
            use_suggested = bool(options.get("use_suggested_durations", False))
            min_scene_duration = float(
                options.get("min_scene_duration", DEFAULT_MIN_SCENE_DURATION)
            )
            max_scene_duration = float(
                options.get("max_scene_duration", DEFAULT_MAX_SCENE_DURATION)
            )
            min_scenes = int(options.get("min_scenes", DEFAULT_MIN_SCENES))
            max_scenes = int(options.get("max_scenes", DEFAULT_MAX_SCENES))
            edge_exclude = float(options.get("edge_exclude", DEFAULT_EDGE_EXCLUDE))
            use_gpu = bool(options.get("use_gpu", False))
            use_gpu_finalize = bool(options.get("use_gpu_finalize", False))
            slice_fps_mode = str(
                options.get("slice_fps_mode", DEFAULT_SLICE_FPS_MODE) or DEFAULT_SLICE_FPS_MODE
            )
            if slice_fps_mode.strip().lower() in ("auto", "авто"):
                slice_fps_mode = DEFAULT_SLICE_FPS_MODE

            if use_gpu or use_gpu_finalize:
                try:
                    log(format_gpu_list(detect_gpus()))
                except Exception:
                    pass
            if use_gpu:
                enc, _ = pick_best_h264_encoder(prefer_gpu=True)
                log(f"GPU сцен: включена (кодирование: {enc}).")
            else:
                log("GPU сцен: выключена (CPU, libx264).")
            if use_gpu_finalize:
                enc_f, _ = pick_best_h264_encoder(prefer_gpu=True)
                log(f"GPU финал (текст/mux): включена (кодирование: {enc_f}).")
            else:
                log("GPU финал: выключена (CPU, libx264).")

            if min_scenes > max_scenes:
                self.finished.emit(
                    False,
                    "Мин. количество сцен не может быть больше максимального.",
                )
                return
            if not use_suggested and min_scene_duration > max_scene_duration:
                self.finished.emit(
                    False,
                    "Мин. длительность сцены не может быть больше максимальной.",
                )
                return

            n_tracks = len(music_pool)
            jobs: List[SliceJob] = []
            for i in range(output_count):
                track_index = i % n_tracks
                music = music_pool[track_index]
                copy_index = i // n_tracks + 1
                track_use_total = (output_count - track_index + n_tracks - 1) // n_tracks
                stem = Path(music).stem
                outp = out_dir / _unique_slice_filename(stem)
                jobs.append(
                    SliceJob(
                        job_idx=i + 1,
                        music_path=Path(music),
                        copy_index=copy_index,
                        track_use_total=track_use_total,
                        output_path=outp,
                    )
                )

            n_jobs = len(jobs)
            if n_jobs == 0:
                self.finished.emit(False, "Нет заданий для нарезки.")
                return

            repeat_videos = max(0, output_count - n_tracks)
            if repeat_videos:
                log(
                    f"Нарезка: {output_count} роликов из {n_tracks} треков "
                    f"({repeat_videos} с повторением треков), "
                    f"клипов {len(clip_pool)}, потоков {num_workers}"
                )
            else:
                log(
                    f"Нарезка: {output_count} роликов из {n_tracks} треков, "
                    f"клипов {len(clip_pool)}, потоков {num_workers}"
                )
            if use_suggested:
                log(
                    f"Длительность сцены: авто (ручные {min_scene_duration:.2f}–"
                    f"{max_scene_duration:.2f} с — запасные)"
                )
            else:
                log(
                    f"Длительность сцены: {min_scene_duration:.2f}–{max_scene_duration:.2f} с"
                )
            log(f"Количество сцен: {min_scenes}–{max_scenes}")
            fps_label = {"30": "30", "60": "60"}.get(
                slice_fps_mode.strip().lower(), slice_fps_mode
            )
            log(f"FPS: {fps_label}")

            gpu_queue = use_gpu or use_gpu_finalize
            max_concurrent = _max_concurrent_slice_jobs(
                num_workers,
                use_gpu=use_gpu,
                use_gpu_finalize=use_gpu_finalize,
            )
            if gpu_queue and max_concurrent < num_workers:
                log(
                    f"GPU-очередь: до {max_concurrent} роликов одновременно "
                    f"(в UI потоков {num_workers} — лишние ждут свободный AMF/NVENC)."
                )

            def cancelled() -> bool:
                return self._cancelled

            done_count = 0
            errors: List[str] = []

            def on_job_done(j: SliceJob) -> None:
                nonlocal done_count
                done_count += 1
                self.progress.emit(done_count, n_jobs, j.output_path.name)
                if j.finished and j.output_path.is_file():
                    try:
                        self.output_saved.emit(
                            str(j.output_path.resolve()), not j.skip_youtube_upload
                        )
                    except RuntimeError:
                        pass
                elif j.error and j.error != "Отменено.":
                    errors.append(f"{j.music_path.name}: {j.error}")

            if max_concurrent <= 1 or n_jobs == 1:
                for job in jobs:
                    if cancelled():
                        self.finished.emit(False, "Отменено.")
                        return
                    _run_slice_job(
                        job,
                        clip_pool=clip_pool,
                        text_overlay_cfg=text_overlay_cfg,
                        use_suggested_durations=use_suggested,
                        min_scene_duration=min_scene_duration,
                        max_scene_duration=max_scene_duration,
                        min_scenes=min_scenes,
                        max_scenes=max_scenes,
                        edge_exclude=edge_exclude,
                        use_gpu=use_gpu,
                        use_gpu_finalize=use_gpu_finalize,
                        slice_fps_mode=slice_fps_mode,
                        log=log,
                        cancel_check=cancelled,
                        n_jobs=n_jobs,
                    )
                    on_job_done(job)
            else:
                with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
                    fut_map: Dict[Future, SliceJob] = {}
                    pending = list(jobs)
                    while pending or fut_map:
                        if cancelled():
                            for f in fut_map:
                                f.cancel()
                            self.finished.emit(False, "Отменено.")
                            return
                        while pending and len(fut_map) < max_concurrent:
                            job = pending.pop(0)
                            fut = pool.submit(
                                _run_slice_job,
                                job,
                                clip_pool=clip_pool,
                                text_overlay_cfg=text_overlay_cfg,
                                use_suggested_durations=use_suggested,
                                min_scene_duration=min_scene_duration,
                                max_scene_duration=max_scene_duration,
                                min_scenes=min_scenes,
                                max_scenes=max_scenes,
                                edge_exclude=edge_exclude,
                                use_gpu=use_gpu,
                                use_gpu_finalize=use_gpu_finalize,
                                slice_fps_mode=slice_fps_mode,
                                log=log,
                                cancel_check=cancelled,
                                n_jobs=n_jobs,
                            )
                            fut_map[fut] = job
                        if not fut_map:
                            break
                        done, _ = wait(fut_map.keys(), return_when=FIRST_COMPLETED)
                        for fut in done:
                            job = fut_map.pop(fut)
                            try:
                                fut.result()
                            except Exception as e:
                                job.error = str(e)
                                job.skip_youtube_upload = True
                            on_job_done(job)

            if cancelled():
                self.finished.emit(False, "Отменено.")
                return
            if done_count < n_jobs:
                self.finished.emit(False, "Не все ролики обработаны.")
                return
            if errors and done_count == len(errors):
                self.finished.emit(False, errors[0])
                return
            if errors:
                log("Часть роликов завершилась с ошибками:")
                for line in errors:
                    log(f"  • {line}")
            self.finished.emit(True, "")
        except Exception as e:
            self.finished.emit(False, str(e).strip() or repr(e))
