from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


class DolphinAntyError(RuntimeError):
    pass


def _normalize_bearer_token(token: str) -> str:
    """
    Users sometimes paste the full header value ("Bearer ...") into the token field.
    Dolphin Public API expects only the JWT in Authorization: Bearer <jwt>.
    """
    tok = (token or "").strip()
    low = tok.lower()
    if low.startswith("bearer "):
        return tok[7:].strip()
    return tok


@dataclass(frozen=True)
class DolphinAutomationConnection:
    port: int
    ws_endpoint: str

    def ws_url(self, host: str = "127.0.0.1") -> str:
        return f"ws://{host}:{self.port}{self.ws_endpoint}"

    def http_url(self, host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self.port}"


class DolphinAntyLocalAPI:
    """
    Minimal wrapper for Dolphin{anty} Local API.

    Notes:
    - Dolphin app must be running on the same machine.
    - Default local API base is http://127.0.0.1:3001/v1.0
    """

    def __init__(self, base_url: str = "http://127.0.0.1:3001/v1.0", timeout_s: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._session = requests.Session()

    def close(self) -> None:
        self._session.close()

    def login_with_token(self, token: str) -> None:
        url = f"{self._base_url}/auth/login-with-token"
        resp = self._session.post(url, json={"token": token}, timeout=self._timeout_s)
        data = _safe_json(resp)
        if resp.status_code != 200 or not data.get("success", False):
            raise DolphinAntyError(f"Token auth failed: status={resp.status_code}, body={data!r}")

        # Dolphin{anty} Local API: successful login returns only {"success": true} (no JWT in body).
        # Authorization for subsequent requests is carried by the session cookie on this Session.
        self._session.headers.pop("Authorization", None)

    def start_profile(self, profile_id: str, *, automation: bool = True, headless: bool = False) -> DolphinAutomationConnection:
        automation_q = "1" if automation else "0"
        headless_q = "1" if headless else "0"
        url = f"{self._base_url}/browser_profiles/{profile_id}/start?automation={automation_q}&headless={headless_q}"
        resp = self._session.get(url, timeout=self._timeout_s)
        data = _safe_json(resp)

        if resp.status_code != 200 or not data.get("success", False):
            raise DolphinAntyError(f"Start profile failed: status={resp.status_code}, body={data!r}")

        automation_data = data.get("automation") or {}
        port = automation_data.get("port")
        ws_endpoint = automation_data.get("wsEndpoint")
        if not isinstance(port, int) or not isinstance(ws_endpoint, str) or not ws_endpoint:
            raise DolphinAntyError(f"Unexpected automation payload: {automation_data!r}")

        return DolphinAutomationConnection(port=port, ws_endpoint=ws_endpoint)

    def list_profiles(self, *, page: int = 1, limit: int = 200, query: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"page": int(page), "limit": int(limit)}
        if query:
            params["query"] = str(query)
        url = f"{self._base_url}/browser_profiles"
        resp = self._session.get(url, params=params, timeout=self._timeout_s)
        data = _safe_json(resp)
        if resp.status_code != 200 or "data" not in data:
            raise DolphinAntyError(f"List profiles failed: status={resp.status_code}, body={data!r}")
        items = data.get("data")
        if not isinstance(items, list):
            raise DolphinAntyError(f"Unexpected list payload: {items!r}")
        return [it for it in items if isinstance(it, dict)]

    def stop_profile(self, profile_id: str) -> None:
        url = f"{self._base_url}/browser_profiles/{profile_id}/stop"
        resp = self._session.get(url, timeout=self._timeout_s)
        data = _safe_json(resp)
        if resp.status_code != 200 or not data.get("success", False):
            raise DolphinAntyError(f"Stop profile failed: status={resp.status_code}, body={data!r}")


class DolphinAntyPublicAPI:
    """
    Dolphin{anty} Public (cloud) API.

    Spec excerpt (OpenAPI "Dolphin{anty} Public API"):
    - Host is fixed to https://dolphin-anty-api.com
    - Authorization: Bearer <JWT token> in Authorization header.
    """

    _PUBLIC_BASE_URL = "https://dolphin-anty-api.com"

    def __init__(self, token: str, timeout_s: float = 30.0) -> None:
        self._base_url = self._PUBLIC_BASE_URL.rstrip("/")
        self._timeout_s = timeout_s
        self._session = requests.Session()
        tok = _normalize_bearer_token(token)
        if not tok:
            raise DolphinAntyError("Public API token is empty.")
        self._session.headers.update({"Authorization": f"Bearer {tok}"})

    def close(self) -> None:
        self._session.close()

    def list_profiles(self, *, page: int = 1, limit: int = 200, query: str | None = None) -> list[dict[str, Any]]:
        lim = int(limit)
        if lim < 1:
            lim = 50
        # Public API schema: limit maximum is 100.
        lim = min(lim, 100)
        params: dict[str, Any] = {"page": int(page), "limit": lim}
        if query:
            params["query"] = str(query)
        url = f"{self._base_url}/browser_profiles"
        resp = self._session.get(url, params=params, timeout=self._timeout_s)
        data = _safe_json(resp)
        # Typical payload shape: {"data": [...], ...}
        if resp.status_code != 200 or "data" not in data:
            raise DolphinAntyError(f"Public list profiles failed: status={resp.status_code}, body={data!r}")
        items = data.get("data")
        if not isinstance(items, list):
            raise DolphinAntyError(f"Unexpected public list payload: {items!r}")
        return [it for it in items if isinstance(it, dict)]


def _safe_json(resp: requests.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except Exception:
        raise DolphinAntyError(f"Non-JSON response: status={resp.status_code}, text={resp.text[:500]!r}") from None
    if not isinstance(data, dict):
        raise DolphinAntyError(f"Unexpected JSON type: {type(data).__name__}: {data!r}")
    return data
