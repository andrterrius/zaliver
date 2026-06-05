"""Dialog for editing YouTube account credentials stored in profile custom_data."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

YT_LOGIN_KEY = "yt_login"
YT_PASSWORD_KEY = "yt_password"
YT_2FA_KEY = "yt_2fa"


def _custom_data_str(custom_data: dict[str, object] | None, key: str) -> str:
    if not custom_data:
        return ""
    val = custom_data.get(key)
    if val is None:
        return ""
    return str(val)


class ProfileAccountDataDialog(QDialog):
    def __init__(
        self,
        *,
        profile_name: str,
        profile_id: str,
        custom_data: dict[str, object] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Данные учетки")
        self.setModal(True)
        self.setMinimumWidth(420)

        root = QVBoxLayout(self)
        root.setSpacing(12)

        hint = QLabel(
            f"Профиль: {profile_name} ({profile_id})\n"
            "Данные сохраняются в custom_data локального антидетекта."
        )
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        root.addWidget(hint)

        form = QFormLayout()
        form.setSpacing(10)

        self._login = QLineEdit()
        self._login.setPlaceholderText("email или логин YouTube / Google")
        login = _custom_data_str(custom_data, YT_LOGIN_KEY).strip()
        if not login:
            login = (profile_name or "").strip()
        self._login.setText(login)
        form.addRow("Логин YouTube:", self._login)

        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._password.setPlaceholderText("Пароль")
        self._password.setText(_custom_data_str(custom_data, YT_PASSWORD_KEY))
        form.addRow("Пароль YouTube:", self._password)

        self._twofa = QLineEdit()
        self._twofa.setPlaceholderText("Секрет 2FA")
        self._twofa.setText(_custom_data_str(custom_data, YT_2FA_KEY))
        form.addRow("2FA:", self._twofa)

        root.addLayout(form)

        btns = QHBoxLayout()
        btns.addStretch()
        btn_cancel = QPushButton("Отмена")
        btn_cancel.setObjectName("secondary")
        btn_save = QPushButton("Сохранить")
        btn_save.setDefault(True)
        btn_save.setAutoDefault(True)
        btn_cancel.clicked.connect(self.reject)
        btn_save.clicked.connect(self.accept)
        btns.addWidget(btn_cancel)
        btns.addWidget(btn_save)
        root.addLayout(btns)

        self._login.setFocus()

    def account_data_payload(self) -> dict[str, str]:
        return {
            YT_LOGIN_KEY: (self._login.text() or "").strip(),
            YT_PASSWORD_KEY: self._password.text() or "",
            YT_2FA_KEY: (self._twofa.text() or "").strip(),
        }
