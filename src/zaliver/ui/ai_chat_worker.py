"""Фоновая генерация текста через OpenAI-совместимый API."""

from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal

from zaliver.ai.openai_compat import OpenAICompatError, chat_completion


class AiChatCompletionWorker(QObject):
    finished_ok = pyqtSignal(str)
    finished_err = pyqtSignal(str)

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict[str, str]],
        timeout_s: float = 120.0,
    ) -> None:
        super().__init__()
        self._base_url = base_url
        self._api_key = api_key
        self._model = model
        self._messages = list(messages)
        self._timeout_s = timeout_s

    def run(self) -> None:
        try:
            text = chat_completion(
                base_url=self._base_url,
                api_key=self._api_key,
                model=self._model,
                messages=self._messages,
                timeout_s=self._timeout_s,
            )
        except OpenAICompatError as e:
            self.finished_err.emit(str(e))
            return
        except Exception as e:
            self.finished_err.emit(f"{type(e).__name__}: {e}")
            return
        self.finished_ok.emit(text)
