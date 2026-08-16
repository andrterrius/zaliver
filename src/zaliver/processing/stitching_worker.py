"""Headless stitching orchestration: two clip pools, beat-synced cut."""

from __future__ import annotations

import os
import secrets
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from zaliver.core.sinks import JobProgressSink
from zaliver.processing.ffmpeg_merge import (
    check_ffmpeg_tools,
    ffmpeg_drawtext_missing_user_message,
    ffmpeg_has_drawtext,
    pick_best_h264_encoder,
)
from zaliver.processing.gpu_detect import detect_gpus, format_gpu_list
from zaliver.processing.slicing_worker import (
    _max_concurrent_slice_jobs,
    _pick_random_tracks_for_jobs,
    _slice_music_try_order,
)
from zaliver.processing.pipeline import materialize_text_overlay_ranges
from zaliver.processing.ready_buffer import (
    buffer_from_options,
    settle_ready_job,
)
from zaliver.processing.stitching import (
    DEFAULT_STITCH_FPS_MODE,
    DEFAULT_STITCH_TRANSITION,
    DEFAULT_STITCH_TRANSITION_DURATION,
    STITCH_TRANSITION_LABELS,
    generate_stitched_video,
    normalize_stitch_transition,
)
from zaliver.processing.text_overlay import TextOverlaySettings

LogCallback = Callable[[str], None]

_UPLOAD_THROTTLE_STITCH_JOBS = 2


def _unique_stitch_filename(music_stem: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in music_stem)[:48]
    return f"{safe}_st_{secrets.token_hex(8)}.mp4"


@dataclass
class StitchJob:
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


def _attempt_stitch_with_music(
    music: str,
    output_path: Path,
    *,
    part1_pool: List[str],
    part2_pool: List[str],
    text_overlay_cfg: Optional[Dict[str, Any]],
    use_gpu: bool,
    use_gpu_finalize: bool,
    stitch_fps_mode: str,
    transition: str,
    transition_duration: float,
    transition_random: bool,
    log: LogCallback,
    cancel_check: Callable[[], bool],
    tag: str,
) -> Optional[str]:
    """Одна попытка склейки. None — успех, иначе текст ошибки."""
    if cancel_check():
        return "Отменено."

    try:
        result = generate_stitched_video(
            music,
            str(output_path),
            part1_pool=part1_pool,
            part2_pool=part2_pool,
            fps=stitch_fps_mode,
            log=log,
            use_gpu=bool(use_gpu),
            use_gpu_finalize=bool(use_gpu_finalize),
            text_overlay_cfg=text_overlay_cfg,
            transition=transition,
            transition_duration=transition_duration,
            transition_random=bool(transition_random),
        )
        if not result or not output_path.is_file():
            return "Не удалось склеить видео из исходников."
        return None
    except Exception as e:
        err = str(e).strip() or repr(e)
        log(f"{tag}: ошибка — {err}")
        return err


