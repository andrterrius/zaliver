from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from PyQt6.QtCore import Qt, QRectF, QPointF, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap, QKeySequence, QShortcut, QMovie, QImage
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QFileDialog, QLabel, QListWidget, QMessageBox, QGroupBox, QCheckBox
from zaliver.ui.text_overlay_preview import load_video_frame_pixmap, _PreviewVideoWorker
from zaliver.ui.fullscreen_preview import FullscreenPreview
from zaliver.ui.widgets import ToggleSwitch
from zaliver.processing.banner_overlay import VIDEO_BANNER_EXTENSIONS
from zaliver.processing.ffmpeg_merge import resolve_ffmpeg_executable
from zaliver.processing.subprocess_flags import popen_creationflags


class BannerCanvas(QWidget):
    changed = pyqtSignal()
    selected = pyqtSignal(int)
    edit_started = pyqtSignal()
    edit_finished = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.items = []
        self.images = {}
        self.movies = {}
        self.background = None
        self.video_path = ''
        self._play_worker = None
        self._playing = False
        self.index = -1
        self.drag = None
        self.setMinimumHeight(300)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.play_button = QPushButton('▶', self)
        self.play_button.setObjectName('textPreviewNav')
        self.play_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.play_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_button.setFixedSize(36, 36)
        self.play_button.setAutoDefault(False)
        self.play_button.setDefault(False)
        self.play_button.setEnabled(False)
        self.play_button.setToolTip('Воспроизвести видео')
        self.play_button.clicked.connect(self.toggle_playback)
        self._fullscreen_preview = FullscreenPreview(self)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.play_button.move(max(10, self.width() - 46), max(10, self.height() - 46))
        self.play_button.raise_()

    def set_video(self, path):
        self.stop_playback(restore=False)
        self.video_path = str(path or '')
        self.background = load_video_frame_pixmap(self.video_path) if self.video_path else None
        self.play_button.setEnabled(bool(self.video_path and Path(self.video_path).is_file()))
        self.update()

    def toggle_playback(self):
        if self._playing:
            self.stop_playback()
            return
        if not self.video_path or not Path(self.video_path).is_file():
            return
        worker = _PreviewVideoWorker(self.video_path)
        self._play_worker = worker
        self._playing = True
        self.play_button.setText('■')
        self.play_button.setToolTip('Остановить просмотр')
        worker.frame_ready.connect(self._video_frame)
        worker.failed.connect(self._video_failed)
        worker.finished_ok.connect(self._video_finished)
        worker.start()

    def _video_frame(self, data, width, height):
        if self._playing:
            image = QImage(data, width, height, width * 3, QImage.Format.Format_RGB888)
            if not image.isNull():
                self.background = QPixmap.fromImage(image.copy())
                self.update()

    def _video_failed(self, _message):
        self.stop_playback()

    def _video_finished(self):
        self.stop_playback()

    def stop_playback(self, *, restore=True):
        worker, self._play_worker = self._play_worker, None
        self._playing = False
        if worker is not None:
            for signal, slot in ((worker.frame_ready, self._video_frame), (worker.failed, self._video_failed), (worker.finished_ok, self._video_finished)):
                try: signal.disconnect(slot)
                except TypeError: pass
            worker.request_stop()
            if worker.isRunning() and not worker.wait(1500):
                worker.terminate(); worker.wait(500)
        self.play_button.setText('▶')
        self.play_button.setToolTip('Воспроизвести видео')
        if restore and self.video_path:
            self.background = load_video_frame_pixmap(self.video_path)
            self.update()

    def load_media(self, path):
        if path in self.images:
            return self.images[path]
        suffix = Path(path).suffix.lower()
        if suffix in VIDEO_BANNER_EXTENSIONS:
            pix = QPixmap()
            exe = resolve_ffmpeg_executable()
            if exe:
                try:
                    result = subprocess.run([exe, '-v', 'error', '-i', path, '-frames:v', '1',
                                             '-vf', 'scale=720:720:force_original_aspect_ratio=decrease',
                                             '-f', 'image2pipe', '-vcodec', 'png', '-'],
                                            capture_output=True, timeout=15, creationflags=popen_creationflags())
                    if result.returncode == 0:
                        pix.loadFromData(result.stdout, 'PNG')
                except (OSError, subprocess.TimeoutExpired):
                    pass
        else:
            pix = QPixmap(path)
        if pix.isNull():
            return pix
        self.images[path] = pix
        if suffix == '.gif':
            movie = QMovie(path, parent=self)
            if movie.isValid():
                movie.frameChanged.connect(lambda _frame, p=path, m=movie: self._movie_frame(p, m))
                movie.finished.connect(lambda p=path, m=movie: QTimer.singleShot(0, lambda: self._restart_movie(p, m)))
                self.movies[path] = movie
                movie.start()
                if not self.isVisible():
                    movie.setPaused(True)
        return pix

    def _restart_movie(self, path, movie):
        if self.movies.get(path) is movie and self.isVisible():
            movie.start()

    def showEvent(self, event):
        super().showEvent(event)
        for movie in self.movies.values():
            if movie.state() == QMovie.MovieState.Paused:
                movie.setPaused(False)
            elif movie.state() == QMovie.MovieState.NotRunning:
                movie.start()

    def hideEvent(self, event):
        self.stop_playback(restore=False)
        for movie in self.movies.values():
            if movie.state() == QMovie.MovieState.Running:
                movie.setPaused(True)
        super().hideEvent(event)

    def _movie_frame(self, path, movie):
        self.images[path] = movie.currentPixmap()
        if self.isVisible():
            self.update()

    def frame(self):
        ratio = (self.background.width() / self.background.height()) if self.background and not self.background.isNull() else 9 / 16
        w = min(self.width() - 16, (self.height() - 16) * ratio)
        h = w / ratio
        return QRectF((self.width()-w)/2, (self.height()-h)/2, w, h)

    def item_rect(self, b):
        f = self.frame()
        return QRectF(f.x()+b['x']*f.width(), f.y()+b['y']*f.height(), b['w']*f.width(), b['h']*f.height())

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor('#20232a'))
        f = self.frame()
        p.fillRect(f, QColor('#101010'))
        if self.background and not self.background.isNull():
            p.drawPixmap(f, self.background, QRectF(self.background.rect()))
        p.save(); p.setClipRect(f)
        for b in self.items:
            pix = self.images.get(b['path'])
            if pix and not pix.isNull(): p.drawPixmap(self.item_rect(b), pix, QRectF(pix.rect()))
        p.restore()
        if 0 <= self.index < len(self.items):
            r = self.item_rect(self.items[self.index])
            p.setPen(QPen(QColor('#48b7ff'), 2)); p.setBrush(Qt.BrushStyle.NoBrush); p.drawRect(r)
            p.setBrush(QColor('white'))
            for point in (r.topLeft(), r.topRight(), r.bottomLeft(), r.bottomRight()):
                p.drawRect(QRectF(point.x()-5, point.y()-5, 10, 10))

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton: return
        self.setFocus()
        pos = e.position()
        if 0 <= self.index < len(self.items):
            r = self.item_rect(self.items[self.index])
            for name, point in [('tl',r.topLeft()),('tr',r.topRight()),('bl',r.bottomLeft()),('br',r.bottomRight())]:
                if abs(pos.x()-point.x()) <= 9 and abs(pos.y()-point.y()) <= 9:
                    self.edit_started.emit(); self.drag = (name, pos, dict(self.items[self.index])); return
        for i in range(len(self.items)-1, -1, -1):
            if self.item_rect(self.items[i]).contains(pos):
                self.index = i; self.selected.emit(i); self.edit_started.emit(); self.drag = ('move', pos, dict(self.items[i])); self.update(); return
        self.index = -1; self.selected.emit(-1); self.update()

    def mouseMoveEvent(self, e):
        if not self.drag or self.index < 0: return
        mode, start, original = self.drag
        f = self.frame(); dx = (e.position().x()-start.x())/f.width(); dy = (e.position().y()-start.y())/f.height()
        b = self.items[self.index]
        if mode == 'move':
            # Keep a small part visible so an oversized banner remains selectable.
            b['x'] = max(.02-original['w'], min(.98, original['x']+dx))
            b['y'] = max(.02-original['h'], min(.98, original['y']+dy))
        else:
            left, top = original['x'], original['y']; right, bottom = left+original['w'], top+original['h']
            if 'l' in mode: left = min(right-.02, left+dx)
            else: right = max(left+.02, right+dx)
            if 't' in mode: top = min(bottom-.02, top+dy)
            else: bottom = max(top+.02, bottom+dy)
            b.update(x=left, y=top, w=right-left, h=bottom-top)
        self.changed.emit(); self.update()

    def mouseReleaseEvent(self, e):
        if self.drag is not None:
            self.drag = None
            self.edit_finished.emit()


