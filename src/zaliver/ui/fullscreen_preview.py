"""Move an interactive preview into a full-screen window and restore it afterwards."""

from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtWidgets import QDialog, QPushButton, QSizePolicy, QVBoxLayout, QWidget


class FullscreenPreview(QObject):
    def __init__(self, preview):
        super().__init__(preview)
        self.preview = preview
        self.dialog = None
        self.button = QPushButton("⛶", preview)
        self.button.setToolTip("Весь экран")
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.button.adjustSize()
        self.button.clicked.connect(self.toggle)
        preview.installEventFilter(self)
        self._position()

    def _position(self):
        self.button.move(max(0, self.preview.width() - self.button.width() - 8), 8)
        self.button.raise_()

    def eventFilter(self, watched, event):
        if watched is self.preview and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self._position()
        return super().eventFilter(watched, event)

    def toggle(self):
        if self.dialog is not None:
            self.dialog.close()
            return
        preview = self.preview
        parent = preview.parentWidget()
        layout = parent.layout() if parent else None
        if layout is None:
            return
        placeholder = QWidget(parent)
        placeholder.setMinimumSize(preview.minimumSize())
        layout.replaceWidget(preview, placeholder)
        maximum = preview.maximumSize()
        policy = preview.sizePolicy()
        dialog = QDialog(parent.window())
        dialog.setWindowTitle("Предпросмотр")
        box = QVBoxLayout(dialog)
        box.setContentsMargins(0, 0, 0, 0)
        preview.setMaximumSize(16777215, 16777215)
        preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        box.addWidget(preview)
        self.dialog = dialog
        self.button.setText("⛶")
        self.button.setToolTip("Выйти из полноэкранного режима")

        def restore(_result):
            box.removeWidget(preview)
            preview.setParent(parent)
            preview.setMaximumSize(maximum)
            preview.setSizePolicy(policy)
            layout.replaceWidget(placeholder, preview)
            placeholder.deleteLater()
            preview.show()
            self.dialog = None
            self.button.setText("⛶")
            self.button.setToolTip("Весь экран")
            self._position()
            dialog.deleteLater()

        dialog.finished.connect(restore)
        dialog.showFullScreen()
        preview.show()
