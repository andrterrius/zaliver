"""Клиент OpenAI-совместимого Chat Completions API."""

from __future__ import annotations

from typing import Any

import requests


class OpenAICompatError(Exception):
    """Ошибка запроса к OpenAI-совместимому API."""


def normalize_openai_base_url(base_url: str) -> str:
    """Корень API без завершающего слэша (обычно …/v1)."""
    return (base_url or "").strip().rstrip("/")


def chat_completion(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    timeout_s: float = 120.0,
) -> str:
    """
    POST ``{base_url}/chat/completions``.
    Возвращает текст ответа ассистента.
    """
    root = normalize_openai_base_url(base_url)
    key = (api_key or "").strip()
    model_name = (model or "").strip()
    if not root:
        raise OpenAICompatError("Не задан URL эндпоинта.")
    if not key:
        raise OpenAICompatError("Не задан API key.")
    if not model_name:
        raise OpenAICompatError("Не задана модель.")
    if not messages:
        raise OpenAICompatError("Пустой список сообщений.")

    url = f"{root}/chat/completions"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "model": model_name,
        "messages": messages,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout_s)
    except requests.Timeout as e:
        raise OpenAICompatError("Таймаут запроса к ИИ.") from e
    except requests.RequestException as e:
        raise OpenAICompatError(f"Сеть: {e}") from e

    body_text = resp.text or ""
    try:
        data = resp.json()
    except Exception:
        data = None

    if resp.status_code >= 400:
        detail = ""
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                detail = str(err.get("message") or err.get("code") or "").strip()
            elif err:
                detail = str(err).strip()
            if not detail:
                detail = str(data.get("message") or "").strip()
        if not detail:
            detail = body_text.strip()[:400]
        raise OpenAICompatError(
            f"HTTP {resp.status_code}" + (f": {detail}" if detail else "")
        )

    if not isinstance(data, dict):
        raise OpenAICompatError("Ответ ИИ не JSON.")

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise OpenAICompatError("В ответе ИИ нет choices.")
    first = choices[0]
    if not isinstance(first, dict):
        raise OpenAICompatError("Некорректный элемент choices.")
    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        # Некоторые провайдеры отдают массив частей
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict):
                    t = part.get("text")
                    if isinstance(t, str):
                        parts.append(t)
            joined = "".join(parts).strip()
            if joined:
                return joined
    text = first.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    raise OpenAICompatError("Пустой текст в ответе ИИ.")
