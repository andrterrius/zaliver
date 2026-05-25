"""ffmpeg: concat segments and mux audio from source."""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, List, Optional, Tuple

LogFn = Optional[Callable[[str], None]]

# Optional full path to ffmpeg.exe set from UI / settings (overrides auto-detect).
_explicit_ffmpeg: Optional[str] = None


def _popen_flags() -> int:
    """Hide console window on Windows for child processes (ffmpeg)."""
    if sys.platform == "win32":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def set_ffmpeg_executable(path: Optional[str]) -> None:
    """Force ffmpeg location. Pass None or empty string to use auto-detection only."""
    global _explicit_ffmpeg
    if path is None or not str(path).strip():
        _explicit_ffmpeg = None
    else:
        _explicit_ffmpeg = str(path).strip()


def _env_path() -> Optional[str]:
    raw = os.environ.get("ZALIVER_FFMPEG", "").strip()
    if not raw:
        return None
    p = Path(raw)
    return str(p.resolve()) if p.is_file() else None


def _scan_os_path() -> Optional[str]:
    """Walk PATH entries (GUI apps on Windows often miss entries that a new shell has)."""
    path_env = os.environ.get("PATH", "")
    if not path_env:
        return None
    names = ("ffmpeg.exe", "ffmpeg") if sys.platform == "win32" else ("ffmpeg",)
    for part in path_env.split(os.pathsep):
        part = part.strip().strip('"')
        if not part:
            continue
        base = Path(part)
        for name in names:
            cand = base / name
            try:
                if cand.is_file():
                    return str(cand.resolve())
            except OSError:
                continue
    return None


def _windows_install_candidates() -> List[Path]:
    paths: List[Path] = []
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    progdata = os.environ.get("ProgramData", r"C:\ProgramData")
    home = Path.home()

    paths.extend(
        [
            Path(pf) / "ffmpeg" / "bin" / "ffmpeg.exe",
            Path(pfx86) / "ffmpeg" / "bin" / "ffmpeg.exe",
            Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
            Path(progdata) / "chocolatey" / "bin" / "ffmpeg.exe",
        ]
    )
    if local:
        paths.append(Path(local) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe")
        paths.append(Path(local) / "scoop" / "shims" / "ffmpeg.exe")
    paths.append(home / "scoop" / "shims" / "ffmpeg.exe")
    return paths


def _winget_ffmpeg_glob() -> Optional[str]:
    if sys.platform != "win32":
        return None
    local = os.environ.get("LOCALAPPDATA", "")
    if not local:
        return None
    root = Path(local) / "Microsoft" / "WinGet" / "Packages"
    if not root.is_dir():
        return None
    try:
        for sub in root.iterdir():
            if not sub.is_dir():
                continue
            if "ffmpeg" not in sub.name.lower():
                continue
            for exe in sub.rglob("ffmpeg.exe"):
                if exe.is_file():
                    return str(exe.resolve())
    except OSError:
        pass
    return None


def _unix_candidates() -> List[Path]:
    return [
        Path("/opt/homebrew/bin/ffmpeg"),
        Path("/usr/local/bin/ffmpeg"),
    ]


def _bundle_roots() -> List[Path]:
    """Dirs where we ship ffmpeg.exe (PyInstaller, Nuitka standalone/onefile)."""
    frozen = bool(getattr(sys, "frozen", False))
    meipass = getattr(sys, "_MEIPASS", None)
    compiled = globals().get("__compiled__")
    if not frozen and not meipass and compiled is None:
        return []

    roots: List[Path] = []
    if meipass:
        roots.append(Path(meipass))
    if compiled is not None:
        try:
            cd = getattr(compiled, "containing_dir", None)
            if cd:
                roots.append(Path(str(cd)))
        except (TypeError, ValueError, OSError):
            pass
    roots.append(Path(sys.executable).resolve().parent)

    seen: set[str] = set()
    out: List[Path] = []
    for r in roots:
        try:
            key = str(r.resolve())
        except OSError:
            key = str(r)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _bundled_ffmpeg() -> Optional[str]:
    """ffmpeg shipped next to the binary (Nuitka / PyInstaller)."""
    for root in _bundle_roots():
        for name in ("ffmpeg.exe", "ffmpeg"):
            p = root / name
            if p.is_file():
                return str(p.resolve())
    return None


def resolve_ffmpeg_executable() -> Optional[str]:
    """Return absolute path to ffmpeg, or None."""
    if _explicit_ffmpeg:
        p = Path(_explicit_ffmpeg)
        if p.is_file():
            return str(p.resolve())
    hit = _env_path()
    if hit:
        return hit
    bundled = _bundled_ffmpeg()
    if bundled:
        return bundled
    w = shutil.which("ffmpeg")
    if w:
        return w
    scanned = _scan_os_path()
    if scanned:
        return scanned
    cands = (
        _windows_install_candidates()
        if sys.platform == "win32"
        else _unix_candidates()
    )
    for c in cands:
        try:
            if c.is_file():
                return str(c.resolve())
        except OSError:
            continue
    return _winget_ffmpeg_glob()


def check_ffmpeg() -> bool:
    return resolve_ffmpeg_executable() is not None


def check_ffmpeg_tools() -> bool:
    """ffmpeg + ffprobe (обработка и метаданные)."""
    if not check_ffmpeg():
        return False
    from zaliver.processing.ffmpeg_probe import resolve_ffprobe_executable

    return resolve_ffprobe_executable() is not None


def run_ffmpeg(
    args: List[str],
    log: LogFn = None,
) -> None:
    exe = resolve_ffmpeg_executable()
    if not exe:
        raise RuntimeError("ffmpeg не найден")
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-y", *args]
    if log:
        log(" ".join(cmd))
    p = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_popen_flags(),
    )
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip()
        raise RuntimeError(err or f"ffmpeg failed with code {p.returncode}")


