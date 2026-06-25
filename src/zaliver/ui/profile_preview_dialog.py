"""In-app CDP tab preview for a remote antidetect profile."""

from __future__ import annotations

import threading

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage, QPixmap, QResizeEvent
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from zaliver.antydetect.local_antidetect_api import LocalAntidetectError, LocalAntidetectHttpAPI
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
        session_id: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._profile_id = (profile_id or "").strip()
        self._base_url = (base_url or "").strip()
        self._session_id = (session_id or "").strip()
        self._stop_in_progress = False
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

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._stop_remote_btn = QPushButton("Закрыть удалённый браузер")
        self._stop_remote_btn.setObjectName("secondary")
        self._stop_remote_btn.setAutoDefault(False)
        self._stop_remote_btn.setDefault(False)
        self._stop_remote_btn.setToolTip(
            "Отправить POST /sessions/{id}/stop в API антидетекта и завершить сессию профиля"
        )
        self._stop_remote_btn.clicked.connect(self._on_stop_remote_browser)
        btn_row.addWidget(self._stop_remote_btn)
        root.addLayout(btn_row)

        self._bridge = ProfileCdpPreviewBridge(self)
        self._bridge.status.connect(self._on_status)
        self._bridge.frame_ready.connect(self._on_frame)
        self._bridge.failed.connect(self._on_failed)
        self._bridge.remote_stop_done.connect(self._on_remote_stop_done)

        self._worker_thread = threading.Thread(
            target=run_profile_cdp_preview_worker,
            kwargs={
                "profile_id": self._profile_id,
                "base_url": self._base_url,
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

    def _on_stop_remote_browser(self) -> None:
        if self._stop_in_progress:
            return
        if not self._base_url:
            self._status.setText("Ошибка: базовый URL API не задан.")
            return
        self._stop_in_progress = True
        self._stop_remote_btn.setEnabled(False)
        self._status.setText("Остановка удалённого браузера…")
        self._cancel_event.set()
        threading.Thread(
            target=self._stop_remote_worker,
            daemon=True,
            name=f"cdp-preview-stop-{self._profile_id}",
        ).start()

    def _stop_remote_worker(self) -> None:
        ok = False
        message = ""
        try:
            api = LocalAntidetectHttpAPI(self._base_url)
            try:
                sid = self._session_id
                if not sid:
                    sid = api.find_running_session_id_for_profile(self._profile_id) or ""
                if not sid:
                    raise LocalAntidetectError(
                        "Не найдена запущенная сессия для этого профиля."
                    )
                api.stop_session(sid)
            finally:
                api.close()
            ok = True
            message = "Запрос на остановку сессии отправлен. Браузер закрывается…"
        except Exception as e:
            message = f"Не удалось остановить сессию: {e}"
        self._bridge.remote_stop_done.emit(ok, message)

    def _on_remote_stop_done(self, ok: bool, message: str) -> None:
        self._status.setText((message or "").strip())
        if ok:
            self.close()
            return
        self._stop_in_progress = False
        self._stop_remote_btn.setEnabled(True)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._repaint_frame()

    def closeEvent(self, event) -> None:
        self._cancel_event.set()
        super().closeEvent(event)