class BannerEditor(QWidget):
    def __init__(self, settings, key, video_provider):
        super().__init__(); self.settings = settings; self.key = key; self.video_provider = video_provider; self.last_video = None
        self.video_index = 0
        self._current_video_key = ''
        try:
            self._per_video = json.loads(settings.value(key + '/per_video', '{}', type=str) or '{}')
            if not isinstance(self._per_video, dict): self._per_video = {}
        except (TypeError, ValueError):
            self._per_video = {}
        self.canvas = BannerCanvas(); self.list = QListWidget(); self.list.setMaximumHeight(100)
        self._undo = []; self._redo = []; self._edit_before = None
        self.enabled = ToggleSwitch('Включить баннеры')
        self.enabled.setChecked(settings.value(key + '/enabled', False, type=bool))
        self.enabled.toggled.connect(lambda checked: self.settings.setValue(self.key + '/enabled', checked))
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        group = QGroupBox('Баннеры на видео')
        outer.addWidget(group)
        group_layout = QVBoxLayout(group)
        group_layout.setSpacing(8)
        group_layout.addWidget(self.enabled)
        self.panel = QWidget()
        group_layout.addWidget(self.panel)
        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        row = QHBoxLayout(); add = QPushButton('Добавить баннеры'); remove = QPushButton('Удалить выбранный'); row.addWidget(add); row.addWidget(remove); layout.addLayout(row)
        center_x = QPushButton('По горизонтали'); center_y = QPushButton('По вертикали')
        center_x.setToolTip('Центрировать выбранный баннер по горизонтали')
        center_y.setToolTip('Центрировать выбранный баннер по вертикали')
        row.addWidget(center_x); row.addWidget(center_y)
        center_x.clicked.connect(lambda: self.center('x'))
        center_y.clicked.connect(lambda: self.center('y'))
        layout.addWidget(self.list); layout.addWidget(self.canvas)
        self.repeat_first = QCheckBox('Повторить эти настройки для остальных видео')
        self.repeat_first.setChecked(settings.value(key + '/repeat_first', False, type=bool))
        layout.addWidget(self.repeat_first, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.repeat_first.toggled.connect(self._repeat_toggled)
        nav = QHBoxLayout()
        nav.setContentsMargins(0, 0, 0, 0)
        nav.setSpacing(8)
        self.video_prev = QPushButton('<'); self.video_next = QPushButton('>')
        for button in (self.video_prev, self.video_next):
            button.setObjectName('textPreviewNav')
            button.setFixedSize(36, 36)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setAutoDefault(False)
            button.setDefault(False)
        self.video_prev.setToolTip('Предыдущее видео'); self.video_next.setToolTip('Следующее видео')
        self.video_label = QLabel()
        self.video_label.setObjectName('hint')
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setWordWrap(True)
        nav.addWidget(self.video_prev); nav.addWidget(self.video_label, 1); nav.addWidget(self.video_next)
        layout.addLayout(nav)
        self.video_prev.clicked.connect(lambda: self.change_video(-1))
        self.video_next.clicked.connect(lambda: self.change_video(1))
        self.panel.setVisible(self.enabled.isChecked())
        self.panel.setEnabled(self.enabled.isChecked())
        self.enabled.toggled.connect(self.panel.setVisible)
        self.enabled.toggled.connect(self.panel.setEnabled)
        self.enabled.toggled.connect(lambda checked: self.update_video() if checked else None)
        add.clicked.connect(self.add); remove.clicked.connect(self.remove); self.list.currentRowChanged.connect(self.select); self.canvas.selected.connect(self.list.setCurrentRow); self.canvas.changed.connect(self.save)
        self.canvas.edit_started.connect(self._begin_edit); self.canvas.edit_finished.connect(self._end_edit)
        self._shortcuts = []
        for widget in (self.canvas, self.list):
            for keys, action in (('Delete', self.remove), ('Ctrl+Z', self.undo), ('Ctrl+Y', self.redo), ('Ctrl+Shift+Z', self.redo)):
                shortcut = QShortcut(QKeySequence(keys), widget)
                shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
                shortcut.activated.connect(action); self._shortcuts.append(shortcut)
        try: data = json.loads(settings.value(key, '[]', type=str) or '[]')
        except (ValueError, TypeError): data = []
        for b in data if isinstance(data, list) else []:
            try:
                path = str(b['path']); pix = self.canvas.load_media(path)
                item = dict(path=path, x=float(b['x']), y=float(b['y']), w=float(b['w']), h=float(b['h']))
                self.canvas.items.append(item); self.canvas.images[path] = pix
            except (KeyError, TypeError, ValueError): continue
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.update_video()

    def change_video(self, delta):
        videos = list(self.video_provider() or [])
        if videos:
            self.video_index = (self.video_index + delta) % len(videos)
        self.update_video()

    def update_video(self):
        videos = list(self.video_provider() or [])
        self.video_index = min(self.video_index, max(0, len(videos) - 1))
        path = str(videos[self.video_index]) if videos else ''
        new_key = self._video_key(path) if path else ''
        self.repeat_first.setVisible(len(videos) > 1 and self.video_index == 0)
        if new_key != self._current_video_key:
            if self._current_video_key:
                self._per_video[self._current_video_key] = [dict(b) for b in self.canvas.items]
            if new_key:
                if new_key not in self._per_video:
                    initial = self.canvas.items if not self._per_video else (self._per_video.get(self._video_key(str(videos[0])), []) if self.repeat_first.isChecked() else [])
                    self._per_video[new_key] = [dict(b) for b in initial]
                self.canvas.items = [dict(b) for b in self._per_video[new_key]]
                for b in self.canvas.items:
                    self.canvas.load_media(b['path'])
                self.canvas.index = -1
                self._undo.clear(); self._redo.clear()
                self.refresh()
                self.list.setCurrentRow(-1)
            self._current_video_key = new_key
        self.video_prev.setEnabled(len(videos) > 1)
        self.video_next.setEnabled(len(videos) > 1)
        self.video_label.setText(f'{self.video_index + 1} / {len(videos)} — {Path(path).name}' if path else 'Нет входных видео')
        self.video_label.setToolTip(path)
        if path != self.last_video:
            self.last_video = path
            self.canvas.set_video(path)

    def add(self):
        before = self._snapshot()
        paths, _ = QFileDialog.getOpenFileNames(self, 'Выберите баннеры', '', 'Изображения и видео (*.png *.jpg *.jpeg *.webp *.bmp *.gif *.mp4 *.mov *.mkv *.webm *.avi *.m4v *.mpeg *.mpg *.ts);;Все файлы (*)')
        for path in paths:
            pix = self.canvas.load_media(path)
            if pix.isNull(): QMessageBox.warning(self, 'Баннеры', f'Не удалось открыть: {path}'); continue
            f = self.canvas.frame(); w = .35; h = min(.8, w * f.width()/f.height() * pix.height()/pix.width())
            self.canvas.items.append(dict(path=path, x=(1-w)/2, y=(1-h)/2, w=w, h=h)); self.canvas.images[path] = pix
        self.refresh(); self.list.setCurrentRow(len(self.canvas.items)-1); self.save(); self._record(before)

    def remove(self):
        i = self.list.currentRow()
        if 0 <= i < len(self.canvas.items):
            before = self._snapshot()
            self.canvas.items.pop(i); self.refresh(); self.list.setCurrentRow(min(i, len(self.canvas.items)-1)); self.save(); self._record(before)

    def _snapshot(self):
        return ([dict(item) for item in self.canvas.items], self.canvas.index)

    def _record(self, before):
        if before[0] == self.canvas.items:
            return
        self._undo.append(before)
        self._undo = self._undo[-100:]
        self._redo.clear()

    def _begin_edit(self):
        self._edit_before = self._snapshot()

    def _end_edit(self):
        if self._edit_before is not None:
            self._record(self._edit_before)
            self._edit_before = None

    def _restore(self, state):
        items, index = state
        self.canvas.items = [dict(item) for item in items]
        for item in items:
            if item['path'] not in self.canvas.images:
                self.canvas.load_media(item['path'])
        self.refresh()
        self.list.setCurrentRow(index if 0 <= index < len(items) else -1)
        self.canvas.index = self.list.currentRow()
        self.canvas.update(); self.save()

    def undo(self):
        if self._undo:
            self._redo.append(self._snapshot())
            self._restore(self._undo.pop())

    def redo(self):
        if self._redo:
            self._undo.append(self._snapshot())
            self._restore(self._redo.pop())

    def center(self, axis):
        i = self.canvas.index
        if 0 <= i < len(self.canvas.items):
            before = self._snapshot()
            item = self.canvas.items[i]
            item[axis] = (1.0 - item['w' if axis == 'x' else 'h']) / 2
            self.canvas.update(); self.save(); self._record(before)

    def select(self, i): self.canvas.index = i; self.canvas.update()
    def refresh(self):
        active_paths = {item['path'] for item in self.canvas.items}
        for path in set(self.canvas.images) - active_paths:
            movie = self.canvas.movies.pop(path, None)
            if movie is not None:
                movie.stop(); movie.setFileName(''); movie.deleteLater()
            self.canvas.images.pop(path, None)
        self.list.blockSignals(True); self.list.clear(); self.list.addItems([Path(b['path']).name + (' (файл не найден)' if not Path(b['path']).is_file() else '') for b in self.canvas.items]); self.list.blockSignals(False); self.canvas.update()
    @staticmethod
    def _video_key(path):
        return os.path.normcase(os.path.abspath(str(path)))

    def _repeat_toggled(self, checked):
        self.settings.setValue(self.key + '/repeat_first', checked)
        if checked and self.video_index == 0:
            self.save()

    def save(self):
        self.settings.setValue(self.key, json.dumps(self.canvas.items, ensure_ascii=False))
        if self._current_video_key:
            self._per_video[self._current_video_key] = [dict(b) for b in self.canvas.items]
            videos = list(self.video_provider() or [])
            if self.repeat_first.isChecked() and self.video_index == 0:
                for path in videos[1:]:
                    self._per_video[self._video_key(path)] = [dict(b) for b in self.canvas.items]
            self.settings.setValue(self.key + '/per_video', json.dumps(self._per_video, ensure_ascii=False))

    def options_by_video(self):
        if not self.enabled.isChecked(): return {}
        # The source list may have changed while this tab was hidden.
        # Bind the visible settings to the selected source before collecting them.
        self.update_video()
        self.save()
        videos = list(self.video_provider() or [])
        first = self._per_video.get(self._video_key(videos[0]), []) if videos else []
        return {self._video_key(p): [dict(b) for b in self._per_video.get(self._video_key(p), first if self.repeat_first.isChecked() else [])]
                for p in videos}
    def options(self): return [dict(b) for b in self.canvas.items] if self.enabled.isChecked() else []
