from __future__ import annotations

import time
from typing import Any

import requests

# Значение по умолчанию для поля «Базовый URL» до первого сохранения настроек.
DEFAULT_LOCAL_API_BASE_URL = "http://127.0.0.1:18765"


class LocalAntidetectError(RuntimeError):
    pass


def _json_body(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except Exception as exc:
        raise LocalAntidetectError(
            f"Non-JSON response: status={resp.status_code}, text={resp.text[:500]!r}"
        ) from exc


class LocalAntidetectHttpAPI:
    """
    Клиент локального HTTP API (OpenAPI «Antidetect — API профилей и сессий»):
    GET /profiles, POST /profiles/{id}/launch, GET /sessions/{id}, POST …/stop.
    """

    def __init__(self, base_url: str, *, timeout_s: float = 30.0) -> None:
        u = (base_url or "").strip().rstrip("/")
        if not u:
            raise LocalAntidetectError("Базовый URL локального API пуст.")
        self._base = u
        self._timeout_s = timeout_s
        self._session = requests.Session()

    def close(self) -> None:
        self._session.close()

    def health(self) -> dict[str, Any]:
        resp = self._session.get(f"{self._base}/health", timeout=self._timeout_s)
        data = _json_body(resp)
        if resp.status_code != 200 or not isinstance(data, dict):
            raise LocalAntidetectError(f"Health failed: status={resp.status_code}, body={data!r}")
        return data

    def list_profiles(self) -> list[dict[str, Any]]:
        resp = self._session.get(f"{self._base}/profiles", timeout=self._timeout_s)
        data = _json_body(resp)
        if resp.status_code != 200:
            raise LocalAntidetectError(f"List profiles failed: status={resp.status_code}, body={data!r}")
        if not isinstance(data, list):
            raise LocalAntidetectError(f"Unexpected list payload: {type(data).__name__}: {data!r}")
        return [x for x in data if isinstance(x, dict)]

    def launch_profile(
        self,
        profile_id: str,
        *,
        headless: bool = False,
        expose_cdp: bool = True,
        start_url: str = "https://2ip.ru",
    ) -> dict[str, Any]:
        url = f"{self._base}/profiles/{profile_id}/launch"
        body: dict[str, Any] = {
            "headless": bool(headless),
            "expose_cdp": bool(expose_cdp),
            "start_url": start_url,
        }
        resp = self._session.post(url, json=body, timeout=self._timeout_s)
        data = _json_body(resp)
        if resp.status_code != 200 or not isinstance(data, dict):
            raise LocalAntidetectError(f"Launch failed: status={resp.status_code}, body={data!r}")
        return data

    def get_session(self, session_id: str) -> dict[str, Any]:
        resp = self._session.get(f"{self._base}/sessions/{session_id}", timeout=self._timeout_s)
        data = _json_body(resp)
        if resp.status_code != 200 or not isinstance(data, dict):
            raise LocalAntidetectError(f"Get session failed: status={resp.status_code}, body={data!r}")
        return data

    def stop_session(self, session_id: str) -> None:
        resp = self._session.post(
            f"{self._base}/sessions/{session_id}/stop",
            timeout=self._timeout_s,
        )
        data = _json_body(resp)
        if resp.status_code != 200 or not isinstance(data, dict):
            raise LocalAntidetectError(f"Stop session failed: status={resp.status_code}, body={data!r}")

    def wait_for_cdp_ws_url(
        self,
        session_id: str,
        *,
        timeout_s: float = 120.0,
        poll_s: float = 0.45,
    ) -> str:
        deadline = time.monotonic() + float(timeout_s)
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            last = self.get_session(session_id)
            ws = last.get("cdp_ws_url")
            if isinstance(ws, str) and ws.strip():
                return ws.strip()
            if last.get("running") is False:
                msg = last.get("result_message")
                raise LocalAntidetectError(
                    "Сессия завершилась до появления CDP WebSocket."
                    + (f" ({msg})" if msg else "")
                )
            time.sleep(poll_s)
        raise LocalAntidetectError(
            f"Таймаут ожидания cdp_ws_url ({timeout_s:.0f} с). Последнее состояние: {last!r}"
        )


def normalize_local_profile_for_ui(raw: dict[str, Any]) -> dict[str, object]:
    """Поля ProfileOut OpenAPI → общий dict для списка профилей (как у Dolphin)."""
    pid = str(raw.get("profile_id") or "").strip()
    name = str(raw.get("name") or "").strip() or "Без названия"
    tags: list[str] = []
    eng = raw.get("engine")
    if isinstance(eng, str) and eng.strip():
        tags.append(eng.strip())
    dev = raw.get("device_preset")
    if isinstance(dev, str) and dev.strip():
        tags.append(dev.strip())

    proxy: dict[str, object] | None = None
    server = raw.get("proxy_server")
    if isinstance(server, str) and server.strip():
        proxy = {}
        if raw.get("proxy_health_ok") is not None:
            lc: dict[str, object] = {
                "status": bool(raw.get("proxy_health_ok")),
            }
            ca = raw.get("proxy_health_checked_at")
            if isinstance(ca, str) and ca.strip():
                lc["createdAt"] = ca.strip()
            pm = raw.get("proxy_health_message")
            if isinstance(pm, str) and pm.strip():
                lc["ip"] = pm.strip()
            proxy["lastCheck"] = lc

    st = ""
    if raw.get("automation_enabled") is True:
        st = "Автоматизация: да"
    elif raw.get("automation_enabled") is False:
        st = "Автоматизация: нет"

    return {
        "id": pid,
        "browserProfileId": pid,
        "profile_id": pid,
        "name": name,
        "mainWebsite": "",
        "tags": tags,
        "status": st,
        "proxy": proxy,
    }
