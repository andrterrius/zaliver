"""Windows-safe flags for child processes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def popen_creationflags() -> int:
    """Hide console windows for ffmpeg/ffprobe/workers."""
    if sys.platform != "win32":
        return 0
    # CREATE_NO_WINDOW only. CREATE_NEW_PROCESS_GROUP causes 0xC0000005 on some hosts.
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def worker_creationflags() -> int:
    """Same as popen_creationflags — keep workers console-less without a new group."""
    return popen_creationflags()


def resolve_python_executable() -> str:
    """Use the same interpreter as the API (python.exe). pythonw can break imports."""
    return str(Path(sys.executable))
