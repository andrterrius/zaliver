"""Dialog for editing YouTube/Instagram/Gmail credentials stored in profile custom_data."""

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

from zaliver.core.profiles.account_data import (
    GMAIL_2FA_KEY,
    GMAIL_LOGIN_KEY,
    GMAIL_PASSWORD_KEY,
    INST_2FA_KEY,
    INST_LOGIN_KEY,
    INST_PASSWORD_KEY,
    SECTION_GMAIL,
    SECTION_INSTAGRAM,
    SECTION_YOUTUBE,
    YT_2FA_KEY,
    YT_LOGIN_KEY,
    YT_PASSWORD_KEY,
    build_account_credentials_payload,
    build_gmail_credentials_payload,
    build_instagram_credentials_payload,
)

# Re-export for older UI imports that still pull keys from this module.
__all__ = [
    "GMAIL_2FA_KEY",
    "GMAIL_LOGIN_KEY",
    "GMAIL_PASSWORD_KEY",
    "INST_2FA_KEY",
    "INST_LOGIN_KEY",
    "INST_PASSWORD_KEY",
    "ProfileAccountDataDialog",
    "SECTION_GMAIL",
    "SECTION_INSTAGRAM",
    "SECTION_YOUTUBE",
    "YT_2FA_KEY",
    "YT_LOGIN_KEY",
    "YT_PASSWORD_KEY",
    "build_account_credentials_payload",
    "build_gmail_credentials_payload",
    "build_instagram_credentials_payload",
]


def _custom_data_str(custom_data: dict[str, object] | None, key: str) -> str:
    if not custom_data:
        return ""
    val = custom_data.get(key)
    if val is None:
        return ""
    return str(val)


def _normalize_section(section: str | None) -> str:
    s = (section or "").strip().lower()
    if s in (SECTION_INSTAGRAM, SECTION_GMAIL, SECTION_YOUTUBE):
        return s
    return SECTION_YOUTUBE


