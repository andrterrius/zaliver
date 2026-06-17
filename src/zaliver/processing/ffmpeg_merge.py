"""ffmpeg: concat segments and mux audio from source."""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import gc
from pathlib import Path
from typing import Callable, List, Optional, Tuple

LogFn = Optional[Callable[[str], None]]
CancelCheck = Optional[Callable[[], bool]]


class BackgroundMusicUnavailableError(RuntimeError):
    """Ни один трек фона не прошёл проверку ffprobe (после ретраев и перебора пула)."""


def is_background_music_failure(exc: BaseException) -> bool:
    """Сбой фоновой музыки: ffprobe, декодирование, filter_complex при mux."""
    if isinstance(exc, BackgroundMusicUnavailableError):
        return True
    if isinstance(exc, RuntimeError):
        s = str(exc).lower()
        if s == "cancelled":
            return False
        if "аудиопоток" in s or "треке фона" in s or "фоновую музыку" in s:
            return True
        markers = (
            "header missing",
            "invalid data found when processing input",
            "error initializing filters",
            "neither number of channels nor channel layout",
            "nothing was written into output file",
            "decode error rate",
            "error submitting packet to decoder",
            "[mp3",
            "incorrect bom value",
        )
        return any(m in s for m in markers)
    return False


def is_background_music_probe_error(exc: BaseException) -> bool:
    return is_background_music_failure(exc)


def _sleep_interruptible(
    seconds: float,
    *,
    cancel_check: CancelCheck = None,
) -> bool:
    """Пауза с проверкой отмены. False — прервано."""
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        if cancel_check and cancel_check():
            return False
        step = min(0.25, remaining)
        time.sleep(step)
        remaining -= step
    return True


# Фоновая музыка: повтор ffprobe при ложном «нет аудио» (облако, блокировка файла).
_BGM_AUDIO_PROBE_RETRIES = 3
_BGM_AUDIO_PROBE_DELAY_SEC = 10.0
# ffmpeg/subprocess: EMFILE и похожие сбои ресурсов ОС.
_FFMPEG_RESOURCE_RETRIES = 5
_FFMPEG_RESOURCE_RETRY_DELAY_SEC = 2.0

# Optional full path to ffmpeg.exe set from UI / settings (overrides auto-detect).
_explicit_ffmpeg: Optional[str] = None


def _popen_flags() -> int:
    """Hide console window on Windows for child processes (ffmpeg)."""
    if sys.platform == "win32":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def is_resource_exhausted_error(exc: BaseException) -> bool:
    """EMFILE / «слишком много открытых файлов» (в т.ч. внутри requests/urllib3)."""
    markers = (
        "too many open files",
        "too many files",
        "errno 24",
        "errno 23",
        "resource temporarily unavailable",
        "[errno 24]",
        "failed to establish a new connection",
    )
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, OSError):
            errno = getattr(cur, "errno", None)
            if errno in (24, 23):  # EMFILE, ENFILE
                return True
        s = str(cur).lower()
        if any(m in s for m in markers):
            if "errno 24" in s or "errno 23" in s or "too many open files" in s:
                return True
            if "failed to establish a new connection" in s and (
                "[errno 24]" in s or "errno 24" in s
            ):
                return True
        cur = cur.__cause__ or cur.__context__
    return False


def set_ffmpeg_executable(path: Optional[str]) -> None:
    """Force ffmpeg location. Pass None or empty string to use auto-detection only."""
    global _explicit_ffmpeg
    if path is None or not str(path).strip():
        _explicit_ffmpeg = None
    else:
        _explicit_ffmpeg = str(path).strip()
        if sys.platform == "darwin" and ffmpeg_binary_has_drawtext(_explicit_ffmpeg):
            persist_ffmpeg_path(_explicit_ffmpeg)
    _reset_ffmpeg_capability_cache()
    sync_ffmpeg_env_for_children()


def sync_ffmpeg_env_for_children() -> Optional[str]:
    """
    Publish resolved ffmpeg path in ZALIVER_FFMPEG for spawn workers
    (macOS .app / кнопка «Установить ffmpeg» — иначе дочерний процесс не видит путь).
    """
    exe = resolve_ffmpeg_executable()
    if exe:
        os.environ["ZALIVER_FFMPEG"] = exe
    else:
        os.environ.pop("ZALIVER_FFMPEG", None)
    return exe


def _env_path() -> Optional[str]:
    raw = os.environ.get("ZALIVER_FFMPEG", "").strip()
    if not raw:
        return None
    p = Path(raw)
    return str(p.resolve()) if p.is_file() else None


def _ffmpeg_which() -> Optional[str]:
    if sys.platform == "darwin":
        hit = shutil.which("ffmpeg", path=_darwin_tool_path_env().get("PATH"))
        if hit:
            return hit
    return shutil.which("ffmpeg")


def _scan_os_path() -> Optional[str]:
    """Walk PATH entries (GUI apps on Windows often miss entries that a new shell has)."""
    path_env = os.environ.get("PATH", "")
    if sys.platform == "darwin":
        path_env = _darwin_tool_path_env().get("PATH", path_env)
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
    if sys.platform == "darwin":
        return _darwin_ffmpeg_std_paths()
    return [
        Path("/opt/homebrew/bin/ffmpeg"),
        Path("/usr/local/bin/ffmpeg"),
    ]


