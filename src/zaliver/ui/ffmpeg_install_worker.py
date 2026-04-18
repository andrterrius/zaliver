"""Фоновая установка ffmpeg (Qt thread)."""

from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal

from zaliver.processing.ffmpeg_install import install_ffmpeg_best_effort


class FfmpegInstallWorker(QObject):
    log_line = pyqtSignal(str)
    progress = pyqtSignal(int, str)
    finished = pyqtSignal(bool, str)

    def run(self) -> None:
        def log(m: str) -> None:
            self.log_line.emit(m)

        def prog(p: int, t: str) -> None:
            self.progress.emit(p, t)

        ok, msg = install_ffmpeg_best_effort(log, prog)
        self.finished.emit(ok, msg)
