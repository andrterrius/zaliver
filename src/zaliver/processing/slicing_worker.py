"""Qt-friendly slicing orchestration: parallel jobs, progress, cancel."""

from __future__ import annotations

import os
import random
import secrets
import shutil
import tempfile
import threading
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
# Параллельный залив: не больше одного ролика нарезки одновременно.
_UPLOAD_THROTTLE_SLICE_JOBS = 1


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


def _pick_random_tracks_for_jobs(music_pool: List[str], output_count: int) -> List[str]:
    """Случайные треки для роликов: без повторов, пока хватает пула; иначе циклы с shuffle."""
    n = max(0, int(output_count))
    if n <= 0 or not music_pool:
        return []
    if n <= len(music_pool):
        return random.sample(music_pool, n)
    assigned: List[str] = []
    while len(assigned) < n:
        batch = list(music_pool)
        random.shuffle(batch)
        assigned.extend(batch)
    return assigned[:n]


def _slice_music_try_order(primary: Path, pool: List[str]) -> List[Path]:
    """Основной трек (дважды), затем остальные треки из пула в случайном порядке."""
    try:
        primary_resolved = primary.resolve()
        pkey = os.path.normcase(str(primary_resolved))
    except OSError:
        return [primary]

    order: List[Path] = [primary_resolved, primary_resolved]
    seen = {pkey}
    rest: List[Path] = []
    for raw in pool:
        try:
            p = Path(raw).resolve()
        except OSError:
            continue
        if not p.is_file():
            continue
        k = os.path.normcase(str(p))
        if k in seen:
            continue
        seen.add(k)
        rest.append(p)
    random.shuffle(rest)
    order.extend(rest)
    return order


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


def _attempt_slice_with_music(
    music: str,
    output_path: Path,
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
    tag: str,
) -> Optional[str]:
    """Одна попытка нарезки. None — успех, иначе текст ошибки."""
    if cancel_check():
        return "Отменено."

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
                f"{tag}: рекомендованные сцены "
                f"{min_dur}–{max_dur} с (BPM≈{suggestion.get('estimated_bpm')})"
            )

    if cancel_check():
        return "Отменено."

    segments = find_segments_with_peaks(
        music,
        min_scene_duration=min_dur,
        max_scene_duration=max_dur,
        min_scenes=min_scenes,
        max_scenes=max_scenes,
        edge_exclude=edge_exclude,
    )
    if not segments:
        return "Не найден подходящий сегмент аудио для нарезки."

    if cancel_check():
        return "Отменено."

    segment = segments[0]
    try:
        result = generate_video_from_segment(
            music,
            segment,
            str(output_path),
            clip_pool=clip_pool,
            fps=slice_fps_mode,
            log=log,
            use_gpu=bool(use_gpu),
            use_gpu_finalize=bool(use_gpu_finalize),
            text_overlay_cfg=text_overlay_cfg,
        )
        if not result or not output_path.is_file():
            return "Не удалось собрать видео из клипов."
        return None
    except Exception as e:
        err = str(e).strip() or repr(e)
        log(f"{tag}: ошибка — {err}")
        return err


def _run_slice_job(
    job: SliceJob,
    *,
    music_pool: List[str],
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

    tag = job.tag(n_jobs)
    try_order = _slice_music_try_order(job.music_path, music_pool)
    last_error: Optional[str] = None

    for attempt_no, music_path in enumerate(try_order):
        if cancel_check():
            job.error = "Отменено."
            return job

        if attempt_no == 0:
            log(f"{tag}: анализ аудио…")
        elif attempt_no == 1:
            log(f"{tag}: повтор с {music_path.name}…")
        else:
            log(f"{tag}: другой трек — {music_path.name}…")

        err = _attempt_slice_with_music(
            str(music_path),
            job.output_path,
            clip_pool=clip_pool,
            text_overlay_cfg=text_overlay_cfg,
            use_suggested_durations=use_suggested_durations,
            min_scene_duration=min_scene_duration,
            max_scene_duration=max_scene_duration,
            min_scenes=min_scenes,
            max_scenes=max_scenes,
            edge_exclude=edge_exclude,
            use_gpu=use_gpu,
            use_gpu_finalize=use_gpu_finalize,
            slice_fps_mode=slice_fps_mode,
            log=log,
            cancel_check=cancel_check,
            tag=tag,
        )
        if err is None:
            job.finished = True
            if attempt_no >= 2:
                log(f"{tag}: готово (трек {music_path.name}) → {job.output_path.name}")
            else:
                log(f"{tag}: готово → {job.output_path.name}")
            return job

        if err == "Отменено.":
            job.error = err
            return job

        last_error = err

    job.error = last_error or "Не удалось выполнить нарезку."
    job.skip_youtube_upload = True
    return job


class SlicingController(QObject):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(bool, str)
    log_line = pyqtSignal(str)
    output_saved = pyqtSignal(str, bool)

    def __init__(self) -> None:
        super().__init__()
        self._cancelled = False
        self._upload_throttle = threading.Event()
        self._upload_throttle_logged = False

    def cancel(self) -> None:
        self._cancelled = True

    def set_upload_throttle(self, enabled: bool) -> None:
        """Приглушить нарезку, пока параллельно идёт залив в браузер."""
        if enabled:
            self._upload_throttle.set()
        else:
            self._upload_throttle.clear()
            self._upload_throttle_logged = False

    def run(self, options: Dict[str, Any]) -> None:
        def safe_log(msg: str) -> None:
            try:
                self.log_line.emit(msg)
            except RuntimeError:
                pass

        log: LogCallback = safe_log
        self._cancelled = False
        self._upload_throttle.clear()
        self._upload_throttle_logged = False

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
            assigned_tracks = _pick_random_tracks_for_jobs(music_pool, output_count)
            use_totals: Dict[str, int] = {}
            for music in assigned_tracks:
                key = os.path.normcase(music)
                use_totals[key] = use_totals.get(key, 0) + 1
            use_seen: Dict[str, int] = {}
            jobs: List[SliceJob] = []
            for i, music in enumerate(assigned_tracks):
                key = os.path.normcase(music)
                use_seen[key] = use_seen.get(key, 0) + 1
                stem = Path(music).stem
                outp = out_dir / _unique_slice_filename(stem)
                jobs.append(
                    SliceJob(
                        job_idx=i + 1,
                        music_path=Path(music),
                        copy_index=use_seen[key],
                        track_use_total=use_totals[key],
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
                        music_pool=music_pool,
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
                    throttle_logged = False

                    def _slice_slot_limit() -> int:
                        nonlocal throttle_logged
                        if self._upload_throttle.is_set():
                            if not throttle_logged:
                                throttle_logged = True
                                self._upload_throttle_logged = True
                                log(
                                    "Залив параллельно с нарезкой: параллельность "
                                    f"сведена к {_UPLOAD_THROTTLE_SLICE_JOBS}, "
                                    "чтобы браузер успевал кликать."
                                )
                            return _UPLOAD_THROTTLE_SLICE_JOBS
                        return max_concurrent

                    while pending or fut_map:
                        if cancelled():
                            for f in fut_map:
                                f.cancel()
                            self.finished.emit(False, "Отменено.")
                            return
                        while pending and len(fut_map) < _slice_slot_limit():
                            job = pending.pop(0)
                            fut = pool.submit(
                                _run_slice_job,
                                job,
                                music_pool=music_pool,
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