class ProfileAccountDataDialog(QDialog):
    def __init__(
        self,
        *,
        profile_name: str,
        profile_id: str,
        custom_data: dict[str, object] | None = None,
        section: str | None = None,
        platform: str | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        # platform оставлен для совместимости вызовов; приоритет у section.
        if section is None and platform is not None:
            from zaliver.ui.platform import PLATFORM_INSTAGRAM, normalize_platform

            section = (
                SECTION_INSTAGRAM
                if normalize_platform(platform) == PLATFORM_INSTAGRAM
                else SECTION_YOUTUBE
            )
        self._section = _normalize_section(section)
        self._custom_data = dict(custom_data) if isinstance(custom_data, dict) else {}
        self._gmail_initial: tuple[str, str, str] | None = None

        titles = {
            SECTION_INSTAGRAM: "Данные Insta",
            SECTION_GMAIL: "Данные Gmail",
            SECTION_YOUTUBE: "Данные учетки",
        }
        self.setWindowTitle(titles.get(self._section, "Данные учетки"))
        self.setModal(True)
        self.setMinimumWidth(420)

        root = QVBoxLayout(self)
        root.setSpacing(12)

        if self._section == SECTION_INSTAGRAM:
            hint_extra = (
                "Редактируются только данные Instagram "
                "(inst_login / inst_password / inst_2fa)."
            )
        elif self._section == SECTION_GMAIL:
            hint_extra = (
                "В полях — gmail_* (если ещё не заданы, стартовые значения "
                "из yt_login / yt_password / yt_2fa).\n"
                "Кнопка подставляет все три поля из YouTube.\n"
                "При сохранении, если изменилось хотя бы одно поле, "
                "в custom_data пишутся все gmail_login / gmail_password / gmail_2fa; "
                "данные YouTube не меняются."
            )
        else:
            hint_extra = (
                "Редактируются данные YouTube.\n"
                "При сохранении сбрасывается yt_oldest_name, если он был задан."
            )
        hint = QLabel(
            f"Профиль: {profile_name} ({profile_id})\n"
            "Данные сохраняются в custom_data локального антидетекта.\n"
            f"{hint_extra}"
        )
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        root.addWidget(hint)

        form = QFormLayout()
        form.setSpacing(10)

        self._login: QLineEdit | None = None
        self._password: QLineEdit | None = None
        self._twofa: QLineEdit | None = None
        self._inst_login: QLineEdit | None = None
        self._inst_password: QLineEdit | None = None
        self._inst_twofa: QLineEdit | None = None
        self._gmail_login: QLineEdit | None = None
        self._gmail_password: QLineEdit | None = None
        self._gmail_twofa: QLineEdit | None = None

        if self._section == SECTION_INSTAGRAM:
            self._inst_login = QLineEdit()
            self._inst_login.setPlaceholderText("email или логин Instagram")
            inst_login = _custom_data_str(self._custom_data, INST_LOGIN_KEY).strip()
            if not inst_login:
                inst_login = (profile_name or "").strip()
            self._inst_login.setText(inst_login)
            form.addRow("Логин Instagram:", self._inst_login)

            self._inst_password = QLineEdit()
            self._inst_password.setEchoMode(QLineEdit.EchoMode.Password)
            self._inst_password.setPlaceholderText("Пароль")
            self._inst_password.setText(
                _custom_data_str(self._custom_data, INST_PASSWORD_KEY)
            )
            form.addRow("Пароль Instagram:", self._inst_password)

            self._inst_twofa = QLineEdit()
            self._inst_twofa.setPlaceholderText("Секрет 2FA")
            self._inst_twofa.setText(
                _custom_data_str(self._custom_data, INST_2FA_KEY)
            )
            form.addRow("2FA Instagram:", self._inst_twofa)
        elif self._section == SECTION_GMAIL:
            self._gmail_login = QLineEdit()
            self._gmail_login.setPlaceholderText("email Gmail")
            gmail_login = _custom_data_str(self._custom_data, GMAIL_LOGIN_KEY).strip()
            yt_login = _custom_data_str(self._custom_data, YT_LOGIN_KEY).strip()
            has_gmail = bool(
                gmail_login
                or _custom_data_str(self._custom_data, GMAIL_PASSWORD_KEY)
                or _custom_data_str(self._custom_data, GMAIL_2FA_KEY)
            )
            shown_login = (
                gmail_login
                if has_gmail
                else (yt_login or (profile_name or "").strip())
            )
            if has_gmail and not shown_login:
                shown_login = yt_login or (profile_name or "").strip()
            self._gmail_login.setText(shown_login)
            form.addRow("Логин Gmail:", self._gmail_login)

            self._gmail_password = QLineEdit()
            self._gmail_password.setEchoMode(QLineEdit.EchoMode.Password)
            self._gmail_password.setPlaceholderText("Пароль")
            gmail_password = _custom_data_str(self._custom_data, GMAIL_PASSWORD_KEY)
            yt_password = _custom_data_str(self._custom_data, YT_PASSWORD_KEY)
            self._gmail_password.setText(
                gmail_password if has_gmail else yt_password
            )
            form.addRow("Пароль Gmail:", self._gmail_password)

            self._gmail_twofa = QLineEdit()
            self._gmail_twofa.setPlaceholderText("Секрет 2FA")
            gmail_twofa = _custom_data_str(self._custom_data, GMAIL_2FA_KEY)
            yt_twofa = _custom_data_str(self._custom_data, YT_2FA_KEY)
            self._gmail_twofa.setText(gmail_twofa if has_gmail else yt_twofa)
            form.addRow("2FA Gmail:", self._gmail_twofa)

            self._gmail_initial = (
                self._gmail_login.text(),
                self._gmail_password.text(),
                self._gmail_twofa.text(),
            )
        else:
            self._login = QLineEdit()
            self._login.setPlaceholderText("email или логин YouTube / Google")
            login = _custom_data_str(self._custom_data, YT_LOGIN_KEY).strip()
            if not login:
                login = (profile_name or "").strip()
            self._login.setText(login)
            form.addRow("Логин YouTube:", self._login)

            self._password = QLineEdit()
            self._password.setEchoMode(QLineEdit.EchoMode.Password)
            self._password.setPlaceholderText("Пароль")
            self._password.setText(
                _custom_data_str(self._custom_data, YT_PASSWORD_KEY)
            )
            form.addRow("Пароль YouTube:", self._password)

            self._twofa = QLineEdit()
            self._twofa.setPlaceholderText("Секрет 2FA")
            self._twofa.setText(_custom_data_str(self._custom_data, YT_2FA_KEY))
            form.addRow("2FA:", self._twofa)

        root.addLayout(form)

        if self._section == SECTION_INSTAGRAM:
            btn_from_gmail = QPushButton("подставить логин и пароль от Gmail")
            btn_from_gmail.setObjectName("secondary")
            btn_from_gmail.setToolTip(
                "Вставить в поля Instagram gmail_* этого профиля "
                "(если gmail_* нет — yt_*)"
            )
            btn_from_gmail.clicked.connect(self._fill_instagram_from_gmail)
            root.addWidget(btn_from_gmail)
        elif self._section == SECTION_GMAIL:
            btn_from_yt = QPushButton("подставить логин и пароль от YouTube")
            btn_from_yt.setObjectName("secondary")
            btn_from_yt.setToolTip(
                "Вставить в поля Gmail значения yt_login, yt_password и yt_2fa "
                "этого профиля"
            )
            btn_from_yt.clicked.connect(self._fill_gmail_from_youtube)
            root.addWidget(btn_from_yt)

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

        if self._section == SECTION_INSTAGRAM:
            focus = self._inst_login
        elif self._section == SECTION_GMAIL:
            focus = self._gmail_login
        else:
            focus = self._login
        if focus is not None:
            focus.setFocus()

    def _fill_instagram_from_gmail(self) -> None:
        if (
            self._inst_login is None
            or self._inst_password is None
            or self._inst_twofa is None
        ):
            return
        login = _custom_data_str(self._custom_data, GMAIL_LOGIN_KEY).strip()
        password = _custom_data_str(self._custom_data, GMAIL_PASSWORD_KEY)
        twofa = _custom_data_str(self._custom_data, GMAIL_2FA_KEY)
        if not (login or password or twofa):
            login = _custom_data_str(self._custom_data, YT_LOGIN_KEY).strip()
            password = _custom_data_str(self._custom_data, YT_PASSWORD_KEY)
            twofa = _custom_data_str(self._custom_data, YT_2FA_KEY)
        self._inst_login.setText(login)
        self._inst_password.setText(password)
        self._inst_twofa.setText(twofa)
        self._inst_login.setFocus()

    def _fill_gmail_from_youtube(self) -> None:
        if (
            self._gmail_login is None
            or self._gmail_password is None
            or self._gmail_twofa is None
        ):
            return
        self._gmail_login.setText(
            _custom_data_str(self._custom_data, YT_LOGIN_KEY).strip()
        )
        self._gmail_password.setText(
            _custom_data_str(self._custom_data, YT_PASSWORD_KEY)
        )
        self._gmail_twofa.setText(
            _custom_data_str(self._custom_data, YT_2FA_KEY)
        )
        self._gmail_login.setFocus()

    def _gmail_current_values(self) -> tuple[str, str, str]:
        assert self._gmail_login is not None
        assert self._gmail_password is not None
        assert self._gmail_twofa is not None
        return (
            self._gmail_login.text(),
            self._gmail_password.text(),
            self._gmail_twofa.text(),
        )

    def gmail_fields_changed(self) -> bool:
        """True, если хотя бы одно поле Gmail отличается от значений при открытии."""
        if self._section != SECTION_GMAIL or self._gmail_initial is None:
            return True
        return self._gmail_current_values() != self._gmail_initial

    def account_data_payload(self) -> dict[str, str]:
        if self._section == SECTION_INSTAGRAM:
            assert self._inst_login is not None
            assert self._inst_password is not None
            assert self._inst_twofa is not None
            return build_instagram_credentials_payload(
                login=self._inst_login.text(),
                password=self._inst_password.text(),
                twofa=self._inst_twofa.text(),
            )
        if self._section == SECTION_GMAIL:
            assert self._gmail_login is not None
            assert self._gmail_password is not None
            assert self._gmail_twofa is not None
            # Если ничего не меняли — пустой payload (не трогаем custom_data).
            if not self.gmail_fields_changed():
                return {}
            # Если изменилось хоть одно поле — сохраняем все gmail_* целиком.
            login, password, twofa = self._gmail_current_values()
            return build_gmail_credentials_payload(
                login=login,
                password=password,
                twofa=twofa,
            )
        assert self._login is not None
        assert self._password is not None
        assert self._twofa is not None
        return build_account_credentials_payload(
            login=self._login.text(),
            password=self._password.text(),
            twofa=self._twofa.text(),
        )
