from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests
from urllib.parse import quote

# Значение по умолчанию для поля «Базовый URL» до первого сохранения настроек.
DEFAULT_LOCAL_API_BASE_URL = "http://127.0.0.1:18765"
DEFAULT_REMOTE_CDP_PORT = 1024
# Совпадает с дефолтом `antidetect … serve` (ANTIDETECT_API_TOKEN).
DEFAULT_LOCAL_API_TOKEN = "secret"

PROFILE_PREVIEW_NOT_RUNNING_MSG = (
    "Профиль не запущен.\n\n"
    "Запустите его в антидетекте (с expose_cdp) и нажмите «Просмотр» снова."
)
PROFILE_PREVIEW_CDP_NOT_READY_MSG = (
    "Профиль запущен, но CDP WebSocket ещё недоступен.\n\n"
    "Подождите несколько секунд и повторите."
)

# Процессный дефолт: выставляется из настроек (веб-API / UI).
# Пустая строка = не слать Authorization (десктоп Qt-антидетект без auth).
_default_api_token: str = ""


def set_default_local_api_token(token: str | None) -> None:
    global _default_api_token
    _default_api_token = (token or "").strip()


def get_default_local_api_token() -> str:
    return _default_api_token


def resolve_local_api_token(explicit: str | None = None) -> str:
    """Явный токен → процессный дефолт → ANTIDETECT_API_TOKEN из env."""
    tok = (explicit or "").strip()
    if tok:
        return tok
    if _default_api_token:
        return _default_api_token
    return (os.environ.get("ANTIDETECT_API_TOKEN") or "").strip()


