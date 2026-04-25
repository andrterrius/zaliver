"""ffmpeg: concat segments and mux audio from source."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, List, Optional, Tuple

LogFn = Optional[Callable[[str], None]]

# Optional full path to ffmpeg.exe set from UI / settings (overrides auto-detect).
_explicit_ffmpeg: Optional[str] = None


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


def pick_best_h264_encoder(*, prefer_gpu: bool = True) -> Tuple[str, List[str]]:
    """
    Return (encoder_name, extra_args) preferring GPU encoders if available.
    If no GPU encoder is found, returns ("libx264", ["-preset","veryfast","-crf","20"]).
    """
    txt = ffmpeg_encoder_list_text().lower()
    # Preference order: NVIDIA -> Intel -> AMD, then CPU.
    if prefer_gpu and "h264_nvenc" in txt and _probe_encoder_runtime("h264_nvenc"):
        # "p1..p7" presets exist on modern FFmpeg; "p4" is a good default.
        return ("h264_nvenc", ["-preset", "p4", "-cq", "23", "-b:v", "0"])
    if prefer_gpu and "h264_qsv" in txt and _probe_encoder_runtime("h264_qsv"):
        # QSV: use global_quality when supported; fallback is still OK.
        return ("h264_qsv", ["-global_quality", "23", "-look_ahead", "1"])
    if prefer_gpu and "h264_amf" in txt and _probe_encoder_runtime("h264_amf"):
        # AMF: tune for throughput (speed) by default.
        # Notes:
        # - We keep CQP for stable quality without bitrate planning overhead.
        # - Disable B-frames for lower latency and typically higher speed.
        # - Raise async_depth to allow deeper internal parallelism.
        return (
            "h264_amf",
            [
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
            ],
        )
    return ("libx264", ["-preset", "veryfast", "-crf", "20"])


def concat_segments(segment_paths: List[str], out_path: str, log: LogFn = None) -> None:
    if not segment_paths:
        raise ValueError("No segments to concat")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as f:
        for p in segment_paths:
            line = Path(p).resolve().as_posix().replace("'", "'\\''")
            f.write(f"file '{line}'\n")
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
                "-preset",
                "veryfast",
                "-crf",
                "20",
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


def _atempo_filter_complex(speed_factor: float) -> str:
    """speed_factor: 1.0 = unchanged; each atempo must be in [0.5, 2.0]."""
    r = float(speed_factor)
    parts: List[str] = []
    cur = "[1:a]"
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
    parts.append(f"{cur}atempo={r:.6f}[aout]")
    return ";".join(parts)


def mux_video_audio(
    video_path: str,
    audio_source_path: str,
    out_path: str,
    audio_atempo: Optional[float] = None,
    audio_chorus: bool = False,
    log: LogFn = None,
) -> None:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    v = Path(video_path).resolve().as_posix()
    a = Path(audio_source_path).resolve().as_posix()
    o = str(out)
    want_atempo = audio_atempo is not None and abs(float(audio_atempo) - 1.0) > 1e-3
    want_chorus = bool(audio_chorus)
    if want_atempo or want_chorus:
        parts: List[str] = []
        if want_atempo:
            parts.append(_atempo_filter_complex(float(audio_atempo)))
            cur = "[aout]"
        else:
            # No tempo change: start from input audio stream.
            cur = "[1:a]"
        if want_chorus:
            # Very subtle chorus to avoid obvious "robotic" artifacts.
            # chorus=in_gain:out_gain:delays:decays:speeds:depths
            parts.append(f"{cur}chorus=0.65:0.75:40:0.20:0.25:2[aout]")
        filt = ";".join(parts)
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
    audio_atempo: Optional[float] = None,
    audio_chorus: bool = False,
    log: LogFn = None,
) -> None:
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    concat_out = work / "concat_video.mp4"
    concat_segments(segment_paths, str(concat_out), log=log)
    mux_video_audio(
        str(concat_out),
        source_video,
        final_output,
        audio_atempo=audio_atempo,
        audio_chorus=audio_chorus,
        log=log,
    )
