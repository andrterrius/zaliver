"""Two-part video stitch: full source clips, cut locked to a music beat."""

from __future__ import annotations

import os
import random
from dataclasses import dataclass
from typing import Optional

from zaliver.processing.slicing import (
    DEFAULT_EDGE_EXCLUDE,
    DEFAULT_SLICE_FPS_MODE,
    LogCallback,
    detect_all_audio_peaks,
    detect_rhythm_peaks,
    generate_video_from_segment,
    get_media_duration,
    load_audio_for_analysis,
)

# Kept for settings/API compatibility (длительность теперь = сумма исходников).
DEFAULT_MIN_PART_DURATION = 2.0
DEFAULT_MAX_PART_DURATION = 6.0
DEFAULT_STITCH_EDGE_EXCLUDE = DEFAULT_EDGE_EXCLUDE
DEFAULT_STITCH_FPS_MODE = DEFAULT_SLICE_FPS_MODE

# Визуальные переходы часть1→часть2 (ffmpeg xfade, кроме cut).
STITCH_TRANSITION_CUT = "cut"
STITCH_TRANSITION_FADE = "fade"
STITCH_TRANSITION_CIRCLE = "circleopen"
STITCH_TRANSITION_ZOOM = "zoomin"
STITCH_TRANSITION_FLASH = "fadewhite"
STITCH_TRANSITION_WHIP = "hblur"
STITCH_TRANSITIONS = (
    STITCH_TRANSITION_CUT,
    STITCH_TRANSITION_FADE,
    STITCH_TRANSITION_CIRCLE,
    STITCH_TRANSITION_ZOOM,
    STITCH_TRANSITION_FLASH,
    STITCH_TRANSITION_WHIP,
)
STITCH_TRANSITION_LABELS = {
    STITCH_TRANSITION_CUT: "Простая склейка",
    STITCH_TRANSITION_FADE: "Растворение",
    STITCH_TRANSITION_CIRCLE: "Круговое раскрытие",
    STITCH_TRANSITION_ZOOM: "Зум-удар",
    STITCH_TRANSITION_FLASH: "Вспышка",
    STITCH_TRANSITION_WHIP: "Whip-смаз",
}
DEFAULT_STITCH_TRANSITION = STITCH_TRANSITION_CUT
DEFAULT_STITCH_TRANSITION_DURATION = 0.40


def normalize_stitch_transition(value: object) -> str:
    key = str(value or "").strip().lower()
    aliases = {
        "": STITCH_TRANSITION_CUT,
        "none": STITCH_TRANSITION_CUT,
        "hard": STITCH_TRANSITION_CUT,
        "concat": STITCH_TRANSITION_CUT,
        "dissolve": STITCH_TRANSITION_FADE,
        "crossfade": STITCH_TRANSITION_FADE,
        "circle": STITCH_TRANSITION_CIRCLE,
        "circleopen": STITCH_TRANSITION_CIRCLE,
        "zoom": STITCH_TRANSITION_ZOOM,
        "zoomin": STITCH_TRANSITION_ZOOM,
        "punch": STITCH_TRANSITION_ZOOM,
        "flash": STITCH_TRANSITION_FLASH,
        "fadewhite": STITCH_TRANSITION_FLASH,
        "whiteflash": STITCH_TRANSITION_FLASH,
        "whip": STITCH_TRANSITION_WHIP,
        "hblur": STITCH_TRANSITION_WHIP,
        "blur": STITCH_TRANSITION_WHIP,
    }
    if key in STITCH_TRANSITIONS:
        return key
    return aliases.get(key, DEFAULT_STITCH_TRANSITION)


def clamp_stitch_transition_duration(
    duration: float,
    part1_duration: float,
    part2_duration: float,
) -> float:
    """Перекрытие xfade не длиннее ~45% каждой части."""
    d = max(0.0, float(duration))
    if d <= 1e-6:
        return 0.0
    cap = min(float(part1_duration), float(part2_duration)) * 0.45
    return max(0.05, min(d, cap)) if cap >= 0.05 else 0.0


@dataclass
class MusicAlignPlan:
    music_start: float
    beat_time: float
    loop_audio: bool
    part1_duration: float
    part2_duration: float
    total_duration: float
    strategy: str


def _peak_list(audio_data, sample_rate: int) -> list[tuple[float, float]]:
    rhythm_times, rhythm_amps = detect_rhythm_peaks(audio_data, sample_rate)
    _, all_times, all_amps, _ = detect_all_audio_peaks(
        audio_data, sample_rate, quiet=True
    )
    merged: dict[float, float] = {}
    for t, a in zip(rhythm_times, rhythm_amps):
        merged[float(t)] = max(merged.get(float(t), 0.0), float(a))
    for t, a in zip(all_times, all_amps):
        key = float(t)
        merged[key] = max(merged.get(key, 0.0), float(a) * 0.85)
    return sorted(merged.items(), key=lambda x: x[0])


