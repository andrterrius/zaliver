"""Установка ffmpeg: Windows — winget / pip+imageio-ffmpeg; macOS — Homebrew / скачивание ZIP."""

from __future__ import annotations

import importlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, List, Optional

from zaliver.processing.ffmpeg_merge import (
    check_ffmpeg,
    resolve_ffmpeg_executable,
    set_ffmpeg_executable,
)

LogCb = Callable[[str], None]
ProgressCb = Callable[[int, str], None]

_PERCENT_RE = re.compile(r"(\d{1,3})\s*%")
_WINGET_ID = "Gyan.FFmpeg"
# Стабильный API evermeet.cx: релизный ffmpeg в ZIP (x86_64, на Apple Silicon часто через Rosetta).
_EVERMEET_RELEASE_ZIP = "https://evermeet.cx/ffmpeg/getrelease/zip"


def _popen_flags() -> int:
    if sys.platform == "win32":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def _run_streaming(
    cmd: List[str],
    log: LogCb,
    progress: ProgressCb,
    pct_lo: int,
    pct_hi: int,
    *,
    env: Optional[dict[str, str]] = None,
) -> int:
    """Запуск команды с построчным логом; progress — примерный процент pct_lo..pct_hi."""
    merged_env = {**os.environ, **(env or {})}
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=_popen_flags(),
        env=merged_env,
    )
    if proc.stdout is None:
        proc.wait()
        return proc.returncode or 0

    last_pct = pct_lo
    lines = 0
    for raw in proc.stdout:
        line = raw.rstrip()
        if line:
            log(line)
        lines += 1
        m = _PERCENT_RE.search(line)
        if m:
            v = max(0, min(100, int(m.group(1))))
            mapped = pct_lo + int((pct_hi - pct_lo) * (v / 100.0))
            last_pct = max(last_pct, min(mapped, pct_hi))
            progress(last_pct, line[:200] if line else "…")
        elif lines % 4 == 0:
            bump = pct_lo + min(pct_hi - pct_lo - 1, lines // 8)
            last_pct = max(last_pct, min(bump, pct_hi - 1))
            progress(last_pct, line[:200] if line else "Выполняется…")
    proc.wait()
    progress(pct_hi, "Команда завершена")
    return int(proc.returncode or 0)


def _resolve_after_pause() -> Optional[str]:
    time.sleep(0.6)
    set_ffmpeg_executable(None)
    return resolve_ffmpeg_executable()


def _install_via_winget(log: LogCb, progress: ProgressCb) -> tuple[bool, str]:
    winget = shutil.which("winget")
    if not winget:
        return False, "winget не найден в PATH"

    progress(2, "Запуск winget…")
    log(f"Команда: winget install {_WINGET_ID}")
    code = _run_streaming(
        [
            winget,
            "install",
            "-e",
            "--id",
            _WINGET_ID,
            "--accept-package-agreements",
            "--accept-source-agreements",
            "--disable-interactivity",
        ],
        log,
        progress,
        5,
        88,
    )
    if code != 0:
        log(f"winget exit code: {code}")

    exe = _resolve_after_pause()
    if exe:
        progress(100, "ffmpeg найден")
        return True, exe

    if code == 0:
        return (
            False,
            "winget завершился без ошибки, но ffmpeg не найден. "
            "Перезапустите Zaliver или добавьте ffmpeg в PATH.",
        )
    return False, f"winget завершился с кодом {code}, ffmpeg не найден"


def _install_via_pip(log: LogCb, progress: ProgressCb) -> tuple[bool, str]:
    progress(5, "Установка пакета imageio-ffmpeg через pip…")
    code = _run_streaming(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--user",
            "imageio-ffmpeg>=0.4.9",
        ],
        log,
        progress,
        8,
        92,
    )
    if code != 0:
        return False, f"pip завершился с кодом {code}"

    importlib.invalidate_caches()
    try:
        imageio_ffmpeg = importlib.import_module("imageio_ffmpeg")
    except ImportError as e:
        return False, f"Не удалось импортировать imageio-ffmpeg: {e}"

    exe = imageio_ffmpeg.get_ffmpeg_exe()
    p = Path(exe)
    if not p.is_file():
        return False, f"Путь от imageio-ffmpeg не существует: {exe}"

    set_ffmpeg_executable(str(p.resolve()))
    progress(100, "Готово")
    return True, str(p.resolve())


def _mac_bundle_bin_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "Zaliver" / "bin"


def _install_via_brew(log: LogCb, progress: ProgressCb) -> tuple[bool, str]:
    brew = shutil.which("brew")
    if not brew:
        return False, "Homebrew (brew) не найден в PATH"

    progress(2, "Установка через Homebrew (brew install ffmpeg)…")
    log("Команда: brew install ffmpeg")
    code = _run_streaming(
        [brew, "install", "ffmpeg"],
        log,
        progress,
        5,
        88,
        env={"HOMEBREW_NO_ANALYTICS": "1"},
    )
    if code != 0:
        log(f"brew exit code: {code}")

    exe = _resolve_after_pause()
    if exe:
        progress(100, "ffmpeg найден")
        return True, exe

    if code == 0:
        return False, "brew завершился, но ffmpeg не найден в стандартных путях."
    return False, f"brew завершился с кодом {code}"


