"""macOS py2app entry script — produces dist/Zaliver.app (run from macos_app/)."""

from __future__ import annotations

import runpy

if __name__ == "__main__":
    runpy.run_module("zaliver.__main__", run_name="__main__")
