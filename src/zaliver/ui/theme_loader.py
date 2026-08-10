"""Загрузка theme.qss с подстановкой путей к иконкам."""

from __future__ import annotations

from pathlib import Path


def theme_qss_path() -> Path:
    return Path(__file__).with_name("theme.qss")


def load_theme_qss() -> str:
    path = theme_qss_path()
    if not path.is_file():
        return ""
    qss = path.read_text(encoding="utf-8")
    icons = Path(__file__).parent / "icons"
    up = (icons / "spin_up.png").resolve().as_posix()
    down = (icons / "spin_down.png").resolve().as_posix()
    return (
        qss.replace("__SPIN_UP_ARROW__", up).replace("__SPIN_DOWN_ARROW__", down)
    )