def _pick_random_existing(pool: list[str]) -> Optional[str]:
    files = [p for p in pool if p and os.path.isfile(p)]
    if not files:
        return None
    return random.choice(files)


def plan_music_for_stitch(
    audio_file: str,
    part1_duration: float,
    part2_duration: float,
    *,
    transition_overlap: float = 0.0,
    log: Optional[LogCallback] = None,
) -> Optional[MusicAlignPlan]:
    """
    Музыка с начала песни (или почти с начала).

    Переход часть1→часть2 должен попасть на бит: ищем сильный бит около
    момента смены (середина xfade или стык cut) от начала ролика,
    тогда ``music_start = beat - anchor ≈ 0``.

    Продление после EOF — только если ролик длиннее остатка трека.
    """
    def _log(msg: str) -> None:
        if log is not None:
            log(msg)

    d1 = max(0.05, float(part1_duration))
    d2 = max(0.05, float(part2_duration))
    overlap = max(0.0, float(transition_overlap))
    if overlap >= min(d1, d2) - 1e-3:
        overlap = 0.0
    total = d1 + d2 - overlap
    # Бит на середине визуального перехода (для cut overlap=0 → якорь = d1).
    beat_anchor = d1 - overlap * 0.5

    sample_rate, audio_data, music_duration = load_audio_for_analysis(audio_file)
    if audio_data is None or music_duration is None or music_duration <= 0:
        _log("Не удалось прочитать аудио для склейки.")
        return None

    container_dur = get_media_duration(audio_file)
    if container_dur is not None and container_dur > 0:
        music_duration = min(float(music_duration), float(container_dur))

    peaks = _peak_list(audio_data, sample_rate)
    # Идеальная точка бита: через beat_anchor секунд от начала песни → старт ≈ 0.
    target_beat = beat_anchor
    # Допустимый сдвиг старта от начала трека (чтобы поймать более сильный бит).
    max_music_start = max(15.0, music_duration * 0.20)
    # Окно поиска бита после target (не уводим старт далеко вглубь трека).
    search_hi = min(
        music_duration - 0.05,
        beat_anchor + max(15.0, music_duration * 0.20),
    )

    usable: list[tuple[float, float, float]] = []  # score, beat, start
    for beat, amp in peaks:
        b = float(beat)
        if b + 1e-6 < beat_anchor:
            continue
        start = b - beat_anchor
        if start < -1e-3:
            continue
        start = max(0.0, start)
        # Штрафуем поздний старт — но разрешаем чуть дальше от нуля.
        if start > max_music_start:
            continue
        if b > search_hi + 1e-6:
            # Поздние биты только если совсем нет ранних.
            dist_pen = 1.0 + (b - search_hi) / max(1.0, music_duration)
        else:
            dist_pen = 1.0 + abs(b - target_beat) / max(1.0, beat_anchor * 0.5 + 2.0)
        # Мягче штраф за сдвиг: сильный бит на 2–8с важнее, чем старт ровно с 0.
        score = float(amp) / dist_pen / (1.0 + start * 0.55)
        usable.append((score, b, start))

    if not usable:
        # Любой бит после якоря, с минимальным start.
        for beat, amp in peaks:
            b = float(beat)
            if b + 1e-6 < beat_anchor:
                continue
            start = max(0.0, b - beat_anchor)
            score = float(amp) / (1.0 + start)
            usable.append((score, b, start))

    if usable:
        usable.sort(key=lambda x: x[0], reverse=True)
        # Среди топа предпочитаем самый ранний старт при близком score.
        top = usable[: min(12, len(usable))]
        best_score = top[0][0]
        early_pool = [c for c in top if c[0] >= best_score * 0.72]
        _score, beat, start = min(early_pool, key=lambda x: (x[2], -x[0]))
        fits = start + total <= music_duration + 1e-3
        plan = MusicAlignPlan(
            music_start=float(start),
            beat_time=float(beat),
            loop_audio=not fits,
            part1_duration=d1,
            part2_duration=d2,
            total_duration=total,
            strategy="start_near_zero_beat" if fits else "start_near_zero_extend",
        )
        _log(
            f"Музыка с {plan.music_start:.3f}с (начало трека), "
            f"переход на бите {plan.beat_time:.3f}с "
            f"(через {beat_anchor:.2f}с ролика)"
            + ("." if fits else " — после конца песни продлим с начала.")
        )
        return plan

    # Нет пиков: старт с 0, «бит» условно на якоре.
    start = 0.0
    fits = total <= music_duration + 1e-3
    plan = MusicAlignPlan(
        music_start=start,
        beat_time=min(beat_anchor, max(0.1, music_duration * 0.5)),
        loop_audio=not fits,
        part1_duration=d1,
        part2_duration=d2,
        total_duration=total,
        strategy="start_zero_plain",
    )
    _log(
        f"Пики не найдены — музыка с 0с, переход около {plan.beat_time:.3f}с"
        + ("." if fits else " (продление после EOF).")
    )
    return plan


