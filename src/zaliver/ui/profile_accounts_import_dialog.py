"""Dialog for importing YouTube account credentials from a .txt file."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from zaliver.ui.account_import_parser import (
    assign_accounts_to_selected_profiles,
    build_selected_profile_rows,
    parse_accounts_text,
)
from zaliver.ui.profile_account_data_dialog import (
    YT_2FA_KEY,
    YT_LOGIN_KEY,
    YT_PASSWORD_KEY,
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
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Импорт данных учёток")
        self.setModal(True)
        self.setMinimumSize(720, 420)

        self._profiles = list(selected_profiles)
        self._rows = build_selected_profile_rows(self._profiles)

        root = QVBoxLayout(self)
        root.setSpacing(12)

        hint = QLabel(
            "В таблице — отмеченные профили. Выберите .txt-файл со строками вида:\n"
            "email@gmail.com:пароль:пересылка@mail.com::секрет_2FA\n\n"
            "Учётки из файла сопоставляются с профилями по порядку. "
            "После проверки нажмите «Сохранить»."
        )
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        root.addWidget(hint)

        file_row = QHBoxLayout()
        self._file_label = QLabel("Файл не выбран")
        self._file_label.setObjectName("hint")
        btn_pick = QPushButton("Выбрать файл…")
        btn_pick.setObjectName("secondary")
        btn_pick.clicked.connect(self._pick_file)
        file_row.addWidget(self._file_label, 1)
        file_row.addWidget(btn_pick)
        root.addLayout(file_row)

        self._status = QLabel("")
        self._status.setObjectName("hint")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

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
        self._status.setText(f"Отмечено профилей: {len(self._profiles)}. Выберите файл с учётками.")

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
                    {
                        YT_LOGIN_KEY: str(account.get(YT_LOGIN_KEY) or "").strip(),
                        YT_PASSWORD_KEY: str(account.get(YT_PASSWORD_KEY) or ""),
                        YT_2FA_KEY: str(account.get(YT_2FA_KEY) or "").strip(),
                    },
                )
            )
        return out

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

        accounts = parse_accounts_text(text)
        if not accounts:
            self._rows = build_selected_profile_rows(self._profiles)
            self._populate_table()
            self._btn_save.setEnabled(False)
            self._file_label.setText(path.name)
            self._status.setText(
                "В файле не найдено ни одной строки с @gmail.com в ожидаемом формате."
            )
            return

        self._rows = assign_accounts_to_selected_profiles(self._profiles, accounts)
        self._populate_table()
        self._file_label.setText(str(path))

        matched = sum(1 for row in self._rows if row.get("can_save"))
        extra = sum(
            1
            for row in self._rows
            if not row.get("can_save") and str(row.get("status") or "").startswith("Лишняя")
        )
        missing = len(self._profiles) - matched
        parts = [
            f"Распознано учёток: {len(accounts)}.",
            f"Готово к сохранению: {matched}.",
        ]
        if missing > 0:
            parts.append(f"Без учётки: {missing}.")
        if extra:
            parts.append(f"Лишних учёток: {extra}.")
        self._status.setText(" ".join(parts))
        self._btn_save.setEnabled(matched > 0)

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
            if profile_name and profile_id:
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
