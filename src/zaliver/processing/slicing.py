"""Audio-peak video slicing: random scene cuts synced to music."""

from __future__ import annotations

import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import numpy as np
import scipy.io.wavfile as wavfile
from scipy.ndimage import gaussian_filter1d
from scipy.signal import find_peaks

from zaliver.processing.ffmpeg_gpu import (
    GpuPipeline,
    gpu_pipeline_label,
    is_gpu_filter_fallback_error,
    resolve_gpu_pipeline,
)
from zaliver.processing.ffmpeg_merge import pick_best_h264_encoder, run_ffmpeg
from zaliver.processing.ffmpeg_probe import ffprobe_json, probe_media_duration_seconds
from zaliver.processing.text_overlay import (
    ScaledTextOverlay,
    TextOverlaySettings,
    build_text_overlay_filters,
    compute_scaled_overlay,
)
from zaliver.processing.worker import _filter_complex_argv

SAMPLE_RATE = 16000
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}

LogCallback = Callable[[str], None]

# Defaults for UI / batch slicing (see slicing.py __main__ for CLI).
DEFAULT_MIN_SCENES = 12
DEFAULT_MAX_SCENES = 23
DEFAULT_EDGE_EXCLUDE = 0.05
DEFAULT_MIN_SCENE_DURATION = 1.0
DEFAULT_MAX_SCENE_DURATION = 1.3
DEFAULT_SLICE_FPS = 30
DEFAULT_SLICE_FPS_MODE = "30"
SLICE_SCENE_BATCH_SIZE = 5
SLICE_GPU_MAX_CONCURRENT_BATCHES = 2
SLICE_ENCODE_CRF = 16
SLICE_ENCODE_GPU_CQ = 19
SLICE_ENCODE_VIDEOTOOLBOX_Q = 75


def _popen_flags() -> int:
    from zaliver.processing.subprocess_flags import popen_creationflags

    return popen_creationflags()


def _log(msg: str, log: Optional[LogCallback] = None) -> None:
    if log is not None:
        log(msg)
    else:
        print(msg)


def read_audio_file(filename):
    """Читает аудио файл"""
    try:
        sample_rate, audio_data = wavfile.read(filename)

        if len(audio_data.shape) > 1:
            audio_data = np.mean(audio_data, axis=1)

        if audio_data.dtype == np.int16:
            audio_data = audio_data / 32768.0
        elif audio_data.dtype == np.int32:
            audio_data = audio_data / 2147483648.0

        return sample_rate, audio_data.astype(np.float32)

    except Exception as e:
        print(f"Не удалось прочитать как WAV: {e}")
        return None, None


