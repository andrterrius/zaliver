"""Корневое окно: выбор платформы → основное UI."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import (
    QApplication,
    QSizePolicy,
    QStackedWidget,
    QStyleFactory,
    QVBoxLayout,
    QWidget,
)

from zaliver.ui.desktop_notify import DesktopNotifier
from zaliver.ui.main_window import MainWindow
from zaliver.ui.platform import normalize_platform, platform_display_name
from zaliver.ui.platform_select import PlatformSelectPane
from zaliver.ui.widgets import install_spinbox_input_policy


def _dark_app_palette() -> QPalette:
    """Dark palette so native Windows light theme doesn't leak into popups."""
    p = QPalette()
    window = QColor("#12141c")
    base = QColor("#0f1118")
    alt = QColor("#1a1d26")
    text = QColor("#e4e6ef")
    button = QColor("#252836")
    highlight = QColor("#6366f1")
    highlighted = QColor("#f8fafc")
    disabled = QColor("#6b7280")
    p.setColor(QPalette.ColorRole.Window, window)
    p.setColor(QPalette.ColorRole.WindowText, text)
    p.setColor(QPalette.ColorRole.Base, base)
    p.setColor(QPalette.ColorRole.AlternateBase, alt)
    p.setColor(QPalette.ColorRole.Text, text)
    p.setColor(QPalette.ColorRole.Button, button)
    p.setColor(QPalette.ColorRole.ButtonText, text)
    p.setColor(QPalette.ColorRole.ToolTipBase, alt)
    p.setColor(QPalette.ColorRole.ToolTipText, text)
    p.setColor(QPalette.ColorRole.Highlight, highlight)
    p.setColor(QPalette.ColorRole.HighlightedText, highlighted)
    p.setColor(QPalette.ColorRole.PlaceholderText, disabled)
    p.setColor(QPalette.ColorRole.Link, QColor("#a5b4fc"))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, disabled)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled)
    return p


class AppShell(QWidget):
    """Стек: экран выбора режима, затем MainWindow выбранной платформы."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("zaliverRoot")
        self.setWindowTitle("Zaliver — выбор режима")
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

        self._stack = QStackedWidget()
        self._select = PlatformSelectPane()
        self._select.platform_chosen.connect(self._on_platform_chosen)
        self._stack.addWidget(self._select)

        self._main: MainWindow | None = None
        self._notifier = DesktopNotifier(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._stack)

        self._spinbox_policy = None
        app = QApplication.instance()
        if isinstance(app, QApplication):
            self._spinbox_policy = install_spinbox_input_policy(app)

        self._apply_theme()
        self._fit_to_available_screen()
        self.showMaximized()

    def _fit_to_available_screen(self) -> None:
        """Не раздувать min size сверх доступной области (Dock / Stage Manager на macOS)."""
        app = QApplication.instance()
        screen = app.primaryScreen() if app is not None else None
        if screen is None:
            self.setMinimumSize(720, 480)
            return
        geo = screen.availableGeometry()
        # Окно должно уметь ужаться в доступную геометрию, иначе Qt обрежет справа.
        min_w = min(720, max(560, geo.width()))
        min_h = min(480, max(400, geo.height()))
        self.setMinimumSize(min_w, min_h)
        if geo.width() < 1100 or geo.height() < 700:
            self.resize(
                min(1100, geo.width()),
                min(720, geo.height()),
            )

    @property
    def desktop_notifier(self) -> DesktopNotifier:
        return self._notifier

    def show_desktop_notification(
        self,
        title: str,
        message: str,
        *,
        msecs: int = 20000,
    ) -> bool:
        """Системное уведомление Windows (tray / Action Center)."""
        return self._notifier.notify(title, message, msecs=msecs)

    def _theme_path(self) -> Path:
        return Path(__file__).with_name("theme.qss")

    def _apply_theme(self) -> None:
        app = QApplication.instance()
        if app is not None:
            # Fusion + dark palette: Windows native style ignores combo popup QSS
            # under light OS theme and paints white bg with light text from "*".
            fusion = QStyleFactory.create("Fusion")
            if fusion is not None:
                app.setStyle(fusion)
            app.setPalette(_dark_app_palette())
        theme = self._theme_path()
        if theme.is_file():
            qss = theme.read_text(encoding="utf-8")
            self.setStyleSheet(qss)
            if app is not None:
                # App-level QSS so QComboBox popups (top-level) inherit styles.
                app.setStyleSheet(qss)

    def _dispose_main(self) -> None:
        if self._main is None:
            return
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self._main)
        self._stack.removeWidget(self._main)
        self._main.deleteLater()
        self._main = None

    def _on_platform_chosen(self, platform: str) -> None:
        platform = normalize_platform(platform)
        self._dispose_main()

        self._main = MainWindow(platform=platform, embedded=True)
        self._main.back_to_modes.connect(self._on_back_to_modes)
        self._stack.addWidget(self._main)
        self._stack.setCurrentWidget(self._main)
        self.setWindowTitle(f"Zaliver — {platform_display_name(platform)}")
        self._main.setStyleSheet(self.styleSheet())

    def _on_back_to_modes(self) -> None:
        self._dispose_main()
        self._stack.setCurrentWidget(self._select)
        self.setWindowTitle("Zaliver — выбор режима")
