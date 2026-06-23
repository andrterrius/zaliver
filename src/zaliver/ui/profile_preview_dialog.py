"""In-app CDP tab preview for a remote antidetect profile."""

from __future__ import annotations

import threading

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage, QPixmap, QResizeEvent
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

from zaliver.ui.profile_cdp_preview import ProfileCdpPreviewBridge, run_profile_cdp_preview_worker


class ProfileCdpPreviewDialog(QWidget):
    """Немодальное окно с JPEG-кадрами вкладки через CDP screencast."""

    def __init__(
        self,
        *,
        profile_id: str,
        profile_name: str,
        base_url: str,
        cdp_ws_url: str,
        parent=None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self._profile_id = (profile_id or "").strip()
        self.setWindowTitle(f"Просмотр — {profile_name} ({self._profile_id})")
        self.setMinimumSize(720, 480)
        self.resize(960, 600)

        self._cancel_event = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._last_jpeg: bytes | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        self._status = QLabel("Подключение…")
        self._status.setObjectName("hint")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        self._image = QLabel("Ожидание кадров…")
        self._image.setObjectName("profilePreviewImage")
        self._image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image.setMinimumHeight(360)
        self._image.setStyleSheet("background: #111; color: #888;")
        self._image.setScaledContents(False)
        root.addWidget(self._image, 1)

        self._bridge = ProfileCdpPreviewBridge(self)
        self._bridge.status.connect(self._on_status)
        self._bridge.frame_ready.connect(self._on_frame)
        self._bridge.failed.connect(self._on_failed)

        self._worker_thread = threading.Thread(
            target=run_profile_cdp_preview_worker,
            kwargs={
                "profile_id": self._profile_id,
                "base_url": base_url,
                "cdp_ws_url": cdp_ws_url,
                "cancel_event": self._cancel_event,
                "bridge": self._bridge,
            },
            daemon=True,
            name=f"cdp-preview-{self._profile_id}",
        )
        self._worker_thread.start()

    def _on_status(self, text: str) -> None:
        self._status.setText((text or "").strip() or "…")

    def _on_failed(self, message: str) -> None:
        self._status.setText(f"Ошибка: {(message or '').strip()}")

    def _on_frame(self, jpeg: bytes) -> None:
        if not jpeg:
            return
        self._last_jpeg = jpeg
        self._repaint_frame()

    def _repaint_frame(self) -> None:
        data = self._last_jpeg
        if not data:
            return
        img = QImage.fromData(data, "JPEG")
        if img.isNull():
            return
        pix = QPixmap.fromImage(img)
        target = self._image.size()
        if target.width() > 0 and target.height() > 0:
            pix = pix.scaled(
                target,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self._image.setPixmap(pix)
        self._image.setText("")

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._repaint_frame()

    def closeEvent(self, event) -> None:
        self._cancel_event.set()
        th = self._worker_thread
        if th is not None and th.is_alive():
            th.join(timeout=8.0)
        super().closeEvent(event)
