"""Диалог генерации через ИИ: выбор промпта → ожидание → результат."""

from __future__ import annotations

from PyQt6.QtCore import Qt, QThread, QTimer
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from zaliver.ui.ai_chat_worker import AiChatCompletionWorker

_REPLY_LINES_SUFFIX = "Количество необходимых строк-ответов: {n}"


class AiGenerateDialog(QDialog):
    """
    Выпадающий список всех промптов (встроенные и свои).
    По умолчанию выбран ``default_prompt_id``.
    «Сгенерировать» → анимация ожидания → при успехе диалог закрывается.

    Если ``ask_reply_lines=True``, показывается число строк-ответов;
    к тексту промпта добавляется суффикс с этим числом.
    """

    def __init__(
        self,
        *,
        prompts: list[tuple[str, str, str]],
        default_prompt_id: str,
        base_url: str,
        api_key: str,
        model: str,
        window_title: str = "Генерация ИИ",
        ask_reply_lines: bool = False,
        default_reply_lines: int = 1,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(window_title)
        self.setModal(True)
        self.setMinimumWidth(420)
        self._base_url = (base_url or "").strip()
        self._api_key = (api_key or "").strip()
        self._model = (model or "").strip()
        self._ask_reply_lines = bool(ask_reply_lines)
        self._result = ""
        self._thread: QThread | None = None
        self._worker: AiChatCompletionWorker | None = None
        self._busy = False
        self._dot_n = 0

        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(16, 16, 16, 16)

        root.addWidget(QLabel("Промпт:"))
        self._combo = QComboBox()
        self._combo.setMinimumHeight(32)
        default_idx = 0
        for i, (pid, title, text) in enumerate(prompts):
            label = (title or "").strip() or pid
            self._combo.addItem(label, (pid, text))
            if pid == default_prompt_id:
                default_idx = i
        if self._combo.count() > 0:
            self._combo.setCurrentIndex(default_idx)
        root.addWidget(self._combo)

        self._lines_spin: QSpinBox | None = None
        if self._ask_reply_lines:
            lines_row = QHBoxLayout()
            lines_row.setSpacing(8)
            lines_lbl = QLabel("Количество строк:")
            lines_lbl.setToolTip(
                "Сколько отдельных ответов (по строкам) должен вернуть ИИ. "
                "Добавляется в конец промпта."
            )
            self._lines_spin = QSpinBox()
            self._lines_spin.setMinimum(1)
            self._lines_spin.setMaximum(500)
            self._lines_spin.setValue(max(1, min(500, int(default_reply_lines or 1))))
            self._lines_spin.setMinimumWidth(80)
            lines_row.addWidget(lines_lbl)
            lines_row.addWidget(self._lines_spin)
            lines_row.addStretch(1)
            root.addLayout(lines_row)

        self._status = QLabel("")
        self._status.setObjectName("hint")
        self._status.setWordWrap(True)
        self._status.setVisible(False)
        root.addWidget(self._status)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)  # indeterminate
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(8)
        self._progress.setVisible(False)
        root.addWidget(self._progress)

        self._wait_label = QLabel("")
        self._wait_label.setObjectName("hint")
        self._wait_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._wait_label.setVisible(False)
        root.addWidget(self._wait_label)

        self._dots_timer = QTimer(self)
        self._dots_timer.setInterval(400)
        self._dots_timer.timeout.connect(self._tick_dots)

        btns = QHBoxLayout()
        btns.addStretch()
        self._btn_cancel = QPushButton("Отмена")
        self._btn_cancel.setObjectName("danger")
        self._btn_cancel.setAutoDefault(False)
        self._btn_cancel.clicked.connect(self._on_cancel)
        self._btn_generate = QPushButton("Сгенерировать")
        self._btn_generate.setDefault(True)
        self._btn_generate.setAutoDefault(True)
        self._btn_generate.clicked.connect(self._start_generate)
        btns.addWidget(self._btn_cancel)
        btns.addWidget(self._btn_generate)
        root.addLayout(btns)

        if self._combo.count() == 0:
            self._btn_generate.setEnabled(False)
            self._status.setText("Нет доступных промптов. Добавьте их во вкладке «ИИ».")
            self._status.setVisible(True)

    def result_text(self) -> str:
        return self._result

    def _selected_prompt(self) -> tuple[str, str]:
        data = self._combo.currentData()
        if not isinstance(data, tuple) or len(data) != 2:
            return "", ""
        return str(data[0] or ""), str(data[1] if data[1] is not None else "")

    def _build_prompt_content(self, prompt: str) -> str:
        text = prompt if prompt is not None else ""
        if not self._ask_reply_lines or self._lines_spin is None:
            return text
        n = int(self._lines_spin.value())
        suffix = _REPLY_LINES_SUFFIX.format(n=n)
        if text and not text.endswith("\n"):
            return f"{text}\n{suffix}"
        return f"{text}{suffix}"

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._combo.setEnabled(not busy)
        if self._lines_spin is not None:
            self._lines_spin.setEnabled(not busy)
        self._btn_generate.setEnabled(not busy and self._combo.count() > 0)
        self._progress.setVisible(busy)
        self._wait_label.setVisible(busy)
        if busy:
            self._dot_n = 0
            self._tick_dots()
            self._dots_timer.start()
            self._btn_cancel.setText("Отмена")
        else:
            self._dots_timer.stop()
            self._wait_label.setText("")

    def _tick_dots(self) -> None:
        self._dot_n = (self._dot_n + 1) % 4
        dots = "." * self._dot_n
        self._wait_label.setText(f"Ожидание ответа ИИ{dots}")

    def _start_generate(self) -> None:
        if self._busy:
            return
        _pid, prompt = self._selected_prompt()
        if not (prompt or "").strip():
            QMessageBox.warning(
                self,
                "ИИ",
                "Выбранный промпт пустой. Заполните его во вкладке «ИИ».",
            )
            return
        if not self._base_url or not self._api_key or not self._model:
            QMessageBox.warning(
                self,
                "ИИ",
                "Заполните URL эндпоинта, API key и модель в разделе «Настройки» → «ИИ».",
            )
            return

        content = self._build_prompt_content(prompt)
        self._status.setVisible(False)
        self._set_busy(True)

        thread = QThread(self)
        worker = AiChatCompletionWorker(
            base_url=self._base_url,
            api_key=self._api_key,
            model=self._model,
            messages=[{"role": "user", "content": content}],
        )
        worker.moveToThread(thread)
        self._thread = thread
        self._worker = worker

        thread.started.connect(worker.run)
        worker.finished_ok.connect(self._on_ok)
        worker.finished_err.connect(self._on_err)
        thread.start()

    def _stop_worker(self) -> None:
        thread = self._thread
        worker = self._worker
        self._thread = None
        self._worker = None
        if thread is None:
            return
        try:
            if thread.isRunning():
                thread.quit()
                thread.wait(3_000)
                if thread.isRunning():
                    thread.terminate()
                    thread.wait(1_000)
        except RuntimeError:
            pass
        if worker is not None:
            try:
                worker.deleteLater()
            except RuntimeError:
                pass
        try:
            thread.deleteLater()
        except RuntimeError:
            pass

    def _on_ok(self, text: str) -> None:
        self._result = text if text is not None else ""
        self._set_busy(False)
        self._stop_worker()
        self.accept()

    def _on_err(self, msg: str) -> None:
        self._set_busy(False)
        self._stop_worker()
        self._status.setText(f"Ошибка: {msg}")
        self._status.setVisible(True)
        QMessageBox.warning(self, "ИИ", f"Не удалось сгенерировать:\n{msg}")

    def _on_cancel(self) -> None:
        if self._busy:
            self._set_busy(False)
            self._stop_worker()
            self._status.setText("Генерация отменена.")
            self._status.setVisible(True)
            return
        self.reject()

    def reject(self) -> None:
        if self._busy:
            self._set_busy(False)
            self._stop_worker()
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._busy:
            self._set_busy(False)
            self._stop_worker()
        super().closeEvent(event)
