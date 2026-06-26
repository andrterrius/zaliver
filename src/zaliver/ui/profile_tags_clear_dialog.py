"""Диалог выбора служебных тегов Zaliver для снятия с профилей."""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, QPoint, QRect, Qt, pyqtSignal
from PyQt6.QtGui import QCursor, QMouseEvent
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStyle,
    QStyleOptionButton,
    QVBoxLayout,
    QWidget,
)

from zaliver.antydetect.profile_tags import ZALIVER_PROFILE_TAGS
from zaliver.ui.antic_profile_row import _profile_tag_list, _tag_semantic_kind
from zaliver.ui.profile_list_helpers import _tag_chip_object_name

_DRAG_THRESHOLD_PX = 5


def collect_zaliver_tags_for_profiles(
    profiles_by_id: dict[str, dict[str, object]],
    profile_ids: list[str],
) -> tuple[str, ...]:
    """Уникальные служебные теги Zaliver, присутствующие у выбранных профилей."""
    zaliver_set = set(ZALIVER_PROFILE_TAGS)
    found: set[str] = set()
    for pid in profile_ids:
        p = profiles_by_id.get((pid or "").strip())
        if not p:
            continue
        for tag in _profile_tag_list(p):
            if tag in zaliver_set:
                found.add(tag)
    return tuple(t for t in ZALIVER_PROFILE_TAGS if t in found)


def _checkbox_indicator_rect(cb: QCheckBox) -> QRect:
    opt = QStyleOptionButton()
    cb.initStyleOption(opt)
    return cb.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, opt, cb)


def _apply_tag_clear_label_style(lbl: QLabel, tag: str) -> None:
    kind = _tag_semantic_kind(tag)
    lbl.setProperty("tagSemantic", kind or "")
    lbl.style().unpolish(lbl)
    lbl.style().polish(lbl)