@dataclass(frozen=True)
class RemoteCdpLaunchOptions:
    cdp_public_host: str
    cdp_port: int = DEFAULT_REMOTE_CDP_PORT
    cdp_bind: str = "all"


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

    Если задан Bearer-токен (явный / дефолт процесса / ANTIDETECT_API_TOKEN) —
    все запросы кроме публичного /health идут с Authorization.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        u = (base_url or "").strip().rstrip("/")
        if not u:
            raise LocalAntidetectError("Базовый URL локального API пуст.")
        self._base = u
        self._timeout_s = timeout_s
        self._session = requests.Session()
        tok = resolve_local_api_token(token)
        if tok:
            self._session.headers["Authorization"] = f"Bearer {tok}"

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

    def add_profile_tag(self, profile_id: str, tag: str) -> dict[str, Any]:
        pid = (profile_id or "").strip()
        t = (tag or "").strip()
        if not pid:
            raise LocalAntidetectError("profile_id пуст.")
        if not t:
            raise LocalAntidetectError("tag пуст.")
        url = f"{self._base}/profiles/{quote(pid)}/tags/{quote(t)}"
        resp = self._session.post(url, timeout=self._timeout_s)
        data = _json_body(resp)
        if resp.status_code != 200 or not isinstance(data, dict):
            raise LocalAntidetectError(f"Add tag failed: status={resp.status_code}, body={data!r}")
        return data

    def remove_profile_tag(self, profile_id: str, tag: str) -> dict[str, Any]:
        pid = (profile_id or "").strip()
        t = (tag or "").strip()
        if not pid:
            raise LocalAntidetectError("profile_id пуст.")
        if not t:
            raise LocalAntidetectError("tag пуст.")
        url = f"{self._base}/profiles/{quote(pid)}/tags/{quote(t)}"
        resp = self._session.delete(url, timeout=self._timeout_s)
        data = _json_body(resp)
        if resp.status_code != 200 or not isinstance(data, dict):
            raise LocalAntidetectError(f"Remove tag failed: status={resp.status_code}, body={data!r}")
        return data

    def get_profile(self, profile_id: str) -> dict[str, Any]:
        pid = (profile_id or "").strip()
        if not pid:
            raise LocalAntidetectError("profile_id пуст.")
        url = f"{self._base}/profiles/{quote(pid)}"
        resp = self._session.get(url, timeout=self._timeout_s)
        data = _json_body(resp)
        if resp.status_code != 200 or not isinstance(data, dict):
            raise LocalAntidetectError(f"Get profile failed: status={resp.status_code}, body={data!r}")
        return data

    def update_profile_name(self, profile_id: str, name: str) -> dict[str, Any]:
        pid = (profile_id or "").strip()
        new_name = (name or "").strip()
        if not pid:
            raise LocalAntidetectError("profile_id пуст.")
        if not new_name:
            raise LocalAntidetectError("name пуст.")
        url = f"{self._base}/profiles/{quote(pid)}"
        resp = self._session.patch(url, json={"name": new_name}, timeout=self._timeout_s)
        body = _json_body(resp)
        if resp.status_code != 200 or not isinstance(body, dict):
            raise LocalAntidetectError(
                f"Update profile name failed: status={resp.status_code}, body={body!r}"
            )
        return body

    def merge_profile_custom_data(
        self, profile_id: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        pid = (profile_id or "").strip()
        if not pid:
            raise LocalAntidetectError("profile_id пуст.")
        if not isinstance(data, dict):
            raise LocalAntidetectError("custom_data должен быть объектом.")
        url = f"{self._base}/profiles/{quote(pid)}/custom-data"
        resp = self._session.patch(url, json={"data": data}, timeout=self._timeout_s)
        body = _json_body(resp)
        if resp.status_code != 200 or not isinstance(body, dict):
            raise LocalAntidetectError(
                f"Merge custom_data failed: status={resp.status_code}, body={body!r}"
            )
        return body

    def launch_profile(
        self,
        profile_id: str,
        *,
        headless: bool = False,
        expose_cdp: bool = True,
        start_url: str = "https://studio.youtube.com/",
        remote_cdp: RemoteCdpLaunchOptions | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base}/profiles/{profile_id}/launch"
        body: dict[str, Any] = {
            "headless": bool(headless),
            "expose_cdp": bool(expose_cdp),
            "start_url": start_url,
        }
        if remote_cdp is not None:
            host = (remote_cdp.cdp_public_host or "").strip()
            if not host:
                raise LocalAntidetectError("cdp_public_host пуст для удалённого launch.")
            body["cdp_port"] = int(remote_cdp.cdp_port)
            body["cdp_bind"] = (remote_cdp.cdp_bind or "all").strip() or "all"
            body["cdp_public_host"] = host
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

    def list_sessions(self) -> list[dict[str, Any]]:
        resp = self._session.get(f"{self._base}/sessions", timeout=self._timeout_s)
        data = _json_body(resp)
        if resp.status_code != 200:
            raise LocalAntidetectError(f"List sessions failed: status={resp.status_code}, body={data!r}")
        if not isinstance(data, list):
            raise LocalAntidetectError(f"Unexpected sessions payload: {type(data).__name__}: {data!r}")
        return [x for x in data if isinstance(x, dict)]

    def resolve_running_cdp_ws_url_for_profile(
        self, profile_id: str
    ) -> tuple[str | None, str | None, str | None]:
        """
        Один проход GET /sessions (без ожидания).
        Возвращает (cdp_ws_url, session_id, сообщение для пользователя при отсутствии url).
        """
        pid = (profile_id or "").strip()
        if not pid:
            return None, None, "Не указан ID профиля."
        running_seen = False
        last_session_id: str | None = None
        for row in self.list_sessions():
            if str(row.get("profile_id") or "").strip() != pid:
                continue
            if row.get("running") is False:
                continue
            running_seen = True
            sid = str(row.get("session_id") or "").strip()
            if sid:
                last_session_id = sid
            ws = row.get("cdp_ws_url")
            if isinstance(ws, str) and ws.strip():
                return ws.strip(), sid or last_session_id, None
            if not sid:
                continue
            try:
                detail = self.get_session(sid)
            except LocalAntidetectError:
                continue
            ws2 = detail.get("cdp_ws_url")
            if isinstance(ws2, str) and ws2.strip():
                return ws2.strip(), sid, None
        if running_seen:
            return None, last_session_id, PROFILE_PREVIEW_CDP_NOT_READY_MSG
        return None, None, PROFILE_PREVIEW_NOT_RUNNING_MSG

    def find_running_session_id_for_profile(self, profile_id: str) -> str | None:
        """session_id запущенной сессии профиля (один проход GET /sessions)."""
        _ws, sid, _msg = self.resolve_running_cdp_ws_url_for_profile(profile_id)
        return sid

    def find_running_cdp_ws_url_for_profile(
        self,
        profile_id: str,
        *,
        timeout_s: float = 30.0,
        poll_s: float = 0.45,
        cancel_check: Callable[[], bool] | None = None,
    ) -> str:
        """CDP WebSocket уже запущенного профиля (без POST /launch)."""
        pid = (profile_id or "").strip()
        if not pid:
            raise LocalAntidetectError("profile_id пуст.")
        deadline = time.monotonic() + float(timeout_s)
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            if cancel_check is not None and cancel_check():
                raise LocalAntidetectError("Отменено.")
            for row in self.list_sessions():
                if str(row.get("profile_id") or "").strip() != pid:
                    continue
                if row.get("running") is False:
                    continue
                last = row
                ws = row.get("cdp_ws_url")
                if isinstance(ws, str) and ws.strip():
                    return ws.strip()
                sid = str(row.get("session_id") or "").strip()
                if sid:
                    detail = self.get_session(sid)
                    last = detail
                    ws2 = detail.get("cdp_ws_url")
                    if isinstance(ws2, str) and ws2.strip():
                        return ws2.strip()
            time.sleep(poll_s)
        raise LocalAntidetectError(
            PROFILE_PREVIEW_CDP_NOT_READY_MSG
            if last and last.get("running") is not False
            else PROFILE_PREVIEW_NOT_RUNNING_MSG
        )

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

    def refresh_cdp_ws_url(
        self,
        session_id: str,
        *,
        timeout_s: float = 15.0,
        poll_s: float = 0.45,
    ) -> str:
        """Повторный опрос cdp_ws_url активной сессии (после ECONNREFUSED и т.п.)."""
        deadline = time.monotonic() + float(timeout_s)
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            last = self.get_session(session_id)
            if last.get("running") is False:
                msg = last.get("result_message")
                raise LocalAntidetectError(
                    "Сессия завершилась при повторном опросе CDP WebSocket."
                    + (f" ({msg})" if msg else "")
                )
            ws = last.get("cdp_ws_url")
            if isinstance(ws, str) and ws.strip():
                return ws.strip()
            time.sleep(poll_s)
        raise LocalAntidetectError(
            "Таймаут повторного опроса cdp_ws_url "
            f"({timeout_s:.0f} с). Последнее состояние: {last!r}"
        )


def _strip_automation_from_tag_label(s: str) -> str:
    t = (
        (s or "")
        .replace("\ufeff", "")
        .replace("\u200b", "")
        .replace("\u200c", "")
        .replace("\u200d", "")
        .strip()
    )
    if not t:
        return ""
    t = re.sub(
        r"[\s·•,;—–\-]+автоматизация(?:[:,\s]+(да|нет))?\s*$",
        "",
        t,
        count=1,
        flags=re.IGNORECASE,
    )
    t = re.sub(
        r"[\s·•,;—–\-]+automation(?:[:,\s]+(yes|no))?\s*$",
        "",
        t,
        count=1,
        flags=re.IGNORECASE,
    )
    return t.strip(" ·•,;—–-")


def _local_profile_tag_strings(raw: dict[str, Any]) -> list[str]:
    """Теги из API (tags), без engine / device_preset и дубликатов."""
    tags: list[str] = []
    seen: set[str] = set()
    skip: set[str] = set()
    eng = raw.get("engine")
    if isinstance(eng, str) and eng.strip():
        skip.add(eng.strip().lower())
    dev = raw.get("device_preset")
    if isinstance(dev, str) and dev.strip():
        skip.add(dev.strip().lower())

    def add(s: str) -> None:
        t = _strip_automation_from_tag_label(s)
        if not t:
            return
        low = t.lower()
        if low in skip:
            return
        if low == "автоматизация" or low.startswith("автоматизация"):
            return
        if low == "automation" or low.startswith("automation"):
            return
        if low in seen:
            return
        seen.add(low)
        tags.append(t)

    raw_tags = raw.get("tags")
    if isinstance(raw_tags, list):
        for t in raw_tags:
            if isinstance(t, str) and t.strip():
                add(t.strip())
            elif isinstance(t, dict):
                bit = str(
                    t.get("name") or t.get("title") or t.get("tag") or t.get("id") or ""
                ).strip()
                if bit:
                    add(bit)
    return tags


def normalize_local_profile_for_ui(raw: dict[str, Any]) -> dict[str, object]:
    """Поля ProfileOut OpenAPI → общий dict для списка профилей (как у Dolphin)."""
    pid = str(raw.get("profile_id") or "").strip()
    name = str(raw.get("name") or "").strip() or "Без названия"
    tags = _local_profile_tag_strings(raw)

    desc_raw = raw.get("description")
    description = desc_raw.strip() if isinstance(desc_raw, str) else ""

    proxy: dict[str, object] | None = None
    server = raw.get("proxy_server")
    if isinstance(server, str) and server.strip():
        # server без auth; логин/пароль — отдельные поля API.
        proxy = {"server": server.strip()}
        user = raw.get("proxy_username")
        if isinstance(user, str) and user.strip():
            proxy["login"] = user.strip()
        pwd = raw.get("proxy_password")
        if isinstance(pwd, str) and pwd:
            proxy["password"] = pwd
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

    custom_data_raw = raw.get("custom_data")
    custom_data: dict[str, object] = (
        dict(custom_data_raw) if isinstance(custom_data_raw, dict) else {}
    )

    return {
        "id": pid,
        "browserProfileId": pid,
        "profile_id": pid,
        "name": name,
        "mainWebsite": "",
        "tags": tags,
        "description": description,
        "status": "",
        "proxy": proxy,
        "custom_data": custom_data,
    }