_cached_encoder_list: Optional[str] = None
_encoder_runtime_ok: dict[str, bool] = {}
_encoder_runtime_err: dict[str, str] = {}


def ffmpeg_encoder_list_text() -> str:
    """Return ffmpeg -encoders output (cached)."""
    global _cached_encoder_list
    if _cached_encoder_list is not None:
        return _cached_encoder_list
    exe = resolve_ffmpeg_executable()
    if not exe:
        _cached_encoder_list = ""
        return _cached_encoder_list
    try:
        p = subprocess.run(
            [exe, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
            creationflags=_popen_flags(),
        )
        _cached_encoder_list = (p.stdout or "") + "\n" + (p.stderr or "")
    except Exception:
        _cached_encoder_list = ""
    return _cached_encoder_list


def _probe_encoder_runtime(encoder: str) -> bool:
    """
    Some encoders show up in `ffmpeg -encoders` but are not usable at runtime
    (e.g. NVENC without NVIDIA driver -> cannot load nvcuda.dll).
    We do a tiny 1-frame encode to null and cache result.
    """
    enc = str(encoder).strip()
    if not enc:
        return False
    if enc in _encoder_runtime_ok:
        return _encoder_runtime_ok[enc]
    exe = resolve_ffmpeg_executable()
    if not exe:
        _encoder_runtime_ok[enc] = False
        return False
    # Some encoders are picky about pixel format and/or minimum frame size.
    # Use a "realistic" small HD-ish frame and a safe hw-friendly pix_fmt.
    if enc in ("h264_amf", "hevc_amf", "av1_amf"):
        lavfi = "color=c=black:s=1280x720:r=30"
        vf = "format=nv12"
    elif enc in ("h264_qsv", "hevc_qsv", "av1_qsv"):
        lavfi = "color=c=black:s=1280x720:r=30"
        vf = "format=nv12"
    else:
        lavfi = "color=c=black:s=640x360:r=30"
        vf = "format=yuv420p"
    try:
        # Some HW encoders need a tiny bit more time on first init (driver spin-up).
        p = subprocess.run(
            [
                exe,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                lavfi,
                "-frames:v",
                "1",
                "-vf",
                vf,
                "-an",
                "-c:v",
                enc,
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=_popen_flags(),
        )
        ok = p.returncode == 0
        if not ok:
            _encoder_runtime_err[enc] = (p.stderr or p.stdout or "").strip()[:800]
    except Exception as e:
        ok = False
        _encoder_runtime_err[enc] = str(e)[:800]
    _encoder_runtime_ok[enc] = ok
    return ok


def encoder_runtime_error(encoder: str) -> str:
    """Return cached runtime probe error text (if any)."""
    return _encoder_runtime_err.get(str(encoder).strip(), "")


_MIN_TARGET_VIDEO_BPS = 300_000
_MAX_TARGET_VIDEO_BPS = 150_000_000


def clamp_target_video_bps(bps: int) -> int:
    return max(_MIN_TARGET_VIDEO_BPS, min(int(bps), _MAX_TARGET_VIDEO_BPS))


def libx264_encode_args_for_target(target_video_bps: Optional[int]) -> List[str]:
    """CRF по умолчанию или VBV по целевому битрейту (подгонка размера к исходнику)."""
    if target_video_bps is None or target_video_bps <= 0:
        return ["-preset", "veryfast", "-crf", "20"]
    b = clamp_target_video_bps(target_video_bps)
    maxr = max(b + 1, int(b * 1.35))
    buf = max(b * 2, int(b * 2))
    return ["-preset", "veryfast", "-b:v", str(b), "-maxrate", str(maxr), "-bufsize", str(buf)]


def _h264_nvenc_args(target_video_bps: Optional[int]) -> List[str]:
    if target_video_bps is None or target_video_bps <= 0:
        return ["-preset", "p4", "-cq", "23", "-b:v", "0"]
    b = clamp_target_video_bps(target_video_bps)
    return [
        "-preset",
        "p4",
        "-tune",
        "hq",
        "-b:v",
        str(b),
        "-maxrate",
        str(max(b + 1, int(b * 1.45))),
        "-bufsize",
        str(max(b * 2, int(b * 2))),
    ]


def _h264_qsv_args(target_video_bps: Optional[int]) -> List[str]:
    if target_video_bps is None or target_video_bps <= 0:
        return ["-global_quality", "23", "-look_ahead", "1"]
    b = clamp_target_video_bps(target_video_bps)
    maxr = max(b + 1, int(b * 1.45))
    buf = max(b * 2, int(b * 2))
    return [
        "-look_ahead",
        "1",
        "-b:v",
        str(b),
        "-maxrate",
        str(maxr),
        "-bufsize",
        str(buf),
    ]


def _h264_amf_args(target_video_bps: Optional[int]) -> List[str]:
    if target_video_bps is None or target_video_bps <= 0:
        return [
            "-usage",
            "transcoding",
            "-quality",
            "speed",
            "-rc",
            "cqp",
            "-qp_i",
            "23",
            "-qp_p",
            "23",
            "-qp_b",
            "23",
            "-bf",
            "0",
            "-async_depth",
            "32",
        ]
    b = clamp_target_video_bps(target_video_bps)
    maxr = max(b + 1, int(b * 1.5))
    return [
        "-usage",
        "transcoding",
        "-quality",
        "balanced",
        "-rc",
        "vbr_peak",
        "-b:v",
        str(b),
        "-maxrate",
        str(maxr),
        "-async_depth",
        "32",
    ]


def pick_best_h264_encoder(
    *, prefer_gpu: bool = True, target_video_bps: Optional[int] = None
) -> Tuple[str, List[str]]:
    """
    Return (encoder_name, extra_args) preferring GPU encoders if available.
    If target_video_bps is set, args target that video bitrate (VBR) to approximate source file size.
    Otherwise NVENC/QSV/AMF use quality (CQ) presets and libx264 uses CRF 20.
    """
    txt = ffmpeg_encoder_list_text().lower()
    if prefer_gpu and "h264_nvenc" in txt and _probe_encoder_runtime("h264_nvenc"):
        return ("h264_nvenc", _h264_nvenc_args(target_video_bps))
    if prefer_gpu and "h264_qsv" in txt and _probe_encoder_runtime("h264_qsv"):
        return ("h264_qsv", _h264_qsv_args(target_video_bps))
    if prefer_gpu and "h264_amf" in txt and _probe_encoder_runtime("h264_amf"):
        return ("h264_amf", _h264_amf_args(target_video_bps))
    return ("libx264", libx264_encode_args_for_target(target_video_bps))


# concat demuxer открывает все входы сразу; на Windows лимит ~512 дескрипторов.
_MAX_FFMPEG_CONCAT_FILES = 16


def _write_concat_demuxer_list(segment_paths: List[str], list_path: str) -> None:
    with open(list_path, "w", encoding="utf-8") as f:
        for p in segment_paths:
            line = Path(p).resolve().as_posix().replace("'", "'\\''")
            f.write(f"file '{line}'\n")


def _concat_segments_once(
    segment_paths: List[str],
    out_path: str,
    log: LogFn = None,
    target_video_bps: Optional[int] = None,
) -> None:
    """Один вызов ffmpeg concat (не больше _MAX_FFMPEG_CONCAT_FILES входов)."""
    if not segment_paths:
        raise ValueError("No segments to concat")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if len(segment_paths) == 1:
        src = Path(segment_paths[0]).resolve()
        if src != out.resolve():
            shutil.copy2(src, out)
        return
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as f:
        _write_concat_demuxer_list(segment_paths, f.name)
        list_path = f.name
    try:
        run_ffmpeg(
            [
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                list_path,
                "-c",
                "copy",
                str(out),
            ],
            log=log,
        )
    except RuntimeError:
        vargs = libx264_encode_args_for_target(target_video_bps)
        run_ffmpeg(
            [
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                list_path,
                "-c:v",
                "libx264",
                *vargs,
                "-an",
                str(out),
            ],
            log=log,
        )
    finally:
        try:
            Path(list_path).unlink(missing_ok=True)
        except OSError:
            pass


def concat_segments(
    segment_paths: List[str],
    out_path: str,
    log: LogFn = None,
    target_video_bps: Optional[int] = None,
) -> None:
    paths = [str(p) for p in segment_paths if p]
    if not paths:
        raise ValueError("No segments to concat")
    if len(paths) <= _MAX_FFMPEG_CONCAT_FILES:
        _concat_segments_once(
            paths, out_path, log=log, target_video_bps=target_video_bps
        )
        return

    if log:
        log(
            f"Склейка {len(paths)} частей пакетами по {_MAX_FFMPEG_CONCAT_FILES} "
            f"(обход лимита открытых файлов ОС)"
        )
    work = Path(out_path).parent
    temps: List[Path] = []
    current = paths
    batch_idx = 0
    try:
        while len(current) > _MAX_FFMPEG_CONCAT_FILES:
            nxt: List[str] = []
            for i in range(0, len(current), _MAX_FFMPEG_CONCAT_FILES):
                batch = current[i : i + _MAX_FFMPEG_CONCAT_FILES]
                if len(batch) == 1:
                    nxt.append(batch[0])
                    continue
                tmp = work / f".zaliver_concat_{batch_idx:04d}.mp4"
                batch_idx += 1
                _concat_segments_once(
                    batch,
                    str(tmp),
                    log=log,
                    target_video_bps=target_video_bps,
                )
                temps.append(tmp)
                nxt.append(str(tmp))
            current = nxt
        _concat_segments_once(
            current, out_path, log=log, target_video_bps=target_video_bps
        )
    finally:
        out_resolved = Path(out_path).resolve()
        for tmp in temps:
            try:
                if tmp.resolve() != out_resolved:
                    tmp.unlink(missing_ok=True)
            except OSError:
                pass


def _atempo_chain(inp: str, speed_factor: float, out_label: str) -> str:
    """speed_factor: 1.0 = unchanged; each atempo must be in [0.5, 2.0].
    `inp` / `out_label` are ffmpeg pad names, e.g. '[1:a]' and '[aout]'."""
    r = float(speed_factor)
    if r <= 0.0:
        r = 1.0
    parts: List[str] = []
    cur = inp
    n = 0
    while r > 2.0 + 1e-9:
        nxt = f"[at{n}]"
        parts.append(f"{cur}atempo=2.0{nxt}")
        cur, r, n = nxt, r / 2.0, n + 1
    while r < 0.5 - 1e-9:
        nxt = f"[at{n}]"
        parts.append(f"{cur}atempo=0.5{nxt}")
        cur, r, n = nxt, r / 0.5, n + 1
    r = min(max(r, 0.5), 2.0)
    parts.append(f"{cur}atempo={r:.6f}{out_label}")
    return ";".join(parts)


def _chorus_filter(inp: str, out_label: str) -> str:
    # Very subtle chorus to avoid obvious "robotic" artifacts.
    # chorus=in_gain:out_gain:delays:decays:speeds:depths
    return f"{inp}chorus=0.65:0.75:40:0.20:0.25:2{out_label}"


def source_file_has_audio(path: str) -> bool:
    """Есть ли в файле хотя бы один аудиопоток (для filter_complex с [1:a])."""
    try:
        from zaliver.processing.ffmpeg_probe import resolve_ffprobe_executable
    except ImportError:
        return False
    probe = resolve_ffprobe_executable()
    pth = Path(path)
    if not probe or not pth.is_file():
        return False
    cmd = [
        probe,
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type",
        "-select_streams",
        "a",
        "-of",
        "json",
        str(pth),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_popen_flags(),
            timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if proc.returncode != 0:
        return False
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return False
    streams = data.get("streams") or []
    return len(streams) > 0


def mux_video_audio(
    video_path: str,
    audio_source_path: str,
    out_path: str,
    playback_speed: Optional[float] = None,
    audio_chorus: bool = False,
    log: LogFn = None,
    target_video_bps: Optional[int] = None,
) -> None:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    v = Path(video_path).resolve().as_posix()
    a = Path(audio_source_path).resolve().as_posix()
    o = str(out)
    spd = float(playback_speed) if playback_speed is not None else 1.0
    want_speed = abs(spd - 1.0) > 1e-3
    want_chorus = bool(audio_chorus)
    has_audio = source_file_has_audio(a)

    if want_speed or want_chorus:
        if not has_audio:
            if log:
                if want_chorus and not want_speed:
                    log("В исходнике нет аудио — хорус не применяется, копируется только видео.")
                elif want_chorus and want_speed:
                    log(
                        "В исходнике нет аудио — ускоряется только видео, хорус и звук пропущены."
                    )
                elif want_speed:
                    log("В исходнике нет аудио — сохраняется только ускоренное видео (без звука).")
            if want_speed:
                filt = f"[0:v]setpts=PTS/{spd:.9f}[vout]"
                vargs = libx264_encode_args_for_target(target_video_bps)
                run_ffmpeg(
                    [
                        "-i",
                        v,
                        "-filter_complex",
                        filt,
                        "-map",
                        "[vout]",
                        "-an",
                        "-c:v",
                        "libx264",
                        *vargs,
                        "-pix_fmt",
                        "yuv420p",
                        "-movflags",
                        "+faststart",
                        o,
                    ],
                    log=log,
                )
            else:
                run_ffmpeg(
                    ["-i", v, "-c:v", "copy", "-an", o],
                    log=log,
                )
            return

        parts: List[str] = []
        if want_speed:
            # Видео: setpts=PTS/s — то же относительное ускорение, что и atempo=s для аудио.
            parts.append(f"[0:v]setpts=PTS/{spd:.9f}[vout]")
            if want_chorus:
                parts.append(_atempo_chain("[1:a]", spd, "[aspd]"))
                parts.append(_chorus_filter("[aspd]", "[aout]"))
            else:
                parts.append(_atempo_chain("[1:a]", spd, "[aout]"))
            filt = ";".join(parts)
            vargs = libx264_encode_args_for_target(target_video_bps)
            run_ffmpeg(
                [
                    "-i",
                    v,
                    "-i",
                    a,
                    "-filter_complex",
                    filt,
                    "-map",
                    "[vout]",
                    "-map",
                    "[aout]",
                    "-c:v",
                    "libx264",
                    *vargs,
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    "-c:a",
                    "aac",
                    "-shortest",
                    o,
                ],
                log=log,
            )
            return

        # Только хорус: видео без перекодирования.
        filt = _chorus_filter("[1:a]", "[aout]")
        run_ffmpeg(
            [
                "-i",
                v,
                "-i",
                a,
                "-filter_complex",
                filt,
                "-map",
                "0:v:0",
                "-map",
                "[aout]",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-shortest",
                o,
            ],
            log=log,
        )
        return

    try:
        run_ffmpeg(
            [
                "-i",
                v,
                "-i",
                a,
                "-map",
                "0:v:0",
                "-map",
                "1:a:0?",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-shortest",
                o,
            ],
            log=log,
        )
    except RuntimeError:
        if log:
            log("Повтор без аудиодорожки (копия только видео)")
        run_ffmpeg(
            ["-i", v, "-c", "copy", "-an", o],
            log=log,
        )


def merge_segments_with_source_audio(
    segment_paths: List[str],
    source_video: str,
    final_output: str,
    work_dir: str,
    playback_speed: Optional[float] = None,
    audio_chorus: bool = False,
    log: LogFn = None,
    target_video_bps: Optional[int] = None,
    background_music_path: Optional[str] = None,
    music_video_meta: Optional[Tuple[int, float]] = None,
    background_music_mix: bool = False,
    background_music_volume_pct: float = 35.0,
) -> None:
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    concat_out = work / "concat_video.mp4"
    concat_segments(
        segment_paths, str(concat_out), log=log, target_video_bps=target_video_bps
    )
    if background_music_path and str(background_music_path).strip():
        fc, fpsi = music_video_meta or (0, 30.0)
        mux_video_background_music(
            str(concat_out),
            str(background_music_path).strip(),
            final_output,
            frame_count=max(1, int(fc)),
            fps=float(fpsi) if float(fpsi) > 1e-6 else 30.0,
            playback_speed=playback_speed,
            log=log,
            target_video_bps=target_video_bps,
            mix_with_source=bool(background_music_mix),
            source_video_path=str(source_video),
            audio_chorus=bool(audio_chorus),
            music_volume_pct=float(background_music_volume_pct),
        )
    else:
        mux_video_audio(
            str(concat_out),
            source_video,
            final_output,
            playback_speed=playback_speed,
            audio_chorus=audio_chorus,
            log=log,
            target_video_bps=target_video_bps,
        )


def _output_duration_after_speed(
    *, frame_count: int, fps: float, playback_speed: float
) -> float:
    if fps <= 1e-6:
        fps = 30.0
    spd = float(playback_speed)
    if spd <= 1e-9:
        spd = 1.0
    return (float(frame_count) / fps) / spd


def _random_music_trim_start_sec(
    music_duration_sec: Optional[float], needed_sec: float
) -> float:
    """Случайная фаза на шкале времени (сек); при зацикленном входе задаёт «случайный отрезок»."""
    need = max(0.05, float(needed_sec))
    if music_duration_sec is None or music_duration_sec <= 0.05:
        return random.uniform(0.0, max(need, 600.0))
    d = float(music_duration_sec)
    # Равномерно по кругу длины d (зацикленный поток).
    return random.uniform(0.0, d)


def _music_volume_linear_from_pct(pct: float) -> float:
    """0…100 % → множитель для фильтра volume (0 = без музыки)."""
    try:
        p = float(pct)
    except (TypeError, ValueError):
        p = 35.0
    return max(0.0, min(100.0, p)) / 100.0


def _mux_bgm_replace_only(
    video_path: str,
    music_path: str,
    out_path: str,
    *,
    trim_start: float,
    dur_needed: float,
    want_speed: bool,
    spd: float,
    log: LogFn,
    target_video_bps: Optional[int],
) -> None:
    """Видео + только музыка (как раньше)."""
    v = Path(video_path).resolve().as_posix()
    m = Path(music_path).resolve().as_posix()
    o = str(out_path)
    a_filt = (
        f"[1:a]atrim=start={trim_start:.6f}:duration={dur_needed:.6f},"
        f"asetpts=PTS-STARTPTS[aout]"
    )
    vargs = libx264_encode_args_for_target(target_video_bps)
    if want_speed:
        filt = f"[0:v]setpts=PTS/{spd:.9f}[vout];{a_filt}"
        run_ffmpeg(
            [
                "-i",
                v,
                "-stream_loop",
                "-1",
                "-i",
                m,
                "-filter_complex",
                filt,
                "-map",
                "[vout]",
                "-map",
                "[aout]",
                "-c:v",
                "libx264",
                *vargs,
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-c:a",
                "aac",
                "-shortest",
                o,
            ],
            log=log,
        )
    else:
        run_ffmpeg(
            [
                "-i",
                v,
                "-stream_loop",
                "-1",
                "-i",
                m,
                "-filter_complex",
                a_filt,
                "-map",
                "0:v:0",
                "-map",
                "[aout]",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-shortest",
                o,
            ],
            log=log,
        )


def mux_video_background_music(
    video_path: str,
    music_path: str,
    out_path: str,
    *,
    frame_count: int,
    fps: float,
    playback_speed: Optional[float] = None,
    log: LogFn = None,
    target_video_bps: Optional[int] = None,
    mix_with_source: bool = False,
    source_video_path: Optional[str] = None,
    audio_chorus: bool = False,
    music_volume_pct: float = 35.0,
) -> None:
    """
    Видео без звука + фоновая музыка (случайный отрезок снаружи).
    При mix_with_source и наличии аудио в source — amix: исходник (скорость/хорус как mux_video_audio)
    + музыка с громкостью music_volume_pct (0…100 % от полной амплитуды слоя).
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    v = Path(video_path).resolve().as_posix()
    m = Path(music_path).resolve().as_posix()
    o = str(out)
    spd = float(playback_speed) if playback_speed is not None else 1.0
    want_speed = abs(spd - 1.0) > 1e-3
    want_chorus = bool(audio_chorus)
    dur_needed = _output_duration_after_speed(
        frame_count=int(frame_count),
        fps=float(fps),
        playback_speed=spd,
    )
    dur_needed = max(0.05, dur_needed)

    music_dur: Optional[float] = None
    try:
        from zaliver.processing.ffmpeg_probe import probe_media_duration_seconds

        music_dur = probe_media_duration_seconds(m)
    except Exception:
        music_dur = None
    if not source_file_has_audio(m):
        raise RuntimeError(f"Нет аудиопотока в файле фоновой музыки: {Path(m).name} ({m})")

    trim_start = _random_music_trim_start_sec(music_dur, dur_needed)
    vol_lin = _music_volume_linear_from_pct(music_volume_pct)

    src_p = (source_video_path or "").strip()
    use_mix = (
        bool(mix_with_source)
        and bool(src_p)
        and source_file_has_audio(str(Path(src_p).resolve()))
    )
    if log:
        md = f"{music_dur:.2f}s" if music_dur is not None else "?"
        mode = "наложение на звук видео" if use_mix else "замена звука"
        log(
            f"Фоновая музыка ({mode}): {Path(m).name}, фаза ~{trim_start:.2f}s, "
            f"длина {dur_needed:.2f}s (трек {md})"
            + (f", громкость музыки {vol_lin * 100:.0f}%" if use_mix else "")
        )

    if not use_mix:
        _mux_bgm_replace_only(
            video_path,
            music_path,
            out_path,
            trim_start=trim_start,
            dur_needed=dur_needed,
            want_speed=want_speed,
            spd=spd,
            log=log,
            target_video_bps=target_video_bps,
        )
        return

    sx = Path(src_p).resolve().as_posix()
    music_f = (
        f"[2:a]atrim=start={trim_start:.6f}:duration={dur_needed:.6f},"
        f"asetpts=PTS-STARTPTS,volume={vol_lin:.6f}[a_mus]"
    )
    amix_f = (
        "[a_src][a_mus]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
    )

    if want_speed:
        vargs = libx264_encode_args_for_target(target_video_bps)
        parts: List[str] = [f"[0:v]setpts=PTS/{spd:.9f}[vout]"]
        if want_chorus:
            parts.append(_atempo_chain("[1:a]", spd, "[aspd]"))
            parts.append(_chorus_filter("[aspd]", "[a_src]"))
        else:
            parts.append(_atempo_chain("[1:a]", spd, "[a_src]"))
        parts.append(music_f)
        parts.append(amix_f)
        filt = ";".join(parts)
        run_ffmpeg(
            [
                "-i",
                v,
                "-i",
                sx,
                "-stream_loop",
                "-1",
                "-i",
                m,
                "-filter_complex",
                filt,
                "-map",
                "[vout]",
                "-map",
                "[aout]",
                "-c:v",
                "libx264",
                *vargs,
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-c:a",
                "aac",
                "-shortest",
                o,
            ],
            log=log,
        )
        return

    if want_chorus:
        filt = ";".join([_chorus_filter("[1:a]", "[a_src]"), music_f, amix_f])
        run_ffmpeg(
            [
                "-i",
                v,
                "-i",
                sx,
                "-stream_loop",
                "-1",
                "-i",
                m,
                "-filter_complex",
                filt,
                "-map",
                "0:v:0",
                "-map",
                "[aout]",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-shortest",
                o,
            ],
            log=log,
        )
        return

    # Только наложение музыки, видео без перекодирования, исходник без фильтров по скорости/хорусу.
    filt = ";".join([music_f, "[1:a][a_mus]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"])
    run_ffmpeg(
        [
            "-i",
            v,
            "-i",
            sx,
            "-stream_loop",
            "-1",
            "-i",
            m,
            "-filter_complex",
            filt,
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-shortest",
            o,
        ],
        log=log,
    )