def generate_stitched_video(
    audio_file: str,
    output_video: str,
    *,
    part1_pool: list[str],
    part2_pool: list[str],
    min_part_duration: float = DEFAULT_MIN_PART_DURATION,
    max_part_duration: float = DEFAULT_MAX_PART_DURATION,
    edge_exclude: float = DEFAULT_STITCH_EDGE_EXCLUDE,
    fps=DEFAULT_STITCH_FPS_MODE,
    log: Optional[LogCallback] = None,
    use_gpu: bool = False,
    use_gpu_finalize: bool = False,
    text_overlay_cfg: dict | None = None,
    transition: str = DEFAULT_STITCH_TRANSITION,
    transition_duration: float = DEFAULT_STITCH_TRANSITION_DURATION,
    transition_random: bool = False,
) -> Optional[str]:
    """
    Склейка: полный клип из пула 1 + полный клип из пула 2.
    Длительность ролика = d1 + d2 (− перекрытие xfade). Переход на бит музыки.
    """
    del min_part_duration, max_part_duration, edge_exclude  # API/compat, unused

    def _log(msg: str) -> None:
        if log is not None:
            log(msg)

    clip1 = _pick_random_existing(part1_pool)
    clip2 = _pick_random_existing(part2_pool)
    if clip1 is None:
        _log("Нет видеофайлов для части 1.")
        return None
    if clip2 is None:
        _log("Нет видеофайлов для части 2.")
        return None

    d1 = get_media_duration(clip1)
    d2 = get_media_duration(clip2)
    if d1 is None or d1 <= 0.05:
        _log(f"Не удалось определить длительность: {os.path.basename(clip1)}")
        return None
    if d2 is None or d2 <= 0.05:
        _log(f"Не удалось определить длительность: {os.path.basename(clip2)}")
        return None

    d1 = float(d1)
    d2 = float(d2)
    if transition_random:
        transition_id = random.choice(list(STITCH_TRANSITIONS))
        _log(
            "Случайный переход: "
            f"«{STITCH_TRANSITION_LABELS.get(transition_id, transition_id)}»"
        )
    else:
        transition_id = normalize_stitch_transition(transition)
    overlap = 0.0
    if transition_id != STITCH_TRANSITION_CUT:
        overlap = clamp_stitch_transition_duration(
            float(transition_duration), d1, d2
        )
        if overlap <= 1e-6:
            _log(
                "Клипы слишком короткие для выбранного перехода — "
                "используется простая склейка."
            )
            transition_id = STITCH_TRANSITION_CUT
            overlap = 0.0
    total = d1 + d2 - overlap
    label = STITCH_TRANSITION_LABELS.get(transition_id, transition_id)
    _log(
        f"Исходники целиком: часть1={os.path.basename(clip1)} ({d1:.2f}с), "
        f"часть2={os.path.basename(clip2)} ({d2:.2f}с), "
        f"переход «{label}»"
        + (f" {overlap:.2f}с, " if overlap > 0 else ", ")
        + f"итого {total:.2f}с"
    )

    plan = plan_music_for_stitch(
        audio_file, d1, d2, transition_overlap=overlap, log=log
    )
    if plan is None:
        return None

    segment = {
        "start_time": float(plan.music_start),
        "end_time": float(plan.music_start + total),
        "duration": float(total),
        "transitions": [{"time": float(plan.beat_time), "amplitude": 1.0}],
        "scene_durations": [float(d1), float(d2)],
        "num_transitions": 1,
        "num_scenes": 2,
        "min_interval": 0.05,
        "transition_time": float(plan.beat_time),
        "loop_audio": bool(plan.loop_audio),
        # Склейка: не укорачивать/не лупить видео под аудио-snap.
        "stitch_preserve_full_clips": True,
        "stitch_transition": transition_id,
        "stitch_transition_duration": float(overlap),
    }

    fixed_clips = [
        {
            "path": clip1,
            "start": 0.0,
            "duration": float(d1),
            "loop": False,
        },
        {
            "path": clip2,
            "start": 0.0,
            "duration": float(d2),
            "loop": False,
        },
    ]

    return generate_video_from_segment(
        audio_file,
        segment,
        output_video,
        fixed_scene_clips=fixed_clips,
        fps=fps,
        log=log,
        use_gpu=use_gpu,
        use_gpu_finalize=use_gpu_finalize,
        text_overlay_cfg=text_overlay_cfg,
        loop_audio=bool(plan.loop_audio),
    )
