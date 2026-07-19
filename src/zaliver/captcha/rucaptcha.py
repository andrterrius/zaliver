"""Клиент RuCaptcha (api.rucaptcha.com) — Proxyless reCAPTCHA и ImageToText."""

from __future__ import annotations

import time
from typing import Any, Callable

import requests

RUCAPTCHA_CREATE_URL = "https://api.rucaptcha.com/createTask"
RUCAPTCHA_RESULT_URL = "https://api.rucaptcha.com/getTaskResult"

_POLL_INTERVAL_S = 5.0
_DEFAULT_TIMEOUT_S = 180.0


class RuCaptchaError(RuntimeError):
    """Ошибка API RuCaptcha или решения капчи."""

    @property
    def error_code(self) -> str:
        text = str(self)
        for code in (
            "ERROR_CAPTCHA_UNSOLVABLE",
            "ERROR_NO_SLOT_AVAILABLE",
            "ERROR_ZERO_BALANCE",
            "ERROR_WRONG_USER_KEY",
            "ERROR_KEY_DOES_NOT_EXIST",
        ):
            if code in text:
                return code
        return ""


def _post_json(url: str, payload: dict[str, Any], *, timeout_s: float = 60.0) -> dict:
    try:
        resp = requests.post(url, json=payload, timeout=timeout_s)
    except requests.Timeout as e:
        raise RuCaptchaError(f"RuCaptcha timeout: {url}") from e
    except requests.RequestException as e:
        raise RuCaptchaError(f"RuCaptcha request failed: {e}") from e
    try:
        data = resp.json()
    except Exception as e:
        raise RuCaptchaError(
            f"RuCaptcha: не JSON ответ (HTTP {resp.status_code}): {resp.text[:300]!r}"
        ) from e
    if not isinstance(data, dict):
        raise RuCaptchaError(f"RuCaptcha: неожиданный ответ: {data!r}")
    return data


def create_recaptcha_v2_task(
    api_key: str,
    *,
    website_url: str,
    website_key: str,
    is_invisible: bool = False,
    is_enterprise: bool = True,
    api_domain: str | None = None,
    enterprise_payload: dict[str, Any] | None = None,
) -> int:
    """Создать Proxyless-задачу reCAPTCHA v2 / Enterprise. Возвращает taskId."""
    key = (api_key or "").strip()
    if not key:
        raise RuCaptchaError("RuCaptcha: пустой API key.")
    sitekey = (website_key or "").strip()
    page_url = (website_url or "").strip()
    if not sitekey:
        raise RuCaptchaError("RuCaptcha: пустой websiteKey (sitekey).")
    if not page_url:
        raise RuCaptchaError("RuCaptcha: пустой websiteURL.")

    task_type = (
        "RecaptchaV2EnterpriseTaskProxyless"
        if is_enterprise
        else "RecaptchaV2TaskProxyless"
    )
    task: dict[str, Any] = {
        "type": task_type,
        "websiteURL": page_url,
        "websiteKey": sitekey,
        "isInvisible": bool(is_invisible),
    }
    domain = (api_domain or "").strip()
    if domain:
        task["apiDomain"] = domain
    if is_enterprise and enterprise_payload:
        task["enterprisePayload"] = enterprise_payload

    data = _post_json(
        RUCAPTCHA_CREATE_URL,
        {"clientKey": key, "task": task},
    )
    err_id = int(data.get("errorId") or 0)
    if err_id != 0:
        raise RuCaptchaError(
            f"RuCaptcha createTask errorId={err_id}: "
            f"{data.get('errorCode')!r} {data.get('errorDescription')!r}"
        )
    task_id = data.get("taskId")
    if task_id is None:
        raise RuCaptchaError(f"RuCaptcha createTask: нет taskId в ответе: {data!r}")
    return int(task_id)


def wait_task_result(
    api_key: str,
    task_id: int,
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    poll_interval_s: float = _POLL_INTERVAL_S,
) -> str:
    """Ждать ready и вернуть gRecaptchaResponse / token / text."""
    key = (api_key or "").strip()
    if not key:
        raise RuCaptchaError("RuCaptcha: пустой API key.")
    deadline = time.monotonic() + max(30.0, float(timeout_s))
    while True:
        data = _post_json(
            RUCAPTCHA_RESULT_URL,
            {"clientKey": key, "taskId": int(task_id)},
        )
        err_id = int(data.get("errorId") or 0)
        if err_id != 0:
            raise RuCaptchaError(
                f"RuCaptcha getTaskResult errorId={err_id}: "
                f"{data.get('errorCode')!r} {data.get('errorDescription')!r}"
            )
        status = (data.get("status") or "").strip().lower()
        if status == "ready":
            solution = data.get("solution") or {}
            if not isinstance(solution, dict):
                raise RuCaptchaError(f"RuCaptcha: странный solution: {solution!r}")
            token = (
                (
                    solution.get("gRecaptchaResponse")
                    or solution.get("token")
                    or solution.get("text")
                    or ""
                )
                .strip()
            )
            if not token:
                raise RuCaptchaError(f"RuCaptcha: пустой токен/text в solution: {data!r}")
            return token
        if status and status != "processing":
            raise RuCaptchaError(f"RuCaptcha: неожиданный status={status!r}: {data!r}")
        if time.monotonic() >= deadline:
            raise RuCaptchaError(
                f"RuCaptcha: задача {task_id} не готова за {timeout_s:.0f} с."
            )
        time.sleep(max(2.0, float(poll_interval_s)))


