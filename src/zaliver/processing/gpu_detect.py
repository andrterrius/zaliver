"""GPU discovery helpers (best-effort, no extra deps).

Goal: list adapters (NVIDIA/AMD/Intel) and provide a short log-friendly summary.
This is informational: actual usability for encoding is decided by ffmpeg runtime probes.
"""

from __future__ import annotations

import platform
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class GPUInfo:
    name: str
    vendor: str  # "nvidia" | "amd" | "intel" | "unknown"
    driver: Optional[str] = None


def _classify_vendor(name: str) -> str:
    s = (name or "").lower()
    if "nvidia" in s or "geforce" in s or "quadro" in s or "rtx" in s or "gtx" in s:
        return "nvidia"
    if "amd" in s or "radeon" in s or "rx " in s or "vega" in s:
        return "amd"
    if "intel" in s or "uhd" in s or "iris" in s or "arc" in s:
        return "intel"
    return "unknown"


def _run(cmd: List[str], timeout_s: float = 6.0) -> str:
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if sys.platform == "win32"
            else 0,
        )
        return (p.stdout or p.stderr or "").strip()
    except Exception:
        return ""


def _detect_windows() -> List[GPUInfo]:
    # Prefer PowerShell CIM query (modern). Fall back to wmic (deprecated but often present).
    ps = _run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_VideoController | "
            "Select-Object Name, DriverVersion | "
            "Format-Table -HideTableHeaders",
        ],
        timeout_s=8.0,
    )
    rows: List[str] = []
    if ps:
        rows = [ln.strip() for ln in ps.splitlines() if ln.strip()]
    else:
        wmic = _run(
            ["wmic", "path", "win32_VideoController", "get", "Name,DriverVersion"],
            timeout_s=8.0,
        )
        if wmic:
            rows = [ln.strip() for ln in wmic.splitlines() if ln.strip()]
            # drop header line if present
            if rows and "DriverVersion" in rows[0] and "Name" in rows[0]:
                rows = rows[1:]

    gpus: List[GPUInfo] = []
    for ln in rows:
        # PowerShell output tends to be: "<name>  <driver>"
        parts = [p for p in ln.split("  ") if p.strip()]
        parts = [p.strip() for p in parts if p.strip()]
        if not parts:
            continue
        name = parts[0]
        drv = parts[-1] if len(parts) > 1 else None
        gpus.append(GPUInfo(name=name, vendor=_classify_vendor(name), driver=drv))
    # Deduplicate by name
    seen: set[str] = set()
    out: List[GPUInfo] = []
    for g in gpus:
        key = g.name.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(g)
    return out


def _detect_macos() -> List[GPUInfo]:
    sp = _run(["system_profiler", "SPDisplaysDataType"], timeout_s=10.0)
    if not sp:
        return []
    gpus: List[GPUInfo] = []
    for ln in sp.splitlines():
        s = ln.strip()
        if s.lower().startswith("chipset model:"):
            name = s.split(":", 1)[1].strip()
            gpus.append(GPUInfo(name=name, vendor=_classify_vendor(name)))
    return gpus


def _detect_linux() -> List[GPUInfo]:
    # lspci is common but not guaranteed.
    lspci = _run(["bash", "-lc", "lspci -nn | grep -Ei 'vga|3d|display'"], timeout_s=6.0)
    if not lspci:
        return []
    gpus: List[GPUInfo] = []
    for ln in lspci.splitlines():
        name = ln.strip()
        if name:
            gpus.append(GPUInfo(name=name, vendor=_classify_vendor(name)))
    return gpus


def detect_gpus() -> List[GPUInfo]:
    sysname = platform.system().lower()
    if sysname == "windows":
        return _detect_windows()
    if sysname == "darwin":
        return _detect_macos()
    if sysname == "linux":
        return _detect_linux()
    return []


def format_gpu_list(gpus: List[GPUInfo]) -> str:
    if not gpus:
        return "GPU: не обнаружено (или нет прав/инструментов для определения)."
    parts: List[str] = []
    for g in gpus:
        if g.driver:
            parts.append(f"{g.name} ({g.vendor}, driver {g.driver})")
        else:
            parts.append(f"{g.name} ({g.vendor})")
    return "GPU: " + " · ".join(parts)

