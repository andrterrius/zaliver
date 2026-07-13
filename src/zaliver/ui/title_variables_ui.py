"""UI-хелперы для полей с переменными в названии."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QWidget,
)

from zaliver.ui.title_variables_dialog import TitleVariablesDialog
from zaliver.title_variables import MAX_YOUTUBE_TITLE_LENGTH, collect_title_template_warnings


@dataclass(slots=True)
class _FieldCursorState:
    plain_cursor: QTextCursor | None = None
    line_pos: int = 0
    line_sel_start: int = -1
    line_sel_end: int = -1


def _line_edit_target(field: QComboBox | QLineEdit) -> QLineEdit | None:
    if isinstance(field, QComboBox):
        le = field.lineEdit()
        return le if isinstance(le, QLineEdit) else None
    return field


def capture_field_cursor_state(
    field: QComboBox | QPlainTextEdit | QLineEdit,
) -> _FieldCursorState | None:
    if isinstance(field, QPlainTextEdit):
        return _FieldCursorState(plain_cursor=QTextCursor(field.textCursor()))
    line_edit = _line_edit_target(field)
    if line_edit is None:
        return None
    sel_start = line_edit.selectionStart()
    sel_end = line_edit.selectionEnd()
    if sel_start < 0 or sel_end < 0:
        sel_start = sel_end = -1
    return _FieldCursorState(
        line_pos=line_edit.cursorPosition(),
        line_sel_start=sel_start,
        line_sel_end=sel_end,
    )


def insert_text_at_field_cursor(
    field: QComboBox | QPlainTextEdit | QLineEdit,
    extra: str,
    *,
    cursor_state: _FieldCursorState | None = None,
) -> None:
    extra = extra or ""
    if not extra:
        return

    if isinstance(field, QPlainTextEdit):
        if cursor_state is not None and cursor_state.plain_cursor is not None:
            cursor = QTextCursor(cursor_state.plain_cursor)
        else:
            cursor = field.textCursor()
        if cursor.hasSelection():
            cursor.removeSelectedText()
        cursor.insertText(extra)
        field.setTextCursor(cursor)
        field.setFocus()
        return

    line_edit = _line_edit_target(field)
    if line_edit is None:
        return

    text = line_edit.text()
    if (
        cursor_state is not None
        and cursor_state.line_sel_start >= 0
        and cursor_state.line_sel_end > cursor_state.line_sel_start
    ):
        start = cursor_state.line_sel_start
        end = cursor_state.line_sel_end
    else:
        start = end = (
            cursor_state.line_pos
            if cursor_state is not None
            else line_edit.cursorPosition()
        )
    line_edit.setText(text[:start] + extra + text[end:])
    line_edit.setCursorPosition(start + len(extra))
    line_edit.setFocus()


def append_text_to_field(
    field: QComboBox | QPlainTextEdit | QLineEdit,
    extra: str,
) -> None:
    insert_text_at_field_cursor(field, extra)


def append_text_to_editable_combo(combo: QComboBox, extra: str) -> None:
    insert_text_at_field_cursor(combo, extra)


def make_variables_hint_button(
    *,
    parent: QWidget,
    field: QComboBox | QPlainTextEdit | QLineEdit | None = None,
    on_insert: Callable[[str], None] | None = None,
) -> QPushButton:
    btn = QPushButton("Подсказки")
    btn.setObjectName("secondary")
    btn.setToolTip("Переменные для названия и описания")

    def _open_hints() -> None:
        cursor_state = capture_field_cursor_state(field) if field is not None else None

        def _insert(token: str) -> None:
            if field is not None:
                insert_text_at_field_cursor(field, token, cursor_state=cursor_state)
            elif on_insert is not None:
                on_insert(token)

        dlg = TitleVariablesDialog(on_insert=_insert, parent=parent)
        dlg.exec()

    btn.clicked.connect(_open_hints)
    return btn


def attach_variables_hint_button(
    field_row: QWidget,
    *,
    parent: QWidget,
    field: QComboBox | QPlainTextEdit | QLineEdit,
) -> QPushButton:
    layout = field_row.layout()
    if not isinstance(layout, QHBoxLayout):
        raise TypeError("field_row must use QHBoxLayout")
    btn = make_variables_hint_button(parent=parent, field=field)
    layout.addWidget(btn, 0, Qt.AlignmentFlag.AlignTop)
    return btn


def title_field_with_variables_hint(
    field: QComboBox,
    *,
    parent: QWidget,
) -> tuple[QWidget, QPushButton]:
    row = QWidget(parent)
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(6)
    lay.addWidget(field, 1)
    btn_hints = make_variables_hint_button(parent=parent, field=field)
    lay.addWidget(btn_hints, 0, Qt.AlignmentFlag.AlignVCenter)
    return row, btn_hints


def show_youtube_title_warnings(
    parent: QWidget,
    templates: list[str] | tuple[str, ...],
    *,
    window_title: str = "Название видео",
) -> None:
    warnings = collect_title_template_warnings(templates)
    if not warnings:
        return
    body = (
        f"YouTube допускает до {MAX_YOUTUBE_TITLE_LENGTH} символов в названии видео.\n\n"
        + "\n".join(f"• {message}" for message in warnings)
        + "\n\nЕсли итоговое название всё же окажется длиннее, конец будет "
        "обрезан при заливе."
    )
    QMessageBox.warning(parent, window_title, body)