def extract_features(audio_data, sample_rate, hop_length=512):
    """Извлекает аудио-фичи для анализа структуры"""

    envelope = np.abs(audio_data)
    envelope_smooth = gaussian_filter1d(envelope, sigma=100)

    n_fft = 2048
    hop = 512
    spectral_centroids = []

    for i in range(0, len(audio_data) - n_fft, hop):
        segment = audio_data[i:i + n_fft]
        spectrum = np.abs(np.fft.fft(segment))
        freqs = np.fft.fftfreq(n_fft, 1 / sample_rate)
        centroid = np.sum(freqs[:n_fft // 2] * spectrum[:n_fft // 2]) / np.sum(spectrum[:n_fft // 2] + 1e-10)
        spectral_centroids.append(centroid)

    spectral_centroids = np.array(spectral_centroids)
    spectral_centroids = np.interp(
        np.linspace(0, len(spectral_centroids), len(audio_data)),
        np.arange(len(spectral_centroids)),
        spectral_centroids
    )

    energy_delta = np.diff(envelope_smooth, prepend=envelope_smooth[0])
    energy_delta = gaussian_filter1d(np.abs(energy_delta), sigma=50)

    return {
        'envelope': envelope_smooth,
        'spectral_centroid': spectral_centroids,
        'energy_delta': energy_delta,
        'time': np.arange(len(audio_data)) / sample_rate
    }


def detect_all_audio_peaks(audio_data, sample_rate, quiet=False):
    """
    Детектирует все пики в аудио, включая наименьшие колебания.
    Объединяет пики огибающей, атак энергии и микроколебаний сигнала.
    """
    time = np.arange(len(audio_data)) / sample_rate
    min_distance = max(1, int(sample_rate * 0.05))  # 20 мс между пиками

    envelope = np.abs(audio_data)
    env_smooth = gaussian_filter1d(envelope, sigma=max(1, int(sample_rate * 0.002)))

    all_peak_indices = set()

    # 1. Пики огибающей — очень низкий порог
    env_std = np.std(env_smooth)
    env_mean = np.mean(env_smooth)
    peaks_env, _ = find_peaks(
        env_smooth,
        height=env_mean * 0.03,
        distance=min_distance,
        prominence=env_std * 0.01,
    )
    all_peak_indices.update(peaks_env)

    # 2. Пики прироста энергии (атаки / нарастания)
    energy_delta = np.diff(env_smooth, prepend=env_smooth[0])
    energy_rise = np.maximum(energy_delta, 0)
    rise_std = np.std(energy_rise)
    peaks_rise, _ = find_peaks(
        energy_rise,
        height=np.percentile(energy_rise, 5),
        distance=min_distance,
        prominence=rise_std * 0.02 if rise_std > 0 else 0,
    )
    all_peak_indices.update(peaks_rise)

    # 3. Пики амплитуды исходного сигнала (микроколебания)
    abs_signal = np.abs(audio_data)
    sig_std = np.std(abs_signal)
    peaks_sig, _ = find_peaks(
        abs_signal,
        height=np.percentile(abs_signal, 8),
        distance=min_distance,
        prominence=sig_std * 0.02 if sig_std > 0 else 0,
    )
    all_peak_indices.update(peaks_sig)

    # 4. Пики спада→роста (нулевые пересечения с производной)
    peaks_deriv, _ = find_peaks(
        -np.diff(env_smooth, prepend=env_smooth[0]),
        height=np.percentile(np.abs(np.diff(env_smooth)), 5),
        distance=min_distance,
    )
    all_peak_indices.update(peaks_deriv)

    peak_indices = sorted(all_peak_indices)
    peak_times = time[peak_indices]
    peak_amplitudes = env_smooth[peak_indices]

    if not quiet:
        print(f"    Пики огибающей: {len(peaks_env)}")
        print(f"    Пики атак энергии: {len(peaks_rise)}")
        print(f"    Пики амплитуды: {len(peaks_sig)}")
        print(f"    Всего уникальных пиков: {len(peak_times)}")

    return peak_indices, peak_times, peak_amplitudes, env_smooth


def build_peak_chain(peaks, amplitudes, start_idx, min_scene_duration, max_scene_duration,
                       max_peaks, pick='strongest', exact_peaks=None):
    """
    Строит цепочку пиков для смены кадров.
    Каждый следующий пик выбирается из окна [min_scene, max_scene] после предыдущего.
    exact_peaks — точное число пиков (смен кадра); иначе до max_peaks.
    """
    target_peaks = exact_peaks if exact_peaks is not None else max_peaks
    chain = [start_idx]
    search_from = start_idx + 1

    while len(chain) < target_peaks and search_from < len(peaks):
        window_lo = peaks[chain[-1]] + min_scene_duration
        window_hi = peaks[chain[-1]] + max_scene_duration

        while search_from < len(peaks) and peaks[search_from] < window_lo:
            search_from += 1

        best_idx = None
        best_amp = -1.0
        j = search_from
        while j < len(peaks) and peaks[j] <= window_hi:
            if pick == 'first':
                best_idx = j
                break
            if amplitudes[j] > best_amp:
                best_amp = amplitudes[j]
                best_idx = j
            j += 1

        if best_idx is None:
            break

        chain.append(best_idx)
        search_from = best_idx + 1

    return chain


def compute_scene_count_range(total_duration, min_scene_duration, min_scenes, max_scenes,
                              edge_exclude=0.10):
    """
    Вычисляет допустимый диапазон числа сцен с учётом длины всего аудио.
    N сцен → минимум N * min_scene_duration секунд.
    """
    usable = total_duration * (1.0 - 2.0 * edge_exclude) if edge_exclude > 0 else total_duration
    max_fit_audio = int(total_duration / min_scene_duration)
    max_fit_zone = int(usable / min_scene_duration)
    max_fit = min(max_fit_audio, max_fit_zone)

    if max_fit < 1:
        return None, None, 0

    lo = min(min_scenes, max_fit)
    hi = min(max_scenes, max_fit)
    if lo > hi:
        lo = hi

    return lo, hi, max_fit


def load_audio_for_analysis(audio_file):
    """Конвертирует аудио в WAV и возвращает (sample_rate, audio_data, total_duration)"""
    temp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    try:
        run_ffmpeg(
            ["-i", audio_file, "-ac", "1", "-ar", str(SAMPLE_RATE), temp_wav],
        )
    except Exception:
        return None, None, None

    sample_rate, audio_data = read_audio_file(temp_wav)
    os.unlink(temp_wav)

    if audio_data is None:
        return None, None, None

    total_duration = len(audio_data) / sample_rate
    return sample_rate, audio_data, total_duration


def _round_duration(value, step=0.05):
    return round(max(step, round(value / step) * step), 2)


def detect_rhythm_peaks(audio_data, sample_rate):
    """Грубая детекция ритмических пиков для оценки темпа трека"""
    time = np.arange(len(audio_data)) / sample_rate
    min_distance = max(1, int(sample_rate * 0.18))

    envelope = np.abs(audio_data)
    env_smooth = gaussian_filter1d(envelope, sigma=max(1, int(sample_rate * 0.008)))
    env_std = np.std(env_smooth)

    peaks, _ = find_peaks(
        env_smooth,
        height=np.percentile(env_smooth, 55),
        distance=min_distance,
        prominence=max(env_std * 0.12, np.percentile(env_smooth, 10) * 0.05),
    )

    return time[peaks], env_smooth[peaks]


def analyze_peak_intervals(peak_times, peak_amplitudes, valid_start, valid_end):
    """Возвращает интервалы между заметными пиками в допустимой зоне аудио"""
    peaks = []
    amps = []
    for peak_time, amplitude in zip(peak_times, peak_amplitudes):
        if valid_start <= peak_time <= valid_end:
            peaks.append(float(peak_time))
            amps.append(float(amplitude))

    if len(peaks) < 2:
        return None

    peaks = np.array(peaks)
    amps = np.array(amps)
    threshold = np.percentile(amps, 60)
    strong_peaks = peaks[amps >= threshold]

    if len(strong_peaks) < 2:
        strong_peaks = peaks

    intervals = np.diff(strong_peaks)
    intervals = intervals[(intervals >= 0.15) & (intervals <= 8.0)]
    if len(intervals) == 0:
        return None

    return intervals


def analyze_rhythm_intervals(audio_data, sample_rate, valid_start, valid_end):
    """Оценивает типичные интервалы между ритмическими ударами"""
    peak_times, peak_amplitudes = detect_rhythm_peaks(audio_data, sample_rate)
    return analyze_peak_intervals(peak_times, peak_amplitudes, valid_start, valid_end)


def suggest_scene_durations(
        audio_file,
        edge_exclude=0.0,
        min_scenes=12,
        max_scenes=18,
        verbose=True,
):
    """
    Анализирует аудио и предлагает MIN/MAX_SCENE_DURATION по интервалам между пиками.
    Возвращает dict с рекомендациями или None при ошибке.
    """
    sample_rate, audio_data, total_duration = load_audio_for_analysis(audio_file)
    if audio_data is None:
        if verbose:
            print("Не удалось проанализировать аудио для рекомендаций")
        return None

    valid_start = total_duration * edge_exclude
    valid_end = total_duration * (1.0 - edge_exclude)

    intervals = analyze_rhythm_intervals(audio_data, sample_rate, valid_start, valid_end)
    if intervals is None:
        _, peak_times, peak_amplitudes, _ = detect_all_audio_peaks(
            audio_data, sample_rate, quiet=True,
        )
        intervals = analyze_peak_intervals(peak_times, peak_amplitudes, valid_start, valid_end)

    if intervals is None:
        if verbose:
            print("Недостаточно пиков для рекомендации длительности сцен")
        return None

    median_interval = float(np.median(intervals))
    min_scene_duration = _round_duration(float(np.percentile(intervals, 30)))
    max_scene_duration = _round_duration(float(np.percentile(intervals, 70)))

    if max_scene_duration <= min_scene_duration:
        max_scene_duration = _round_duration(min_scene_duration + median_interval * 0.5)

    min_scene_duration = max(0.3, min(min_scene_duration, 2.0))
    max_scene_duration = max(min_scene_duration + 0.15, min(max_scene_duration, 4.0))

    suggestion_boost = 1.25
    min_scene_duration = _round_duration(min_scene_duration * suggestion_boost)
    max_scene_duration = _round_duration(max_scene_duration * suggestion_boost)
    max_scene_duration = max(min_scene_duration + 0.15, max_scene_duration)

    estimated_bpm = 60.0 / median_interval if median_interval > 0 else None

    _, peak_times, _, _ = detect_all_audio_peaks(audio_data, sample_rate, quiet=True)

    scene_lo, scene_hi, max_fit = compute_scene_count_range(
        total_duration, min_scene_duration, min_scenes, max_scenes, edge_exclude,
    )

    result = {
        'min_scene_duration': min_scene_duration,
        'max_scene_duration': max_scene_duration,
        'median_peak_interval': _round_duration(median_interval),
        'estimated_bpm': round(estimated_bpm, 1) if estimated_bpm else None,
        'peak_count': len(peak_times),
        'strong_peak_intervals': len(intervals),
        'scene_count_range': (scene_lo, scene_hi),
        'max_scenes_fit': max_fit,
        'total_duration': total_duration,
    }

    if verbose:
        print("\n" + "=" * 60)
        print("РЕКОМЕНДАЦИИ ПО ДЛИТЕЛЬНОСТИ СЦЕН")
        print("=" * 60)
        print(f"Аудио: {audio_file} ({total_duration:.2f} сек)")
        print(f"Найдено пиков: {result['peak_count']}, интервалов между сильными: {result['strong_peak_intervals']}")
        if result['estimated_bpm']:
            print(
                f"Оценка темпа: ~{result['estimated_bpm']} BPM "
                f"(медианный интервал {result['median_peak_interval']:.2f} сек)"
            )
        print("\nРекомендуемые значения:")
        print(f"  MIN_SCENE_DURATION = {min_scene_duration}")
        print(f"  MAX_SCENE_DURATION = {max_scene_duration}")
        if scene_lo is not None:
            print(
                f"  При этих настройках влезет примерно {scene_lo}–{scene_hi} сцен "
                f"(максимум {max_fit})"
            )
        else:
            print("  ⚠️ Аудио слишком короткое для текущего MIN_SCENES — уменьшите число сцен")
        print("=" * 60)

    return result


def find_peak_based_segments(
        peak_times,
        peak_amplitudes,
        total_duration,
        min_scene_duration,
        max_scene_duration,
        min_scenes,
        max_scenes,
        edge_exclude=0.10,
):
    """
    Находит валидные сегменты: цепочки пиков с интервалами в [min_scene, max_scene].
    Сегменты только из средней зоны (без первых и последних edge_exclude%).
    """
    valid_start = total_duration * edge_exclude
    valid_end = total_duration * (1.0 - edge_exclude)

    peaks = [float(p) for p in peak_times if valid_start <= p <= valid_end]
    amps = [float(peak_amplitudes[i]) for i, p in enumerate(peak_times) if valid_start <= p <= valid_end]

    print(f"    Пиков в зоне {edge_exclude * 100:.0f}%-{100 - edge_exclude * 100:.0f}%: {len(peaks)}")

    min_peaks = min_scenes - 1
    max_peaks = max_scenes - 1

    if len(peaks) < min_peaks:
        print(f"  Недостаточно пиков: {len(peaks)} < {min_peaks}")
        return []

    candidates = []
    seen = set()

    start_indices = list(range(len(peaks)))
    if len(start_indices) > 3000:
        start_indices = sorted(random.sample(start_indices, 3000))

    for pick_mode in ('strongest', 'first'):
        for i in start_indices:
            chain = build_peak_chain(
                peaks, amps, i,
                min_scene_duration, max_scene_duration, max_peaks,
                pick=pick_mode,
                exact_peaks=min_peaks if min_scenes == max_scenes else None,
            )
            if len(chain) < min_peaks:
                continue
            if min_scenes == max_scenes and len(chain) != min_peaks:
                continue

            seq = tuple(peaks[idx] for idx in chain)
            if min_scenes != max_scenes and len(seq) > max_peaks:
                seq = seq[:max_peaks]
            if len(seq) < min_peaks:
                continue
            if seq in seen:
                continue

            start_lo = max(valid_start, seq[0] - max_scene_duration)
            start_hi = seq[0] - min_scene_duration
            end_lo = seq[-1] + min_scene_duration
            end_hi = min(valid_end, seq[-1] + max_scene_duration, total_duration)

            if start_lo > start_hi or end_lo > end_hi:
                continue

            min_segment_len = (len(seq) + 1) * min_scene_duration
            if min_segment_len > total_duration:
                continue

            seen.add(seq)
            candidates.append({
                'peak_times': list(seq),
                'start_range': (start_lo, start_hi),
                'end_range': (end_lo, end_hi),
                'num_scenes': len(seq) + 1,
            })

        if candidates:
            break

    print(f"    Найдено {len(candidates)} валидных сегментов")
    return candidates


def build_fallback_segment(
        peaks, amplitudes, valid_start, valid_end,
        min_scene_duration, max_scene_duration, min_scenes, max_scenes,
        total_duration=None, target_num_scenes=None,
):
    """
    Запасной поиск: случайная стартовая точка, в каждом окне [min, max] сек
    выбирается самый сильный пик. Работает даже при нерегулярной музыке.
    """
    if target_num_scenes is not None:
        min_scenes = max_scenes = target_num_scenes

    min_peaks = min_scenes - 1
    max_peaks = max_scenes - 1
    min_segment_len = min_scenes * min_scene_duration
    max_segment_len = max_scenes * max_scene_duration

    if total_duration is not None and min_segment_len > total_duration:
        return None

    for _ in range(500):
        num_peaks_target = random.randint(min_peaks, max_peaks) if min_peaks != max_peaks else min_peaks
        seg_len = random.uniform(
            num_peaks_target * min_scene_duration,
            num_peaks_target * max_scene_duration,
        )
        seg_len = min(seg_len, valid_end - valid_start - min_scene_duration)

        latest_start = valid_end - seg_len - min_scene_duration
        if latest_start < valid_start:
            continue

        start_time = random.uniform(valid_start, latest_start)
        end_time = start_time + seg_len

        transition_times = []
        cursor = start_time + random.uniform(min_scene_duration, max_scene_duration)

        while cursor < end_time - min_scene_duration and len(transition_times) < max_peaks:
            search_lo = cursor
            search_hi = min(cursor + max_scene_duration, end_time - min_scene_duration)

            best_time = None
            best_amp = -1.0
            for p, a in zip(peaks, amplitudes):
                if search_lo <= p <= search_hi:
                    if a > best_amp:
                        best_amp = a
                        best_time = p

            if best_time is None:
                break

            transition_times.append(best_time)
            cursor = best_time + random.uniform(min_scene_duration, max_scene_duration)

        if len(transition_times) < min_peaks:
            continue
        if min_scenes == max_scenes and len(transition_times) != min_peaks:
            continue

        seq = transition_times
        intervals_ok = all(
            min_scene_duration <= seq[k] - seq[k - 1] <= max_scene_duration
            for k in range(1, len(seq))
        )
        if not intervals_ok:
            continue

        start_lo = max(valid_start, seq[0] - max_scene_duration)
        start_hi = seq[0] - min_scene_duration
        end_lo = seq[-1] + min_scene_duration
        end_hi = min(valid_end, seq[-1] + max_scene_duration)
        if total_duration is not None:
            end_hi = min(end_hi, total_duration)

        if start_lo > start_hi or end_lo > end_hi:
            continue

        if total_duration is not None and (len(seq) + 1) * min_scene_duration > total_duration:
            continue

        return {
            'peak_times': seq,
            'start_range': (start_lo, start_hi),
            'end_range': (end_lo, end_hi),
            'num_scenes': len(seq) + 1,
        }

    return None


def build_segment_from_peaks(candidate, min_scene_duration, max_scene_duration, total_duration=None):
    """Собирает финальный сегмент со случайными границами в допустимых диапазонах."""
    start_lo, start_hi = candidate['start_range']
    end_lo, end_hi = candidate['end_range']
    peak_times = candidate['peak_times']

    if total_duration is not None:
        end_hi = min(end_hi, total_duration)
        end_lo = min(end_lo, total_duration)
        start_hi = min(start_hi, total_duration - min_scene_duration)
        start_lo = min(start_lo, start_hi)

    if start_lo > start_hi or end_lo > end_hi:
        return None

    start_time = random.uniform(start_lo, start_hi)
    min_end = max(end_lo, peak_times[-1] + min_scene_duration)
    max_end = end_hi
    if total_duration is not None:
        max_end = min(max_end, total_duration)
    if min_end > max_end:
        return None

    end_time = random.uniform(min_end, max_end)

    if total_duration is not None:
        latest_start = total_duration - (end_time - start_time)
        if latest_start < start_lo:
            return None
        if start_time > latest_start:
            start_time = latest_start
        if end_time > total_duration:
            end_time = total_duration
        if end_time - peak_times[-1] < min_scene_duration - 1e-3:
            return None
        if peak_times[0] - start_time < min_scene_duration - 1e-3:
            start_time = peak_times[0] - min_scene_duration
        if start_time < 0:
            return None

    transitions = [{'time': t, 'type': 'peak', 'source': 'peak'} for t in peak_times]

    scene_durations = [peak_times[0] - start_time]
    for k in range(1, len(peak_times)):
        scene_durations.append(peak_times[k] - peak_times[k - 1])
    scene_durations.append(end_time - peak_times[-1])

    if not validate_scene_durations(scene_durations, min_scene_duration, max_scene_duration):
        return None

    segment = {
        'start_time': start_time,
        'end_time': end_time,
        'duration': end_time - start_time,
        'num_transitions': len(transitions),
        'num_scenes': len(scene_durations),
        'transitions': transitions,
        'peak_times': peak_times,
        'scene_durations': scene_durations,
        'min_interval': min_scene_duration,
        'max_interval': max_scene_duration,
    }

    if total_duration is not None:
        segment = clamp_segment_to_audio(segment, total_duration)
        if segment is None:
            return None

    return segment


def validate_scene_durations(scene_durations, min_scene_duration, max_scene_duration=None):
    """Проверяет, что длительности сцен укладываются в допустимые границы"""
    for duration in scene_durations:
        if duration < min_scene_duration - 1e-3:
            return False
        if max_scene_duration is not None and duration > max_scene_duration + 1e-3:
            return False
    return True


def clamp_segment_to_audio(segment, audio_duration):
    """
    Гарантирует, что видео не длиннее доступного аудио.
    Сначала сдвигает начало назад, затем укорачивает только последнюю сцену.
    Не сжимает все сцены пропорционально — это давало слишком короткие кадры.
    """
    if not audio_duration:
        return segment

    min_scene_duration = segment.get('min_interval')
    start = segment['start_time']
    scene_durations = list(segment['scene_durations'])
    total = sum(scene_durations)

    if start >= audio_duration:
        return None

    available = audio_duration - start

    if total > available + 1e-6:
        shift = total - available
        new_start = max(0.0, start - shift)
        if new_start < start:
            delta = start - new_start
            start = new_start
            scene_durations[0] += delta
            available = audio_duration - start
            print(
                f"    Начало сдвинуто на {start:.3f}с, первая сцена удлинена до "
                f"{scene_durations[0]:.3f}с"
            )

    total = sum(scene_durations)
    if total > available + 1e-6:
        excess = total - available
        last_duration = scene_durations[-1]
        trimmed_last = max(
            min_scene_duration if min_scene_duration else 0.0,
            last_duration - excess,
        )

        if min_scene_duration and trimmed_last < min_scene_duration - 1e-3:
            print(
                f"    Сегмент не влезает в аудио без нарушения MIN_SCENE_DURATION "
                f"({min_scene_duration}с)"
            )
            return None

        if trimmed_last < last_duration - 1e-6:
            scene_durations[-1] = trimmed_last
            print(
                f"    Последняя сцена укорочена: {last_duration:.3f}с → {trimmed_last:.3f}с"
            )

    if min_scene_duration and not validate_scene_durations(scene_durations, min_scene_duration):
        short = [
            i + 1 for i, duration in enumerate(scene_durations)
            if duration < min_scene_duration - 1e-3
        ]
        print(f"    Отклонено: сцены {short} короче {min_scene_duration}с")
        return None

    end_time = start + sum(scene_durations)
    segment = dict(segment)
    segment['start_time'] = start
    segment['scene_durations'] = scene_durations
    segment['end_time'] = end_time
    segment['duration'] = sum(scene_durations)
    segment['num_scenes'] = len(scene_durations)
    segment['max_video_duration'] = audio_duration - start
    return segment


def find_segments_with_peaks(
        audio_file,
        min_scene_duration=0.5,
        max_scene_duration=2.0,
        min_scenes=5,
        max_scenes=12,
        edge_exclude=0.10,
):
    """
    Находит сегмент аудио со сменой кадра на каждом пике.
    Случайно выбирает один сегмент из средней зоны (без 10% начала и конца).
    """
    print("=" * 60)
    print("ПОИСК СЕГМЕНТА ПО ПИКАМ АУДИО")
    print("=" * 60)

    sample_rate, audio_data, total_duration = load_audio_for_analysis(audio_file)
    if audio_data is None:
        print("Ошибка: не удалось прочитать аудиофайл")
        return None

    print(f"Общая длительность аудио: {total_duration:.2f} сек")
    print(f"Допустимая зона: {total_duration * edge_exclude:.2f} — "
          f"{total_duration * (1 - edge_exclude):.2f} сек")

    print(f"\nНастройки:")
    print(f"  Длительность сцены: {min_scene_duration} — {max_scene_duration} сек")
    print(f"  Количество сцен (диапазон): {min_scenes} — {max_scenes}")

    scene_lo, scene_hi, max_fit = compute_scene_count_range(
        total_duration, min_scene_duration, min_scenes, max_scenes, edge_exclude,
    )
    if scene_lo is None:
        print(f"  ОШИБКА: аудио слишком короткое (макс. {max_fit} сцен при min {min_scene_duration}с)")
        return None

    if scene_hi < min_scenes or scene_lo < min_scenes:
        print(f"  Диапазон сцен скорректирован под длину аудио ({total_duration:.1f}с): {scene_lo} — {scene_hi}")

    target_num_scenes = random.randint(scene_lo, scene_hi)
    print(f"  Случайно выбрано сцен: {target_num_scenes} (влезает в {total_duration:.1f}с аудио)")

    print("\nДетектирование всех пиков...")
    _, peak_times, peak_amplitudes, _ = detect_all_audio_peaks(audio_data, sample_rate)

    if len(peak_times) == 0:
        print("ОШИБКА: пики не найдены")
        return None

    print("\nПоиск валидных сегментов...")
    candidates = []
    scene_counts_to_try = [target_num_scenes] + [
        n for n in random.sample(range(scene_lo, scene_hi + 1), scene_hi - scene_lo + 1)
        if n != target_num_scenes
    ]

    for num_scenes in scene_counts_to_try:
        candidates = find_peak_based_segments(
            peak_times,
            peak_amplitudes,
            total_duration,
            min_scene_duration,
            max_scene_duration,
            num_scenes,
            num_scenes,
            edge_exclude=edge_exclude,
        )
        if candidates:
            if num_scenes != target_num_scenes:
                print(f"    Для {target_num_scenes} сцен не найдено, используем {num_scenes}")
                target_num_scenes = num_scenes
            break

    if not candidates:
        print("    Основной поиск не дал результатов, пробуем запасной...")
        valid_start = total_duration * edge_exclude
        valid_end = total_duration * (1.0 - edge_exclude)
        peaks = [float(p) for p in peak_times if valid_start <= p <= valid_end]
        amps = [float(peak_amplitudes[i]) for i, p in enumerate(peak_times) if valid_start <= p <= valid_end]
        for num_scenes in scene_counts_to_try:
            fallback = build_fallback_segment(
                peaks, amps, valid_start, valid_end,
                min_scene_duration, max_scene_duration, num_scenes, num_scenes,
                total_duration=total_duration,
                target_num_scenes=num_scenes,
            )
            if fallback:
                candidates = [fallback]
                target_num_scenes = num_scenes
                print(f"    Запасной поиск: сегмент найден ({num_scenes} сцен)")
                break
        else:
            print("  Не найдено подходящих сегментов")
            print("  Советы:")
            print(f"    - Уменьшите MIN_SCENES (сейчас {min_scenes})")
            print(f"    - Расширьте MAX_SCENE_DURATION (сейчас {max_scene_duration})")
            print(f"    - Уменьшите MIN_SCENE_DURATION (сейчас {min_scene_duration})")
            return None

    chosen = random.choice(candidates)
    segment = None
    for _ in range(50):
        segment = build_segment_from_peaks(
            chosen, min_scene_duration, max_scene_duration, total_duration=total_duration,
        )
        if segment is not None:
            break
        chosen = random.choice(candidates)

    if segment is None:
        print("  Не удалось собрать сегмент в пределах аудио")
        return None

    print(f"\n  Выбран СЛУЧАЙНЫЙ сегмент из {len(candidates)} вариантов ({target_num_scenes} сцен)")
    print("\n" + "=" * 60)
    print("ВЫБРАННЫЙ СЕГМЕНТ:")
    print("=" * 60)
    print(f"  Начало: {segment['start_time']:.3f} сек")
    print(f"  Конец: {segment['end_time']:.3f} сек")
    print(f"  Длительность: {segment['duration']:.3f} сек")
    print(f"  Пиков (смен кадра): {segment['num_transitions']}")
    print(f"  Сцен: {segment['num_scenes']}")
    for i, dur in enumerate(segment['scene_durations'], 1):
        print(f"    Сцена {i}: {dur:.3f} сек")

    return [segment]


def collect_video_clips(clip_dir):
    """Собирает видеофайлы из папки"""
    if not os.path.isdir(clip_dir):
        return []

    clips = []
    for name in os.listdir(clip_dir):
        if os.path.splitext(name)[1].lower() in VIDEO_EXTENSIONS:
            clips.append(os.path.join(clip_dir, name))
    return sorted(clips)


def get_media_duration(media_file):
    """Получает длительность медиафайла в секундах"""
    return get_audio_duration(media_file)


def get_video_dimensions(video_file, dimension_cache=None):
    """Возвращает (ширина, высота) видео с учётом поворота"""
    if dimension_cache is not None and video_file in dimension_cache:
        return dimension_cache[video_file]

    try:
        data = ffprobe_json(video_file)
        video_stream = next(
            (s for s in data.get("streams") or [] if s.get("codec_type") == "video"),
            None,
        )
        if video_stream is None:
            return None

        width = int(video_stream["width"])
        height = int(video_stream["height"])

        rotation = 0
        tags = video_stream.get("tags") or {}
        if "rotate" in tags:
            rotation = int(tags["rotate"])
        else:
            for side_data in video_stream.get("side_data_list") or []:
                if side_data.get("rotation") is not None:
                    rotation = int(side_data["rotation"])
                    break

        if abs(rotation) in (90, 270):
            width, height = height, width

        dimensions = (width, height)
        if dimension_cache is not None:
            dimension_cache[video_file] = dimensions
        return dimensions
    except Exception as e:
        print(f"    Ошибка получения размеров {video_file}: {e}")
        return None


def get_largest_video_dimensions(clip_paths, dimension_cache=None):
    """Находит размеры самого большого видео среди выбранных клипов"""
    dimension_cache = dimension_cache if dimension_cache is not None else {}
    largest = None

    for clip_path in clip_paths:
        dimensions = get_video_dimensions(clip_path, dimension_cache)
        if dimensions is None:
            continue

        width, height = dimensions
        area = width * height
        if largest is None or area > largest['area']:
            largest = {
                'width': width,
                'height': height,
                'area': area,
                'path': clip_path,
            }

    if largest is None:
        return None

    return largest['width'], largest['height'], largest['path']


def resolve_slice_fps(fps_mode: str | int | float) -> float:
    """30 или 60 fps для нарезки."""
    if isinstance(fps_mode, (int, float)):
        v = float(fps_mode)
        if v > 0:
            return v
    mode = str(fps_mode).strip().lower()
    if mode in ("60", "60fps"):
        return 60.0
    return float(DEFAULT_SLICE_FPS)


def ensure_even_dimensions(width, height):
    """Делает ширину и высоту чётными для совместимости с yuv420p"""
    if width % 2:
        width -= 1
    if height % 2:
        height -= 1
    return max(width, 2), max(height, 2)


def pick_random_clip_fragment(clip_pool, duration, duration_cache, exclude_paths=None):
    """Выбирает случайное видео и случайный фрагмент нужной длительности"""
    if not clip_pool:
        return None

    exclude_paths = exclude_paths or set()
    candidates = [c for c in clip_pool if c not in exclude_paths]
    if not candidates:
        candidates = list(clip_pool)

    random.shuffle(candidates)

    for clip_path in candidates:
        clip_duration = duration_cache.get(clip_path)
        if clip_duration is None:
            clip_duration = get_media_duration(clip_path)
            duration_cache[clip_path] = clip_duration

        if clip_duration is None or clip_duration <= 0:
            continue

        if clip_duration >= duration:
            max_start = clip_duration - duration
            start = random.uniform(0, max_start) if max_start > 0 else 0.0
            return {
                'path': clip_path,
                'start': start,
                'duration': duration,
                'loop': False,
            }

    clip_path = random.choice(candidates)
    clip_duration = duration_cache.get(clip_path) or get_media_duration(clip_path) or 1.0
    duration_cache[clip_path] = clip_duration
    max_start = max(0.0, clip_duration - 0.1)
    start = random.uniform(0, max_start) if max_start > 0 else 0.0
    return {
        'path': clip_path,
        'start': start,
        'duration': duration,
        'loop': True,
    }


def min_scene_frames(min_scene_duration, fps):
    """Минимальное число кадров для сцены при заданном MIN_SCENE_DURATION"""
    if not min_scene_duration:
        return 1
    return max(1, math.ceil(min_scene_duration * fps - 1e-9))


def snap_scene_durations_to_frames(scene_durations, fps, min_scene_duration=None):
    """Привязывает длительности сцен к целому числу кадров"""
    min_frames = min_scene_frames(min_scene_duration, fps)
    frame_counts = []
    snapped = []
    for duration in scene_durations:
        frames = max(min_frames, int(round(duration * fps)))
        frame_counts.append(frames)
        snapped.append(frames / fps)
    return snapped, frame_counts


def clamp_frame_counts_to_audio(frame_counts, fps, available_seconds, min_scene_duration=None):
    """
    Укорачивает хвостовые сцены по кадрам, если после snap сумма не влезает в аудио.
    Каждая сцена остаётся не короче MIN_SCENE_DURATION.
    """
    if available_seconds is None or available_seconds <= 0:
        return None

    min_frames = min_scene_frames(min_scene_duration, fps)
    counts = list(frame_counts)
    max_total = int(math.floor(available_seconds * fps + 1e-6))
    excess = sum(counts) - max_total

    if excess <= 0:
        return counts

    for i in range(len(counts) - 1, -1, -1):
        if excess <= 0:
            break
        reducible = counts[i] - min_frames
        if reducible <= 0:
            continue
        take = min(excess, reducible)
        counts[i] -= take
        excess -= take

    if excess > 0:
        return None

    return counts


def _slice_hw_input_args(gpu_pipeline: GpuPipeline | None) -> tuple[str, ...]:
    """
    Аргументы hwaccel для каждого входа нарезки.
    AMD/D3D11: только декод на GPU, кадры в RAM — иначе fps/scale не работают
  с десятками входов в одном filter_complex.
    """
    if gpu_pipeline is None:
        return ()
    if gpu_pipeline.name == "d3d11va":
        return ("-hwaccel", "d3d11va")
    return gpu_pipeline.input_args


def _scene_input_args(
    fragment: dict,
    gpu_pipeline: GpuPipeline | None = None,
) -> list[str]:
    """Быстрый seek: -ss перед -i (декодер прыгает к нужной позиции)."""
    start = f"{fragment['start']:.6f}"
    path = fragment['path']
    hw = list(_slice_hw_input_args(gpu_pipeline))
    if fragment['loop']:
        return ['-stream_loop', '-1', *hw, '-ss', start, '-i', path]
    return [*hw, '-ss', start, '-i', path]


def _cpu_scale_pad_chain(width: int, height: int) -> str:
    pad = f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"{pad},setsar=1,format=yuv420p"
    )


def _scene_filter_chain(
    input_index: int,
    width: int,
    height: int,
    fps: int | float,
    frame_count: int,
    out_label: str,
    *,
    gpu_pipeline: GpuPipeline | None = None,
) -> str:
    fc = max(1, int(frame_count))
    last_frame = max(0, fc - 1)
    # Если probe-длительность чуть длиннее реальных кадров, fps/энкодер
    # иначе добивают чёрным → вспышка на стыке сцен. Клонируем последний кадр.
    tail = (
        f"tpad=stop_mode=clone:stop={fc},"
        f"select='lte(n\\,{last_frame})',setpts=N/FRAME_RATE/TB[{out_label}]"
    )
    if gpu_pipeline is None or gpu_pipeline.name in ("videotoolbox", "d3d11va"):
        return (
            f"[{input_index}:v]fps={fps},"
            f"{_cpu_scale_pad_chain(width, height)},"
            f"{tail}"
        )
    if gpu_pipeline.name == "cuda":
        return (
            f"[{input_index}:v]scale_cuda={width}:{height}:force_original_aspect_ratio=decrease,"
            f"hwdownload,format=nv12,fps={fps},"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,"
            f"{tail}"
        )
    if gpu_pipeline.name == "qsv":
        return (
            f"[{input_index}:v]scale_qsv={width}:{height},"
            f"hwdownload,format=nv12,fps={fps},"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,"
            f"{tail}"
        )
    return (
        f"[{input_index}:v]fps={fps},"
        f"{_cpu_scale_pad_chain(width, height)},"
        f"{tail}"
    )


def _append_text_overlay_to_graph(
    concat_label: str,
    overlay: ScaledTextOverlay,
    *,
    total_frames: int,
    fps: float,
    total_duration_sec: float,
    emoji_input_start: int = 1,
) -> tuple[str, list[str]]:
    built = build_text_overlay_filters(
        overlay,
        "v0",
        start_frame=0,
        frame_count=int(total_frames),
        total_frames=int(total_frames),
        fps=float(fps),
        total_duration_sec=total_duration_sec,
        emoji_input_start=int(emoji_input_start),
    )
    if not built.has_content:
        return f"{concat_label}null[outv]", []
    return f"{concat_label}null[v0];{built.graph}", list(built.emoji_input_argv)


def _scene_parallel_workers(n_scenes: int) -> int:
    cpu = os.cpu_count() or 4
    return max(1, min(n_scenes, max(2, cpu // 2)))


def _batch_parallel_workers(n_batches: int, *, prefer_gpu: bool) -> int:
    n = max(1, int(n_batches))
    if prefer_gpu:
        return max(1, min(n, SLICE_GPU_MAX_CONCURRENT_BATCHES))
    cpu = os.cpu_count() or 4
    return max(1, min(n, max(2, cpu // 2)))


def _encode_args_for_scenes(
    scene_clips: list[dict],
    width: int,
    height: int,
    fps: int | float,
    *,
    gpu_pipeline: GpuPipeline | None = None,
    scaled_overlay: ScaledTextOverlay | None = None,
    total_duration_sec: float | None = None,
    video_transition: str | None = None,
    video_transition_duration: float = 0.0,
) -> tuple[list[str], list[str], int, list[str]]:
    """Собирает input_args, filter_complex, суммарное число кадров и global hw args."""
    input_args: list[str] = []
    filters: list[str] = []
    concat_labels: list[str] = []
    global_hw: list[str] = (
        list(gpu_pipeline.global_args) if gpu_pipeline is not None else []
    )
    for i, fragment in enumerate(scene_clips):
        input_args.extend(_scene_input_args(fragment, gpu_pipeline))
        label = f"s{i}"
        concat_labels.append(f"[{label}]")
        filters.append(
            _scene_filter_chain(
                i, width, height, fps, int(fragment['frame_count']), label,
                gpu_pipeline=gpu_pipeline,
            )
        )
    n = len(scene_clips)
    fps_f = float(fps)
    total_frames = sum(int(fragment['frame_count']) for fragment in scene_clips)
    concat_out = "[outv]"
    if scaled_overlay is not None and scaled_overlay.lines:
        concat_out = "[concatv]"

    xfade_name = str(video_transition or "").strip().lower()
    use_xfade = (
        n == 2
        and xfade_name not in ("", "cut", "none")
        and float(video_transition_duration) > 1e-3
    )
    if use_xfade:
        fc0 = max(1, int(scene_clips[0]["frame_count"]))
        fc1 = max(1, int(scene_clips[1]["frame_count"]))
        overlap_frames = max(
            1, int(round(float(video_transition_duration) * fps_f))
        )
        overlap_frames = min(overlap_frames, fc0 - 1, fc1 - 1)
        if overlap_frames < 1:
            use_xfade = False
        else:
            offset = (fc0 - overlap_frames) / fps_f
            dur = overlap_frames / fps_f
            total_frames = fc0 + fc1 - overlap_frames
            # ffmpeg xfade transition names; unknown → fade
            xfade_aliases = {
                "fade": "fade",
                "dissolve": "fade",
                "crossfade": "fade",
                "circleopen": "circleopen",
                "circle": "circleopen",
                "wipeleft": "wipeleft",
                "slideleft": "slideleft",
                "zoomin": "zoomin",
                "zoom": "zoomin",
                "punch": "zoomin",
                "fadewhite": "fadewhite",
                "flash": "fadewhite",
                "whiteflash": "fadewhite",
                "hblur": "hblur",
                "whip": "hblur",
                "blur": "hblur",
            }
            xname = xfade_aliases.get(xfade_name, "fade")
            filter_complex = (
                ";".join(filters)
                + f";[s0][s1]xfade=transition={xname}:duration={dur:.6f}"
                f":offset={offset:.6f}{concat_out}"
            )

    if not use_xfade:
        filter_complex = (
            ";".join(filters)
            + ";"
            + "".join(concat_labels)
            + f"concat=n={n}:v=1:a=0{concat_out}"
        )
    if concat_out == "[concatv]":
        dur = float(total_duration_sec) if total_duration_sec else total_frames / fps_f
        emoji_start = sum(1 for a in input_args if a == "-i")
        ov_graph, emoji_argv = _append_text_overlay_to_graph(
            "[concatv]",
            scaled_overlay,
            total_frames=total_frames,
            fps=fps_f,
            total_duration_sec=dur,
            emoji_input_start=emoji_start,
        )
        input_args.extend(emoji_argv)
        filter_complex += ";" + ov_graph
    return input_args, [filter_complex], total_frames, global_hw


def _run_scene_encode(
    input_args: list[str],
    filter_parts: list[str],
    output_path: str,
    *,
    total_frames: int,
    fps: int | float,
    prefer_gpu: bool,
    global_hw_args: list[str] | None = None,
) -> None:
    enc, enc_args = pick_best_h264_encoder(
        prefer_gpu=bool(prefer_gpu),
        crf=SLICE_ENCODE_CRF,
        gpu_cq=SLICE_ENCODE_GPU_CQ,
        videotoolbox_q=SLICE_ENCODE_VIDEOTOOLBOX_Q,
    )
    tail = [
        '-map', '[outv]',
        '-frames:v', str(total_frames),
        '-c:v', enc,
        *enc_args,
        '-pix_fmt', 'yuv420p',
        '-r', str(fps),
        output_path,
    ]
    filter_complex = filter_parts[0]
    filter_argv, filter_script = _filter_complex_argv(filter_complex)
    cmd_base = [
        *(global_hw_args or []),
        *input_args,
        '-an',
        *filter_argv,
        *tail,
    ]
    try:
        run_ffmpeg(cmd_base)
    finally:
        if filter_script is not None:
            try:
                filter_script.unlink(missing_ok=True)
            except OSError:
                pass


def _render_with_gpu_fallback(
    scene_clips: list[dict],
    output_path: str,
    width: int,
    height: int,
    fps: int | float,
    *,
    prefer_gpu: bool,
    scaled_overlay: ScaledTextOverlay | None = None,
    total_duration_sec: float | None = None,
    video_transition: str | None = None,
    video_transition_duration: float = 0.0,
    log: Optional[LogCallback] = None,
) -> None:
    enc, _ = pick_best_h264_encoder(
        prefer_gpu=bool(prefer_gpu),
        crf=SLICE_ENCODE_CRF,
        gpu_cq=SLICE_ENCODE_GPU_CQ,
        videotoolbox_q=SLICE_ENCODE_VIDEOTOOLBOX_Q,
    )
    pipelines: list[GpuPipeline | None] = []
    if prefer_gpu:
        pipe = resolve_gpu_pipeline(prefer_gpu=True, encoder=enc)
        if pipe is not None:
            pipelines.append(pipe)
    pipelines.append(None)

    last_err: Exception | None = None
    for pipeline in pipelines:
        try:
            input_args, filter_parts, total_frames, global_hw = _encode_args_for_scenes(
                scene_clips,
                width,
                height,
                fps,
                gpu_pipeline=pipeline,
                scaled_overlay=scaled_overlay,
                total_duration_sec=total_duration_sec,
                video_transition=video_transition,
                video_transition_duration=video_transition_duration,
            )
            if pipeline is not None and log is not None:
                _log(f"    GPU: {gpu_pipeline_label(pipeline)}", log)
            _run_scene_encode(
                input_args,
                filter_parts,
                output_path,
                total_frames=total_frames,
                fps=fps,
                prefer_gpu=prefer_gpu,
                global_hw_args=global_hw,
            )
            return
        except RuntimeError as exc:
            last_err = exc
            if pipeline is not None and is_gpu_filter_fallback_error([str(exc)]):
                if log is not None:
                    detail = str(exc).strip().replace("\n", " ")[:280]
                    _log(
                        f"    GPU-фильтры недоступны ({detail}), повтор на CPU…",
                        log,
                    )
                continue
            raise
    if last_err is not None:
        raise last_err


def render_scenes_combined(
    scene_clips: list[dict],
    output_path: str,
    width: int,
    height: int,
    fps: int | float,
    *,
    prefer_gpu: bool = False,
    scaled_overlay: ScaledTextOverlay | None = None,
    total_duration_sec: float | None = None,
    video_transition: str | None = None,
    video_transition_duration: float = 0.0,
    log: Optional[LogCallback] = None,
) -> None:
    """Рендерит все сцены одним ffmpeg (общий filter_complex + concat/xfade)."""
    _render_with_gpu_fallback(
        scene_clips,
        output_path,
        width,
        height,
        fps,
        prefer_gpu=prefer_gpu,
        scaled_overlay=scaled_overlay,
        total_duration_sec=total_duration_sec,
        video_transition=video_transition,
        video_transition_duration=video_transition_duration,
        log=log,
    )


def _scene_batches(
    scene_clips: list[dict],
    batch_size: int = SLICE_SCENE_BATCH_SIZE,
) -> list[list[dict]]:
    size = max(1, int(batch_size))
    return [scene_clips[i:i + size] for i in range(0, len(scene_clips), size)]


def render_scenes_batched(
    scene_clips: list[dict],
    output_path: str,
    temp_dir: str,
    width: int,
    height: int,
    fps: int | float,
    *,
    prefer_gpu: bool = False,
    scaled_overlay: ScaledTextOverlay | None = None,
    total_duration_sec: float | None = None,
    batch_size: int = SLICE_SCENE_BATCH_SIZE,
    video_transition: str | None = None,
    video_transition_duration: float = 0.0,
    log: Optional[LogCallback] = None,
) -> None:
    """
    Рендер сцен батчами (по умолчанию 5 входов на ffmpeg), склейка -c copy.
    Текст — один проход после concat всех батчей.
    """
    batches = _scene_batches(scene_clips, batch_size)
    n_batches = len(batches)

    if n_batches == 1:
        _render_with_gpu_fallback(
            scene_clips,
            output_path,
            width,
            height,
            fps,
            prefer_gpu=prefer_gpu,
            scaled_overlay=scaled_overlay,
            total_duration_sec=total_duration_sec,
            video_transition=video_transition,
            video_transition_duration=video_transition_duration,
            log=log,
        )
        return

    workers = _batch_parallel_workers(n_batches, prefer_gpu=prefer_gpu)
    if log is not None:
        _log(
            f"    Рендер батчами: {len(scene_clips)} сцен → {n_batches} проходов "
            f"ffmpeg (до {batch_size} сцен, параллельно до {workers}), склейка copy…",
            log,
        )

    batch_files: list[str | None] = [None] * n_batches

    def _render_batch(bi: int, batch: list[dict]) -> tuple[int, str]:
        batch_path = os.path.join(temp_dir, f"scene_batch_{bi:03d}.mp4")
        if log is not None:
            _log(f"    Батч {bi + 1}/{n_batches}: {len(batch)} сцен…", log)
        _render_with_gpu_fallback(
            batch,
            batch_path,
            width,
            height,
            fps,
            prefer_gpu=prefer_gpu,
            scaled_overlay=None,
            log=log,
        )
        return bi, batch_path

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_render_batch, bi, batch)
            for bi, batch in enumerate(batches)
        ]
        for future in as_completed(futures):
            bi, batch_path = future.result()
            batch_files[bi] = batch_path

    if any(path is None for path in batch_files):
        raise RuntimeError("Не все батчи отрендерены")

    concat_path = os.path.join(temp_dir, "batches_concat.mp4")
    _concat_scene_files([str(p) for p in batch_files], concat_path)

    if scaled_overlay is not None and scaled_overlay.lines:
        from zaliver.processing.slicing_overlay import apply_text_overlay_to_video

        if log is not None:
            _log(
                f"    Текст после склейки батчей ({len(scaled_overlay.lines)} строк)…",
                log,
            )
        apply_text_overlay_to_video(
            concat_path,
            output_path,
            scaled_overlay,
            log=log,
            prefer_gpu=prefer_gpu,
        )
        try:
            os.unlink(concat_path)
        except OSError:
            pass
    else:
        shutil.move(concat_path, output_path)


def render_scene_clip(
    fragment,
    output_path,
    width,
    height,
    fps,
    frame_count,
    *,
    prefer_gpu: bool = False,
    log: Optional[LogCallback] = None,
):
    """Рендерит одну сцену с точным числом кадров."""
    _render_with_gpu_fallback(
        [fragment],
        output_path,
        width,
        height,
        fps,
        prefer_gpu=prefer_gpu,
        log=log,
    )


def render_scenes_parallel(
    scene_clips: list[dict],
    temp_dir: str,
    width: int,
    height: int,
    fps: int | float,
    *,
    prefer_gpu: bool = False,
    log: Optional[LogCallback] = None,
) -> list[str]:
    """Параллельный рендер сцен (fallback, если один проход не удался)."""
    scene_files: list[str | None] = [None] * len(scene_clips)
    workers = _scene_parallel_workers(len(scene_clips))

    def _render_one(index: int, fragment: dict) -> tuple[int, str]:
        scene_file = os.path.join(temp_dir, f"scene_{index:04d}.mp4")
        render_scene_clip(
            fragment,
            scene_file,
            width,
            height,
            fps,
            fragment['frame_count'],
            prefer_gpu=prefer_gpu,
            log=log,
        )
        return index, scene_file

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_render_one, i, fragment)
            for i, fragment in enumerate(scene_clips)
        ]
        for future in as_completed(futures):
            index, scene_file = future.result()
            scene_files[index] = scene_file

    if any(path is None for path in scene_files):
        raise RuntimeError("Не все сцены отрендерены")
    return scene_files


def _concat_scene_files(scene_files: list[str], output_path: str) -> None:
    concat_list = os.path.join(os.path.dirname(output_path), "concat_list.txt")
    with open(concat_list, 'w', encoding='utf-8') as concat_file:
        for scene_file in scene_files:
            escaped_path = os.path.abspath(scene_file).replace('\\', '/').replace("'", "'\\''")
            concat_file.write(f"file '{escaped_path}'\n")

    run_ffmpeg([
        '-f', 'concat',
        '-safe', '0',
        '-i', concat_list,
        '-c', 'copy',
        output_path,
    ])


def get_audio_duration(audio_file):
    """Получает длительность аудиофайла в секундах"""
    try:
        return probe_media_duration_seconds(audio_file)
    except Exception as e:
        print(f"    Ошибка получения длительности: {e}")
        return None


def _pcm_audio_argv_tail() -> list[str]:
    return ["-acodec", "pcm_s16le", "-ac", "2", "-ar", "44100"]


def _trim_trailing_fade_wav(src_wav: str, out_wav: str) -> bool:
    """Срезает затухание/тишину в конце, чтобы стык с началом песни не проваливался."""
    try:
        run_ffmpeg(
            [
                "-i",
                src_wav,
                "-af",
                # reverse → убрать тихий старт (= бывший хвост) → reverse обратно
                "areverse,"
                "silenceremove=start_periods=1:start_duration=0.08:"
                "start_threshold=-40dB:detection=peak,"
                "areverse",
                *_pcm_audio_argv_tail(),
                out_wav,
            ],
        )
        got = probe_media_duration_seconds(out_wav) or 0.0
        src_len = probe_media_duration_seconds(src_wav) or 0.0
        # Оставляем результат, если после среза fade ещё осталась полезная длина.
        if got < 0.35:
            return False
        if src_len <= 0:
            return True
        return got >= min(src_len - 0.05, max(src_len * 0.35, src_len - 3.5))
    except Exception:
        return False


def _trim_leading_silence_wav(src_wav: str, out_wav: str) -> bool:
    """Срезает тишину/тихий вступ в начале перед стыком."""
    try:
        run_ffmpeg(
            [
                "-i",
                src_wav,
                "-af",
                "silenceremove=start_periods=1:start_duration=0.05:"
                "start_threshold=-42dB:detection=peak",
                *_pcm_audio_argv_tail(),
                out_wav,
            ],
        )
        got = probe_media_duration_seconds(out_wav) or 0.0
        return got >= 0.2
    except Exception:
        return False


def _concat_audio_with_crossfade(
    part_a: str,
    part_b: str,
    out_wav: str,
    *,
    duration_sec: float,
    crossfade_sec: float = 0.12,
    log: Optional[LogCallback] = None,
) -> bool:
    """Склеивает A+B с коротким crossfade, без щелчка и провала громкости."""
    dur_a = probe_media_duration_seconds(part_a) or 0.0
    dur_b = probe_media_duration_seconds(part_b) or 0.0
    if dur_a < 0.1 or dur_b < 0.1:
        return False
    cf = min(float(crossfade_sec), dur_a * 0.35, dur_b * 0.35, 0.35)
    cf = max(0.04, cf)
    try:
        # acrossfade: выход короче на cf, чем сумма длин — это как раз убирает хвост fade.
        run_ffmpeg(
            [
                "-i",
                part_a,
                "-i",
                part_b,
                "-filter_complex",
                f"[0:a][1:a]acrossfade=d={cf:.4f}:c1=tri:c2=tri[aout]",
                "-map",
                "[aout]",
                "-t",
                f"{duration_sec:.6f}",
                *_pcm_audio_argv_tail(),
                out_wav,
            ],
        )
        got = probe_media_duration_seconds(out_wav) or 0.0
        if got >= duration_sec - 0.15:
            _log(f"    Стык музыки: crossfade {cf:.3f}с без затухания", log)
            return True
        # Если короче — добьём pad (редко).
        if got > 0.2:
            padded = out_wav + ".pad.wav"
            run_ffmpeg(
                [
                    "-i",
                    out_wav,
                    "-af",
                    f"apad=whole_dur={duration_sec:.6f}",
                    "-t",
                    f"{duration_sec:.6f}",
                    *_pcm_audio_argv_tail(),
                    padded,
                ],
            )
            if os.path.exists(padded) and os.path.getsize(padded) > 1000:
                shutil.copy2(padded, out_wav)
                try:
                    os.remove(padded)
                except OSError:
                    pass
                return True
    except Exception as e:
        _log(f"    Crossfade не удался ({e}), пробуем простую склейку…", log)
    return False


def _prepare_loopable_song_body(
    audio_file: str,
    body_wav: str,
    temp_dir: str,
    log: Optional[LogCallback] = None,
) -> Optional[str]:
    """Полный трек без хвостового fade и без тишины в начале — для бесшовного повтора."""
    raw = os.path.join(temp_dir, "song_body_raw.wav")
    no_tail = os.path.join(temp_dir, "song_body_notail.wav")
    try:
        run_ffmpeg(
            [
                "-i",
                audio_file,
                *_pcm_audio_argv_tail(),
                raw,
            ],
        )
    except Exception:
        return None
    src = raw
    if _trim_trailing_fade_wav(raw, no_tail):
        src = no_tail
        _log("    Убрано затухание в конце трека для бесшовного продления", log)
    if _trim_leading_silence_wav(src, body_wav):
        _log("    Убрана тишина в начале трека для стыка", log)
        return body_wav
    try:
        shutil.copy2(src, body_wav)
        return body_wav
    except Exception:
        return src if os.path.exists(src) else None


def build_audio_wav_exact_duration(
    audio_file: str,
    *,
    start_sec: float,
    duration_sec: float,
    out_wav: str,
    temp_dir: str,
    log: Optional[LogCallback] = None,
    prefer_loop: bool = False,
) -> bool:
    """
    Непрерывный кусок музыки с ``start_sec`` длиной ``duration_sec``.

    Если до EOF не хватает — дописываем с начала песни без хвостового затухания
    (trim fade + короткий crossfade).
    """
    del prefer_loop
    start_sec = max(0.0, float(start_sec))
    duration_sec = max(0.05, float(duration_sec))
    src_dur = get_audio_duration(audio_file)
    remaining = None if src_dur is None else max(0.0, float(src_dur) - start_sec)

    def _direct_cut(target: str, *, ss: float, dur: float) -> bool:
        try:
            run_ffmpeg(
                [
                    "-ss",
                    f"{ss:.6f}",
                    "-i",
                    audio_file,
                    "-t",
                    f"{dur:.6f}",
                    *_pcm_audio_argv_tail(),
                    target,
                ],
            )
        except Exception:
            return False
        got = probe_media_duration_seconds(target) or 0.0
        return got >= dur - 0.12

    if remaining is not None and remaining + 0.05 >= duration_sec:
        if _direct_cut(out_wav, ss=start_sec, dur=duration_sec):
            return True
        _log("    Не хватило аудио до EOF — допишем с начала песни…", log)

    part_a = os.path.join(temp_dir, "audio_part_a.wav")
    part_a_trim = os.path.join(temp_dir, "audio_part_a_trim.wav")
    part_b = os.path.join(temp_dir, "audio_part_b.wav")
    part_b_trim = os.path.join(temp_dir, "audio_part_b_trim.wav")
    list_path = os.path.join(temp_dir, "audio_concat.txt")
    loop_body = os.path.join(temp_dir, "song_loop_body.wav")

    if remaining is None or remaining < 0.05:
        _log(
            f"    Музыка с начала трека на {duration_sec:.3f}с "
            f"(без затухания на стыках повтора)",
            log,
        )
        body = _prepare_loopable_song_body(audio_file, loop_body, temp_dir, log=log)
        src_loop = body or audio_file
        try:
            run_ffmpeg(
                [
                    "-stream_loop",
                    "-1",
                    "-i",
                    src_loop,
                    "-t",
                    f"{duration_sec:.6f}",
                    *_pcm_audio_argv_tail(),
                    out_wav,
                ],
            )
            got = probe_media_duration_seconds(out_wav) or 0.0
            return got >= duration_sec - 0.12
        except Exception as e:
            _log(f"    Ошибка аудио с начала: {e}", log)
            return False

    # Берём с запасом на обрезку fade в конце (~1.2с), чтобы после trim длина была близка к need.
    fade_budget = 1.25
    take_a = min(remaining, duration_sec + fade_budget)
    need_b = max(0.0, duration_sec - (take_a - fade_budget * 0.5))
    _log(
        f"    Музыка: кусок с {start_sec:.3f}с + начало песни без затухания на стыке",
        log,
    )
    if not _direct_cut(part_a, ss=start_sec, dur=take_a):
        body = _prepare_loopable_song_body(audio_file, loop_body, temp_dir, log=log)
        src_loop = body or audio_file
        try:
            run_ffmpeg(
                [
                    "-stream_loop",
                    "-1",
                    "-i",
                    src_loop,
                    "-t",
                    f"{duration_sec:.6f}",
                    *_pcm_audio_argv_tail(),
                    out_wav,
                ],
            )
            return (probe_media_duration_seconds(out_wav) or 0.0) >= duration_sec - 0.12
        except Exception:
            return False

    a_src = part_a
    if _trim_trailing_fade_wav(part_a, part_a_trim):
        a_src = part_a_trim
        _log("    Срезано затухание в конце перед продлением", log)

    dur_a = probe_media_duration_seconds(a_src) or 0.0
    need_b = max(0.05, duration_sec - dur_a + 0.15)  # чуть с запасом под crossfade

    if dur_a >= duration_sec - 0.05:
        try:
            run_ffmpeg(
                [
                    "-i",
                    a_src,
                    "-t",
                    f"{duration_sec:.6f}",
                    *_pcm_audio_argv_tail(),
                    out_wav,
                ],
            )
            return (probe_media_duration_seconds(out_wav) or 0.0) >= duration_sec - 0.12
        except Exception:
            try:
                shutil.copy2(a_src, out_wav)
                return True
            except Exception:
                return False

    body = _prepare_loopable_song_body(audio_file, loop_body, temp_dir, log=log)
    try:
        run_ffmpeg(
            [
                "-stream_loop",
                "-1",
                "-i",
                body or audio_file,
                "-t",
                f"{need_b:.6f}",
                *_pcm_audio_argv_tail(),
                part_b,
            ],
        )
    except Exception as e:
        _log(f"    Ошибка дописки с начала песни: {e}", log)
        return False

    b_src = part_b
    if _trim_leading_silence_wav(part_b, part_b_trim):
        b_src = part_b_trim

    if _concat_audio_with_crossfade(
        a_src, b_src, out_wav, duration_sec=duration_sec, log=log
    ):
        return True

    # Fallback: concat без crossfade
    try:
        def _esc(p: str) -> str:
            return p.replace("\\", "/").replace("'", "'\\''")

        with open(list_path, "w", encoding="utf-8") as f:
            f.write(f"file '{_esc(a_src)}'\n")
            f.write(f"file '{_esc(b_src)}'\n")
        run_ffmpeg(
            [
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                list_path,
                "-t",
                f"{duration_sec:.6f}",
                *_pcm_audio_argv_tail(),
                out_wav,
            ],
        )
        got = probe_media_duration_seconds(out_wav) or 0.0
        return got >= duration_sec - 0.12
    except Exception as e:
        _log(f"    Ошибка склейки аудиочастей: {e}", log)
        return False


def generate_video_from_segment(
        audio_file,
        segment,
        output_video,
        *,
        clip_pool: list[str] | None = None,
        scene_clip_pools: list[list[str]] | None = None,
        fixed_scene_clips: list[dict] | None = None,
        clip_dir: str = "clips",
        fps=DEFAULT_SLICE_FPS_MODE,
        log: Optional[LogCallback] = None,
        use_gpu: bool = False,
        use_gpu_finalize: bool = False,
        text_overlay_cfg: dict | None = None,
        loop_audio: bool = False,
):
    """Генерирует видео: смена фрагмента на каждом пике аудио.

    ``scene_clip_pools`` — опционально отдельный пул клипов на каждую сцену
    (для склейки: часть 1 / часть 2).
    ``fixed_scene_clips`` — готовые фрагменты (path/start/duration[/loop]),
    если заданы — пулы не используются.
    ``loop_audio`` — зациклить аудио с ``start_time``, если ролику не хватает хвоста трека.
    """
    _log(f"\n  Генерация видео: {output_video}", log)

    loop_audio = bool(loop_audio or segment.get("loop_audio"))

    if fixed_scene_clips:
        pool = []
        for frag in fixed_scene_clips:
            p = str((frag or {}).get("path") or "")
            if p and os.path.isfile(p):
                pool.append(p)
        if not pool:
            _log("    Ошибка: в fixed_scene_clips нет видеофайлов", log)
            return None
        per_scene_pools = None
        _log(f"    Фиксированные клипы сцен: {len(fixed_scene_clips)}", log)
    else:
        if clip_pool:
            pool = [p for p in clip_pool if os.path.isfile(p)]
        else:
            pool = collect_video_clips(clip_dir)

        per_scene_pools: list[list[str]] | None = None
        if scene_clip_pools:
            per_scene_pools = [
                [p for p in (scene_pool or []) if os.path.isfile(p)]
                for scene_pool in scene_clip_pools
            ]
            if not any(per_scene_pools):
                _log("    Ошибка: в пулах сцен нет видеофайлов", log)
                return None
            if not pool:
                merged: list[str] = []
                seen: set[str] = set()
                for sp in per_scene_pools:
                    for p in sp:
                        key = os.path.normcase(p)
                        if key not in seen:
                            seen.add(key)
                            merged.append(p)
                pool = merged

        if not pool:
            src = "списка клипов" if clip_pool or scene_clip_pools else f"папки '{clip_dir}'"
            _log(f"    Ошибка: в {src} нет видеофайлов", log)
            return None

        if per_scene_pools:
            _log(
                "    Источник клипов по сценам: "
                + ", ".join(f"сцена {i + 1}={len(sp)}" for i, sp in enumerate(per_scene_pools)),
                log,
            )
        else:
            _log(f"    Источник клипов: {len(pool)} файлов", log)

    dimension_cache: dict = {}
    fps = resolve_slice_fps(fps)
    _log(f"    FPS рендера: {fps:g}", log)

    video_start = segment['start_time']

    if 'scene_durations' in segment:
        scene_durations = list(segment['scene_durations'])
    else:
        video_end = segment['end_time']
        transition_times = [t['time'] for t in segment['transitions']]
        relative_transitions = [t - video_start for t in transition_times]
        scene_durations = [relative_transitions[0]]
        for i in range(len(relative_transitions) - 1):
            scene_durations.append(relative_transitions[i + 1] - relative_transitions[i])
        scene_durations.append(video_end - transition_times[-1])

    audio_duration = get_audio_duration(audio_file)
    preserve_full = bool(segment.get("stitch_preserve_full_clips"))
    if preserve_full or loop_audio:
        # Видео фиксированной длины (склейка полных клипов) — не ужимаем под аудио.
        video_start = float(segment.get("start_time", video_start) or 0.0)
        min_scene = segment.get("min_interval")
        available_audio = float(sum(scene_durations)) + 1.0
        clamped = {
            "start_time": video_start,
            "scene_durations": list(scene_durations),
            "max_video_duration": available_audio,
        }
    else:
        clamped = clamp_segment_to_audio(
            {**segment, 'start_time': video_start, 'scene_durations': scene_durations},
            audio_duration,
        )
        if clamped is None:
            min_scene = segment.get('min_interval')
            if min_scene:
                print(f"    Ошибка: сегмент не влезает в аудио без нарушения MIN_SCENE_DURATION ({min_scene}с)")
            else:
                print("    Ошибка: сегмент начинается после конца аудио")
            return None
        video_start = clamped['start_time']
        scene_durations = clamped['scene_durations']
        min_scene = segment.get('min_interval')
        available_audio = clamped['max_video_duration']

    if preserve_full:
        # Полные исходники: floor, чтобы не запрашивать кадр за EOF (чёрный стык).
        frame_counts = [
            max(1, int(math.floor(float(d) * float(fps) + 1e-9)))
            for d in scene_durations
        ]
        scene_durations = [fc / float(fps) for fc in frame_counts]
    else:
        scene_durations, frame_counts = snap_scene_durations_to_frames(
            scene_durations, fps, min_scene_duration=min_scene,
        )

        if not loop_audio:
            frame_counts = clamp_frame_counts_to_audio(
                frame_counts, fps, available_audio, min_scene_duration=min_scene,
            )
            if frame_counts is None:
                print(
                    f"    Ошибка: {len(scene_durations)} сцен не влезают в аудио "
                    f"({available_audio:.3f}с) без нарушения MIN_SCENE_DURATION "
                    f"({min_scene}с)"
                )
                return None

        scene_durations = [frames / fps for frames in frame_counts]

    if min_scene and not validate_scene_durations(scene_durations, min_scene):
        short = [
            i + 1 for i, duration in enumerate(scene_durations)
            if duration < min_scene - 1e-3
        ]
        print(f"    Ошибка: после привязки к кадрам сцены {short} короче {min_scene}с")
        return None

    total_video_duration = sum(scene_durations)
    video_end = video_start + total_video_duration

    print(f"\n    Начало: {video_start:.3f}с, конец: {video_end:.3f}с")
    print(f"    Доступно аудио от начала: {available_audio:.3f}с")
    print(f"    Пиков: {segment['num_transitions']}, сцен: {len(scene_durations)}")
    print(f"\n    СЦЕНЫ (смена кадра на каждом пике):")
    for i, dur in enumerate(scene_durations, 1):
        print(f"    Сцена {i}: {dur:.3f}с")

    print(f"\n    Длительность видео: {total_video_duration:.3f}с, сцен: {len(scene_durations)}")

    # ====== ВЫБИРАЕМ ФРАГМЕНТЫ ВИДЕО ======
    num_scenes = len(scene_durations)
    duration_cache = {}
    scene_clips = []
    used_paths: set[str] = set()
    used_paths_by_pool: list[set[str]] = (
        [set() for _ in per_scene_pools] if per_scene_pools else []
    )

    if fixed_scene_clips:
        if len(fixed_scene_clips) < num_scenes:
            print(
                f"    Ошибка: fixed_scene_clips ({len(fixed_scene_clips)}) "
                f"< числа сцен ({num_scenes})"
            )
            return None
        rebuilt_durs: list[float] = []
        rebuilt_frames: list[int] = []
        for i, duration in enumerate(scene_durations):
            raw = fixed_scene_clips[i] or {}
            path = str(raw.get("path") or "")
            if not path or not os.path.isfile(path):
                print(f"    Ошибка: нет файла для сцены {i + 1}")
                return None
            start = float(raw.get("start", 0.0) or 0.0)
            src_dur = duration_cache.get(path)
            if src_dur is None:
                src_dur = get_media_duration(path)
                duration_cache[path] = src_dur
            available = max(0.0, float(src_dur or 0.0) - start)
            # Склейка: всегда целый клип с 0, без video-loop «кусками».
            fc = int(frame_counts[i])
            if preserve_full:
                max_fc = max(1, int(math.floor(available * float(fps) + 1e-6)))
                fc = min(fc, max_fc)
                duration = fc / float(fps)
            fragment = {
                "path": path,
                "start": start,
                "duration": float(duration),
                "loop": False,
                "frame_count": fc,
            }
            scene_clips.append(fragment)
            used_paths.add(path)
            rebuilt_durs.append(float(duration))
            rebuilt_frames.append(fc)
            clip_name = os.path.basename(path)
            print(
                f"    Часть {i + 1}: {duration:.2f}с — {clip_name} целиком с {start:.2f}с"
            )
        if preserve_full:
            scene_durations = rebuilt_durs
            frame_counts = rebuilt_frames
            total_video_duration = sum(scene_durations)
            video_end = video_start + total_video_duration
    else:
        if not per_scene_pools:
            force_unique_clips = len(pool) >= num_scenes
            if force_unique_clips:
                _log(
                    f"    Каждая сцена — из отдельного видео ({num_scenes} сцен, {len(pool)} файлов)",
                    log,
                )
            else:
                _log(
                    f"    Видео меньше сцен ({len(pool)} < {num_scenes}), "
                    f"повторное использование неизбежно",
                    log,
                )

        for i, duration in enumerate(scene_durations):
            if per_scene_pools:
                pool_i = per_scene_pools[i] if i < len(per_scene_pools) else []
                if not pool_i and per_scene_pools:
                    pool_i = per_scene_pools[-1]
                if not pool_i:
                    print(f"    Ошибка: пустой пул клипов для сцены {i + 1}")
                    return None
                used_i = used_paths_by_pool[i] if i < len(used_paths_by_pool) else set()
                unused_clips = [c for c in pool_i if c not in used_i]
                exclude_paths = used_i if unused_clips else set()
            else:
                pool_i = pool
                unused_clips = [c for c in pool_i if c not in used_paths]
                if force_unique_clips or unused_clips:
                    exclude_paths = used_paths
                else:
                    exclude_paths = set()

            fragment = pick_random_clip_fragment(
                pool_i, duration, duration_cache, exclude_paths=exclude_paths
            )
            if fragment is None:
                print(f"    Ошибка: не удалось выбрать фрагмент для сцены {i + 1}")
                return None

            scene_clips.append(fragment)
            used_paths.add(fragment['path'])
            if per_scene_pools and i < len(used_paths_by_pool):
                used_paths_by_pool[i].add(fragment['path'])
            fragment['frame_count'] = frame_counts[i]
            fragment['duration'] = scene_durations[i]
            clip_name = os.path.basename(fragment['path'])
            loop_note = " (зациклено)" if fragment['loop'] else ""
            print(
                f"    Сцена {i + 1}: {duration:.2f}с — {clip_name} "
                f"с {fragment['start']:.2f}с{loop_note}"
            )

    output_parent = os.path.dirname(os.path.abspath(output_video)) or "."
    temp_dir = os.path.join(
        output_parent,
        f"temp_clips_{os.path.splitext(os.path.basename(output_video))[0]}",
    )
    os.makedirs(temp_dir, exist_ok=True)

    if not scene_clips:
        print("    Ошибка: нет сцен")
        return None

    selected_paths = list({fragment['path'] for fragment in scene_clips})
    size_info = get_largest_video_dimensions(selected_paths, dimension_cache)
    if size_info is None:
        print("    Ошибка: не удалось определить размеры выбранных видео")
        return None

    width, height, size_source = size_info
    width, height = ensure_even_dimensions(width, height)
    print(
        f"    Размер итогового видео: {width}x{height} "
        f"(как у {os.path.basename(size_source)})"
    )

    stitch_transition = str(segment.get("stitch_transition") or "cut").strip().lower()
    stitch_overlap = max(0.0, float(segment.get("stitch_transition_duration") or 0.0))
    if stitch_transition in ("", "none"):
        stitch_transition = "cut"
    if stitch_transition == "cut" or stitch_overlap <= 1e-6 or len(scene_clips) != 2:
        stitch_transition = "cut"
        stitch_overlap = 0.0
    else:
        # Перекрытие xfade укорачивает ролик; клипы остаются полными.
        total_video_duration = max(0.05, sum(scene_durations) - stitch_overlap)
        video_end = video_start + total_video_duration
        _log(
            f"    Визуальный переход «{stitch_transition}» "
            f"{stitch_overlap:.2f}с → длительность {total_video_duration:.2f}с",
            log,
        )

    scaled_overlay: ScaledTextOverlay | None = None
    if text_overlay_cfg:
        toc = TextOverlaySettings.from_dict(text_overlay_cfg)
        scaled_overlay = compute_scaled_overlay(toc, video_w=width, video_h=height)
        if scaled_overlay is not None and scaled_overlay.lines:
            if bool(getattr(toc, "after_frame_change", False)) and len(scene_durations) >= 2:
                # Текст с середины визуального перехода (или стыка cut).
                enable_at = float(scene_durations[0]) - stitch_overlap * 0.5
                scaled_overlay.enable_after_sec = max(0.0, enable_at)
                scaled_overlay.from_middle = False
                _log(
                    f"    Текст после смены кадра (с {scaled_overlay.enable_after_sec:.3f}с)…",
                    log,
                )
            else:
                _log(
                    f"    Текст в одном проходе ({len(scaled_overlay.lines)} строк)…",
                    log,
                )
        else:
            scaled_overlay = None

    prefer_gpu_render = bool(use_gpu or use_gpu_finalize)
    # ====== СОЗДАЕМ ВИДЕО ======
    temp_video = os.path.join(temp_dir, "temp_video.mp4")

    try:
        try:
            if stitch_transition != "cut":
                # xfade только в одном filter_complex (не через -c copy).
                render_scenes_combined(
                    scene_clips,
                    temp_video,
                    width,
                    height,
                    fps,
                    prefer_gpu=prefer_gpu_render,
                    scaled_overlay=scaled_overlay,
                    total_duration_sec=total_video_duration,
                    video_transition=stitch_transition,
                    video_transition_duration=stitch_overlap,
                    log=log,
                )
            else:
                render_scenes_batched(
                    scene_clips,
                    temp_video,
                    temp_dir,
                    width,
                    height,
                    fps,
                    prefer_gpu=prefer_gpu_render,
                    scaled_overlay=scaled_overlay,
                    total_duration_sec=total_video_duration,
                    log=log,
                )
        except Exception as batched_err:
            if stitch_transition != "cut":
                raise
            workers = _scene_parallel_workers(num_scenes)
            _log(
                f"    Батчевый рендер не удался ({batched_err}), "
                f"параллельный рендер по сценам ({workers} потоков)…",
                log,
            )
            scene_files = render_scenes_parallel(
                scene_clips,
                temp_dir,
                width,
                height,
                fps,
                prefer_gpu=prefer_gpu_render,
                log=log,
            )
            concat_out = os.path.join(temp_dir, "concat_no_text.mp4")
            _concat_scene_files(scene_files, concat_out)
            if scaled_overlay is not None:
                from zaliver.processing.slicing_overlay import apply_text_overlay_to_video

                _log("    Наложение текста после склейки сцен…", log)
                apply_text_overlay_to_video(
                    concat_out,
                    temp_video,
                    scaled_overlay,
                    log=log,
                    prefer_gpu=bool(use_gpu_finalize),
                )
            else:
                shutil.move(concat_out, temp_video)

    except subprocess.CalledProcessError as e:
        print(f"    Ошибка при создании видео: {e}")
        if e.stderr:
            print(f"    ffmpeg: {e.stderr.decode(errors='replace')[:800]}")
        return None
    except Exception as e:
        print(f"    Ошибка при создании видео: {e}")
        return None

    if not os.path.exists(temp_video) or os.path.getsize(temp_video) < 1000:
        print("    Ошибка: видео не создано")
        return None

    # Получаем длительность видео
    video_duration = probe_media_duration_seconds(temp_video) or total_video_duration
    _log(f"    Фактическая длительность видео: {video_duration:.2f}с", log)

    # Обрезаем видео, если ffmpeg сделал его длиннее запланированного
    if video_duration > total_video_duration + 0.05:
        trimmed_video = os.path.join(temp_dir, "trimmed_video.mp4")
        try:
            run_ffmpeg(["-i", temp_video, "-t", f"{total_video_duration:.6f}", "-c", "copy", trimmed_video])
            temp_video = trimmed_video
            video_duration = total_video_duration
            _log(f"    Видео обрезано до {total_video_duration:.3f}с", log)
        except Exception as e:
            _log(f"    Предупреждение: не удалось обрезать видео: {e}", log)

    # Аудио должно покрывать фактическую длину ролика (иначе тишина во второй половине).
    final_duration = max(float(total_video_duration), float(video_duration or 0.0))

    # ====== ДОБАВЛЯЕМ АУДИО ======
    temp_audio = os.path.join(temp_dir, "temp_audio.wav")

    try:
        ok_audio = build_audio_wav_exact_duration(
            audio_file,
            start_sec=float(video_start),
            duration_sec=float(final_duration),
            out_wav=temp_audio,
            temp_dir=temp_dir,
            log=log,
            prefer_loop=bool(loop_audio),
        )

        if ok_audio and os.path.exists(temp_audio) and os.path.getsize(temp_audio) > 1000:
            run_ffmpeg(
                [
                    "-i",
                    temp_video,
                    "-i",
                    temp_audio,
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-t",
                    f"{final_duration:.6f}",
                    output_video,
                ],
            )
            _log(
                f"    Видео сохранено: {output_video} ({final_duration:.2f}с, {num_scenes} сцен)",
                log,
            )
        else:
            _log("    ⚠️ Аудио не добавлено, сохраняем без звука", log)
            shutil.copy2(temp_video, output_video)

    except Exception as e:
        _log(f"    Ошибка при добавлении аудио: {e}", log)
        shutil.copy2(temp_video, output_video)

    # Очищаем
    shutil.rmtree(temp_dir, ignore_errors=True)

    return output_video


def generate_all_videos(
        audio_file,
        segments,
        output_prefix="segment_video",
        *,
        clip_pool: list[str] | None = None,
        clip_dir: str = "clips",
        fps=DEFAULT_SLICE_FPS,
        log: Optional[LogCallback] = None,
):
    """Генерирует видео для всех сегментов"""
    _log("\n" + "=" * 60, log)
    _log("ГЕНЕРАЦИЯ ВИДЕО ДЛЯ ВСЕХ СЕГМЕНТОВ", log)
    _log("=" * 60, log)

    videos = []

    for i, segment in enumerate(segments, 1):
        output_video = f"{output_prefix}_{i:02d}.mp4"
        _log(f"\n--- Обработка сегмента {i} из {len(segments)} ---", log)

        video = generate_video_from_segment(
            audio_file=audio_file,
            segment=segment,
            output_video=output_video,
            clip_pool=clip_pool,
            clip_dir=clip_dir,
            fps=fps,
            log=log,
        )

        if video:
            videos.append(video)
            # Проверяем длительность
            try:
                probe = ffmpeg.probe(video)
                duration = float(probe['format']['duration'])
                file_size = os.path.getsize(video) / (1024 * 1024)
                print(f"    ✅ Видео создано: {video} ({duration:.2f} сек, {file_size:.2f} MB)")
            except:
                print(f"    ✅ Видео создано: {video}")

    print(f"\n✅ Создано {len(videos)} видео:")
    for video in videos:
        file_size = os.path.getsize(video) / (1024 * 1024)
        print(f"  - {video} ({file_size:.2f} MB)")

    return videos


# ============= ИСПОЛЬЗОВАНИЕ =============

if __name__ == "__main__":
    audio_file = "марабу.mp3"

    # ====== НАСТРОЙКИ ======
    CLIP_DIR = "clips"          # папка с исходными видео для сцен
    USE_SUGGESTED_DURATIONS = True  # True — взять MIN/MAX из анализа аудио
    MIN_SCENE_DURATION = 1   # используется, если USE_SUGGESTED_DURATIONS = False
    MAX_SCENE_DURATION = 1.3   # используется, если USE_SUGGESTED_DURATIONS = False
    MIN_SCENES = 12             # минимальное количество сцен
    MAX_SCENES = 23            # максимальное количество сцен
    EDGE_EXCLUDE = 0.05       # исключить какой-то процент из начала и конца видео

    print("СМЕНА КАДРОВ НА КАЖДОМ ПИКЕ АУДИО")
    print("-" * 40)
    print(f"Папка с видео: {CLIP_DIR}")

    duration_suggestions = suggest_scene_durations(
        audio_file=audio_file,
        edge_exclude=EDGE_EXCLUDE,
        min_scenes=MIN_SCENES,
        max_scenes=MAX_SCENES,
    )

    if USE_SUGGESTED_DURATIONS and duration_suggestions:
        MIN_SCENE_DURATION = duration_suggestions['min_scene_duration']
        MAX_SCENE_DURATION = duration_suggestions['max_scene_duration']
        print("\nИспользуются рекомендованные длительности сцен")
    elif duration_suggestions:
        print("\nРекомендации выше; сейчас используются ручные значения:")
        print(f"  MIN_SCENE_DURATION = {MIN_SCENE_DURATION}")
        print(f"  MAX_SCENE_DURATION = {MAX_SCENE_DURATION}")

    print(f"Длительность сцены: {MIN_SCENE_DURATION} — {MAX_SCENE_DURATION} сек")
    print(f"Количество сцен: {MIN_SCENES} — {MAX_SCENES}")
    print(f"Исключение краёв: {EDGE_EXCLUDE * 100:.0f}% начала и конца")

    segments = find_segments_with_peaks(
        audio_file=audio_file,
        min_scene_duration=MIN_SCENE_DURATION,
        max_scene_duration=MAX_SCENE_DURATION,
        min_scenes=MIN_SCENES,
        max_scenes=MAX_SCENES,
        edge_exclude=EDGE_EXCLUDE,
    )

    if segments is None or not segments:
        print("\nНе найдено подходящих сегментов")
        exit()

    print(f"\nНайден {len(segments)} сегмент для генерации")

    generate_all_videos(
        audio_file=audio_file,
        segments=segments,
        output_prefix="peak_segment",
        clip_dir=CLIP_DIR,
        fps=DEFAULT_SLICE_FPS,
    )