def _strip_macos_quarantine(binary: Path, log: LogCb) -> None:
    xattr = shutil.which("xattr")
    if not xattr:
        return
    try:
        r = subprocess.run(
            [xattr, "-dr", "com.apple.quarantine", str(binary)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            creationflags=_popen_flags(),
        )
        if r.returncode != 0 and (r.stderr or r.stdout):
            log((r.stderr or r.stdout).strip()[:500])
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"xattr: {e}")


def _download_https_to_file(
    url: str,
    out_path: Path,
    log: LogCb,
    progress: ProgressCb,
    pct_lo: int,
    pct_hi: int,
) -> None:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Zaliver/0.1 (ffmpeg static download)"},
    )
    progress(pct_lo, "Скачивание ffmpeg…")
    with urllib.request.urlopen(req, timeout=600) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        chunk = 256 * 1024
        with open(out_path, "wb") as f:
            while True:
                block = resp.read(chunk)
                if not block:
                    break
                f.write(block)
                done += len(block)
                if total > 0:
                    frac = min(1.0, done / float(total))
                    p = pct_lo + int((pct_hi - pct_lo) * frac)
                    mb = done // (1024 * 1024)
                    tmb = total // (1024 * 1024)
                    progress(min(p, pct_hi), f"Скачано ~{mb} / ~{tmb} МиБ")
                else:
                    p = pct_lo + min(pct_hi - pct_lo - 1, done // (3 * 1024 * 1024))
                    progress(p, f"Скачано ~{done // (1024 * 1024)} МиБ…")


def _install_via_evermeet_zip(log: LogCb, progress: ProgressCb) -> tuple[bool, str]:
    dest_dir = _mac_bundle_bin_dir()
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, f"Не удалось создать папку: {e}"

    dest_bin = dest_dir / "ffmpeg"
    try:
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            zip_path = Path(tmp.name)
        try:
            _download_https_to_file(
                _EVERMEET_RELEASE_ZIP, zip_path, log, progress, 10, 75
            )
            progress(78, "Распаковка…")
            with tempfile.TemporaryDirectory() as td:
                td_path = Path(td)
                with zipfile.ZipFile(zip_path, "r") as zf:
                    names = [n for n in zf.namelist() if not n.endswith("/")]
                    root_ffmpeg = [
                        n for n in names if "/" not in n and Path(n).name == "ffmpeg"
                    ]
                    if not root_ffmpeg:
                        return False, (
                            "В архиве нет бинарника ffmpeg в корне "
                            "(ожидался evermeet ZIP)."
                        )
                    member = root_ffmpeg[0]
                    zf.extract(member, td_path)
                    extracted = td_path / member
                    if not extracted.is_file():
                        return False, "После распаковки ffmpeg не найден"

                progress(85, "Установка в Application Support…")
                try:
                    dest_bin.unlink(missing_ok=True)
                except OSError:
                    pass
                shutil.copy2(extracted, dest_bin)
        finally:
            try:
                zip_path.unlink(missing_ok=True)
            except OSError:
                pass

        dest_bin.chmod(dest_bin.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        _strip_macos_quarantine(dest_bin, log)

        progress(92, "Проверка ffmpeg…")
        try:
            r = subprocess.run(
                [str(dest_bin), "-hide_banner", "-version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                creationflags=_popen_flags(),
            )
            if r.returncode != 0:
                err = (r.stderr or r.stdout or "").strip()[:800]
                return False, f"ffmpeg не запускается: {err or r.returncode}"
        except OSError as e:
            if "Exec format error" in str(e) or "Bad CPU" in str(e):
                return (
                    False,
                    "Сборка с evermeet.cx — под Intel; на Apple Silicon без Rosetta "
                    "она не запускается. Установите Homebrew и повторите "
                    "(brew install ffmpeg).",
                )
            return False, str(e)

        set_ffmpeg_executable(str(dest_bin.resolve()))
        progress(100, "Готово")
        return True, str(dest_bin.resolve())
    except Exception as e:
        return False, str(e)


def _install_macos(log: LogCb, progress: ProgressCb) -> tuple[bool, str]:
    progress(1, "macOS: пробуем Homebrew…")
    ok, msg = _install_via_brew(log, progress)
    if ok:
        return True, msg

    log(f"Homebrew: {msg}")
    log("Пробуем загрузку статической сборки (evermeet.cx)…")
    progress(5, "Загрузка статической сборки…")
    return _install_via_evermeet_zip(log, progress)


def install_ffmpeg_best_effort(log: LogCb, progress: ProgressCb) -> tuple[bool, str]:
    """
    Пытается поставить ffmpeg и зарегистрировать в приложении.
    Возвращает (успех, сообщение или путь к exe).
    """
    progress(0, "Проверка…")
    if check_ffmpeg():
        p = resolve_ffmpeg_executable()
        progress(100, "Уже установлен")
        return True, p or "ffmpeg уже доступен"

    if sys.platform == "win32":
        progress(1, "Пробуем winget…")
        ok, msg = _install_via_winget(log, progress)
        if ok:
            return True, msg
        log(f"winget: {msg}")
        log("Пробуем pip + imageio-ffmpeg…")

        progress(3, "Пробуем pip + imageio-ffmpeg…")
        return _install_via_pip(log, progress)

    if sys.platform == "darwin":
        return _install_macos(log, progress)

    progress(3, "Пробуем pip + imageio-ffmpeg…")
    return _install_via_pip(log, progress)
