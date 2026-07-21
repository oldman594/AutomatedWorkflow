from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, Request, status

from app.config import Settings, get_settings
from app.models import EmailCodeRequest, ProjectRole, User
from app.storage import Storage

SESSION_COOKIE = "autoflow_session"
ROLE_LEVEL = {
    ProjectRole.VIEWER: 1,
    ProjectRole.EDITOR: 2,
    ProjectRole.OWNER: 3,
}


@dataclass(frozen=True, slots=True)
class RunnerPrincipal:
    runner_id: str
    project_id: str


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session_token() -> str:
    return "afs_" + secrets.token_urlsafe(48)


def new_runner_token() -> str:
    return "afr_" + secrets.token_urlsafe(48)


def initialize_identity(storage: Storage, settings: Settings) -> None:
    if not settings.auth_enabled or storage.count_users() > 0:
        return
    if not settings.bootstrap_admin_email:
        return
    email = EmailCodeRequest(email=settings.bootstrap_admin_email).email
    user = storage.create_user(
        email,
        email.split("@", 1)[0],
        "!email-code-only",
        is_admin=True,
    )
    storage.upsert_project_member("default", user.id, ProjectRole.OWNER)


def create_user_session(storage: Storage, user: User, settings: Settings) -> tuple[str, datetime]:
    token = new_session_token()
    expires_at = datetime.now(UTC) + timedelta(hours=settings.auth_session_hours)
    storage.create_session(hash_token(token), user.id, expires_at.isoformat())
    return token, expires_at


def current_user(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> User:
    if not settings.auth_enabled:
        return User(
            id="local",
            email="local@autoflow.invalid",
            display_name="Local Administrator",
            is_admin=True,
            created_at=datetime.now(UTC).isoformat(),
        )
    storage: Storage = request.app.state.storage
    if storage.count_users() == 0:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not initialized; configure bootstrap admin credentials",
        )
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        authorization = request.headers.get("authorization", "")
        scheme, _, bearer = authorization.partition(" ")
        token = bearer if scheme.lower() == "bearer" else None
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        return storage.get_session_user(hash_token(token), datetime.now(UTC).isoformat())
    except KeyError as exc:
        raise HTTPException(status_code=401, detail="Session is invalid or expired") from exc


def require_project_role(
    storage: Storage,
    user: User,
    project_id: str,
    minimum: ProjectRole,
    *,
    auth_enabled: bool = True,
) -> ProjectRole:
    if not auth_enabled or user.is_admin:
        return ProjectRole.OWNER
    try:
        role = storage.get_project_role(project_id, user.id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc
    if ROLE_LEVEL[role] < ROLE_LEVEL[minimum]:
        raise HTTPException(status_code=403, detail="Insufficient project permission")
    return role


def visible_project_ids(storage: Storage, user: User, settings: Settings) -> list[str] | None:
    if not settings.auth_enabled or user.is_admin:
        return None
    return [project.id for project in storage.list_user_projects(user.id)]


def authenticate_runner(
    storage: Storage,
    settings: Settings,
    runner_id: str,
    token: str,
) -> str:
    try:
        return storage.verify_runner_token(runner_id, hash_token(token))
    except KeyError:
        if storage.has_active_runner_token(runner_id):
            raise HTTPException(status_code=401, detail="Invalid Runner token") from None
        if settings.runner_token and secrets.compare_digest(token, settings.runner_token):
            return "default"
        raise HTTPException(status_code=401, detail="Invalid Runner token") from None