def _darwin_ffmpeg_std_paths() -> List[Path]:
    """Стандартные пути Homebrew (GUI .app часто не видит их в PATH)."""
    paths = [
        # ffmpeg-full — полная сборка с drawtext (harfbuzz + freetype)
        Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"),
        Path("/usr/local/opt/ffmpeg-full/bin/ffmpeg"),
        Path("/opt/homebrew/bin/ffmpeg"),
        Path("/opt/homebrew/opt/ffmpeg/bin/ffmpeg"),
        Path("/usr/local/bin/ffmpeg"),
        Path("/usr/local/opt/ffmpeg/bin/ffmpeg"),
    ]
    for brew in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        if not Path(brew).is_file():
            continue
        for formula in ("ffmpeg-full", "ffmpeg"):
            try:
                r = subprocess.run(
                    [brew, "--prefix", formula],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=20,
                    creationflags=_popen_flags(),
                )
                if r.returncode == 0:
                    prefix = (r.stdout or "").strip()
                    if prefix:
                        paths.append(Path(prefix) / "bin" / "ffmpeg")
            except (OSError, subprocess.TimeoutExpired):
                continue
    seen: set[str] = set()
    out: List[Path] = []
    for p in paths:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _darwin_tool_path_env() -> dict[str, str]:
    env = dict(os.environ)
    extra = [
        "/opt/homebrew/bin",
        "/opt/homebrew/sbin",
        "/usr/local/bin",
        "/usr/local/sbin",
    ]
    cur = env.get("PATH", "")
    parts = extra + ([cur] if cur else [])
    env["PATH"] = os.pathsep.join(parts)
    return env


def ffmpeg_path_config_file() -> Path:
    return Path.home() / "Library/Application Support/Zaliver/ffmpeg.path"


def load_persisted_ffmpeg_path() -> Optional[str]:
    if sys.platform != "darwin":
        return None
    cfg = ffmpeg_path_config_file()
    if not cfg.is_file():
        return None
    try:
        raw = cfg.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        p = Path(raw)
        if p.is_file():
            return str(p.resolve())
    except OSError:
        pass
    return None


def persist_ffmpeg_path(exe: Optional[str]) -> None:
    if sys.platform != "darwin":
        return
    cfg = ffmpeg_path_config_file()
    try:
        cfg.parent.mkdir(parents=True, exist_ok=True)
        if not exe:
            cfg.unlink(missing_ok=True)
            return
        resolved = str(Path(exe).resolve())
        if ffmpeg_binary_has_drawtext(resolved):
            cfg.write_text(resolved + "\n", encoding="utf-8")
    except OSError:
        pass


def clear_persisted_ffmpeg_path() -> None:
    if sys.platform != "darwin":
        return
    try:
        ffmpeg_path_config_file().unlink(missing_ok=True)
    except OSError:
        pass


def _macos_install_candidates() -> List[Path]:
    """ffmpeg, установленный приложением (evermeet / Application Support)."""
    return [zaliver_managed_ffmpeg_path()]


def zaliver_managed_ffmpeg_path() -> Path:
    return Path.home() / "Library" / "Application Support" / "Zaliver" / "bin" / "ffmpeg"


def remove_zaliver_managed_ffmpeg() -> None:
    """Удалить старую копию ffmpeg, скачанную приложением (evermeet без drawtext)."""
    if sys.platform != "darwin":
        return
    managed = zaliver_managed_ffmpeg_path()
    managed_s = str(managed)
    try:
        if managed.is_file():
            managed_s = str(managed.resolve())
    except OSError:
        pass
    persisted = load_persisted_ffmpeg_path()
    try:
        managed.unlink(missing_ok=True)
    except OSError:
        pass
    if persisted and persisted == managed_s:
        clear_persisted_ffmpeg_path()


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


def _add_ffmpeg_candidate(paths: List[str], seen: set[str], raw: Optional[str | Path]) -> None:
    if not raw:
        return
    try:
        p = Path(raw)
        if not p.is_file():
            return
        resolved = str(p.resolve())
    except OSError:
        return
    if resolved in seen:
        return
    seen.add(resolved)
    paths.append(resolved)


def _ffmpeg_candidate_paths() -> List[str]:
    """Все известные пути к ffmpeg в порядке приоритета (без дубликатов)."""
    paths: List[str] = []
    seen: set[str] = set()
    if _explicit_ffmpeg:
        _add_ffmpeg_candidate(paths, seen, _explicit_ffmpeg)
    _add_ffmpeg_candidate(paths, seen, load_persisted_ffmpeg_path())
    if sys.platform == "darwin":
        for c in _darwin_ffmpeg_std_paths():
            _add_ffmpeg_candidate(paths, seen, c)
    _add_ffmpeg_candidate(paths, seen, _env_path())
    _add_ffmpeg_candidate(paths, seen, _bundled_ffmpeg())
    _add_ffmpeg_candidate(paths, seen, _ffmpeg_which())
    _add_ffmpeg_candidate(paths, seen, _scan_os_path())
    if sys.platform == "win32":
        platform_cands = _windows_install_candidates()
    elif sys.platform == "darwin":
        platform_cands = _macos_install_candidates()
    else:
        platform_cands = _unix_candidates()
    for c in platform_cands:
        _add_ffmpeg_candidate(paths, seen, c)
    _add_ffmpeg_candidate(paths, seen, _winget_ffmpeg_glob())
    return paths


def resolve_ffmpeg_executable() -> Optional[str]:
    """Return absolute path to ffmpeg, or None."""
    candidates = _ffmpeg_candidate_paths()
    if not candidates:
        return None
    if sys.platform == "darwin":
        for exe in candidates:
            if ffmpeg_binary_has_drawtext(exe):
                persist_ffmpeg_path(exe)
                return exe
    return candidates[0]