class _TagClearListDragSelect(QObject):
    """Клик и протягивание ЛКМ только по квадратикам чекбоксов."""

    selection_changed = pyqtSignal()

    def __init__(
        self,
        *,
        viewport: QWidget,
        rows: list[tuple[str, QFrame, QCheckBox]],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._viewport = viewport
        self._rows = list(rows)
        self._tags = [tag for tag, _, _ in self._rows]
        self._checkbox_by_tag = {tag: cb for tag, _, cb in self._rows}
        self._checkbox_to_index = {cb: idx for idx, (_, _, cb) in enumerate(self._rows)}

        self._checked: set[str] = {
            tag for tag, _, cb in self._rows if cb.isChecked()
        }
        self._syncing = False

        self._paint_active = False
        self._paint_additive = False
        self._paint_base: set[str] = set()
        self._paint_visited: set[str] = set()
        self._paint_last_idx: int | None = None

        self._pending_idx: int | None = None
        self._pending_global: QPoint | None = None
        self._pending_modifiers = Qt.KeyboardModifier.NoModifier

        self._viewport.installEventFilter(self)
        self._viewport.setMouseTracking(True)
        for _tag, _frame, cb in self._rows:
            cb.installEventFilter(self)
            cb.setMouseTracking(True)
            cb.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            cb.stateChanged.connect(self._on_checkbox_state_changed)

    def set_all_checked(self, checked: bool) -> None:
        self._checked = set(self._tags) if checked else set()
        self._apply_visuals()

    def checked_tags(self) -> list[str]:
        return [tag for tag in self._tags if tag in self._checked]

    def _on_checkbox_state_changed(self, _state: int) -> None:
        if self._syncing:
            return
        sender = self.sender()
        if not isinstance(sender, QCheckBox):
            return
        for tag, cb in self._checkbox_by_tag.items():
            if cb is sender:
                if cb.isChecked():
                    self._checked.add(tag)
                else:
                    self._checked.discard(tag)
                self.selection_changed.emit()
                return

    def _vp_pos(self, event: QMouseEvent) -> QPoint:
        return self._viewport.mapFromGlobal(event.globalPosition().toPoint())

    @staticmethod
    def _hit_checkbox_indicator(cb: QCheckBox, global_pos: QPoint) -> bool:
        local = cb.mapFromGlobal(global_pos)
        return _checkbox_indicator_rect(cb).contains(local)

    def _index_at_vp(self, vp_pos: QPoint, *, global_pos: QPoint | None = None) -> int | None:
        gp = global_pos if global_pos is not None else self._viewport.mapToGlobal(vp_pos)
        child = self._viewport.childAt(vp_pos)
        if child is None:
            return None
        cur: QWidget | None = child
        while cur is not None and cur is not self._viewport:
            if isinstance(cur, QCheckBox) and cur in self._checkbox_to_index:
                if self._hit_checkbox_indicator(cur, gp):
                    return self._checkbox_to_index[cur]
                return None
            cur = cur.parentWidget()
        return None

    def _index_for_checkbox_event(self, cb: QCheckBox, event: QMouseEvent) -> int | None:
        if cb not in self._checkbox_to_index:
            return None
        if not self._hit_checkbox_indicator(cb, event.globalPosition().toPoint()):
            return None
        return self._checkbox_to_index[cb]

    def _tag_at_index(self, idx: int) -> str | None:
        if 0 <= idx < len(self._rows):
            return self._rows[idx][0]
        return None

    def _apply_visuals(self) -> None:
        self._syncing = True
        try:
            for tag, cb in self._checkbox_by_tag.items():
                cb.blockSignals(True)
                cb.setChecked(tag in self._checked)
                cb.blockSignals(False)
        finally:
            self._syncing = False
        self.selection_changed.emit()

    def _recompute_paint_selection(self) -> None:
        if self._paint_additive:
            self._checked = (self._paint_base | self._paint_visited) & set(self._tags)
        else:
            self._checked = set(self._paint_visited) & set(self._tags)
        self._apply_visuals()

    def _visit_index_range(self, i0: int, i1: int) -> None:
        lo, hi = (i0, i1) if i0 <= i1 else (i1, i0)
        lo = max(0, lo)
        hi = min(len(self._rows) - 1, hi)
        changed = False
        for i in range(lo, hi + 1):
            tag = self._tag_at_index(i)
            if tag and tag not in self._paint_visited:
                self._paint_visited.add(tag)
                changed = True
        if changed:
            self._recompute_paint_selection()

    def _begin_paint_at(self, idx: int, modifiers: Qt.KeyboardModifier) -> bool:
        if self._paint_active:
            return False
        if not self._tag_at_index(idx):
            return False
        self._paint_active = True
        self._paint_additive = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        self._paint_base = set(self._checked) if self._paint_additive else set()
        self._paint_visited = set()
        self._paint_last_idx = idx
        try:
            self._viewport.grabMouse()
        except Exception:
            pass
        self._visit_index_range(idx, idx)
        return True

    def _update_paint_hover(self, vp_pos: QPoint, *, global_pos: QPoint) -> None:
        if not self._paint_active:
            return
        cur = self._index_at_vp(vp_pos, global_pos=global_pos)
        if cur is None:
            return
        last = self._paint_last_idx
        self._paint_last_idx = cur
        if last is None:
            self._visit_index_range(cur, cur)
        else:
            self._visit_index_range(last, cur)

    def _end_paint(self) -> None:
        if not self._paint_active:
            return
        self._paint_active = False
        self._paint_additive = False
        self._paint_base.clear()
        self._paint_visited.clear()
        self._paint_last_idx = None
        try:
            self._viewport.releaseMouse()
        except Exception:
            pass

    def _clear_pending_click(self) -> None:
        self._pending_idx = None
        self._pending_global = None
        self._pending_modifiers = Qt.KeyboardModifier.NoModifier

    def _begin_pending_click(
        self, idx: int, global_pos: QPoint, modifiers: Qt.KeyboardModifier
    ) -> None:
        self._clear_pending_click()
        self._pending_idx = idx
        self._pending_global = global_pos
        self._pending_modifiers = modifiers
        try:
            self._viewport.grabMouse()
        except Exception:
            pass

    def _try_begin_paint_from_pending(self) -> bool:
        idx = self._pending_idx
        if idx is None:
            return False
        modifiers = self._pending_modifiers
        self._clear_pending_click()
        return self._begin_paint_at(idx, modifiers)

    def _toggle_index(self, idx: int) -> None:
        tag = self._tag_at_index(idx)
        if not tag:
            return
        if tag in self._checked:
            self._checked.discard(tag)
        else:
            self._checked.add(tag)
        self._apply_visuals()

    def _finish_click_without_drag(self) -> None:
        try:
            self._viewport.releaseMouse()
        except Exception:
            pass
        idx = self._pending_idx
        self._clear_pending_click()
        if idx is None or self._paint_active:
            return
        self._toggle_index(idx)

    def _mouse_active(self, watched: QWidget) -> bool:
        if watched is self._viewport:
            return self._paint_active or self._pending_idx is not None
        return isinstance(watched, QCheckBox) and watched in self._checkbox_to_index

    def eventFilter(self, watched: object, event: object) -> bool:  # type: ignore[override]
        if not isinstance(watched, QWidget) or not isinstance(event, QMouseEvent):
            return super().eventFilter(watched, event)

        if not self._mouse_active(watched):
            return super().eventFilter(watched, event)

        et = event.type()
        global_pos = event.globalPosition().toPoint()
        vp_pos = self._vp_pos(event)
        paint_mode = self._paint_active or self._pending_idx is not None

        if paint_mode and et == QEvent.Type.MouseMove:
            if self._paint_active:
                self._update_paint_hover(vp_pos, global_pos=global_pos)
                return True
            if (
                self._pending_idx is not None
                and self._pending_global is not None
                and (event.buttons() & Qt.MouseButton.LeftButton)
            ):
                delta = global_pos - self._pending_global
                if abs(delta.x()) + abs(delta.y()) >= _DRAG_THRESHOLD_PX:
                    self._try_begin_paint_from_pending()
                return True

        if et == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            if self._paint_active:
                self._end_paint()
                return True
            if self._pending_idx is not None:
                self._finish_click_without_drag()
                return True

        if et == QEvent.Type.MouseMove and (event.buttons() & Qt.MouseButton.LeftButton):
            if self._paint_active:
                self._update_paint_hover(vp_pos, global_pos=global_pos)
                return True
            if self._pending_idx is not None and self._pending_global is not None:
                delta = global_pos - self._pending_global
                if abs(delta.x()) + abs(delta.y()) >= _DRAG_THRESHOLD_PX:
                    self._try_begin_paint_from_pending()
                return True

        if (
            et == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
            and isinstance(watched, QCheckBox)
        ):
            idx = self._index_for_checkbox_event(watched, event)
            if idx is None:
                return True
            self._begin_pending_click(idx, global_pos, event.modifiers())
            return True

        return super().eventFilter(watched, event)


class ProfileTagsClearDialog(QDialog):
    def __init__(
        self,
        *,
        profile_count: int,
        tags: tuple[str, ...],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Очистка тегов Zaliver")
        self.setModal(True)
        self.setMinimumWidth(520)

        self._checks: dict[str, QCheckBox] = {}
        self._drag_select: _TagClearListDragSelect | None = None
        row_items: list[tuple[str, QFrame, QCheckBox]] = []

        root = QVBoxLayout(self)
        root.setSpacing(12)

        hint = QLabel(
            f"Снять выбранные служебные теги с {int(profile_count)} отмеченных профилей. "
            "Снимите галочку с тега, если его не нужно очищать. "
            "Кликайте и ведите только по квадратикам слева; удерживайте ЛКМ — "
            "отметятся все на пути (Ctrl — добавить к уже выбранным)."
        )
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        root.addWidget(hint)

        root.addWidget(QLabel("Теги для очистки:"))

        if not tags:
            empty = QLabel("У отмеченных профилей нет служебных тегов Zaliver.")
            empty.setWordWrap(True)
            empty.setObjectName("hint")
            root.addWidget(empty)
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            inner = QWidget()
            inner_layout = QVBoxLayout(inner)
            inner_layout.setContentsMargins(0, 0, 0, 0)
            inner_layout.setSpacing(6)
            for tag in tags:
                t = (tag or "").strip()
                if not t:
                    continue
                row = QFrame()
                row.setObjectName(_tag_chip_object_name(t))
                row_lay = QHBoxLayout(row)
                row_lay.setContentsMargins(10, 6, 10, 6)
                row_lay.setSpacing(10)
                cb = QCheckBox()
                cb.setObjectName("zaliverTagClearCheck")
                cb.setChecked(True)
                cb.setToolTip(t)
                lbl = QLabel(t)
                lbl.setObjectName("zaliverTagClearLabel")
                lbl.setWordWrap(True)
                _apply_tag_clear_label_style(lbl, t)
                self._checks[t] = cb
                row_items.append((t, row, cb))
                row_lay.addWidget(cb, 0, Qt.AlignmentFlag.AlignTop)
                row_lay.addWidget(lbl, 1)
                inner_layout.addWidget(row)
            inner_layout.addStretch(1)
            scroll.setWidget(inner)
            scroll.setMinimumHeight(min(360, 40 * max(1, len(self._checks)) + 16))
            root.addWidget(scroll, 1)

            self._drag_select = _TagClearListDragSelect(
                viewport=inner,
                rows=row_items,
                parent=self,
            )

        bulk_layout = QHBoxLayout()
        btn_all = QPushButton("Выбрать все")
        btn_all.setObjectName("secondary")
        btn_none = QPushButton("Снять все")
        btn_none.setObjectName("secondary")
        btn_all.clicked.connect(self._select_all)
        btn_none.clicked.connect(self._select_none)
        btn_all.setEnabled(bool(tags))
        btn_none.setEnabled(bool(tags))
        bulk_layout.addWidget(btn_all)
        bulk_layout.addWidget(btn_none)
        bulk_layout.addStretch(1)
        root.addLayout(bulk_layout)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_btn is not None:
            ok_btn.setEnabled(bool(tags))
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _select_all(self) -> None:
        if self._drag_select is not None:
            self._drag_select.set_all_checked(True)

    def _select_none(self) -> None:
        if self._drag_select is not None:
            self._drag_select.set_all_checked(False)

    def _on_accept(self) -> None:
        if not self.selected_tags():
            QMessageBox.warning(
                self,
                "Очистка тегов",
                "Выберите хотя бы один тег для очистки.",
            )
            return
        self.accept()

    def selected_tags(self) -> list[str]:
        if self._drag_select is None:
            return []
        return self._drag_select.checked_tags()
