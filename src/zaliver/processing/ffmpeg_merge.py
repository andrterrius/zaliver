"""ffmpeg: concat segments and mux audio from source."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, List, Optional

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
    log: LogFn = None,
) -> None:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    v = Path(video_path).resolve().as_posix()
    a = Path(audio_source_path).resolve().as_posix()
    o = str(out)
    if audio_atempo is not None and abs(audio_atempo - 1.0) > 1e-3:
        filt = _atempo_filter_complex(audio_atempo)
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
        log=log,
    )