def check_ffmpeg() -> bool:
    return resolve_ffmpeg_executable() is not None


def check_ffmpeg_tools() -> bool:
    """ffmpeg + ffprobe (обработка и метаданные)."""
    if not check_ffmpeg():
        return False
    from zaliver.processing.ffmpeg_probe import resolve_ffprobe_executable

    return resolve_ffprobe_executable() is not None


MACOS_BREW_FFMPEG_FORMULA = "ffmpeg-full"


def macos_ffmpeg_needs_full_install() -> bool:
    """На macOS есть ffmpeg, но без drawtext (обычный brew ffmpeg / evermeet)."""
    if sys.platform != "darwin":
        return False
    if not check_ffmpeg():
        return False
    return not ffmpeg_has_drawtext()


def needs_ffmpeg_install_prompt() -> bool:
    """Показывать в UI строку с кнопкой установки ffmpeg."""
    if not check_ffmpeg():
        return True
    from zaliver.processing.ffmpeg_probe import resolve_ffprobe_executable

    if resolve_ffprobe_executable() is None:
        return True
    if macos_ffmpeg_needs_full_install():
        return True
    return False


_drawtext_available: Optional[bool] = None
_drawtext_by_exe: dict[str, bool] = {}
_DRAWTEXT_FILTER_RE = re.compile(r"(?:^|\s|\.)drawtext(?:\s|$|[.:(])", re.MULTILINE)


def _reset_ffmpeg_capability_cache() -> None:
    global _drawtext_available
    _drawtext_available = None
    _drawtext_by_exe.clear()