def _run_stitch_job(
    job: StitchJob,
    *,
    music_pool: List[str],
    part1_pool: List[str],
    part2_pool: List[str],
    text_overlay_cfg: Optional[Dict[str, Any]],
    use_gpu: bool,
    use_gpu_finalize: bool,
    stitch_fps_mode: str,
    transition: str,
    transition_duration: float,
    transition_random: bool,
    log: LogCallback,
    cancel_check: Callable[[], bool],
    n_jobs: int,
) -> StitchJob:
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
            log(f"{tag}: анализ ритма и склейка…")
        elif attempt_no == 1:
            log(f"{tag}: повтор с {music_path.name}…")
        else:
            log(f"{tag}: другой трек — {music_path.name}…")

        err = _attempt_stitch_with_music(
            str(music_path),
            job.output_path,
            part1_pool=part1_pool,
            part2_pool=part2_pool,
            text_overlay_cfg=text_overlay_cfg,
            use_gpu=use_gpu,
            use_gpu_finalize=use_gpu_finalize,
            stitch_fps_mode=stitch_fps_mode,
            transition=transition,
            transition_duration=transition_duration,
            transition_random=transition_random,
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

    job.error = last_error or "Не удалось выполнить склейку."
    job.skip_youtube_upload = True
    return job


class StitchingService:
    """Stitching pipeline. Bind a JobProgressSink (Qt or web)."""

    def __init__(self, sink: JobProgressSink | None = None) -> None:
        self._sink = sink or JobProgressSink()
        self._cancelled = False
        self._upload_throttle = threading.Event()
        self._upload_throttle_logged = False
        self._ready_buffer = None

    def cancel(self) -> None:
        self._cancelled = True
        buf = getattr(self, "_ready_buffer", None)
        if buf is not None:
            buf.close()

    def set_upload_throttle(self, enabled: bool) -> None:
        if enabled:
            self._upload_throttle.set()
        else:
            self._upload_throttle.clear()
            self._upload_throttle_logged = False

    def release_ready_buffer_path(self, path: str) -> None:
        buf = getattr(self, "_ready_buffer", None)
        if buf is not None:
            buf.release_path(path)

    def run(self, options: Dict[str, Any]) -> None:
        def safe_log(msg: str) -> None:
            try:
                self._sink.on_log(msg)
            except RuntimeError:
                pass

        log: LogCallback = safe_log
        self._cancelled = False
        self._upload_throttle.clear()
        self._upload_throttle_logged = False
        self._ready_buffer = buffer_from_options(options)
        ready_buf = self._ready_buffer
        if ready_buf is not None:
            log(
                f"Буфер готовых видео: максимум {ready_buf.limit} сделанных, "
                "но ещё не залитых (профили×2). Когда слот освобождается — "
                "обрабатывается следующее."
            )

        try:
            out_dir = Path(options["output_dir"])
            part1_pool: List[str] = []
            for x in options.get("part1_files") or []:
                p = Path(str(x))
                if p.is_file():
                    part1_pool.append(str(p.resolve()))
            part2_pool: List[str] = []
            for x in options.get("part2_files") or []:
                p = Path(str(x))
                if p.is_file():
                    part2_pool.append(str(p.resolve()))
            music_pool: List[str] = []
            for x in options.get("music_files") or []:
                p = Path(str(x))
                if p.is_file():
                    music_pool.append(str(p.resolve()))

            if not part1_pool:
                self._sink.on_finished(
                    False, "Выберите хотя бы одно видео для первой части."
                )
                return
            if not part2_pool:
                self._sink.on_finished(
                    False, "Выберите хотя бы одно видео для второй части."
                )
                return
            if not music_pool:
                self._sink.on_finished(
                    False, "Добавьте хотя бы один аудиотрек для склейки."
                )
                return
            if not check_ffmpeg_tools():
                self._sink.on_finished(False, "Нужны ffmpeg и ffprobe в PATH.")
                return

            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                self._sink.on_finished(False, f"Не удалось создать выходную папку: {e}")
                return

            text_overlay_enabled = False
            raw_text_overlay = options.get("text_overlay")
            if isinstance(raw_text_overlay, dict):
                toc = TextOverlaySettings.from_dict(raw_text_overlay)
                text_overlay_enabled = bool(toc.enabled and (toc.text or "").strip())
                if text_overlay_enabled:
                    try:
                        from zaliver.processing.text_overlay import (
                            prefetch_color_emoji_for_text,
                        )

                        prefetch_color_emoji_for_text(toc.text)
                    except Exception:
                        pass
            if text_overlay_enabled and not ffmpeg_has_drawtext():
                self._sink.on_finished(False, ffmpeg_drawtext_missing_user_message())
                return

            def _text_overlay_for_job() -> Optional[Dict[str, Any]]:
                if not text_overlay_enabled or not isinstance(raw_text_overlay, dict):
                    return None
                sampled = materialize_text_overlay_ranges(raw_text_overlay)
                if not sampled:
                    return None
                toc = TextOverlaySettings.from_dict(sampled)
                if toc.enabled and (toc.text or "").strip():
                    return toc.to_dict()
                return None

            output_count = max(1, int(options.get("copies_per_track", 1)))
            num_workers = max(1, int(options.get("num_workers", 1)))
            use_gpu = bool(options.get("use_gpu", False))
            use_gpu_finalize = bool(options.get("use_gpu_finalize", False))
            stitch_fps_mode = str(
                options.get("slice_fps_mode")
                or options.get("stitch_fps_mode")
                or DEFAULT_STITCH_FPS_MODE
            )
            if stitch_fps_mode.strip().lower() in ("auto", "авто"):
                stitch_fps_mode = DEFAULT_STITCH_FPS_MODE
            transition = normalize_stitch_transition(
                options.get("transition") or options.get("stitch_transition")
            )
            transition_random = bool(
                options.get("transition_random")
                or options.get("stitch_transition_random")
            )
            try:
                transition_duration = float(
                    options.get("transition_duration")
                    if options.get("transition_duration") is not None
                    else options.get(
                        "stitch_transition_duration",
                        DEFAULT_STITCH_TRANSITION_DURATION,
                    )
                )
            except (TypeError, ValueError):
                transition_duration = DEFAULT_STITCH_TRANSITION_DURATION
            if (
                not transition_random
                and transition == DEFAULT_STITCH_TRANSITION
            ):
                transition_duration = 0.0
            elif transition_random:
                # Для эффектов нужен overlap; cut сам обнулит внутри.
                transition_duration = max(
                    float(DEFAULT_STITCH_TRANSITION_DURATION),
                    float(transition_duration),
                )

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

            n_tracks = len(music_pool)
            assigned_tracks = _pick_random_tracks_for_jobs(music_pool, output_count)
            use_totals: Dict[str, int] = {}
            for music in assigned_tracks:
                key = os.path.normcase(music)
                use_totals[key] = use_totals.get(key, 0) + 1
            use_seen: Dict[str, int] = {}
            jobs: List[StitchJob] = []
            for i, music in enumerate(assigned_tracks):
                key = os.path.normcase(music)
                use_seen[key] = use_seen.get(key, 0) + 1
                stem = Path(music).stem
                outp = out_dir / _unique_stitch_filename(stem)
                jobs.append(
                    StitchJob(
                        job_idx=i + 1,
                        music_path=Path(music),
                        copy_index=use_seen[key],
                        track_use_total=use_totals[key],
                        output_path=outp,
                    )
                )

            n_jobs = len(jobs)
            if n_jobs == 0:
                self._sink.on_finished(False, "Нет заданий для склейки.")
                return

            repeat_videos = max(0, output_count - n_tracks)
            if repeat_videos:
                log(
                    f"Склейка: {output_count} роликов из {n_tracks} треков "
                    f"({repeat_videos} с повторением треков), "
                    f"часть1={len(part1_pool)}, часть2={len(part2_pool)}, "
                    f"потоков {num_workers}"
                )
            else:
                log(
                    f"Склейка: {output_count} роликов из {n_tracks} треков, "
                    f"часть1={len(part1_pool)}, часть2={len(part2_pool)}, "
                    f"потоков {num_workers}"
                )
            trans_label = STITCH_TRANSITION_LABELS.get(transition, transition)
            if transition_random:
                log(
                    "Хронометраж: сумма полных исходников; переход на бит; "
                    "эффект случайный из всех переходов (включая простую склейку); "
                    "при нехватке музыки — старт ~10% и/или зацикливание хвоста"
                )
            else:
                log(
                    "Хронометраж: сумма полных исходников; переход на бит; "
                    f"эффект «{trans_label}»"
                    + (
                        f" ({transition_duration:.2f}с)"
                        if transition != DEFAULT_STITCH_TRANSITION
                        else ""
                    )
                    + "; при нехватке музыки — старт ~10% и/или зацикливание хвоста"
                )
            fps_label = {"30": "30", "60": "60"}.get(
                stitch_fps_mode.strip().lower(), stitch_fps_mode
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
                    f"(в UI потоков {num_workers})."
                )

            def cancelled() -> bool:
                return self._cancelled

            done_count = 0
            errors: List[str] = []

            def on_job_done(j: StitchJob) -> None:
                nonlocal done_count
                done_count += 1
                self._sink.on_progress(done_count, n_jobs, j.output_path.name)
                keep = bool(
                    j.finished
                    and j.output_path.is_file()
                    and not j.skip_youtube_upload
                )
                if j.finished and j.output_path.is_file():
                    try:
                        self._sink.on_output_saved(
                            str(j.output_path.resolve()), not j.skip_youtube_upload
                        )
                    except RuntimeError:
                        pass
                elif j.error and j.error != "Отменено.":
                    errors.append(f"{j.music_path.name}: {j.error}")
                settle_ready_job(
                    ready_buf,
                    str(j.output_path.resolve()) if keep else "",
                    keep=keep,
                )

            job_kwargs = dict(
                music_pool=music_pool,
                part1_pool=part1_pool,
                part2_pool=part2_pool,
                use_gpu=use_gpu,
                use_gpu_finalize=use_gpu_finalize,
                stitch_fps_mode=stitch_fps_mode,
                transition=transition,
                transition_duration=transition_duration,
                transition_random=transition_random,
                log=log,
                cancel_check=cancelled,
                n_jobs=n_jobs,
            )

            if max_concurrent <= 1 or n_jobs == 1:
                for job in jobs:
                    if cancelled():
                        self._sink.on_finished(False, "Отменено.")
                        return
                    if ready_buf is not None and not ready_buf.acquire(
                        cancelled, log=log
                    ):
                        self._sink.on_finished(False, "Отменено.")
                        return
                    _run_stitch_job(job, text_overlay_cfg=_text_overlay_for_job(), **job_kwargs)
                    on_job_done(job)
            else:
                with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
                    fut_map: Dict[Future, StitchJob] = {}
                    pending = list(jobs)

                    def _stitch_slot_limit() -> int:
                        if self._upload_throttle.is_set():
                            if not self._upload_throttle_logged:
                                self._upload_throttle_logged = True
                                log(
                                    "Залив параллельно со склейкой: параллельность "
                                    f"сведена к {_UPLOAD_THROTTLE_STITCH_JOBS}."
                                )
                            return _UPLOAD_THROTTLE_STITCH_JOBS
                        return max_concurrent

                    while pending or fut_map:
                        if cancelled():
                            for f in fut_map:
                                f.cancel()
                            self._sink.on_finished(False, "Отменено.")
                            return
                        if ready_buf is not None:
                            ready_buf.reclaim()
                        while pending and len(fut_map) < _stitch_slot_limit():
                            if ready_buf is not None and not ready_buf.try_acquire():
                                break
                            job = pending.pop(0)
                            fut = pool.submit(
                                _run_stitch_job,
                                job,
                                text_overlay_cfg=_text_overlay_for_job(),
                                **job_kwargs,
                            )
                            fut_map[fut] = job
                        if not fut_map:
                            if pending and ready_buf is not None:
                                if not ready_buf.wait_has_room(cancelled, log=log):
                                    self._sink.on_finished(False, "Отменено.")
                                    return
                                continue
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
                self._sink.on_finished(False, "Отменено.")
                return
            if done_count < n_jobs:
                self._sink.on_finished(False, "Не все ролики обработаны.")
                return
            if errors and done_count == len(errors):
                self._sink.on_finished(False, errors[0])
                return
            if errors:
                log("Часть роликов завершилась с ошибками:")
                for line in errors:
                    log(f"  • {line}")
            self._sink.on_finished(True, "")
        except Exception as e:
            self._sink.on_finished(False, str(e).strip() or repr(e))
        finally:
            buf = getattr(self, "_ready_buffer", None)
            if buf is not None:
                buf.close()
            self._ready_buffer = None
