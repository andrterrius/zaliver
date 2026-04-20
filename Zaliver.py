"""macOS py2app entry script — produces dist/Zaliver.app (see setup_py2app.py)."""

from __future__ import annotations

import runpy

if __name__ == "__main__":
    runpy.run_module("zaliver.__main__", run_name="__main__")
