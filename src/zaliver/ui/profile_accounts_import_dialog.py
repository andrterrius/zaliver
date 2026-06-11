"""Dialog for importing YouTube account credentials from text or a .txt file."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from zaliver.ui.account_import_parser import (
    annotate_import_duplicate_warnings,
    assign_accounts_to_selected_profiles,
    build_selected_profile_rows,
    parse_accounts_text,
)
from zaliver.ui.profile_account_data_dialog import (
    YT_2FA_KEY,
    YT_LOGIN_KEY,
    YT_PASSWORD_KEY,
    build_account_credentials_payload,
)


def _mask_secret(value: str, *, visible: int = 4) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) <= visible:
        return "*" * len(text)
    return "*" * (len(text) - visible) + text[-visible:]


class ProfileAccountsImportDialog(QDialog):
    def __init__(
        self,
        *,
        selected_profiles: list[dict[str, object]],
        all_profiles: list[dict[str, object]] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Импорт данных учёток")
        self.setModal(True)
        self.setMinimumSize(720, 520)

        self._profiles = list(selected_profiles)
        self._all_profiles = list(all_profiles or selected_profiles)
        self._rows = build_selected_profile_rows(self._profiles)
        self._duplicate_warnings: list[str] = []

        root = QVBoxLayout(self)
        root.setSpacing(12)

        hint = QLabel(
            "В таблице — отмеченные профили. Вставьте строки учёток в поле ниже "
            "или выберите .txt-файл. Формат строки:\n"
            "email@gmail.com:пароль:пересылка@mail.com::секрет_2FA\n\n"
            "Учётки сопоставляются с профилями по порядку. "
            "При сохранении сбрасывается yt_oldest_name, если он был задан. "
            "Почты, уже привязанные к другим профилям, будут отмечены как конфликт. "
            "После проверки нажмите «Сохранить»."
        )
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        root.addWidget(hint)

        self._text_input = QPlainTextEdit()
        self._text_input.setPlaceholderText(
            "Вставьте учётки — по одной строке, например:\n"
            "email@gmail.com:пароль:пересылка@mail.com::секрет_2FA"
        )
        self._text_input.setMinimumHeight(120)
        root.addWidget(self._text_input)

        source_row = QHBoxLayout()
        self._source_label = QLabel("Источник не выбран")
        self._source_label.setObjectName("hint")
        btn_parse = QPushButton("Разобрать текст")
        btn_parse.clicked.connect(self._parse_pasted_text)
        btn_pick = QPushButton("Выбрать файл…")
        btn_pick.setObjectName("secondary")
        btn_pick.clicked.connect(self._pick_file)
        source_row.addWidget(self._source_label, 1)
        source_row.addWidget(btn_parse)
        source_row.addWidget(btn_pick)
        root.addLayout(source_row)

        self._status = QLabel("")
        self._status.setObjectName("hint")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        self._rename_to_email = QCheckBox("Поменять названия профилей на почту")
        self._rename_to_email.stateChanged.connect(self._populate_table)
        root.addWidget(self._rename_to_email)

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["Логин", "Пароль", "2FA", "Профиль", "Статус"]
        )
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        root.addWidget(self._table, 1)

        btns = QHBoxLayout()
        btns.addStretch()
        btn_cancel = QPushButton("Отмена")
        btn_cancel.setObjectName("secondary")
        self._btn_save = QPushButton("Сохранить")
        self._btn_save.setDefault(True)
        self._btn_save.setAutoDefault(True)
        self._btn_save.setEnabled(False)
        btn_cancel.clicked.connect(self.reject)
        self._btn_save.clicked.connect(self._on_save)
        btns.addWidget(btn_cancel)
        btns.addWidget(self._btn_save)
        root.addLayout(btns)

        self._populate_table()
        self._status.setText(
            f"Отмечено профилей: {len(self._profiles)}. "
            "Вставьте учётки в поле или выберите файл."
        )

    def save_payloads(self) -> list[tuple[str, dict[str, str]]]:
        """Matched (profile_id, account payload) pairs after successful save click."""
        out: list[tuple[str, dict[str, str]]] = []
        for row in self._rows:
            if not row.get("can_save"):
                continue
            pid = str(row.get("profile_id") or "").strip()
            account = row.get("account")
            if not pid or not isinstance(account, dict):
                continue
            out.append(
                (
                    pid,
                    build_account_credentials_payload(
                        login=str(account.get(YT_LOGIN_KEY) or ""),
                        password=str(account.get(YT_PASSWORD_KEY) or ""),
                        twofa=str(account.get(YT_2FA_KEY) or ""),
                    ),
                )
            )
        return out

    def rename_profiles_to_email(self) -> bool:
        return self._rename_to_email.isChecked()

    def _parse_pasted_text(self) -> None:
        text = self._text_input.toPlainText()
        if not text.strip():
            QMessageBox.warning(
                self,
                "Импорт данных учёток",
                "Вставьте учётки в поле ввода или выберите файл.",
            )
            return
        self._apply_accounts_text(text, source="вставленный текст")

    def _pick_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите файл с учётками",
            "",
            "Текстовые файлы (*.txt);;Все файлы (*.*)",
        )
        if not path:
            return
        self._load_file(Path(path))

    def _load_file(self, path: Path) -> None:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            QMessageBox.warning(
                self,
                "Импорт данных учёток",
                f"Не удалось прочитать файл:\n{e}",
            )
            return
        self._text_input.setPlainText(text)
        self._apply_accounts_text(text, source=str(path))

    def _apply_accounts_text(self, text: str, *, source: str) -> None:
        accounts = parse_accounts_text(text)
        if not accounts:
            self._rows = build_selected_profile_rows(self._profiles)
            self._populate_table()
            self._btn_save.setEnabled(False)
            self._source_label.setText(source)
            self._status.setText(
                "Не найдено ни одной строки с @gmail.com в ожидаемом формате."
            )
            return

        self._rows = assign_accounts_to_selected_profiles(self._profiles, accounts)
        self._duplicate_warnings = annotate_import_duplicate_warnings(
            self._rows, self._all_profiles
        )
        self._populate_table()
        self._source_label.setText(source)

        matched = sum(1 for row in self._rows if row.get("can_save"))
        extra = sum(
            1
            for row in self._rows
            if not row.get("can_save") and str(row.get("status") or "").startswith("Лишняя")
        )
        conflicts = len(self._duplicate_warnings)
        missing = sum(
            1
            for row in self._rows
            if str(row.get("status") or "") == "Нет учётки в файле"
        )
        parts = [
            f"Распознано учёток: {len(accounts)}.",
            f"Готово к сохранению: {matched}.",
        ]
        if missing > 0:
            parts.append(f"Без учётки: {missing}.")
        if extra:
            parts.append(f"Лишних учёток: {extra}.")
        if conflicts:
            parts.append(f"Конфликтов почты: {conflicts}.")
        self._status.setText(" ".join(parts))
        self._btn_save.setEnabled(matched > 0)
        if self._duplicate_warnings:
            preview = "\n".join(self._duplicate_warnings[:6])
            if len(self._duplicate_warnings) > 6:
                preview += f"\n… и ещё {len(self._duplicate_warnings) - 6}."
            QMessageBox.warning(
                self,
                "Импорт данных учёток",
                "Обнаружены конфликты почты:\n\n" + preview,
            )

    def _populate_table(self) -> None:
        self._table.setRowCount(len(self._rows))
        for i, row in enumerate(self._rows):
            account = row.get("account")
            if not isinstance(account, dict):
                account = {}

            login = str(account.get(YT_LOGIN_KEY) or "") or "—"
            password = _mask_secret(str(account.get(YT_PASSWORD_KEY) or "")) or "—"
            twofa = _mask_secret(str(account.get(YT_2FA_KEY) or ""), visible=6) or "—"
            profile_name = str(row.get("profile_name") or "")
            profile_id = str(row.get("profile_id") or "")
            email = str(account.get(YT_LOGIN_KEY) or "").strip()
            if (
                self._rename_to_email.isChecked()
                and email
                and profile_id
                and row.get("can_save")
            ):
                if profile_name and profile_name != email:
                    profile_cell = f"{profile_name} → {email}"
                else:
                    profile_cell = email
                if profile_id:
                    profile_cell = f"{profile_cell} ({profile_id})"
            elif profile_name and profile_id:
                profile_cell = f"{profile_name} ({profile_id})"
            elif profile_name:
                profile_cell = profile_name
            else:
                profile_cell = "—"
            status = str(row.get("status") or "")

            for col, value in enumerate(
                [login, password, twofa, profile_cell, status]
            ):
                item = QTableWidgetItem(value)
                if col in (0, 1, 2):
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                    )
                if not row.get("can_save") and col == 4:
                    item.setForeground(Qt.GlobalColor.darkRed)
                self._table.setItem(i, col, item)

        self._table.resizeColumnsToContents()

    def _on_save(self) -> None:
        matched = sum(1 for row in self._rows if row.get("can_save"))
        if matched <= 0:
            QMessageBox.warning(
                self,
                "Импорт данных учёток",
                "Нет учёток, сопоставленных с профилями. Сохранять нечего.",
            )
            return
        unmatched = len(self._rows) - matched
        msg = f"Сохранить данные для {matched} профилей?"
        if self._rename_to_email.isChecked():
            msg += "\n\nНазвания профилей будут заменены на почту из учёток."
        msg += "\n\nСохранённый yt_oldest_name будет сброшен у импортируемых профилей."
        if unmatched:
            msg += f"\n\n{unmatched} учёток без совпадения будут пропущены."
        answer = QMessageBox.question(
            self,
            "Импорт данных учёток",
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.accept()
