"""Checkbox selection and drag-paint for antidetect-style profile QListWidget."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from PyQt6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QKeySequence, QMouseEvent, QShortcut
from PyQt6.QtWidgets import (
    QCheckBox,
    QListWidget,
    QListWidgetItem,
    QScrollBar,
    QWidget,
)

from zaliver.ui.antic_profile_row import _profile_id
from zaliver.ui.profile_list_helpers import (
    profile_has_account_data,
    profile_has_any_status_error,
    profile_has_yt_oldest_name,
    profile_is_upload_available,
)
from zaliver.ui.profile_list_row import ProfileListRow

if TYPE_CHECKING:
    from zaliver.db.upload_store import UploadStore


class ProfilesListInteraction(QObject):
    """Отметка профилей (checked) по клику/drag по строке; без Qt item selection."""

    selection_changed = pyqtSignal()

    def __init__(
        self,
        list_widget: QListWidget,
        upload_store: UploadStore,
        *,
        on_upload_pause_click: Callable[[str], None] | None = None,
        on_account_data_click: Callable[[str], None] | None = None,
        on_preview_click: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(list_widget)
        self.lw = list_widget
        self._upload_store = upload_store
        self._on_upload_pause_click = on_upload_pause_click
        self._on_account_data_click = on_account_data_click
        self._on_preview_click = on_preview_click

        self.checked_profile_ids: set[str] = set()
        self._profile_id_to_item: dict[str, QListWidgetItem] = {}
        self._profile_id_to_checkbox: dict[str, QCheckBox] = {}
        self._profile_id_to_row: dict[str, ProfileListRow] = {}
        self._profile_row_filter_widgets: set[QWidget] = set()
        self._syncing_selection_check = False

        self._lmb_select_active = False
        self._lmb_select_additive = False
        self._lmb_select_base: set[str] = set()
        self._lmb_select_visited: set[str] = set()
        self._lmb_select_last_row: int | None = None

        self._checkbox_row_pending: int | None = None
        self._checkbox_press_global: QPoint | None = None
        self._checkbox_press_modifiers = Qt.KeyboardModifier.NoModifier
        self._account_data_armed_row: ProfileListRow | None = None
        self._copy_id_armed_row: ProfileListRow | None = None
        self._preview_armed_row: ProfileListRow | None = None

        self.lw.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.lw.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.lw.installEventFilter(self)
        self.lw.viewport().installEventFilter(self)

        select_all_sc = QShortcut(QKeySequence.StandardKey.SelectAll, self.lw)
        select_all_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        select_all_sc.activated.connect(self.select_all_visible)

    def checked_count(self) -> int:
        return len(self.checked_profile_ids)

    def batch_profile_ids(self) -> list[str]:
        """Отмеченные профили сверху вниз (только checked, без Qt-selection)."""
        if not self.checked_profile_ids:
            return []
        return [
            pid
            for pid in self._profile_ids_in_list_order()
            if pid in self.checked_profile_ids
        ]

    def _profile_ids_in_list_order(self) -> list[str]:
        out: list[str] = []
        for i in range(self.lw.count()):
            it = self.lw.item(i)
            if it is None:
                continue
            pid = str(it.data(Qt.ItemDataRole.UserRole) or "").strip()
            if pid:
                out.append(pid)
        return out

    def clear_checked_selection(self) -> None:
        if self._lmb_select_active:
            self._lmb_select_active = False
            self._lmb_select_additive = False
            self._lmb_select_base.clear()
            self._lmb_select_visited.clear()
            self._lmb_select_last_row = None
            try:
                self.lw.viewport().releaseMouse()
            except Exception:
                pass
        if self._checkbox_row_pending is not None:
            try:
                self.lw.viewport().releaseMouse()
            except Exception:
                pass
            self._clear_checkbox_click_pending()
        self.checked_profile_ids.clear()
        self._apply_checkbox_visuals()

    def select_all_visible(self) -> None:
        """Отметить все профили, видимые в текущем списке (Ctrl+A / ⌘A)."""
        self.checked_profile_ids = set(self._profile_id_to_item.keys())
        self._apply_checkbox_visuals()

    def focus_profile(
        self,
        profile_id: str,
        *,
        check: bool = True,
        attention: bool = True,
    ) -> bool:
        """Прокрутить к профилю, опционально отметить и подсветить."""
        pid = (profile_id or "").strip()
        if not pid:
            return False
        # Снять прошлую attention-подсветку.
        for row in list(self._profile_id_to_row.values()):
            try:
                if row.objectName() == "profileRowAttention":
                    checked = False
                    try:
                        checked = bool(row.checkbox.isChecked())
                    except Exception:
                        pass
                    row.setObjectName("profileRowChecked" if checked else "profileRow")
                    row.style().unpolish(row)
                    row.style().polish(row)
            except Exception:
                continue

        item = self._profile_id_to_item.get(pid)
        row = self._profile_id_to_row.get(pid)
        if item is None and row is None:
            return False
        if check:
            self.checked_profile_ids.add(pid)
            self._apply_checkbox_visuals()
        if item is not None:
            try:
                self.lw.scrollToItem(
                    item, QListWidget.ScrollHint.PositionAtCenter
                )
            except Exception:
                try:
                    self.lw.scrollToItem(item)
                except Exception:
                    pass
        if attention and row is not None:
            try:
                row.setObjectName("profileRowAttention")
                row.style().unpolish(row)
                row.style().polish(row)
            except Exception:
                pass
        if check:
            self.selection_changed.emit()
        return True

    def select_checked_by_filter(self, mode: str, profiles_by_id: dict[str, dict[str, object]], last_upload_map: dict[str, str]) -> None:
        """mode: all | available | no_errors | with_errors | no_account_data | no_oldest_channel"""
        existing = set(profiles_by_id.keys())
        if mode == "all":
            self.checked_profile_ids = set(existing)
        elif mode == "available":
            picked: set[str] = set()
            for pid in existing:
                if profile_is_upload_available(last_upload_map.get(pid)):
                    picked.add(pid)
            self.checked_profile_ids = picked
        elif mode == "no_errors":
            picked = set()
            for pid, prof in profiles_by_id.items():
                if not profile_has_any_status_error(prof, upload_store=self._upload_store):
                    picked.add(pid)
            self.checked_profile_ids = picked
        elif mode == "with_errors":
            picked = set()
            for pid, prof in profiles_by_id.items():
                if profile_has_any_status_error(prof, upload_store=self._upload_store):
                    picked.add(pid)
            self.checked_profile_ids = picked
        elif mode == "no_account_data":
            picked = set()
            for pid, prof in profiles_by_id.items():
                if not profile_has_account_data(prof):
                    picked.add(pid)
            self.checked_profile_ids = picked
        elif mode == "no_oldest_channel":
            picked = set()
            for pid, prof in profiles_by_id.items():
                if not profile_has_yt_oldest_name(prof):
                    picked.add(pid)
            self.checked_profile_ids = picked
        else:
            return
        self._apply_checkbox_visuals()

    def populate(
        self,
        profiles: list[dict[str, object]],
        last_upload_map: dict[str, str],
        *,
        preserve_checked: set[str] | None = None,
        prune_checked_to_existing: bool = True,
        show_account_data_button: bool = False,
        show_preview_button: bool = False,
    ) -> None:
        list_scroll = self.lw.verticalScrollBar().value()
        existing_ids = {_profile_id(p) for p in profiles}
        existing_ids.discard("")

        preserve = set(preserve_checked or self.checked_profile_ids)
        if prune_checked_to_existing:
            preserve.intersection_update(existing_ids)
        self.checked_profile_ids = preserve

        self._reset_pointer_interaction_state()

        self.lw.blockSignals(True)
        self.lw.clear()
        self._profile_id_to_item.clear()
        self._profile_id_to_checkbox.clear()
        self._profile_id_to_row.clear()
        self._profile_row_filter_widgets.clear()

        for p in profiles:
            pid = _profile_id(p)
            if not pid:
                continue
            it = QListWidgetItem()
            it.setData(Qt.ItemDataRole.UserRole, pid)
            it.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.lw.addItem(it)
            self._profile_id_to_item[pid] = it

            row = ProfileListRow(
                p,
                last_uploaded_at=last_upload_map.get(pid),
                on_upload_pause_click=(
                    (lambda pid=pid: self._on_upload_pause_click(pid))
                    if self._on_upload_pause_click
                    else None
                ),
                show_account_data_button=show_account_data_button,
                on_account_data_click=(
                    (lambda pid=pid: self._emit_account_data_click(pid))
                    if show_account_data_button and self._on_account_data_click
                    else None
                ),
                show_preview_button=show_preview_button,
                on_preview_click=(
                    (lambda pid=pid: self._emit_preview_click(pid))
                    if show_preview_button and self._on_preview_click
                    else None
                ),
            )
            row.checkbox.setChecked(pid in self.checked_profile_ids)
            row.checkbox.stateChanged.connect(
                lambda _st, profile_id=pid: self._on_checkbox_state_changed(profile_id)
            )
            row.checkbox.installEventFilter(self)
            self._profile_id_to_checkbox[pid] = row.checkbox
            self._profile_id_to_row[pid] = row
            self._register_row_mouse_targets(row)

            self.lw.setItemWidget(it, row)
            row.adjustSize()
            hint = row.minimumSizeHint()
            it.setSizeHint(QSize(hint.width(), hint.height() + 6))

        self.lw.blockSignals(False)
        self._apply_checkbox_visuals()
        self._sync_profile_row_widths()
        self.lw.verticalScrollBar().setValue(list_scroll)
        self.selection_changed.emit()

    def update_upload_cooldown_for_profile(self, profile_id: str, last_uploaded_iso: str | None) -> None:
        row = self._profile_id_to_row.get(profile_id)
        if row is not None:
            row.set_last_upload_cooldown(last_uploaded_iso)

    def _register_row_mouse_targets(self, row: ProfileListRow) -> None:
        """Клики/drag по всей строке — через корень row; пауза и чекбокс отдельно."""
        row.setCursor(Qt.CursorShape.PointingHandCursor)
        row.installEventFilter(self)
        self._profile_row_filter_widgets.add(row)
        row.checkbox.installEventFilter(self)
        row.upload_label.installEventFilter(self)
        if row.account_data_btn is not None:
            row.account_data_btn.installEventFilter(self)
        if row.preview_btn is not None:
            row.preview_btn.installEventFilter(self)
        if row.copy_id_btn is not None:
            row.copy_id_btn.installEventFilter(self)
        for ch in row.findChildren(QWidget):
            if ch is row or ch is row.checkbox or ch is row.upload_label:
                continue
            if row.account_data_btn is not None and (
                ch is row.account_data_btn or row.account_data_btn.isAncestorOf(ch)
            ):
                continue
            if row.preview_btn is not None and (
                ch is row.preview_btn or row.preview_btn.isAncestorOf(ch)
            ):
                continue
            if row.copy_id_btn is not None and (
                ch is row.copy_id_btn or row.copy_id_btn.isAncestorOf(ch)
            ):
                continue
            ch.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def _sync_profile_row_widths(self) -> None:
        w = max(0, self.lw.viewport().width() - 8)
        for row in self._profile_id_to_row.values():
            row.setMinimumWidth(w)

    @staticmethod
    def _repolish(w: QWidget) -> None:
        st = w.style()
        st.unpolish(w)
        st.polish(w)
        w.update()

    def _apply_checkbox_visuals(self) -> None:
        self._syncing_selection_check = True
        try:
            for pid, cb in self._profile_id_to_checkbox.items():
                cb.blockSignals(True)
                cb.setChecked(pid in self.checked_profile_ids)
                cb.blockSignals(False)
        finally:
            self._syncing_selection_check = False
        self._apply_checked_row_visuals()
        self.selection_changed.emit()

    def _apply_checked_row_visuals(self) -> None:
        for pid, row in self._profile_id_to_row.items():
            on = pid in self.checked_profile_ids
            name = "profileRowChecked" if on else "profileRow"
            if row.objectName() != name:
                row.setObjectName(name)
                self._repolish(row)

    def _on_checkbox_state_changed(self, profile_id: str) -> None:
        if self._syncing_selection_check:
            return
        cb = self._profile_id_to_checkbox.get(profile_id)
        if cb is None:
            return
        if cb.isChecked():
            self.checked_profile_ids.add(profile_id)
        else:
            self.checked_profile_ids.discard(profile_id)
        self._apply_checked_row_visuals()
        self.selection_changed.emit()

    def _on_upload_pause_click(self, profile_id: str) -> None:
        if self._on_upload_pause_click:
            self._on_upload_pause_click(profile_id)

    def _mouse_filter_active(self, watched: object) -> bool:
        if not isinstance(watched, QWidget):
            return False
        if watched is self.lw.viewport() or watched is self.lw:
            return True
        if isinstance(watched, QCheckBox) and watched in self._profile_id_to_checkbox.values():
            return True
        if watched in self._profile_row_filter_widgets:
            return True
        for row in self._profile_id_to_row.values():
            if watched is row or watched is row.upload_label or row.isAncestorOf(watched):
                return True
        return False

    @staticmethod
    def _vp_pos_from_event(list_widget: QListWidget, event: QMouseEvent) -> QPoint:
        return list_widget.viewport().mapFromGlobal(event.globalPosition().toPoint())

    def _row_at_vp(self, pos: QPoint) -> int | None:
        idx = self.lw.indexAt(pos)
        if not idx.isValid():
            return None
        return int(idx.row())

    def _pid_at_row(self, row: int) -> str | None:
        if row < 0 or row >= self.lw.count():
            return None
        it = self.lw.item(row)
        if it is None:
            return None
        pid = str(it.data(Qt.ItemDataRole.UserRole) or "").strip()
        return pid or None

    def _profile_id_at_vp(self, pos: QPoint) -> str | None:
        r = self._row_at_vp(pos)
        return self._pid_at_row(r) if r is not None else None

    def _row_for_checkbox(self, cb: QCheckBox) -> int | None:
        for pid, box in self._profile_id_to_checkbox.items():
            if box is cb:
                it = self._profile_id_to_item.get(pid)
                if it is not None:
                    return self.lw.row(it)
        return None

    def _lmb_select_recompute(self) -> None:
        existing = set(self._profile_id_to_item.keys())
        if self._lmb_select_additive:
            self.checked_profile_ids = (self._lmb_select_base | self._lmb_select_visited) & existing
        else:
            self.checked_profile_ids = set(self._lmb_select_visited) & existing
        self._apply_checkbox_visuals()

    def _lmb_select_visit_row_range(self, r0: int, r1: int) -> None:
        lo, hi = (r0, r1) if r0 <= r1 else (r1, r0)
        n = self.lw.count()
        lo = max(0, lo)
        hi = min(n - 1, hi)
        changed = False
        for r in range(lo, hi + 1):
            pid = self._pid_at_row(r)
            if pid and pid not in self._lmb_select_visited:
                self._lmb_select_visited.add(pid)
                changed = True
        if changed:
            self._lmb_select_recompute()

    def _lmb_select_begin_at_row(self, row: int, modifiers: Qt.KeyboardModifier) -> bool:
        if self._lmb_select_active:
            return False
        pid = self._pid_at_row(row)
        if not pid:
            return False
        self._lmb_select_active = True
        self._lmb_select_additive = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        self._lmb_select_base = (
            set(self.checked_profile_ids) if self._lmb_select_additive else set()
        )
        self._lmb_select_visited = set()
        self._lmb_select_last_row = row
        try:
            self.lw.viewport().grabMouse()
        except Exception:
            pass
        self._lmb_select_visit_row_range(row, row)
        return True

    def _lmb_select_update_hover(self, vp_pos: QPoint) -> None:
        if not self._lmb_select_active:
            return
        cur = self._row_at_vp(vp_pos)
        if cur is None:
            return
        last = self._lmb_select_last_row
        self._lmb_select_last_row = cur
        if last is None:
            self._lmb_select_visit_row_range(cur, cur)
        else:
            self._lmb_select_visit_row_range(last, cur)

    def _lmb_select_end(self) -> None:
        if not self._lmb_select_active:
            return
        self._lmb_select_active = False
        self._lmb_select_additive = False
        self._lmb_select_base.clear()
        self._lmb_select_visited.clear()
        self._lmb_select_last_row = None
        try:
            self.lw.viewport().releaseMouse()
        except Exception:
            pass

    def _clear_checkbox_click_pending(self) -> None:
        self._checkbox_row_pending = None
        self._checkbox_press_global = None
        self._checkbox_press_modifiers = Qt.KeyboardModifier.NoModifier

    def _begin_checkbox_click_pending(
        self, row: int, global_pos: QPoint, modifiers: Qt.KeyboardModifier
    ) -> None:
        self._clear_checkbox_click_pending()
        self._checkbox_row_pending = row
        self._checkbox_press_global = global_pos
        self._checkbox_press_modifiers = modifiers
        try:
            self.lw.viewport().grabMouse()
        except Exception:
            pass

    def _try_begin_checkbox_paint_from_pending(self, modifiers: Qt.KeyboardModifier) -> bool:
        row = self._checkbox_row_pending
        if row is None:
            return False
        self._clear_checkbox_click_pending()
        return self._lmb_select_begin_at_row(row, modifiers)

    def _toggle_checkbox_row(self, row: int) -> None:
        pid = self._pid_at_row(row)
        if not pid:
            return
        if pid in self.checked_profile_ids:
            self.checked_profile_ids.discard(pid)
        else:
            self.checked_profile_ids.add(pid)
        self._apply_checkbox_visuals()

    def _finish_checkbox_click_without_drag(self) -> None:
        try:
            self.lw.viewport().releaseMouse()
        except Exception:
            pass
        row = self._checkbox_row_pending
        self._clear_checkbox_click_pending()
        if row is None or self._lmb_select_active:
            return
        self._toggle_checkbox_row(row)

    def _emit_account_data_click(self, profile_id: str) -> None:
        cb = self._on_account_data_click
        if cb is None:
            return
        pid = (profile_id or "").strip()
        if not pid:
            return
        QTimer.singleShot(0, lambda p=pid: cb(p))

    def _emit_preview_click(self, profile_id: str) -> None:
        cb = self._on_preview_click
        if cb is None:
            return
        pid = (profile_id or "").strip()
        if not pid:
            return
        QTimer.singleShot(0, lambda p=pid: cb(p))

    def _profile_row_for_account_button_hit(
        self, watched: QWidget, vp_pos: QPoint
    ) -> ProfileListRow | None:
        widgets_to_check: list[QWidget] = [watched]
        child = self.lw.viewport().childAt(vp_pos)
        if child is not None and child is not watched:
            widgets_to_check.append(child)

        for w in widgets_to_check:
            cur: QWidget | None = w
            while cur is not None and cur is not self.lw.viewport():
                for row in self._profile_id_to_row.values():
                    btn = row.account_data_btn
                    if btn is not None and (cur is btn or btn.isAncestorOf(cur)):
                        return row
                cur = cur.parentWidget()

        for row in self._profile_id_to_row.values():
            btn = row.account_data_btn
            if btn is None or not btn.isVisible():
                continue
            top_left = btn.mapTo(self.lw.viewport(), QPoint(0, 0))
            if QRect(top_left, btn.size()).contains(vp_pos):
                return row
        return None

    def _invoke_account_data_click(self, row: ProfileListRow) -> None:
        cb = row._account_data_cb
        if cb is None:
            return
        cb()

    def _account_data_button_at_vp(self, vp_pos: QPoint):
        for row in self._profile_id_to_row.values():
            btn = row.account_data_btn
            if btn is None or not btn.isVisible():
                continue
            top_left = btn.mapTo(self.lw.viewport(), QPoint(0, 0))
            if QRect(top_left, btn.size()).contains(vp_pos):
                return btn
        return None

    def _invoke_preview_click(self, row: ProfileListRow) -> None:
        cb = row._preview_cb
        if cb is not None:
            cb()

    def _profile_row_for_preview_button_hit(
        self, watched: QWidget, vp_pos: QPoint, *, global_pos: QPoint | None = None
    ) -> ProfileListRow | None:
        gp = global_pos if global_pos is not None else self.lw.viewport().mapToGlobal(vp_pos)
        for row in self._profile_id_to_row.values():
            btn = row.preview_btn
            if btn is None or not btn.isVisible() or not btn.isEnabled():
                continue
            if btn.rect().contains(btn.mapFromGlobal(gp)):
                return row

        widgets_to_check: list[QWidget] = [watched]
        child = self.lw.viewport().childAt(vp_pos)
        if child is not None and child is not watched:
            widgets_to_check.append(child)

        for w in widgets_to_check:
            cur: QWidget | None = w
            while cur is not None and cur is not self.lw.viewport():
                for row in self._profile_id_to_row.values():
                    btn = row.preview_btn
                    if btn is not None and (cur is btn or btn.isAncestorOf(cur)):
                        return row
                cur = cur.parentWidget()
        return None

    def _is_preview_button(self, watched: QWidget) -> bool:
        for row in self._profile_id_to_row.values():
            btn = row.preview_btn
            if btn is not None and (watched is btn or btn.isAncestorOf(watched)):
                return True
        return False

    def _is_account_data_button(self, watched: QWidget) -> bool:
        for row in self._profile_id_to_row.values():
            btn = row.account_data_btn
            if btn is not None and (watched is btn or btn.isAncestorOf(watched)):
                return True
        return False

    def _profile_row_for_copy_id_button_hit(
        self, watched: QWidget, vp_pos: QPoint
    ) -> ProfileListRow | None:
        widgets_to_check: list[QWidget] = [watched]
        child = self.lw.viewport().childAt(vp_pos)
        if child is not None and child is not watched:
            widgets_to_check.append(child)

        for w in widgets_to_check:
            cur: QWidget | None = w
            while cur is not None and cur is not self.lw.viewport():
                for row in self._profile_id_to_row.values():
                    btn = row.copy_id_btn
                    if btn is not None and (cur is btn or btn.isAncestorOf(cur)):
                        return row
                cur = cur.parentWidget()

        for row in self._profile_id_to_row.values():
            btn = row.copy_id_btn
            if btn is None or not btn.isVisible():
                continue
            top_left = btn.mapTo(self.lw.viewport(), QPoint(0, 0))
            if QRect(top_left, btn.size()).contains(vp_pos):
                return row
        return None

    def _invoke_copy_id_click(self, row: ProfileListRow) -> None:
        btn = row.copy_id_btn
        if btn is None:
            return
        btn.click()

    def _reset_pointer_interaction_state(self) -> None:
        self._account_data_armed_row = None
        self._copy_id_armed_row = None
        self._preview_armed_row = None
        self._lmb_select_end()
        self._cancel_checkbox_click_pending()

    def _cancel_checkbox_click_pending(self) -> None:
        try:
            self.lw.viewport().releaseMouse()
        except Exception:
            pass
        self._clear_checkbox_click_pending()

    def _is_upload_pause_click_widget(self, watched: QWidget, row_index: int) -> bool:
        pid = self._pid_at_row(row_index)
        if not pid:
            return False
        row_w = self._profile_id_to_row.get(pid)
        if row_w is None:
            return False
        if watched is not row_w.upload_label:
            return False
        return row_w._upload_cooldown_kind == "wait" and row_w._upload_pause_cb is not None

    def _row_index_for_event(self, watched: QWidget, event: QMouseEvent) -> int | None:
        vp_pos = self._vp_pos_from_event(self.lw, event)
        row = self._row_at_vp(vp_pos)
        if row is not None:
            return row
        if isinstance(watched, QCheckBox) and watched in self._profile_id_to_checkbox.values():
            return self._row_for_checkbox(watched)
        return None

    def eventFilter(self, watched: object, event: object) -> bool:  # type: ignore[override]
        try:
            if isinstance(watched, QScrollBar):
                return super().eventFilter(watched, event)
            if not isinstance(watched, QWidget):
                return super().eventFilter(watched, event)
            if event.type() == QEvent.Type.Resize and watched is self.lw.viewport():
                self._sync_profile_row_widths()
                return super().eventFilter(watched, event)
            if not isinstance(event, QMouseEvent):
                return super().eventFilter(watched, event)

            vp_pos = self._vp_pos_from_event(self.lw, event)
            global_pos = event.globalPosition().toPoint()
            et = event.type()

            if (
                et == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton
                and self._preview_armed_row is not None
            ):
                row = self._preview_armed_row
                self._preview_armed_row = None
                self._cancel_checkbox_click_pending()
                self._lmb_select_end()
                self._invoke_preview_click(row)
                return True

            if (
                et == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton
            ):
                preview_row = self._profile_row_for_preview_button_hit(
                    watched, vp_pos, global_pos=global_pos
                )
                if preview_row is not None:
                    self._cancel_checkbox_click_pending()
                    self._lmb_select_end()
                    self._invoke_preview_click(preview_row)
                    return True

            if (
                et == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton
                and self._copy_id_armed_row is not None
            ):
                row = self._copy_id_armed_row
                self._copy_id_armed_row = None
                self._cancel_checkbox_click_pending()
                self._lmb_select_end()
                self._invoke_copy_id_click(row)
                return True

            if (
                et == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton
                and self._account_data_armed_row is not None
            ):
                row = self._account_data_armed_row
                self._account_data_armed_row = None
                self._cancel_checkbox_click_pending()
                self._lmb_select_end()
                self._invoke_account_data_click(row)
                return True

            if et == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                copy_row = self._profile_row_for_copy_id_button_hit(watched, vp_pos)
                if copy_row is not None:
                    self._copy_id_armed_row = copy_row
                    self._account_data_armed_row = None
                    self._preview_armed_row = None
                    self._cancel_checkbox_click_pending()
                    return True
                self._copy_id_armed_row = None

                account_row = self._profile_row_for_account_button_hit(watched, vp_pos)
                if account_row is not None:
                    self._account_data_armed_row = account_row
                    self._preview_armed_row = None
                    self._cancel_checkbox_click_pending()
                    return True
                self._account_data_armed_row = None

                preview_row = self._profile_row_for_preview_button_hit(
                    watched, vp_pos, global_pos=global_pos
                )
                if preview_row is not None:
                    self._preview_armed_row = preview_row
                    self._account_data_armed_row = None
                    self._copy_id_armed_row = None
                    self._cancel_checkbox_click_pending()
                    return True
                self._preview_armed_row = None

            paint_mode = self._lmb_select_active or self._checkbox_row_pending is not None

            if paint_mode and et == QEvent.Type.MouseMove:
                if self._lmb_select_active:
                    self._lmb_select_update_hover(vp_pos)
                    return True
                if (
                    self._checkbox_row_pending is not None
                    and self._checkbox_press_global is not None
                    and (event.buttons() & Qt.MouseButton.LeftButton)
                ):
                    dg = event.globalPosition().toPoint() - self._checkbox_press_global
                    if abs(dg.x()) + abs(dg.y()) >= 5:
                        self._try_begin_checkbox_paint_from_pending(self._checkbox_press_modifiers)
                    return True

            if not self._mouse_filter_active(watched):
                return super().eventFilter(watched, event)

            if et == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                for row_w in self._profile_id_to_row.values():
                    if row_w.try_handle_upload_pause_click(watched):
                        return True
                if self._lmb_select_active:
                    self._lmb_select_end()
                    return True
                if self._checkbox_row_pending is not None:
                    self._finish_checkbox_click_without_drag()
                    return True
                return False

            if et == QEvent.Type.MouseMove and (event.buttons() & Qt.MouseButton.LeftButton):
                if self._lmb_select_active:
                    self._lmb_select_update_hover(vp_pos)
                    return True
                if self._checkbox_row_pending is not None and self._checkbox_press_global is not None:
                    dg = event.globalPosition().toPoint() - self._checkbox_press_global
                    if abs(dg.x()) + abs(dg.y()) >= 5:
                        self._try_begin_checkbox_paint_from_pending(self._checkbox_press_modifiers)
                    return True
                return False

            if et == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                if self._is_preview_button(watched) or self._is_account_data_button(watched):
                    return False
                if self._profile_row_for_preview_button_hit(
                    watched, vp_pos, global_pos=global_pos
                ):
                    return False
                row = self._row_index_for_event(watched, event)
                if row is None:
                    return False
                if self._is_upload_pause_click_widget(watched, row):
                    return False
                self._begin_checkbox_click_pending(
                    row, event.globalPosition().toPoint(), event.modifiers()
                )
                return True
        except Exception:
            pass
        return super().eventFilter(watched, event)
