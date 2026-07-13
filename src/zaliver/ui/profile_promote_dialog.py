"""Диалог настроек продвижения для отмеченных профилей."""

from __future__ import annotations

from typing import NamedTuple

from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
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

from zaliver.ui.channel_setup_helpers import field_with_recent_picker

_DEFAULT_PROMOTE_COMMENTS = (
    "nice!\n"
    "wow\n"
    "cool\n"
    "🔥\n"
    "❤️\n"
    "love it\n"
    "😍\n"
    "great\n"
    "top\n"
    "🔥🔥\n"
    "so good\n"
    "💯"
)


class ProfilePromoteSettings(NamedTuple):
    subscribe_to_channels: bool
    shorts_count: int
    like_probability_pct: float
    shorts_watch_min_s: int
    shorts_watch_max_s: int
    watch_full_video: bool
    enable_comments: bool
    comments: list[str]
    comment_probability_pct: float


class ProfilePromoteDialog(QDialog):
    def __init__(
        self,
        *,
        parent: QWidget | None = None,
        recent_comments: list[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Продвижение")
        self.setModal(True)
        self.setMinimumWidth(460)

        recent = [c for c in (recent_comments or []) if (c or "").strip()]

        root = QVBoxLayout(self)
        root.setSpacing(12)

        hint = QLabel(
            "1) Studio — проверка, что аккаунт не заблокирован.\n"
            "2) При галочке «Подписаться на каналы» — по одному видео "
            "каждого видимого профиля и подписка.\n"
            "3) Лента подписок → полка Shorts → просмотр "
            "(лайки / комментарии)."
        )
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        root.addWidget(hint)

        self._subscribe_cb = QCheckBox("Подписаться на каналы")
        self._subscribe_cb.setChecked(False)
        self._subscribe_cb.setToolTip(
            "Открыть по одному уникальному видео видимых профилей "
            "(с просмотрами в БД) и подписаться. По умолчанию выключено."
        )
        root.addWidget(self._subscribe_cb)

        form = QFormLayout()
        self._count_spin = QSpinBox()
        self._count_spin.setRange(1, 9999)
        self._count_spin.setValue(10)
        form.addRow("Количество просмотренных Shorts:", self._count_spin)

        self._like_spin = QDoubleSpinBox()
        self._like_spin.setRange(0.0, 100.0)
        self._like_spin.setDecimals(1)
        self._like_spin.setSingleStep(1.0)
        self._like_spin.setSuffix(" %")
        self._like_spin.setValue(10.0)
        form.addRow("Вероятность лайка:", self._like_spin)

        watch_range_row = QHBoxLayout()
        self._watch_min_spin = QSpinBox()
        self._watch_min_spin.setRange(1, 9999)
        self._watch_min_spin.setValue(5)
        self._watch_min_spin.setSuffix(" с")
        self._watch_max_spin = QSpinBox()
        self._watch_max_spin.setRange(1, 9999)
        self._watch_max_spin.setValue(25)
        self._watch_max_spin.setSuffix(" с")
        watch_range_row.addWidget(self._watch_min_spin)
        watch_range_row.addWidget(QLabel("—"))
        watch_range_row.addWidget(self._watch_max_spin)
        watch_range_row.addStretch()
        watch_range_w = QWidget()
        watch_range_w.setLayout(watch_range_row)
        self._watch_range_lbl = QLabel("Длительность просмотра Short:")
        form.addRow(self._watch_range_lbl, watch_range_w)
        self._watch_range_w = watch_range_w

        self._watch_full_cb = QCheckBox("Смотреть каждый Short до конца")
        self._watch_full_cb.setToolTip(
            "Дождаться конца ролика, затем листать дальше. "
            "Если снять галочку — случайное время в указанном диапазоне."
        )
        form.addRow("", self._watch_full_cb)

        def _sync_watch_mode(full_watch: bool) -> None:
            self._watch_range_lbl.setVisible(not full_watch)
            self._watch_range_w.setVisible(not full_watch)

        self._watch_full_cb.toggled.connect(_sync_watch_mode)
        _sync_watch_mode(False)

        self._comments_cb = QCheckBox("Комментарии")
        self._comments_cb.setChecked(False)
        self._comments_cb.setToolTip(
            "С заданной вероятностью открыть комментарии, написать "
            "случайный текст из списка и отправить."
        )
        form.addRow("", self._comments_cb)

        self._comment_prob_spin = QDoubleSpinBox()
        self._comment_prob_spin.setRange(0.0, 100.0)
        self._comment_prob_spin.setDecimals(1)
        self._comment_prob_spin.setSingleStep(1.0)
        self._comment_prob_spin.setSuffix(" %")
        self._comment_prob_spin.setValue(10.0)
        self._comment_prob_lbl = QLabel("Вероятность комментария:")
        form.addRow(self._comment_prob_lbl, self._comment_prob_spin)

        root.addLayout(form)

        self._comments_block = QWidget()
        comments_l = QVBoxLayout(self._comments_block)
        comments_l.setContentsMargins(0, 0, 0, 0)
        comments_l.setSpacing(6)
        comments_hint = QLabel("Комментарии (по одному на строку):")
        comments_hint.setObjectName("hint")
        comments_l.addWidget(comments_hint)
        self._comments_edit = QPlainTextEdit()
        if recent:
            self._comments_edit.setPlainText(recent[0])
        else:
            self._comments_edit.setPlainText(_DEFAULT_PROMOTE_COMMENTS)
        self._comments_edit.setPlaceholderText("nice!\nwow\n🔥\ncool")
        self._comments_edit.setMinimumHeight(120)
        comments_field_row, self._comments_recent_combo = field_with_recent_picker(
            self._comments_edit,
            recent=recent,
            tooltip="Недавние списки комментариев",
        )
        comments_l.addWidget(comments_field_row)
        root.addWidget(self._comments_block)

        def _sync_comments(enabled: bool) -> None:
            self._comment_prob_lbl.setVisible(enabled)
            self._comment_prob_spin.setVisible(enabled)
            self._comments_block.setVisible(enabled)

        self._comments_cb.toggled.connect(_sync_comments)
        _sync_comments(False)

        row = QHBoxLayout()
        row.addStretch()
        btn_cancel = QPushButton("Отмена")
        btn_cancel.setObjectName("danger")
        btn_start = QPushButton("Старт")
        btn_start.setDefault(True)
        btn_start.setAutoDefault(True)
        btn_cancel.clicked.connect(self.reject)
        btn_start.clicked.connect(self._try_accept)
        row.addWidget(btn_cancel)
        row.addWidget(btn_start)
        root.addLayout(row)

    def comments_field_text(self) -> str:
        return self._comments_edit.toPlainText() or ""

    def _try_accept(self) -> None:
        if (
            not self._watch_full_cb.isChecked()
            and self._watch_min_spin.value() > self._watch_max_spin.value()
        ):
            QMessageBox.warning(
                self,
                "Продвижение",
                "Минимальная длительность просмотра Short не может быть "
                "больше максимальной.",
            )
            return
        if self._comments_cb.isChecked():
            comments = self._parsed_comments()
            if not comments:
                QMessageBox.warning(
                    self,
                    "Продвижение",
                    "Укажите хотя бы один комментарий (по одному на строку) "
                    "или снимите галочку «Комментарии».",
                )
                return
        self.accept()

    def _parsed_comments(self) -> list[str]:
        raw = self._comments_edit.toPlainText() or ""
        out: list[str] = []
        seen: set[str] = set()
        for line in raw.splitlines():
            s = line.strip()
            if not s or s in seen:
                continue
            seen.add(s)
            out.append(s)
        return out

    def settings(self) -> ProfilePromoteSettings:
        return ProfilePromoteSettings(
            subscribe_to_channels=bool(self._subscribe_cb.isChecked()),
            shorts_count=int(self._count_spin.value()),
            like_probability_pct=float(self._like_spin.value()),
            shorts_watch_min_s=int(self._watch_min_spin.value()),
            shorts_watch_max_s=int(self._watch_max_spin.value()),
            watch_full_video=bool(self._watch_full_cb.isChecked()),
            enable_comments=bool(self._comments_cb.isChecked()),
            comments=self._parsed_comments() if self._comments_cb.isChecked() else [],
            comment_probability_pct=float(self._comment_prob_spin.value()),
        )
