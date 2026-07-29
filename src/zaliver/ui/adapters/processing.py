"""Qt adapters that wrap headless core services with pyqtSignal."""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal

from zaliver.core.sinks import JobProgressSink
from zaliver.processing.slicing_worker import SlicingService
from zaliver.processing.stitching_worker import StitchingService
from zaliver.processing.thread_worker import ProcessingService


class ProcessingController(QObject):
    """Qt wrapper around ProcessingService (moveToThread-friendly)."""

    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(bool, str)
    log_line = pyqtSignal(str)
    output_saved = pyqtSignal(str, bool)

    def __init__(self) -> None:
        super().__init__()
        self._service = ProcessingService(
            JobProgressSink(
                on_progress=self._emit_progress,
                on_finished=self._emit_finished,
                on_log=self._emit_log,
                on_output_saved=self._emit_output_saved,
            )
        )

    def _emit_progress(self, cur: int, total: int, msg: str) -> None:
        try:
            self.progress.emit(cur, total, msg)
        except RuntimeError:
            pass

    def _emit_finished(self, ok: bool, message: str) -> None:
        try:
            self.finished.emit(ok, message)
        except RuntimeError:
            pass

    def _emit_log(self, msg: str) -> None:
        try:
            self.log_line.emit(msg)
        except RuntimeError:
            pass

    def _emit_output_saved(self, path: str, skip_upload: bool) -> None:
        try:
            self.output_saved.emit(path, skip_upload)
        except RuntimeError:
            pass

    def cancel(self) -> None:
        self._service.cancel()

    def set_upload_throttle(self, enabled: bool) -> None:
        self._service.set_upload_throttle(enabled)

    def run(self, options: dict[str, Any]) -> None:
        self._service.run(options)


class SlicingController(QObject):
    """Qt wrapper around SlicingService (moveToThread-friendly)."""

    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(bool, str)
    log_line = pyqtSignal(str)
    output_saved = pyqtSignal(str, bool)

    def __init__(self) -> None:
        super().__init__()
        self._service = SlicingService(
            JobProgressSink(
                on_progress=self._emit_progress,
                on_finished=self._emit_finished,
                on_log=self._emit_log,
                on_output_saved=self._emit_output_saved,
            )
        )

    def _emit_progress(self, cur: int, total: int, msg: str) -> None:
        try:
            self.progress.emit(cur, total, msg)
        except RuntimeError:
            pass

    def _emit_finished(self, ok: bool, message: str) -> None:
        try:
            self.finished.emit(ok, message)
        except RuntimeError:
            pass

    def _emit_log(self, msg: str) -> None:
        try:
            self.log_line.emit(msg)
        except RuntimeError:
            pass

    def _emit_output_saved(self, path: str, skip_upload: bool) -> None:
        try:
            self.output_saved.emit(path, skip_upload)
        except RuntimeError:
            pass

    def cancel(self) -> None:
        self._service.cancel()

    def set_upload_throttle(self, enabled: bool) -> None:
        self._service.set_upload_throttle(enabled)

    def run(self, options: dict[str, Any]) -> None:
        self._service.run(options)


class StitchingController(QObject):
    """Qt wrapper around StitchingService (moveToThread-friendly)."""

    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(bool, str)
    log_line = pyqtSignal(str)
    output_saved = pyqtSignal(str, bool)

    def __init__(self) -> None:
        super().__init__()
        self._service = StitchingService(
            JobProgressSink(
                on_progress=self._emit_progress,
                on_finished=self._emit_finished,
                on_log=self._emit_log,
                on_output_saved=self._emit_output_saved,
            )
        )

    def _emit_progress(self, cur: int, total: int, msg: str) -> None:
        try:
            self.progress.emit(cur, total, msg)
        except RuntimeError:
            pass

    def _emit_finished(self, ok: bool, message: str) -> None:
        try:
            self.finished.emit(ok, message)
        except RuntimeError:
            pass

    def _emit_log(self, msg: str) -> None:
        try:
            self.log_line.emit(msg)
        except RuntimeError:
            pass

    def _emit_output_saved(self, path: str, skip_upload: bool) -> None:
        try:
            self.output_saved.emit(path, skip_upload)
        except RuntimeError:
            pass

    def cancel(self) -> None:
        self._service.cancel()

    def set_upload_throttle(self, enabled: bool) -> None:
        self._service.set_upload_throttle(enabled)

    def run(self, options: dict[str, Any]) -> None:
        self._service.run(options)