def create_image_to_text_task(
    api_key: str,
    *,
    body_base64: str,
    numeric: int = 1,
    case: bool = False,
    min_length: int = 0,
    max_length: int = 0,
    comment: str | None = None,
) -> int:
    """Создать ImageToTextTask. Возвращает taskId."""
    key = (api_key or "").strip()
    if not key:
        raise RuCaptchaError("RuCaptcha: пустой API key.")
    body = (body_base64 or "").strip()
    if body.startswith("data:") and "," in body:
        body = body.split(",", 1)[1].strip()
    body = "".join(body.split())
    if not body:
        raise RuCaptchaError("RuCaptcha: пустой body (base64 изображения).")

    task: dict[str, Any] = {
        "type": "ImageToTextTask",
        "body": body,
        "phrase": False,
        "case": bool(case),
        "numeric": int(numeric),
        "math": False,
    }
    if int(min_length) > 0:
        task["minLength"] = int(min_length)
    if int(max_length) > 0:
        task["maxLength"] = int(max_length)
    cmt = (comment or "").strip()
    if cmt:
        task["comment"] = cmt

    data = _post_json(
        RUCAPTCHA_CREATE_URL,
        {"clientKey": key, "task": task},
    )
    err_id = int(data.get("errorId") or 0)
    if err_id != 0:
        raise RuCaptchaError(
            f"RuCaptcha createTask errorId={err_id}: "
            f"{data.get('errorCode')!r} {data.get('errorDescription')!r}"
        )
    task_id = data.get("taskId")
    if task_id is None:
        raise RuCaptchaError(f"RuCaptcha createTask: нет taskId в ответе: {data!r}")
    return int(task_id)


def solve_image_to_text(
    api_key: str,
    *,
    body_base64: str,
    numeric: int = 1,
    case: bool = False,
    min_length: int = 4,
    max_length: int = 6,
    comment: str | None = "цифры с картинки",
    retries: int = 2,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    log: Callable[[str], None] | None = None,
) -> str:
    """
    Решить обычную картинку-капчу (ImageToTextTask).

    По умолчанию ждём только цифры (numeric=1), длина 4–6.
    """

    def _log(msg: str) -> None:
        if callable(log):
            try:
                log(msg)
            except Exception:
                pass

    attempts = max(1, int(retries))
    last_err: RuCaptchaError | None = None
    for attempt in range(1, attempts + 1):
        _log(f"RuCaptcha: ImageToTextTask попытка {attempt}/{attempts}…")
        try:
            task_id = create_image_to_text_task(
                api_key,
                body_base64=body_base64,
                numeric=numeric,
                case=case,
                min_length=min_length,
                max_length=max_length,
                comment=comment,
            )
            text = wait_task_result(api_key, task_id, timeout_s=timeout_s)
            _log(f"RuCaptcha: текст капчи получен (попытка {attempt}/{attempts}).")
            return text
        except RuCaptchaError as e:
            last_err = e
            _log(f"RuCaptcha: ImageToText попытка {attempt}/{attempts} неудачна: {e}")
    if last_err is not None:
        raise last_err
    raise RuCaptchaError("RuCaptcha: не удалось решить image captcha.")


def solve_recaptcha_v2_proxyless(
    api_key: str,
    *,
    website_url: str,
    website_key: str,
    is_invisible: bool = False,
    is_enterprise: bool = True,
    api_domain: str | None = None,
    enterprise_payload: dict[str, Any] | None = None,
    retries: int = 2,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    log: Callable[[str], None] | None = None,
) -> str:
    """
    Решить reCAPTCHA через Proxyless (без прокси и без userAgent).

    По умолчанию 2 попытки ``RecaptchaV2EnterpriseTaskProxyless``.
    """

    def _log(msg: str) -> None:
        if callable(log):
            try:
                log(msg)
            except Exception:
                pass

    attempts = max(1, int(retries))
    task_name = (
        "RecaptchaV2EnterpriseTaskProxyless"
        if is_enterprise
        else "RecaptchaV2TaskProxyless"
    )
    last_err: RuCaptchaError | None = None
    for attempt in range(1, attempts + 1):
        _log(f"RuCaptcha: {task_name} попытка {attempt}/{attempts}…")
        try:
            task_id = create_recaptcha_v2_task(
                api_key,
                website_url=website_url,
                website_key=website_key,
                is_invisible=is_invisible,
                is_enterprise=is_enterprise,
                api_domain=api_domain,
                enterprise_payload=enterprise_payload,
            )
            token = wait_task_result(api_key, task_id, timeout_s=timeout_s)
            _log(f"RuCaptcha: токен получен (попытка {attempt}/{attempts}).")
            return token
        except RuCaptchaError as e:
            last_err = e
            _log(f"RuCaptcha: попытка {attempt}/{attempts} неудачна: {e}")
    if last_err is not None:
        raise last_err
    raise RuCaptchaError("RuCaptcha: не удалось решить капчу.")