def ffmpeg_binary_has_drawtext(exe: str) -> bool:
    """True if this ffmpeg binary includes the drawtext filter."""
    try:
        key = str(Path(exe).resolve())
    except OSError:
        key = exe
    cached = _drawtext_by_exe.get(key)
    if cached is not None:
        return cached
    ok = False
    try:
        r = subprocess.run(
            [key, "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=_popen_flags(),
        )
        out = f"{r.stdout or ''}\n{r.stderr or ''}"
        ok = bool(_DRAWTEXT_FILTER_RE.search(out))
        if not ok:
            probe = subprocess.run(
                [
                    key,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=32x32:d=0.01",
                    "-vf",
                    "drawtext=text='t':fontsize=12:x=1:y=1",
                    "-frames:v",
                    "1",
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                creationflags=_popen_flags(),
            )
            err = f"{probe.stderr or ''}\n{probe.stdout or ''}".lower()
            ok = probe.returncode == 0 and "no such filter" not in err
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    _drawtext_by_exe[key] = ok
    return ok


def ffmpeg_has_drawtext() -> bool:
    """True if the resolved ffmpeg build includes the drawtext filter (libfreetype)."""
    global _drawtext_available
    if _drawtext_available is not None:
        return _drawtext_available
    exe = resolve_ffmpeg_executable()
    if not exe:
        _drawtext_available = False
        return False
    _drawtext_available = ffmpeg_binary_has_drawtext(exe)
    return _drawtext_available


FFMPEG_DRAWTEXT_MISSING_MSG = (
    "Текст на видео требует фильтр drawtext в ffmpeg (libfreetype + libharfbuzz). "
    "На macOS: brew install ffmpeg-full (или brew install harfbuzz freetype && brew reinstall ffmpeg)."
)


def ffmpeg_drawtext_missing_user_message() -> str:
    """Сообщение об ошибке с путём к ffmpeg и шагами для macOS."""
    exe = resolve_ffmpeg_executable()
    checked = _ffmpeg_candidate_paths()
    msg = FFMPEG_DRAWTEXT_MISSING_MSG
    if exe:
        msg += f"\n\nСейчас Zaliver использует:\n{exe}"
    if sys.platform == "darwin":
        msg += (
            "\n\nВ Терминале (проверка — должна быть строка drawtext):\n"
            "  /opt/homebrew/opt/ffmpeg-full/bin/ffmpeg -filters 2>&1 | grep drawtext\n"
            "  /opt/homebrew/bin/ffmpeg -filters 2>&1 | grep drawtext\n\n"
            "Установка полной сборки с drawtext:\n"
            "  brew install ffmpeg-full\n"
            "  # или:\n"
            "  brew install harfbuzz freetype fribidi fontconfig\n"
            "  brew reinstall ffmpeg\n\n"
            "Затем удалите старые копии Zaliver и перезапустите приложение:\n"
            "  rm ~/Library/Application\\ Support/Zaliver/bin/ffmpeg\n"
            "  rm ~/Library/Application\\ Support/Zaliver/ffmpeg.path"
        )
        if checked:
            msg += "\n\nПроверенные пути:\n" + "\n".join(f"  • {p}" for p in checked[:8])
    return msg


def run_ffmpeg(
    args: List[str],
    log: LogFn = None,
    *,
    resource_retries: int = _FFMPEG_RESOURCE_RETRIES,
) -> None:
    exe = resolve_ffmpeg_executable()
    if not exe:
        raise RuntimeError("ffmpeg не найден")
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-y", *args]
    max_retries = max(0, int(resource_retries))
    total_attempts = max_retries + 1
    last_err: Optional[BaseException] = None
    for attempt in range(1, total_attempts + 1):
        if log:
            log(" ".join(cmd))
        try:
            p = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=_popen_flags(),
            )
        except OSError as e:
            last_err = e
            if attempt >= total_attempts or not is_resource_exhausted_error(e):
                raise RuntimeError(str(e)) from e
            if log:
                log(
                    f"ffmpeg: {e} — повтор {attempt}/{max_retries} "
                    f"через {_FFMPEG_RESOURCE_RETRY_DELAY_SEC:.0f} с…"
                )
            gc.collect()
            time.sleep(_FFMPEG_RESOURCE_RETRY_DELAY_SEC)
            continue
        if p.returncode == 0:
            return
        err = (p.stderr or p.stdout or "").strip()
        last_err = RuntimeError(err or f"ffmpeg failed with code {p.returncode}")
        if attempt >= total_attempts or not is_resource_exhausted_error(last_err):
            raise last_err
        if log:
            log(
                f"ffmpeg: {err[:200]} — повтор {attempt}/{max_retries} "
                f"через {_FFMPEG_RESOURCE_RETRY_DELAY_SEC:.0f} с…"
            )
        gc.collect()
        time.sleep(_FFMPEG_RESOURCE_RETRY_DELAY_SEC)
    if last_err is not None:
        raise last_err
    raise RuntimeError("ffmpeg failed")


_cached_encoder_list: Optional[str] = None
_cached_filter_list: Optional[str] = None
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


def ffmpeg_filters_list_text() -> str:
    """Return ffmpeg -filters output (cached)."""
    global _cached_filter_list
    if _cached_filter_list is not None:
        return _cached_filter_list
    exe = resolve_ffmpeg_executable()
    if not exe:
        _cached_filter_list = ""
        return _cached_filter_list
    try:
        p = subprocess.run(
            [exe, "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
            creationflags=_popen_flags(),
        )
        _cached_filter_list = (p.stdout or "") + "\n" + (p.stderr or "")
    except Exception:
        _cached_filter_list = ""
    return _cached_filter_list


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
    elif enc in ("h264_videotoolbox", "hevc_videotoolbox"):
        lavfi = "color=c=black:s=1280x720:r=30"
        vf = "format=yuv420p"
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


def libx264_encode_args_for_target(
    target_video_bps: Optional[int], *, crf: int = 20
) -> List[str]:
    """CRF по умолчанию или VBV по целевому битрейту (подгонка размера к исходнику)."""
    if target_video_bps is None or target_video_bps <= 0:
        return ["-preset", "veryfast", "-crf", str(crf)]
    b = clamp_target_video_bps(target_video_bps)
    maxr = max(b + 1, int(b * 1.35))
    buf = max(b * 2, int(b * 2))
    return ["-preset", "veryfast", "-b:v", str(b), "-maxrate", str(maxr), "-bufsize", str(buf)]


def _h264_nvenc_args(target_video_bps: Optional[int], *, gpu_cq: int = 23) -> List[str]:
    if target_video_bps is None or target_video_bps <= 0:
        return ["-preset", "p4", "-cq", str(gpu_cq), "-b:v", "0"]
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


def _h264_qsv_args(target_video_bps: Optional[int], *, gpu_cq: int = 23) -> List[str]:
    if target_video_bps is None or target_video_bps <= 0:
        return ["-global_quality", str(gpu_cq), "-look_ahead", "1"]
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


def _h264_amf_args(target_video_bps: Optional[int], *, gpu_cq: int = 23) -> List[str]:
    if target_video_bps is None or target_video_bps <= 0:
        qp = str(gpu_cq)
        return [
            "-usage",
            "transcoding",
            "-quality",
            "speed",
            "-rc",
            "cqp",
            "-qp_i",
            qp,
            "-qp_p",
            qp,
            "-qp_b",
            qp,
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


def _h264_videotoolbox_args(
    target_video_bps: Optional[int], *, videotoolbox_q: int = 65
) -> List[str]:
    """Apple VideoToolbox (macOS): аппаратный H.264."""
    if target_video_bps is None or target_video_bps <= 0:
        return ["-q:v", str(videotoolbox_q), "-allow_sw", "1"]
    b = clamp_target_video_bps(target_video_bps)
    maxr = max(b + 1, int(b * 1.35))
    buf = max(b * 2, int(b * 2))
    return [
        "-b:v",
        str(b),
        "-maxrate",
        str(maxr),
        "-bufsize",
        str(buf),
        "-allow_sw",
        "1",
    ]


def _try_h264_videotoolbox(
    target_video_bps: Optional[int],
    *,
    videotoolbox_q: int = 65,
) -> Optional[Tuple[str, List[str]]]:
    if sys.platform != "darwin":
        return None
    txt = ffmpeg_encoder_list_text().lower()
    if "h264_videotoolbox" not in txt:
        return None
    if not _probe_encoder_runtime("h264_videotoolbox"):
        return None
    return (
        "h264_videotoolbox",
        _h264_videotoolbox_args(target_video_bps, videotoolbox_q=videotoolbox_q),
    )


def pick_best_h264_encoder(
    *,
    prefer_gpu: bool = False,
    target_video_bps: Optional[int] = None,
    crf: int = 20,
    gpu_cq: int = 23,
    videotoolbox_q: int = 65,
) -> Tuple[str, List[str]]:
    """
    Return (encoder_name, extra_args) preferring GPU encoders if available.
    On macOS h264_videotoolbox is used by default when ffmpeg supports it.
    If target_video_bps is set, args target that video bitrate (VBR) to approximate source file size.
    Otherwise NVENC/QSV/AMF use quality (CQ) presets and libx264 uses CRF 20.
    """
    vt = _try_h264_videotoolbox(target_video_bps, videotoolbox_q=videotoolbox_q)
    if vt is not None:
        return vt
    txt = ffmpeg_encoder_list_text().lower()
    if prefer_gpu and "h264_nvenc" in txt and _probe_encoder_runtime("h264_nvenc"):
        return ("h264_nvenc", _h264_nvenc_args(target_video_bps, gpu_cq=gpu_cq))
    if prefer_gpu and "h264_qsv" in txt and _probe_encoder_runtime("h264_qsv"):
        return ("h264_qsv", _h264_qsv_args(target_video_bps, gpu_cq=gpu_cq))
    if prefer_gpu and "h264_amf" in txt and _probe_encoder_runtime("h264_amf"):
        return ("h264_amf", _h264_amf_args(target_video_bps, gpu_cq=gpu_cq))
    return ("libx264", libx264_encode_args_for_target(target_video_bps, crf=crf))


def video_encoder_for_mux(
    target_video_bps: Optional[int],
    *,
    prefer_gpu: bool = False,
) -> Tuple[str, List[str]]:
    """Энкодер для склейки/mux (перекодирование при concat fallback, speed, BGM)."""
    return pick_best_h264_encoder(
        prefer_gpu=prefer_gpu, target_video_bps=target_video_bps
    )


# concat demuxer открывает все входы в списке сразу; лимит дескрипторов ОС.
# macOS: soft ulimit -n часто 256 — при фоновой музыке и параллельных чанках EMFILE на склейке.
_MIN_FFMPEG_CONCAT_BATCH = 2


def _max_ffmpeg_concat_batch() -> int:
    return 64


def _max_bgm_alternates() -> int:
    return 2 if sys.platform == "darwin" else 4


def bgm_alternate_paths(
    primary: str,
    pool: List[str],
    *,
    max_alternates: Optional[int] = None,
) -> List[str]:
    """Случайные запасные треки из пула (без primary), не весь список из UI."""
    try:
        pkey = os.path.normcase(str(Path(primary).resolve()))
    except OSError:
        return []
    rest: List[str] = []
    seen: set[str] = set()
    for raw in pool:
        try:
            p = Path(raw).resolve()
        except OSError:
            continue
        if not p.is_file():
            continue
        k = os.path.normcase(str(p))
        if k == pkey or k in seen:
            continue
        seen.add(k)
        rest.append(str(p))
    random.shuffle(rest)
    cap = _max_bgm_alternates() if max_alternates is None else max(0, int(max_alternates))
    return rest[:cap]


def _write_concat_demuxer_list(segment_paths: List[str], list_path: str) -> None:
    with open(list_path, "w", encoding="utf-8") as f:
        for p in segment_paths:
            line = Path(p).resolve().as_posix().replace("'", "'\\''")
            f.write(f"file '{line}'\n")


def _concat_reencode_vf(segment_paths: List[str]) -> Tuple[str, float]:
    """Фильтр нормализации кадра при перекодировании concat (без растягивания)."""
    from zaliver.processing.ffmpeg_probe import probe_video_stream

    w, h, fps, _, _ = probe_video_stream(segment_paths[0])
    w = max(2, w - (w % 2))
    h = max(2, h - (h % 2))
    vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=bilinear,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
    )
    return vf, float(fps) if fps > 1e-6 else 30.0


def _concat_segments_once(
    segment_paths: List[str],
    out_path: str,
    log: LogFn = None,
    target_video_bps: Optional[int] = None,
    prefer_gpu: bool = False,
) -> None:
    """Один вызов ffmpeg concat (число file в list.txt = len(segment_paths))."""
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
                "-fflags",
                "+genpts",
                "-avoid_negative_ts",
                "make_zero",
                "-c",
                "copy",
                str(out),
            ],
            log=log,
        )
    except RuntimeError:
        if log:
            log(
                "Склейка: stream copy не удался — перекодирование с нормализацией кадра"
            )
        enc, enc_args = video_encoder_for_mux(
            target_video_bps, prefer_gpu=prefer_gpu
        )
        vf, fps = _concat_reencode_vf(segment_paths)
        run_ffmpeg(
            [
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                list_path,
                "-vf",
                vf,
                "-r",
                f"{fps:.6f}",
                "-vsync",
                "cfr",
                "-c:v",
                enc,
                *enc_args,
                "-pix_fmt",
                "yuv420p",
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


def _concat_segments_batched(
    segment_paths: List[str],
    out_path: str,
    *,
    batch_limit: int,
    log: LogFn = None,
    target_video_bps: Optional[int] = None,
    prefer_gpu: bool = False,
) -> None:
    paths = [str(p) for p in segment_paths if p]
    if not paths:
        raise ValueError("No segments to concat")
    batch_limit = max(2, int(batch_limit))
    if len(paths) <= batch_limit:
        _concat_segments_once(
            paths, out_path, log=log, target_video_bps=target_video_bps,
            prefer_gpu=prefer_gpu,
        )
        return

    if log:
        log(
            f"Склейка {len(paths)} частей пакетами по {batch_limit} "
            f"(обход лимита открытых файлов ОС)"
        )
    work = Path(out_path).parent
    temps: List[Path] = []
    current = paths
    batch_idx = 0
    try:
        while len(current) > batch_limit:
            nxt: List[str] = []
            for i in range(0, len(current), batch_limit):
                batch = current[i : i + batch_limit]
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
                    prefer_gpu=prefer_gpu,
                )
                temps.append(tmp)
                nxt.append(str(tmp))
            current = nxt
        _concat_segments_once(
            current, out_path, log=log, target_video_bps=target_video_bps,
            prefer_gpu=prefer_gpu,
        )
    finally:
        out_resolved = Path(out_path).resolve()
        for tmp in temps:
            try:
                if tmp.resolve() != out_resolved:
                    tmp.unlink(missing_ok=True)
            except OSError:
                pass


def concat_segments(
    segment_paths: List[str],
    out_path: str,
    log: LogFn = None,
    target_video_bps: Optional[int] = None,
    prefer_gpu: bool = False,
) -> None:
    batch_limit = _max_ffmpeg_concat_batch()
    min_batch = _MIN_FFMPEG_CONCAT_BATCH
    retry_delay = (
        _FFMPEG_RESOURCE_RETRY_DELAY_SEC * 1.5
        if sys.platform == "darwin"
        else _FFMPEG_RESOURCE_RETRY_DELAY_SEC
    )
    last_err: Optional[BaseException] = None
    while batch_limit >= min_batch:
        try:
            _concat_segments_batched(
                segment_paths,
                out_path,
                batch_limit=batch_limit,
                log=log,
                target_video_bps=target_video_bps,
                prefer_gpu=prefer_gpu,
            )
            return
        except RuntimeError as e:
            if not is_resource_exhausted_error(e):
                raise
            last_err = e
            if batch_limit <= min_batch:
                raise
            smaller = max(min_batch, batch_limit // 2)
            if log:
                hint = (
                    " (на macOS часто помогает уменьшить число потоков в настройках)"
                    if sys.platform == "darwin"
                    else ""
                )
                log(
                    f"Склейка: нехватка дескрипторов ({str(e)[:120]}) — "
                    f"повтор пакетами по {smaller}{hint}…"
                )
            gc.collect()
            time.sleep(retry_delay)
            batch_limit = smaller
    if last_err is not None:
        raise last_err


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


def _music_file_decodable_by_ffmpeg(path: str) -> bool:
    """Короткая пробная расшифровка: отсекает битые mp3, которые ffprobe ещё «видит»."""
    exe = resolve_ffmpeg_executable()
    if not exe:
        return False
    try:
        pth = Path(path).resolve()
    except OSError:
        return False
    if not pth.is_file():
        return False
    cmd = [
        exe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(pth),
        "-t",
        "0.25",
        "-ac",
        "2",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_popen_flags(),
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def _music_track_ready(path: str) -> bool:
    return source_file_has_audio(path) and _music_file_decodable_by_ffmpeg(path)


def _require_background_music_audio(
    music_path: str,
    *,
    log: LogFn = None,
    retries: int = _BGM_AUDIO_PROBE_RETRIES,
    delay_sec: float = _BGM_AUDIO_PROBE_DELAY_SEC,
    cancel_check: CancelCheck = None,
) -> None:
    """
    Убедиться, что ffprobe видит аудиопоток и ffmpeg его декодирует.
    При сбое — до `retries` повторов с паузой (прерываемой при отмене).
    """
    m = Path(music_path).resolve()
    pstr = m.as_posix()
    name = m.name
    max_retries = max(0, int(retries))
    delay = max(0.0, float(delay_sec))
    total_attempts = max_retries + 1
    for attempt in range(1, total_attempts + 1):
        if _music_track_ready(pstr):
            if attempt > 1 and log:
                log(
                    f"Фоновая музыка: трек готов с попытки "
                    f"{attempt}/{total_attempts} ({name})"
                )
            return
        if attempt >= total_attempts:
            break
        if log:
            if source_file_has_audio(pstr):
                log(
                    f"Фоновая музыка: ffprobe видит аудио, но ffmpeg не декодирует "
                    f"({name}), повтор {attempt}/{max_retries} через {delay:.0f} с…"
                )
            else:
                log(
                    f"Фоновая музыка: аудиопоток не обнаружен ({name}), "
                    f"повтор {attempt}/{max_retries} через {delay:.0f} с…"
                )
        if not _sleep_interruptible(delay, cancel_check=cancel_check):
            raise RuntimeError("cancelled")
    raise BackgroundMusicUnavailableError(
        f"Нет аудиопотока в файле фоновой музыки: {name} ({pstr})"
    )


def _bgm_candidate_paths(
    music_path: str, alternates: Optional[List[str]]
) -> List[str]:
    """Уникальные существующие пути: сначала выбранный трек, остальные из пула (случайный порядок)."""
    keys: set[str] = set()
    primary_key: Optional[str] = None
    primary_path: Optional[str] = None
    rest: List[str] = []

    def _key(p: Path) -> str:
        return os.path.normcase(str(p))

    def _add(raw: str, *, as_primary: bool = False) -> None:
        nonlocal primary_key, primary_path
        try:
            p = Path(raw).resolve()
        except OSError:
            return
        if not p.is_file():
            return
        k = _key(p)
        if k in keys:
            return
        keys.add(k)
        ps = str(p)
        if as_primary:
            primary_key = k
            primary_path = ps
        else:
            rest.append(ps)

    _add(music_path, as_primary=True)
    pool = list(alternates or [])
    random.shuffle(pool)
    for raw in pool:
        try:
            p = Path(raw).resolve()
        except OSError:
            continue
        k = _key(p)
        if primary_key is not None and k == primary_key:
            continue
        _add(raw)

    if primary_path is None:
        return rest
    return [primary_path, *rest]


def resolve_background_music_path(
    music_path: str,
    *,
    alternates: Optional[List[str]] = None,
    log: LogFn = None,
    cancel_check: CancelCheck = None,
) -> str:
    """
    Сначала ffprobe с ретраями на выбранном треке; только после их исчерпания —
    следующие файлы из пула (у каждого снова полный цикл ретраев).
    """
    candidates = _bgm_candidate_paths(music_path, alternates)
    if not candidates:
        raise BackgroundMusicUnavailableError(
            f"Нет доступных файлов фоновой музыки: {Path(music_path).name}"
        )
    primary = candidates[0]
    fallbacks = candidates[1:]
    last_err: Optional[BackgroundMusicUnavailableError] = None

    if log:
        log(
            f"Фоновая музыка: основной трек {Path(primary).name} "
            f"(до {_BGM_AUDIO_PROBE_RETRIES} повторов по {_BGM_AUDIO_PROBE_DELAY_SEC:.0f} с)…"
        )
    try:
        _require_background_music_audio(
            primary, log=log, cancel_check=cancel_check
        )
        return primary
    except BackgroundMusicUnavailableError as e:
        last_err = e
        if not fallbacks:
            raise
        if log:
            log(
                f"Фоновая музыка: повторы для {Path(primary).name} исчерпаны, "
                f"пробуем другие треки из пула ({len(fallbacks)})…"
            )
    except RuntimeError as e:
        if str(e) == "cancelled":
            raise
        raise

    for idx, candidate in enumerate(fallbacks, start=1):
        if log:
            log(
                f"Фоновая музыка: запасной трек {idx}/{len(fallbacks)} — "
                f"{Path(candidate).name} (без повторов)…"
            )
        try:
            _require_background_music_audio(
                candidate, log=log, retries=0, cancel_check=cancel_check
            )
            if log:
                log(
                    f"Фоновая музыка: вместо {Path(primary).name} — {Path(candidate).name}"
                )
            return candidate
        except BackgroundMusicUnavailableError as e:
            last_err = e
            if log and idx < len(fallbacks):
                log(
                    f"Фоновая музыка: повторы для {Path(candidate).name} исчерпаны, "
                    f"следующий трек ({len(fallbacks) - idx} осталось)…"
                )

    names = ", ".join(Path(p).name for p in candidates)
    raise BackgroundMusicUnavailableError(
        f"Не удалось прочитать аудио ни в одном треке фона ({len(candidates)}): {names}"
    ) from last_err


def mux_video_audio(
    video_path: str,
    audio_source_path: str,
    out_path: str,
    playback_speed: Optional[float] = None,
    audio_chorus: bool = False,
    log: LogFn = None,
    target_video_bps: Optional[int] = None,
    prefer_gpu: bool = False,
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
                enc, enc_args = video_encoder_for_mux(
                    target_video_bps, prefer_gpu=prefer_gpu
                )
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
                        enc,
                        *enc_args,
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
            enc, enc_args = video_encoder_for_mux(
                target_video_bps, prefer_gpu=prefer_gpu
            )
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
                    enc,
                    *enc_args,
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
    background_music_alternates: Optional[List[str]] = None,
    music_video_meta: Optional[Tuple[int, float]] = None,
    background_music_mix: bool = False,
    background_music_volume_pct: float = 35.0,
    cancel_check: CancelCheck = None,
    prefer_gpu: bool = False,
) -> bool:
    """
    Склеить сегменты и добавить звук.
    Returns False, если фоновая музыка была нужна, но недоступна (звук — с исходника).
    """
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    concat_out = work / "concat_video.mp4"
    concat_segments(
        segment_paths,
        str(concat_out),
        log=log,
        target_video_bps=target_video_bps,
        prefer_gpu=prefer_gpu,
    )
    if background_music_path and str(background_music_path).strip():
        fc, fpsi = music_video_meta or (0, 30.0)
        try:
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
                music_path_alternates=background_music_alternates,
                cancel_check=cancel_check,
                prefer_gpu=prefer_gpu,
            )
        except (BackgroundMusicUnavailableError, RuntimeError) as e:
            if str(e) == "cancelled":
                raise
            if not is_background_music_failure(e):
                raise
            if log:
                log(f"Фоновая музыка: {e}")
                log(
                    "Видео сохраняется со звуком исходника (без фона); "
                    "исключено из очереди залива в YouTube."
                )
            mux_video_audio(
                str(concat_out),
                source_video,
                final_output,
                playback_speed=playback_speed,
                audio_chorus=audio_chorus,
                log=log,
                target_video_bps=target_video_bps,
                prefer_gpu=prefer_gpu,
            )
            return False
        return True
    mux_video_audio(
        str(concat_out),
        source_video,
        final_output,
        playback_speed=playback_speed,
        audio_chorus=audio_chorus,
        log=log,
        target_video_bps=target_video_bps,
        prefer_gpu=prefer_gpu,
    )
    return True


def _output_duration_after_speed(
    *, frame_count: int, fps: float, playback_speed: float
) -> float:
    if fps <= 1e-6:
        fps = 30.0
    spd = float(playback_speed)
    if spd <= 1e-9:
        spd = 1.0
    return (float(frame_count) / fps) / spd


_MUSIC_EDGE_SKIP_SEC = 10.0


def _random_music_trim_start_sec(
    music_duration_sec: Optional[float], needed_sec: float
) -> float:
    """Случайная фаза на шкале времени (сек); без первых и последних 10 с трека."""
    need = max(0.05, float(needed_sec))
    edge = _MUSIC_EDGE_SKIP_SEC
    if music_duration_sec is None or music_duration_sec <= 0.05:
        return random.uniform(edge, max(need + edge, 600.0))
    d = float(music_duration_sec)
    lo = edge
    hi = d - edge - need
    if hi >= lo:
        return random.uniform(lo, hi)
    # Трек короче, чем нужно для полного отступа — центрируем отрезок.
    usable = max(0.0, d - need)
    return min(max(usable / 2.0, 0.0), usable)


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
    prefer_gpu: bool = False,
) -> None:
    """Видео + только музыка (как раньше)."""
    v = Path(video_path).resolve().as_posix()
    m = Path(music_path).resolve().as_posix()
    o = str(out_path)
    a_filt = (
        f"[1:a]atrim=start={trim_start:.6f}:duration={dur_needed:.6f},"
        f"asetpts=PTS-STARTPTS[aout]"
    )
    if want_speed:
        enc, enc_args = video_encoder_for_mux(
            target_video_bps, prefer_gpu=prefer_gpu
        )
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
                enc,
                *enc_args,
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


def _mux_video_background_music_impl(
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
    prefer_gpu: bool = False,
) -> None:
    """Один трек фона (путь уже проверен)."""
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
            m,
            out_path,
            trim_start=trim_start,
            dur_needed=dur_needed,
            want_speed=want_speed,
            spd=spd,
            log=log,
            target_video_bps=target_video_bps,
            prefer_gpu=prefer_gpu,
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
        enc, enc_args = video_encoder_for_mux(
            target_video_bps, prefer_gpu=prefer_gpu
        )
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
                enc,
                *enc_args,
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
    music_path_alternates: Optional[List[str]] = None,
    cancel_check: CancelCheck = None,
    prefer_gpu: bool = False,
) -> None:
    """
    Видео без звука + фоновая музыка (случайный отрезок снаружи).
    Проверка трека (ffprobe + пробное декодирование), при сбое mux — другой файл из пула.
    """
    candidates = _bgm_candidate_paths(music_path, music_path_alternates)
    if not candidates:
        raise BackgroundMusicUnavailableError(
            f"Нет доступных файлов фоновой музыки: {Path(music_path).name}"
        )
    primary = candidates[0]
    fallbacks = candidates[1:]
    last_err: Optional[BaseException] = None
    mux_kw = dict(
        frame_count=frame_count,
        fps=fps,
        playback_speed=playback_speed,
        log=log,
        target_video_bps=target_video_bps,
        mix_with_source=mix_with_source,
        source_video_path=source_video_path,
        audio_chorus=audio_chorus,
        music_volume_pct=music_volume_pct,
        prefer_gpu=prefer_gpu,
    )

    if log:
        log(
            f"Фоновая музыка: основной трек {Path(primary).name} "
            f"(до {_BGM_AUDIO_PROBE_RETRIES} повторов по {_BGM_AUDIO_PROBE_DELAY_SEC:.0f} с)…"
        )
    try:
        _require_background_music_audio(
            primary, log=log, cancel_check=cancel_check
        )
    except BackgroundMusicUnavailableError as e:
        last_err = e
        if not fallbacks:
            raise
        if log:
            log(
                f"Фоновая музыка: повторы для {Path(primary).name} исчерпаны, "
                f"пробуем другие треки ({len(fallbacks)})…"
            )
    except RuntimeError as e:
        if str(e) == "cancelled":
            raise
        raise
    else:
        try:
            _mux_video_background_music_impl(
                video_path, primary, out_path, **mux_kw
            )
            return
        except RuntimeError as e:
            if not is_background_music_failure(e):
                raise
            last_err = e
            if log:
                log(
                    f"Фоновая музыка: ffmpeg не смог использовать {Path(primary).name}"
                )
            if not fallbacks:
                raise BackgroundMusicUnavailableError(
                    f"Не удалось наложить фоновую музыку: {Path(primary).name}"
                ) from e
            if log:
                log(f"Пробуем другие треки из пула ({len(fallbacks)})…")

    for idx, candidate in enumerate(fallbacks, start=1):
        if log:
            log(
                f"Фоновая музыка: запасной трек {idx}/{len(fallbacks)} — "
                f"{Path(candidate).name} (без повторов)…"
            )
        try:
            _require_background_music_audio(
                candidate, log=log, retries=0, cancel_check=cancel_check
            )
            _mux_video_background_music_impl(
                video_path, candidate, out_path, **mux_kw
            )
            if log:
                log(f"Фоновая музыка: использован {Path(candidate).name}")
            return
        except BackgroundMusicUnavailableError as e:
            last_err = e
            if log and idx < len(fallbacks):
                log(
                    f"Фоновая музыка: {Path(candidate).name} недоступен, "
                    f"следующий трек ({len(fallbacks) - idx} осталось)…"
                )
        except RuntimeError as e:
            if str(e) == "cancelled":
                raise
            if not is_background_music_failure(e):
                raise
            last_err = e
            if log and idx < len(fallbacks):
                log(
                    f"Фоновая музыка: ffmpeg не смог {Path(candidate).name}, "
                    f"следующий трек ({len(fallbacks) - idx} осталось)…"
                )

    names = ", ".join(Path(p).name for p in candidates)
    raise BackgroundMusicUnavailableError(
        f"Не удалось наложить фоновую музыку ни одним треком ({len(candidates)}): {names}"
    ) from last_err
