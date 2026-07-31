"""Mitigate Windows console CTRL_* killing the API process."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator

_CTRL_C_EVENT = 0
_CTRL_BREAK_EVENT = 1

# Keep handler alive for process lifetime when installed permanently.
_permanent_handler = None


def install_permanent_ctrl_break_guard() -> None:
    """Ignore CTRL_BREAK for the whole API process (children crashing in console)."""
    global _permanent_handler
    if sys.platform != "win32":
        return
    if _permanent_handler is not None:
        return
    import ctypes
    from ctypes import WINFUNCTYPE, wintypes

    HandlerRoutine = WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    def _handler(ctrl_type: int) -> bool:
        # Swallow BREAK; still allow Ctrl+C (0) to stop the server.
        return ctrl_type == _CTRL_BREAK_EVENT

    _permanent_handler = HandlerRoutine(_handler)
    ctypes.windll.kernel32.SetConsoleCtrlHandler(_permanent_handler, True)


@contextmanager
def suppress_console_ctrl(*, also_ctrl_c: bool = False) -> Iterator[None]:
    """Swallow console ctrl events so child tools don't tear down uvicorn."""
    if sys.platform != "win32":
        yield
        return

    import ctypes
    from ctypes import WINFUNCTYPE, wintypes

    HandlerRoutine = WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    allowed = {_CTRL_BREAK_EVENT}
    if also_ctrl_c:
        allowed.add(_CTRL_C_EVENT)

    def _handler(ctrl_type: int) -> bool:
        return ctrl_type in allowed

    handler = HandlerRoutine(_handler)
    kernel32 = ctypes.windll.kernel32
    if not kernel32.SetConsoleCtrlHandler(handler, True):
        yield
        return
    try:
        yield
    finally:
        kernel32.SetConsoleCtrlHandler(handler, False)
