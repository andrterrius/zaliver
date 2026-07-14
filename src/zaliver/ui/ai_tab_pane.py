"""Вкладка «ИИ» — встроенные и пользовательские промпты."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

_COLS = 3
_PROMPT_H = 250
_CUSTOM_ORDER_KEY = "ai/prompt_order"
_EXPORT_VERSION = 1
_EXPORT_FILTER = "Промпты Zaliver (*.json);;Все файлы (*.*)"

# Обязательные промпты программы: id, название, текст по умолчанию.
# Сюда добавлять новые — без удаления в UI. Тексты можно править; сброс = дефолт из кода.
BUILTIN_PROMPTS: tuple[tuple[str, str, str], ...] = (
    ("builtin_video_title", "Название видео", ""),
    ("builtin_video_description", "Описание видео", ""),
    ("builtin_channel_name", "Название канала", ""),
    ("builtin_channel_description", "Описание канала", ""),
    ("builtin_link_title", "Название ссылки", ""),
    ("builtin_youtube_comments", "Комментарии YouTube", ""),
)

# Старые id → текущий встроенный id (для импорта)
_BUILTIN_ID_ALIASES: dict[str, str] = {
    "agent": "builtin_video_description",
    "builtin_agent": "builtin_video_description",
}


@dataclass
class _PromptData:
    id: str
    title: str
    text: str
    builtin: bool = False


class AiTabPane(QWidget):
    """Две сетки: встроенные (неудаляемые) и свои (создание / удаление)."""

    def __init__(self, parent: QWidget | None = None, *, settings: QSettings) -> None:
        super().__init__(parent)
        self._settings = settings
        self._builtin_items: list[_PromptData] = []
        self._custom_items: list[_PromptData] = []
        self._cells: dict[str, QWidget] = {}
        self._title_edits: dict[str, QLineEdit] = {}
        self._text_edits: dict[str, QPlainTextEdit] = {}

        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(12, 12, 12, 12)

        title = QLabel("ИИ")
        title.setObjectName("title")
        hint = QLabel(
            "Встроенные промпты заданы в программе и не удаляются. "
            "Ниже можно добавлять свои. "
            "Параметры подключения — в «Настройки» → «ИИ»."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)

        header = QHBoxLayout()
        header.addWidget(title)
        header.addStretch()
        self._btn_export = QPushButton("Экспорт")
        self._btn_export.setObjectName("secondary")
        self._btn_export.setAutoDefault(False)
        self._btn_export.setDefault(False)
        self._btn_export.setToolTip(
            "Сохранить все промпты (встроенные и свои) в JSON-файл"
        )
        self._btn_export.clicked.connect(self._export_prompts)
        self._btn_import = QPushButton("Импорт")
        self._btn_import.setObjectName("secondary")
        self._btn_import.setAutoDefault(False)
        self._btn_import.setDefault(False)
        self._btn_import.setToolTip(
            "Загрузить промпты из JSON: свои заменятся, встроенные перезапишутся"
        )
        self._btn_import.clicked.connect(self._import_prompts)
        self._btn_add = QPushButton("Добавить промпт")
        self._btn_add.setObjectName("secondary")
        self._btn_add.setAutoDefault(False)
        self._btn_add.setDefault(False)
        self._btn_add.clicked.connect(self._add_prompt)
        header.addWidget(self._btn_export)
        header.addWidget(self._btn_import)
        header.addWidget(self._btn_add)
        root.addLayout(header)
        root.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._body = QWidget()
        self._body_l = QVBoxLayout(self._body)
        self._body_l.setSpacing(14)
        self._body_l.setContentsMargins(0, 0, 0, 0)

        self._builtin_section = QLabel("Встроенные")
        self._builtin_section.setObjectName("hint")
        self._builtin_grid_host = QWidget()
        self._builtin_grid = QGridLayout(self._builtin_grid_host)
        self._builtin_grid.setSpacing(10)
        self._builtin_grid.setContentsMargins(0, 0, 0, 0)
        for col in range(_COLS):
            self._builtin_grid.setColumnStretch(col, 1)

        self._custom_section = QLabel("Свои промпты")
        self._custom_section.setObjectName("hint")
        self._custom_grid_host = QWidget()
        self._custom_grid = QGridLayout(self._custom_grid_host)
        self._custom_grid.setSpacing(10)
        self._custom_grid.setContentsMargins(0, 0, 0, 0)
        for col in range(_COLS):
            self._custom_grid.setColumnStretch(col, 1)

        self._body_l.addWidget(self._builtin_section)
        self._body_l.addWidget(self._builtin_grid_host)
        self._body_l.addWidget(self._custom_section)
        self._body_l.addWidget(self._custom_grid_host)
        self._body_l.addStretch()
        scroll.setWidget(self._body)
        root.addWidget(scroll, 1)

        self._builtin_items = self._load_builtin_items()
        self._custom_items = self._load_custom_items()
        self._rebuild_grids()
        self._persist_all()

    def prompt_text(self, slot_id: str) -> str:
        """Точный текст промпта со вкладки (без изменений)."""
        edit = self._text_edits.get(slot_id)
        if edit is not None:
            return edit.toPlainText() or ""
        for item in (*self._builtin_items, *self._custom_items):
            if item.id == slot_id:
                return item.text or ""
        return str(
            self._settings.value(f"ai/prompts/{slot_id}/text", "", type=str) or ""
        )

    def prompts(self, *, include_builtin: bool = True, include_custom: bool = True) -> list[tuple[str, str, str]]:
        """Список (id, title, text)."""
        out: list[tuple[str, str, str]] = []
        items: list[_PromptData] = []
        if include_builtin:
            items.extend(self._builtin_items)
        if include_custom:
            items.extend(self._custom_items)
        for item in items:
            title_edit = self._title_edits.get(item.id)
            text_edit = self._text_edits.get(item.id)
            if item.builtin:
                title = item.title
            else:
                title = (
                    (title_edit.text() if title_edit is not None else item.title)
                    or ""
                ).strip()
            text = (
                text_edit.toPlainText()
                if text_edit is not None
                else (item.text or "")
            )
            out.append((item.id, title, text))
        return out

    def builtin_prompt_ids(self) -> frozenset[str]:
        return frozenset(pid for pid, _, _ in BUILTIN_PROMPTS)

    def _load_builtin_items(self) -> list[_PromptData]:
        items: list[_PromptData] = []
        for pid, title, default_text in BUILTIN_PROMPTS:
            key = f"ai/prompts/{pid}/text"
            if self._settings.contains(key):
                text = str(self._settings.value(key, "", type=str) or "")
            else:
                text = default_text
            items.append(
                _PromptData(id=pid, title=title, text=text, builtin=True)
            )
        return items

    def _load_custom_items(self) -> list[_PromptData]:
        builtin_ids = self.builtin_prompt_ids()
        # Старые id, которые стали встроенными — не показывать в «своих»
        migrated_away = {"agent", "builtin_agent", *builtin_ids}

        raw_order = self._settings.value(_CUSTOM_ORDER_KEY, "", type=str) or ""
        ids: list[str] = []
        if raw_order.strip():
            try:
                parsed = json.loads(raw_order)
                if isinstance(parsed, list):
                    ids = [str(x) for x in parsed if str(x).strip()]
            except Exception:
                ids = [p.strip() for p in raw_order.split(",") if p.strip()]

        items: list[_PromptData] = []
        for pid in ids:
            if pid in migrated_away:
                continue
            title = str(
                self._settings.value(f"ai/prompts/{pid}/title", "", type=str) or ""
            ).strip()
            text = str(
                self._settings.value(f"ai/prompts/{pid}/text", "", type=str) or ""
            )
            if not title and self._settings.contains(f"ai/prompts/{pid}"):
                text = str(self._settings.value(f"ai/prompts/{pid}", "", type=str) or "")
                title = "Промпт"
            if not title:
                title = "Промпт"
            items.append(_PromptData(id=pid, title=title, text=text, builtin=False))
        return items

    def _clear_grid(self, grid: QGridLayout) -> None:
        while grid.count():
            item = grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _rebuild_grids(self) -> None:
        self._clear_grid(self._builtin_grid)
        self._clear_grid(self._custom_grid)
        self._cells.clear()
        self._title_edits.clear()
        self._text_edits.clear()

        for i, data in enumerate(self._builtin_items):
            cell = self._make_cell(data)
            self._cells[data.id] = cell
            self._builtin_grid.addWidget(cell, i // _COLS, i % _COLS)

        for i, data in enumerate(self._custom_items):
            cell = self._make_cell(data)
            self._cells[data.id] = cell
            self._custom_grid.addWidget(cell, i // _COLS, i % _COLS)

        has_custom = bool(self._custom_items)
        self._custom_section.setVisible(True)
        self._custom_grid_host.setVisible(True)
        if not has_custom:
            self._custom_section.setText("Свои промпты — пока нет, нажмите «Добавить промпт»")
        else:
            self._custom_section.setText("Свои промпты")

    def _make_cell(self, data: _PromptData) -> QWidget:
        cell = QWidget()
        cell.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        lay = QVBoxLayout(cell)
        lay.setSpacing(4)
        lay.setContentsMargins(0, 0, 0, 0)

        head = QHBoxLayout()
        head.setSpacing(6)

        if data.builtin:
            name = QLabel(data.title)
            name.setObjectName("hint")
            name.setToolTip("Встроенный промпт — нельзя удалить")
            head.addWidget(name, 1)
        else:
            title_edit = QLineEdit()
            title_edit.setText(data.title)
            title_edit.setPlaceholderText("Название…")
            title_edit.textChanged.connect(
                lambda _t, pid=data.id: self._on_title_changed(pid)
            )
            self._title_edits[data.id] = title_edit
            head.addWidget(title_edit, 1)
            btn_del = QPushButton("×")
            btn_del.setObjectName("dangerIcon")
            btn_del.setFixedSize(32, 32)
            btn_del.setToolTip("Удалить промпт")
            btn_del.setAutoDefault(False)
            btn_del.setDefault(False)
            btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_del.clicked.connect(
                lambda _checked=False, pid=data.id: self._remove_prompt(pid)
            )
            head.addWidget(btn_del, 0, Qt.AlignmentFlag.AlignVCenter)

        edit = QPlainTextEdit()
        edit.setPlaceholderText("Промпт…")
        edit.setPlainText(data.text)
        edit.setFixedHeight(_PROMPT_H)
        edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        edit.textChanged.connect(lambda *, pid=data.id: self._on_text_changed(pid))

        lay.addLayout(head)
        lay.addWidget(edit)
        self._text_edits[data.id] = edit
        return cell

    def _item_by_id(self, prompt_id: str) -> _PromptData | None:
        for item in (*self._builtin_items, *self._custom_items):
            if item.id == prompt_id:
                return item
        return None

    def _on_title_changed(self, prompt_id: str) -> None:
        item = self._item_by_id(prompt_id)
        edit = self._title_edits.get(prompt_id)
        if item is None or edit is None or item.builtin:
            return
        item.title = edit.text()
        self._settings.setValue(f"ai/prompts/{prompt_id}/title", item.title)
        self._sync()

    def _on_text_changed(self, prompt_id: str) -> None:
        item = self._item_by_id(prompt_id)
        edit = self._text_edits.get(prompt_id)
        if item is None or edit is None:
            return
        item.text = edit.toPlainText() or ""
        self._settings.setValue(f"ai/prompts/{prompt_id}/text", item.text)
        self._sync()

    def _add_prompt(self) -> None:
        n = len(self._custom_items) + 1
        data = _PromptData(
            id=uuid.uuid4().hex[:12],
            title=f"Промпт {n}",
            text="",
            builtin=False,
        )
        self._custom_items.append(data)
        self._rebuild_grids()
        self._persist_all()
        title_edit = self._title_edits.get(data.id)
        if title_edit is not None:
            title_edit.setFocus()
            title_edit.selectAll()

    def _remove_prompt(self, prompt_id: str) -> None:
        item = self._item_by_id(prompt_id)
        if item is None or item.builtin:
            return
        title_edit = self._title_edits.get(prompt_id)
        title = (
            (title_edit.text() if title_edit is not None else item.title) or ""
        ).strip() or "без названия"
        reply = QMessageBox.question(
            self,
            "Удалить промпт",
            f"Удалить промпт «{title}»?\nЭто действие нельзя отменить.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._custom_items = [x for x in self._custom_items if x.id != prompt_id]
        for suffix in ("title", "text"):
            key = f"ai/prompts/{prompt_id}/{suffix}"
            try:
                self._settings.remove(key)
            except Exception:
                self._settings.setValue(key, "")
        try:
            self._settings.remove(f"ai/prompts/{prompt_id}")
        except Exception:
            pass
        self._rebuild_grids()
        self._persist_all()

    def _sync_items_from_widgets(self) -> None:
        for item in self._builtin_items:
            text_edit = self._text_edits.get(item.id)
            if text_edit is not None:
                item.text = text_edit.toPlainText() or ""
        for item in self._custom_items:
            title_edit = self._title_edits.get(item.id)
            text_edit = self._text_edits.get(item.id)
            if title_edit is not None:
                item.title = title_edit.text()
            if text_edit is not None:
                item.text = text_edit.toPlainText() or ""

    def _export_payload(self) -> dict:
        self._sync_items_from_widgets()
        return {
            "version": _EXPORT_VERSION,
            "builtin": [
                {"id": item.id, "title": item.title, "text": item.text}
                for item in self._builtin_items
            ],
            "custom": [
                {"id": item.id, "title": item.title, "text": item.text}
                for item in self._custom_items
            ],
        }

    def _export_prompts(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Экспорт промптов",
            "zaliver_prompts.json",
            _EXPORT_FILTER,
        )
        if not path:
            return
        out = Path(path)
        if out.suffix.lower() != ".json":
            out = out.with_suffix(".json")
        try:
            out.write_text(
                json.dumps(self._export_payload(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            QMessageBox.warning(self, "Экспорт промптов", f"Не удалось сохранить файл:\n{e}")
            return
        QMessageBox.information(
            self,
            "Экспорт промптов",
            f"Сохранено: {out.name}\n"
            f"Встроенных: {len(self._builtin_items)}, своих: {len(self._custom_items)}.",
        )

    def _normalize_builtin_id(self, raw_id: str) -> str | None:
        pid = (raw_id or "").strip()
        if not pid:
            return None
        pid = _BUILTIN_ID_ALIASES.get(pid, pid)
        if pid in self.builtin_prompt_ids():
            return pid
        return None

    def _parse_import_payload(
        self, data: object
    ) -> tuple[dict[str, str], list[_PromptData] | None]:
        """Возвращает (builtin_id → text, custom items | None если блок не в файле)."""
        if not isinstance(data, dict):
            raise ValueError("Корень JSON должен быть объектом.")

        builtin_texts: dict[str, str] = {}
        custom: list[_PromptData] | None = None
        builtin_ids = self.builtin_prompt_ids()
        has_builtin_block = False
        has_custom_block = False

        if isinstance(data.get("prompts"), list) and "builtin" not in data and "custom" not in data:
            raw_builtin_rows: list = []
            raw_custom_rows: list = []
            for row in data["prompts"]:
                if not isinstance(row, dict):
                    continue
                if bool(row.get("builtin")):
                    raw_builtin_rows.append(row)
                else:
                    raw_custom_rows.append(row)
            data = {**data, "builtin": raw_builtin_rows, "custom": raw_custom_rows}

        raw_builtin = data.get("builtin")
        if raw_builtin is not None:
            has_builtin_block = True
            if not isinstance(raw_builtin, list):
                raise ValueError("Поле «builtin» должно быть массивом.")
            for row in raw_builtin:
                if not isinstance(row, dict):
                    continue
                pid = self._normalize_builtin_id(str(row.get("id") or ""))
                if pid is None:
                    title = str(row.get("title") or "").strip().casefold()
                    for bid, btitle, _ in BUILTIN_PROMPTS:
                        if btitle.casefold() == title:
                            pid = bid
                            break
                if pid is None:
                    continue
                builtin_texts[pid] = str(row.get("text") or "")

        raw_custom = data.get("custom")
        if raw_custom is not None:
            has_custom_block = True
            if not isinstance(raw_custom, list):
                raise ValueError("Поле «custom» должно быть массивом.")
            custom = []
            seen: set[str] = set()
            for row in raw_custom:
                if not isinstance(row, dict):
                    continue
                pid = str(row.get("id") or "").strip() or uuid.uuid4().hex[:12]
                if pid in builtin_ids or pid in _BUILTIN_ID_ALIASES:
                    nid = self._normalize_builtin_id(pid)
                    if nid is not None:
                        builtin_texts[nid] = str(row.get("text") or "")
                        has_builtin_block = True
                    continue
                if pid in seen:
                    pid = uuid.uuid4().hex[:12]
                seen.add(pid)
                title = str(row.get("title") or "").strip() or "Промпт"
                custom.append(
                    _PromptData(
                        id=pid,
                        title=title,
                        text=str(row.get("text") or ""),
                        builtin=False,
                    )
                )

        if not has_builtin_block and not has_custom_block:
            raise ValueError(
                "В файле нет полей «builtin» / «custom» (или «prompts»)."
            )
        return builtin_texts, custom

    def _import_prompts(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Импорт промптов",
            "",
            _EXPORT_FILTER,
        )
        if not path:
            return
        try:
            raw = Path(path).read_text(encoding="utf-8-sig")
            data = json.loads(raw)
            builtin_texts, custom = self._parse_import_payload(data)
        except (OSError, json.JSONDecodeError, ValueError) as e:
            QMessageBox.warning(
                self,
                "Импорт промптов",
                f"Не удалось прочитать файл:\n{e}",
            )
            return

        reply = QMessageBox.question(
            self,
            "Импорт промптов",
            "Импорт перезапишет тексты встроенных (системных) промптов из файла "
            "и полностью заменит список своих промптов, если они есть в файле.\n\n"
            "Продолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        for item in self._builtin_items:
            if item.id in builtin_texts:
                item.text = builtin_texts[item.id]

        if custom is not None:
            for pid in [item.id for item in self._custom_items]:
                for suffix in ("title", "text"):
                    key = f"ai/prompts/{pid}/{suffix}"
                    try:
                        self._settings.remove(key)
                    except Exception:
                        self._settings.setValue(key, "")
                try:
                    self._settings.remove(f"ai/prompts/{pid}")
                except Exception:
                    pass
            self._custom_items = custom

        self._rebuild_grids()
        self._persist_all()
        custom_n = len(custom) if custom is not None else 0
        QMessageBox.information(
            self,
            "Импорт промптов",
            f"Готово.\nВстроенных обновлено: {len(builtin_texts)}"
            + (
                f", своих загружено: {custom_n}."
                if custom is not None
                else " (свои не менялись)."
            ),
        )

    def _persist_all(self) -> None:
        self._sync_items_from_widgets()
        order = [item.id for item in self._custom_items]
        self._settings.setValue(
            _CUSTOM_ORDER_KEY, json.dumps(order, ensure_ascii=False)
        )
        for item in self._builtin_items:
            self._settings.setValue(f"ai/prompts/{item.id}/title", item.title)
            self._settings.setValue(f"ai/prompts/{item.id}/text", item.text)
        for item in self._custom_items:
            self._settings.setValue(f"ai/prompts/{item.id}/title", item.title)
            self._settings.setValue(f"ai/prompts/{item.id}/text", item.text)
        self._sync()

    def _sync(self) -> None:
        try:
            self._settings.sync()
        except Exception:
            pass
