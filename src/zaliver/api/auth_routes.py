"""Public auth endpoints (login / logout / me / user admin)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from zaliver.api.auth import AuthContext, auth_from_request, make_auth_dependency
from zaliver.api.state import AppState
from zaliver.api.users import (
    validate_locale,
    validate_password,
    validate_username,
)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class LoginResponse(BaseModel):
    token: str
    user: dict[str, Any]


class UserPublic(BaseModel):
    username: str
    locale: str
    is_admin: bool = False


class PatchMeRequest(BaseModel):
    locale: str | None = None
    password: str | None = None


class CreateUserRequest(BaseModel):
    username: str
    password: str
    locale: str = "ru"
    is_admin: bool = False


def build_auth_router(state: AppState) -> APIRouter:
    router = APIRouter(tags=["auth"])
    require_auth = make_auth_dependency(state)

    @router.post("/v1/auth/login", response_model=LoginResponse)
    def login(body: LoginRequest) -> LoginResponse:
        for gone in state.users.ensure_fresh():
            state.sessions.revoke_user(gone)
        state.sessions.revoke_unknown_users(
            {u.username for u in state.users.list_users()}
        )
        user = state.users.authenticate(body.username, body.password)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Неверный логин или пароль",
            )
        session = state.sessions.create(user.username)
        return LoginResponse(token=session.token, user=user.public_dict())

    @router.post("/v1/auth/logout")
    def logout(
        request: Request,
        _auth: AuthContext = Depends(require_auth),
    ) -> dict[str, bool]:
        ctx = auth_from_request(request)
        state.sessions.revoke(ctx.token)
        return {"ok": True}

    @router.get("/v1/auth/me", response_model=UserPublic)
    def me(
        request: Request,
        _auth: AuthContext = Depends(require_auth),
    ) -> UserPublic:
        ctx = auth_from_request(request)
        # Refresh from store in case locale changed / user was removed from file.
        fresh = state.users.get(ctx.user.username)
        if fresh is None:
            state.sessions.revoke(ctx.token)
            raise HTTPException(
                status_code=401,
                detail="Пользователь удалён или сессия недействительна",
            )
        return UserPublic(**fresh.public_dict())

    @router.patch("/v1/auth/me", response_model=UserPublic)
    def patch_me(
        body: PatchMeRequest,
        request: Request,
        _auth: AuthContext = Depends(require_auth),
    ) -> UserPublic:
        ctx = auth_from_request(request)
        user = ctx.user
        try:
            if body.locale is not None:
                user = state.users.set_locale(user.username, body.locale)
                try:
                    store = state.user_settings(user.username)
                    store.setValue("ui/locale", validate_locale(body.locale))
                    store.sync()
                except Exception:
                    pass
            if body.password is not None:
                user = state.users.set_password(user.username, body.password)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return UserPublic(**user.public_dict())

    @router.get("/v1/auth/users", response_model=list[UserPublic])
    def list_users(
        request: Request,
        _auth: AuthContext = Depends(require_auth),
    ) -> list[UserPublic]:
        ctx = auth_from_request(request)
        if not ctx.user.is_admin:
            raise HTTPException(status_code=403, detail="Только для администратора")
        return [UserPublic(**u.public_dict()) for u in state.users.list_users()]

    @router.post(
        "/v1/auth/users",
        response_model=UserPublic,
        status_code=status.HTTP_201_CREATED,
    )
    def create_user(
        body: CreateUserRequest,
        request: Request,
        _auth: AuthContext = Depends(require_auth),
    ) -> UserPublic:
        ctx = auth_from_request(request)
        if not ctx.user.is_admin:
            raise HTTPException(status_code=403, detail="Только для администратора")
        try:
            validate_username(body.username)
            validate_password(body.password)
            loc = validate_locale(body.locale)
            user = state.users.create_user(
                body.username,
                body.password,
                locale=loc,
                is_admin=bool(body.is_admin),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return UserPublic(**user.public_dict())

    @router.delete("/v1/auth/users/{username}", response_model=UserPublic)
    def delete_user(
        username: str,
        request: Request,
        _auth: AuthContext = Depends(require_auth),
    ) -> UserPublic:
        ctx = auth_from_request(request)
        if not ctx.user.is_admin:
            raise HTTPException(status_code=403, detail="Только для администратора")
        try:
            user = state.users.delete_user(username)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        state.sessions.revoke_user(user.username)
        return UserPublic(**user.public_dict())

    return router
