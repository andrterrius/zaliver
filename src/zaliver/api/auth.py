"""Session-based authentication (login/password → Bearer session token)."""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from zaliver.api.state import AppState
from zaliver.api.users import UserRecord

_bearer = HTTPBearer(auto_error=False)


class AuthContext:
    __slots__ = ("user", "token")

    def __init__(self, user: UserRecord, token: str) -> None:
        self.user = user
        self.token = token


def make_auth_dependency(state: AppState):
    def _auth(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    ) -> AuthContext:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Требуется авторизация",
                headers={"WWW-Authenticate": "Bearer"},
            )
        provided = (credentials.credentials or "").strip()
        if not provided:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Требуется авторизация",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Optional legacy single-token mode for automation (env ZALIVER_API_TOKEN).
        legacy = (state.config.api_token or "").strip()
        if legacy and secrets.compare_digest(provided, legacy):
            admin = state.users.get("admin") or next(
                (u for u in state.users.list_users() if u.is_admin),
                None,
            )
            if admin is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid API token",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            ctx = AuthContext(user=admin, token=provided)
            request.state.zaliver_auth = ctx  # type: ignore[attr-defined]
            return ctx

        session = state.sessions.get(provided)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Сессия недействительна или истекла",
                headers={"WWW-Authenticate": "Bearer"},
            )
        # Pick up users.json edits (delete/rename) without restarting the API.
        for gone in state.users.ensure_fresh():
            state.sessions.revoke_user(gone)
        state.sessions.revoke_unknown_users(
            {u.username for u in state.users.list_users()}
        )
        user = state.users.get(session.username)
        if user is None:
            state.sessions.revoke_user(session.username)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Пользователь не найден",
                headers={"WWW-Authenticate": "Bearer"},
            )
        ctx = AuthContext(user=user, token=provided)
        request.state.zaliver_auth = ctx  # type: ignore[attr-defined]
        return ctx

    return _auth


def auth_from_request(request: Request) -> AuthContext:
    ctx = getattr(request.state, "zaliver_auth", None)
    if ctx is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Требуется авторизация",
        )
    return ctx
