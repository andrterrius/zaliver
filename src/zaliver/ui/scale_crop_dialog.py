"""Interactive source-frame crop selection for the scaling editor."""
from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QPushButton, QVBoxLayout, QWidget


class CropCanvas(QWidget):
    def __init__(self, pixmap: QPixmap, crop, parent=None):
        super().__init__(parent)
        self.pixmap = pixmap
        left, top, right, bottom = crop
        self.selection = QRectF(left, top, 1-left-right, 1-top-bottom)
        self._drag = None
        self.setMinimumSize(360, 380)
        self.setMouseTracking(True)

    def image_rect(self):
        area = self.rect().adjusted(24, 16, -24, -16)
        size = self.pixmap.size().scaled(area.size(), Qt.AspectRatioMode.KeepAspectRatio)
        return QRectF((self.width()-size.width())/2, (self.height()-size.height())/2,
                      size.width(), size.height())

    def selection_rect(self):
        image = self.image_rect()
        r = self.selection
        return QRectF(image.left()+r.left()*image.width(), image.top()+r.top()*image.height(),
                      r.width()*image.width(), r.height()*image.height())

    def crop(self):
        r = self.selection
        return (r.left(), r.top(), 1-r.right(), 1-r.bottom())

    def reset(self):
        self.selection = QRectF(0, 0, 1, 1)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#111827'))
        image = self.image_rect()
        painter.drawPixmap(image, self.pixmap, QRectF(self.pixmap.rect()))
        r = self.selection_rect()
        painter.fillRect(QRectF(image.left(), image.top(), image.width(), r.top()-image.top()), QColor(0, 0, 0, 150))
        painter.fillRect(QRectF(image.left(), r.bottom(), image.width(), image.bottom()-r.bottom()), QColor(0, 0, 0, 150))
        painter.fillRect(QRectF(image.left(), r.top(), r.left()-image.left(), r.height()), QColor(0, 0, 0, 150))
        painter.fillRect(QRectF(r.right(), r.top(), image.right()-r.right(), r.height()), QColor(0, 0, 0, 150))
        painter.setPen(QPen(QColor('#5eead4'), 2))
        painter.drawRect(r)
        pen = QPen(QColor('#5eead4'), 1, Qt.PenStyle.DashLine)
        painter.setPen(pen)
        for i in (1, 2):
            painter.drawLine(QPointF(r.left()+r.width()*i/3, r.top()), QPointF(r.left()+r.width()*i/3, r.bottom()))
            painter.drawLine(QPointF(r.left(), r.top()+r.height()*i/3), QPointF(r.right(), r.top()+r.height()*i/3))
        for x in (r.left(), r.center().x(), r.right()):
            for y in (r.top(), r.center().y(), r.bottom()):
                if x == r.center().x() and y == r.center().y():
                    continue
                painter.fillRect(QRectF(x-4, y-4, 8, 8), QColor('#5eead4'))

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        p, r = event.position(), self.selection_rect()
        near_left, near_right = abs(p.x()-r.left()) < 10, abs(p.x()-r.right()) < 10
        near_top, near_bottom = abs(p.y()-r.top()) < 10, abs(p.y()-r.bottom()) < 10
        horizontal = ('left' if near_left else 'right' if near_right else '') if r.top()-10 <= p.y() <= r.bottom()+10 else ''
        vertical = ('top' if near_top else 'bottom' if near_bottom else '') if r.left()-10 <= p.x() <= r.right()+10 else ''
        if horizontal or vertical or r.contains(p):
            self._drag = (p, QRectF(self.selection), horizontal, vertical)

    def mouseMoveEvent(self, event):
        if not self._drag:
            return
        start, original, horizontal, vertical = self._drag
        image = self.image_rect()
        dx = (event.position().x()-start.x()) / image.width()
        dy = (event.position().y()-start.y()) / image.height()
        l, t, r, b = original.left(), original.top(), original.right(), original.bottom()
        if horizontal == 'left': l = max(0, min(r-.05, l+dx))
        elif horizontal == 'right': r = min(1, max(l+.05, r+dx))
        if vertical == 'top': t = max(0, min(b-.05, t+dy))
        elif vertical == 'bottom': b = min(1, max(t+.05, b+dy))
        if not horizontal and not vertical:
            dx = max(-l, min(1-r, dx))
            dy = max(-t, min(1-b, dy))
            l, r, t, b = l+dx, r+dx, t+dy, b+dy
        self.selection = QRectF(l, t, r-l, b-t)
        self.update()

    def mouseReleaseEvent(self, event):
        self._drag = None


class ScaleCropDialog(QDialog):
    def __init__(self, pixmap: QPixmap, crop, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Обрезка видео')
        self.resize(720, 680)
        layout = QVBoxLayout(self)
        self.canvas = CropCanvas(pixmap, crop, self)
        layout.addWidget(self.canvas, 1)
        buttons = QHBoxLayout()
        reset = QPushButton('Сбросить')
        reset.clicked.connect(self.canvas.reset)
        buttons.addWidget(reset)
        buttons.addStretch()
        cancel = QPushButton('Отмена')
        cancel.clicked.connect(self.reject)
        apply = QPushButton('Применить')
        apply.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(apply)
        layout.addLayout(buttons)

    def crop(self):
        return self.canvas.crop()
