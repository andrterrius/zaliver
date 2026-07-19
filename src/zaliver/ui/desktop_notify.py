"""Системные уведомления Zaliver (Windows toast / tray balloon)."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QWidget


def _app_icon() -> QIcon:
    app = QApplication.instance()
    if app is not None:
        ico = app.windowIcon()
        if not ico.isNull():
            return ico
    try:
        from zaliver.ui import main_window as mw

        base = Path(mw.__file__).resolve().parent / "icons"
        for name in ("app.png", "app.ico", "app.svg"):
            p = base / name
            if p.is_file():
                return QIcon(str(p))
    except Exception:
        pass
    return QIcon()


class DesktopNotifier(QObject):
    """
    Обычные системные уведомления через QSystemTrayIcon.showMessage.
    Клик по уведомлению → ``message_clicked``.
    """

    message_clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tray: QSystemTrayIcon | None = None
        self._ensure_tray()

    def _ensure_tray(self) -> QSystemTrayIcon | None:
        if self._tray is not None:
            return self._tray
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        app = QApplication.instance()
        parent = self.parent() if isinstance(self.parent(), QWidget) else app
        tray = QSystemTrayIcon(_app_icon(), parent)
        tray.setToolTip("Zaliver")
        tray.messageClicked.connect(self.message_clicked.emit)
        tray.show()
        self._tray = tray
        return tray

    def notify(
        self,
        title: str,
        message: str,
        *,
        msecs: int = 20000,
        icon: QSystemTrayIcon.MessageIcon = QSystemTrayIcon.MessageIcon.Warning,
    ) -> bool:
        """Показать системное уведомление. True если удалось."""
        tray = self._ensure_tray()
        if tray is None:
            return False
        try:
            if not tray.isVisible():
                tray.show()
            tray.showMessage(
                (title or "Zaliver").strip() or "Zaliver",
                (message or "").strip() or " ",
                icon,
                max(3000, int(msecs)),
            )
            return True
        except Exception:
            return False
