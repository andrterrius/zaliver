"""Диалог настроек фарма Cookie для отмеченных профилей."""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from zaliver.antydetect.cookie_farm_domains import (
    default_cookie_farm_domains,
    parse_domains_text,
)


class CookieFarmSettings(NamedTuple):
    use_preset_domains: bool
    preset_kind: str
    domains: list[str]
    sites_count: int
    watch_min_s: int
    watch_max_s: int


class ProfileCookieFarmDialog(QDialog):
    def __init__(self, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Фарм Cookie")
        self.setModal(True)
        self.setMinimumWidth(520)

        self._preset_domains = default_cookie_farm_domains(preset="intl")

        root = QVBoxLayout(self)
        root.setSpacing(12)

        hint = QLabel(
            "Для каждого отмеченного профиля браузер по очереди откроет сайты "
            "из источника и медленно прокрутит страницу заданное время."
        )
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        root.addWidget(hint)

        self._preset_cb = QCheckBox("Заготовленный список доменов")
        self._preset_cb.setChecked(True)
        self._preset_cb.setToolTip(
            "Встроенный список популярных сайтов. Снимите галочку, "
            "чтобы указать свой список вручную или загрузить из .txt."
        )
        root.addWidget(self._preset_cb)

        self._preset_pick_row = QWidget()
        preset_pick_l = QHBoxLayout(self._preset_pick_row)
        preset_pick_l.setContentsMargins(20, 0, 0, 0)
        preset_pick_l.setSpacing(8)
        preset_pick_l.addWidget(QLabel("Список:"))
        self._preset_combo = QComboBox()
        self._preset_combo.addItem("Международные сайты", "intl")
        self._preset_combo.addItem("RU сайты", "ru")
        self._preset_combo.setToolTip(
            "Международные — популярные зарубежные сайты. "
            "RU — российские сайты (ok.ru, ozon.ru, wildberries.ru и др.)."
        )
        self._preset_combo.currentIndexChanged.connect(self._on_preset_list_changed)
        preset_pick_l.addWidget(self._preset_combo, 1)
        root.addWidget(self._preset_pick_row)

        self._custom_block = QWidget()
        custom_l = QVBoxLayout(self._custom_block)
        custom_l.setContentsMargins(0, 0, 0, 0)
        custom_l.setSpacing(8)

        self._domains_edit = QPlainTextEdit()
        self._domains_edit.setPlaceholderText(
            "По одному домену на строку, например:\n"
            "google.com\n"
            "https://youtube.com\n"
            "amazon.com"
        )
        self._domains_edit.setMinimumHeight(140)
        self._domains_edit.textChanged.connect(self._update_source_count)
        custom_l.addWidget(self._domains_edit)

        custom_source_row = QHBoxLayout()
        self._custom_source_label = QLabel("Источник: ввод вручную")
        self._custom_source_label.setObjectName("hint")
        btn_pick = QPushButton("Выбрать .txt…")
        btn_pick.setObjectName("secondary")
        btn_pick.clicked.connect(self._pick_txt_file)
        custom_source_row.addWidget(self._custom_source_label, 1)
        custom_source_row.addWidget(btn_pick)
        custom_l.addLayout(custom_source_row)
        root.addWidget(self._custom_block)

        self._source_count_label = QLabel()
        self._source_count_label.setObjectName("hint")
        root.addWidget(self._source_count_label)

        form = QFormLayout()
        self._sites_spin = QSpinBox()
        self._sites_spin.setRange(1, max(1, len(self._preset_domains)))
        self._sites_spin.setValue(min(10, len(self._preset_domains)))
        form.addRow("Просматриваемых сайтов:", self._sites_spin)

        watch_row = QHBoxLayout()
        self._watch_min_spin = QSpinBox()
        self._watch_min_spin.setRange(10, 30)
        self._watch_min_spin.setValue(10)
        self._watch_min_spin.setSuffix(" с")
        self._watch_max_spin = QSpinBox()
        self._watch_max_spin.setRange(10, 30)
        self._watch_max_spin.setValue(30)
        self._watch_max_spin.setSuffix(" с")
        watch_row.addWidget(self._watch_min_spin)
        watch_row.addWidget(QLabel("—"))
        watch_row.addWidget(self._watch_max_spin)
        watch_row.addStretch()
        form.addRow("Время на сайт:", watch_row)
        root.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._preset_cb.toggled.connect(self._sync_preset_mode)
        self._sync_preset_mode(self._preset_cb.isChecked())

    def _on_preset_list_changed(self, _index: int) -> None:
        if not self._preset_cb.isChecked():
            return
        kind = self._preset_combo.currentData()
        if not isinstance(kind, str) or not kind.strip():
            kind = "intl"
        self._preset_domains = default_cookie_farm_domains(preset=kind)
        self._sites_spin.setMaximum(max(1, len(self._preset_domains)))
        if self._sites_spin.value() > len(self._preset_domains):
            self._sites_spin.setValue(len(self._preset_domains))
        self._update_source_count()

    def _current_domains(self) -> list[str]:
        if self._preset_cb.isChecked():
            return list(self._preset_domains)
        return parse_domains_text(self._domains_edit.toPlainText())

    def _update_source_count(self) -> None:
        domains = self._current_domains()
        total = len(domains)
        self._source_count_label.setText(
            f"В источнике: {total} сайтов" if total else "В источнике: 0 сайтов"
        )
        if total > 0:
            self._sites_spin.setMaximum(total)
            if self._sites_spin.value() > total:
                self._sites_spin.setValue(total)
        else:
            self._sites_spin.setMaximum(1)

    def _sync_preset_mode(self, use_preset: bool) -> None:
        self._preset_pick_row.setVisible(use_preset)
        self._custom_block.setVisible(not use_preset)
        if use_preset:
            self._on_preset_list_changed(self._preset_combo.currentIndex())
        self._update_source_count()

    def _pick_txt_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Выбрать список доменов",
            "",
            "Текстовые файлы (*.txt);;Все файлы (*.*)",
        )
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                text = Path(path).read_text(encoding="cp1251")
            except OSError as e:
                QMessageBox.warning(self, "Фарм Cookie", f"Не удалось прочитать файл:\n{e}")
                return
        except OSError as e:
            QMessageBox.warning(self, "Фарм Cookie", f"Не удалось прочитать файл:\n{e}")
            return
        self._domains_edit.setPlainText(text)
        self._custom_source_label.setText(f"Источник: {Path(path).name}")
        self._update_source_count()

    def _on_accept(self) -> None:
        domains = self._current_domains()
        if not domains:
            QMessageBox.warning(
                self,
                "Фарм Cookie",
                "Укажите хотя бы один домен в источнике.",
            )
            return
        if self._watch_min_spin.value() > self._watch_max_spin.value():
            QMessageBox.warning(
                self,
                "Фарм Cookie",
                "Минимальное время на сайт не может быть больше максимального.",
            )
            return
        self.accept()

    def settings(self) -> CookieFarmSettings:
        domains = self._current_domains()
        preset_kind = ""
        if self._preset_cb.isChecked():
            kind = self._preset_combo.currentData()
            preset_kind = kind if isinstance(kind, str) else "intl"
        return CookieFarmSettings(
            use_preset_domains=self._preset_cb.isChecked(),
            preset_kind=preset_kind,
            domains=domains,
            sites_count=min(self._sites_spin.value(), len(domains)),
            watch_min_s=self._watch_min_spin.value(),
            watch_max_s=self._watch_max_spin.value(),
        )
