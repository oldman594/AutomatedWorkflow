from __future__ import annotations

import asyncio
import io
import json
import math
import secrets
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, WebSocket
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.exc import IntegrityError

from app.auth import (
    SESSION_COOKIE,
    RunnerPrincipal,
    authenticate_runner,
    create_user_session,
    current_user,
    hash_token,
    new_runner_token,
    require_project_role,
    visible_project_ids,
)
from app.config import Settings, get_settings
from app.credentials import CredentialVault
from app.email_auth import EmailDeliveryError, SMTPVerificationSender, digest_email_code
from app.gateway import run_runner_websocket
from app.git_platform import GitPublisher
from app.models import (
    ApprovalRequest,
    DeliveryOutput,
    EmailChallenge,
    EmailCodeRequest,
    EmailCodeVerify,
    GitIntegrationCreate,
    GitIntegrationInfo,
    JobStatus,
    PermissionDecision,
    PermissionRequestRecord,
    ProjectAccess,
    ProjectCreate,
    ProjectMember,
    ProjectMemberCreate,
    ProjectRole,
    PublishRequest,
    PublishResult,
    RunnerArtifactCreate,
    RunnerEventCreate,
    RunnerInfo,
    RunnerLease,
    RunnerRegistration,
    RunnerTaskUpdate,
    RunnerTokenCreate,
    RunnerTokenIssued,
    Task,
    TaskCreate,
    TaskDetail,
    TaskStatus,
    User,
    UserCreate,
)
from app.repository import Repository, RepositoryError
from app.storage import Storage
from app.workflow import WorkflowEngine

router = APIRouter(prefix="/api")


def get_storage(request: Request) -> Storage:
    return request.app.state.storage


def get_engine(request: Request) -> WorkflowEngine:
    return request.app.state.engine


def require_runner_token(
    authorization: str | None = Header(default=None),
    runner_id_header: str | None = Header(default=None, alias="X-Runner-ID"),
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> RunnerPrincipal:
    if not runner_id_header:
        raise HTTPException(status_code=401, detail="X-Runner-ID is required")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Invalid runner token")
    project_id = authenticate_runner(storage, settings, runner_id_header, token)
    return RunnerPrincipal(runner_id_header, project_id)


def require_matching_runner(principal: RunnerPrincipal, runner_id: str) -> None:
    if principal.runner_id != runner_id:
        raise HTTPException(status_code=404, detail="Runner not found")


def runner_is_online(runner: RunnerInfo, settings: Settings) -> RunnerInfo:
    last_seen = datetime.fromisoformat(runner.last_seen)
    age = (datetime.now(UTC) - last_seen).total_seconds()
    return runner.model_copy(update={"online": age <= settings.runner_offline_seconds})


def get_runner_task(storage: Storage, runner_id: str, task_id: str) -> Task:
    try:
        task = storage.get_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc
    if task.runner_id != runner_id:
        raise HTTPException(status_code=404, detail="Task not assigned to this runner")
    return task


def authorize_task(
    storage: Storage,
    settings: Settings,
    user: User,
    task_id: str,
    minimum: ProjectRole,
) -> Task:
    try:
        task = storage.get_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc
    require_project_role(
        storage, user, task.project_id, minimum, auth_enabled=settings.auth_enabled
    )
    return task


@router.websocket("/runner/ws")
async def runner_websocket(websocket: WebSocket) -> None:
    await run_runner_websocket(
        websocket,
        websocket.app.state.storage,
        get_settings(),
    )


@router.get("/health")
def health(settings: Settings = Depends(get_settings)) -> dict[str, object]:
    return {
        "status": "ok",
        "provider": settings.provider,
        "model": settings.active_model,
        "mock_llm": settings.mock_llm,
        "api_key_configured": bool(settings.openai_api_key),
        "provider_key_configured": not settings.route_errors(),
        "runner_token_configured": bool(settings.runner_token),
        "auth_enabled": settings.auth_enabled,
        "registration_enabled": settings.registration_enabled,
        "agent_routes": settings.agent_routes,
        "quality_gate": {
            "threshold": settings.product_quality_threshold,
            "product_iterations": settings.max_product_iterations,
            "acceptance_iterations": settings.max_acceptance_iterations,
        },
    }


@router.post("/auth/email/request", response_model=EmailChallenge, status_code=202)
def request_email_code(
    payload: EmailCodeRequest,
    request: Request,
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> EmailChallenge:
    if not settings.auth_enabled:
        raise HTTPException(status_code=409, detail="Authentication is disabled")
    secret = _email_code_secret(settings)
    now = datetime.now(UTC)
    request_ip = request.client.host if request.client else "unknown"
    ip_cutoff = now - timedelta(seconds=settings.email_code_ip_window_seconds)
    if (
        storage.count_email_challenges_since(request_ip, ip_cutoff.isoformat())
        >= settings.email_code_ip_max_requests
    ):
        raise HTTPException(status_code=429, detail="验证码请求过于频繁，请稍后再试")
    latest = storage.latest_email_challenge_at(payload.email)
    if latest:
        retry_after = math.ceil(
            settings.email_code_cooldown_seconds
            - (now - datetime.fromisoformat(latest)).total_seconds()
        )
        if retry_after > 0:
            raise HTTPException(
                status_code=429,
                detail=f"请等待 {retry_after} 秒后重新发送",
                headers={"Retry-After": str(retry_after)},
            )

    challenge_id = uuid.uuid4().hex
    response = EmailChallenge(
        challenge_id=challenge_id,
        expires_in_seconds=settings.email_code_ttl_seconds,
        retry_after_seconds=settings.email_code_cooldown_seconds,
    )
    if not _email_may_authenticate(storage, payload.email, settings):
        return response

    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = (now + timedelta(seconds=settings.email_code_ttl_seconds)).isoformat()
    storage.create_email_challenge(
        challenge_id,
        payload.email,
        request_ip,
        digest_email_code(secret, challenge_id, payload.email, code),
        expires_at,
        settings.email_code_max_attempts,
    )
    try:
        SMTPVerificationSender(settings).send_code(
            payload.email, code, settings.email_code_ttl_seconds
        )
    except EmailDeliveryError as exc:
        storage.delete_email_challenge(challenge_id)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return response


@router.post("/auth/email/verify", response_model=User)
def verify_email_code(
    payload: EmailCodeVerify,
    response: Response,
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> User:
    if not settings.auth_enabled:
        raise HTTPException(status_code=409, detail="Authentication is disabled")
    secret = _email_code_secret(settings)
    valid = storage.consume_email_challenge(
        payload.challenge_id,
        payload.email,
        digest_email_code(secret, payload.challenge_id, payload.email, payload.code),
        datetime.now(UTC).isoformat(),
    )
    if not valid:
        raise HTTPException(status_code=401, detail="验证码无效或已过期")
    try:
        user, _ = storage.get_user_by_email(payload.email)
    except KeyError:
        if not settings.registration_enabled:
            raise HTTPException(status_code=401, detail="验证码无效或已过期") from None
        try:
            user = storage.create_user(
                payload.email,
                payload.email.split("@", 1)[0],
                "!email-code-only",
            )
        except IntegrityError:
            user, _ = storage.get_user_by_email(payload.email)
    if user.disabled:
        raise HTTPException(status_code=403, detail="账号已停用")
    _start_browser_session(response, storage, user, settings)
    return user


def _email_code_secret(settings: Settings) -> str:
    if not settings.email_code_secret or not settings.email_code_secret.get_secret_value():
        raise HTTPException(status_code=503, detail="邮箱验证码服务尚未配置")
    return settings.email_code_secret.get_secret_value()


def _email_may_authenticate(storage: Storage, email: str, settings: Settings) -> bool:
    try:
        user, _ = storage.get_user_by_email(email)
        return not user.disabled
    except KeyError:
        return settings.registration_enabled


def _start_browser_session(
    response: Response,
    storage: Storage,
    user: User,
    settings: Settings,
) -> None:
    token, expires_at = create_user_session(storage, user, settings)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        expires=expires_at,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )


@router.post("/auth/logout", status_code=204)
def logout(
    request: Request,
    response: Response,
    storage: Storage = Depends(get_storage),
) -> Response:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        storage.revoke_session(hash_token(token))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/auth/me", response_model=User)
def me(user: User = Depends(current_user)) -> User:
    return user


@router.post("/users", response_model=User, status_code=201)
def create_user(
    payload: UserCreate,
    user: User = Depends(current_user),
    storage: Storage = Depends(get_storage),
) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Administrator access required")
    try:
        return storage.create_user(
            payload.email,
            payload.display_name,
            "!email-code-only",
            is_admin=payload.is_admin,
        )
    except Exception as exc:
        if "UNIQUE constraint failed" in str(exc):
            raise HTTPException(status_code=409, detail="Email already exists") from exc
        raise


@router.get("/projects", response_model=list[ProjectAccess])
def list_projects(
    user: User = Depends(current_user),
    storage: Storage = Depends(get_storage),
) -> list[ProjectAccess]:
    if user.is_admin:
        projects = storage.list_user_projects(user.id)
        if projects:
            return projects
    return storage.list_user_projects(user.id)


@router.post("/projects", response_model=ProjectAccess, status_code=201)
def create_project(
    payload: ProjectCreate,
    user: User = Depends(current_user),
    storage: Storage = Depends(get_storage),
) -> ProjectAccess:
    try:
        return storage.create_project(payload.name, payload.slug, user.id)
    except Exception as exc:
        if "UNIQUE constraint failed" in str(exc):
            raise HTTPException(status_code=409, detail="Project slug already exists") from exc
        raise


@router.get("/projects/{project_id}/members", response_model=list[ProjectMember])
def list_project_members(
    project_id: str,
    user: User = Depends(current_user),
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> list[ProjectMember]:
    require_project_role(
        storage, user, project_id, ProjectRole.VIEWER, auth_enabled=settings.auth_enabled
    )
    return storage.list_project_members(project_id)


@router.put("/projects/{project_id}/members", response_model=ProjectMember)
def put_project_member(
    project_id: str,
    payload: ProjectMemberCreate,
    user: User = Depends(current_user),
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> ProjectMember:
    require_project_role(
        storage, user, project_id, ProjectRole.OWNER, auth_enabled=settings.auth_enabled
    )
    try:
        member, _ = storage.get_user_by_email(payload.email)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="User not found") from exc
    return storage.upsert_project_member(project_id, member.id, payload.role)


@router.post(
    "/projects/{project_id}/runner-tokens",
    response_model=RunnerTokenIssued,
    status_code=201,
)
def issue_runner_token(
    project_id: str,
    payload: RunnerTokenCreate,
    user: User = Depends(current_user),
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> RunnerTokenIssued:
    require_project_role(
        storage, user, project_id, ProjectRole.OWNER, auth_enabled=settings.auth_enabled
    )
    token = new_runner_token()
    created_at = storage.save_runner_token(
        payload.runner_id,
        project_id,
        payload.label,
        hash_token(token),
        user.id,
    )
    return RunnerTokenIssued(
        runner_id=payload.runner_id,
        project_id=project_id,
        token=token,
        created_at=created_at,
    )


@router.put("/projects/{project_id}/git-integration", response_model=GitIntegrationInfo)
def configure_git_integration(
    project_id: str,
    payload: GitIntegrationCreate,
    user: User = Depends(current_user),
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> GitIntegrationInfo:
    require_project_role(
        storage, user, project_id, ProjectRole.OWNER, auth_enabled=settings.auth_enabled
    )
    if urlsplit(payload.base_url).scheme != "https":
        raise HTTPException(status_code=422, detail="Git platform base URL must use HTTPS")
    try:
        encrypted = CredentialVault(settings).encrypt(payload.token)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return storage.save_git_integration(
        project_id,
        payload.provider,
        payload.base_url,
        payload.repository,
        encrypted,
        user.id,
    )


@router.get("/projects/{project_id}/git-integration", response_model=GitIntegrationInfo)
def get_git_integration(
    project_id: str,
    user: User = Depends(current_user),
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> GitIntegrationInfo:
    require_project_role(
        storage, user, project_id, ProjectRole.VIEWER, auth_enabled=settings.auth_enabled
    )
    try:
        integration, _ = storage.get_git_integration(project_id)
        return integration
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Git integration not configured") from exc


@router.get("/tasks")
def list_tasks(
    user: User = Depends(current_user),
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
):
    return storage.list_tasks(project_ids=visible_project_ids(storage, user, settings))


@router.get("/runners", response_model=list[RunnerInfo])
def list_runners(
    user: User = Depends(current_user),
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> list[RunnerInfo]:
    project_ids = visible_project_ids(storage, user, settings)
    return [
        runner_is_online(item, settings) for item in storage.list_runners(project_ids=project_ids)
    ]


@router.post("/tasks", status_code=201)
def create_task(
    payload: TaskCreate,
    user: User = Depends(current_user),
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
):
    if not payload.project_id:
        projects = storage.list_user_projects(user.id) if settings.auth_enabled else []
        payload.project_id = projects[0].id if projects else "default"
    require_project_role(
        storage,
        user,
        payload.project_id,
        ProjectRole.EDITOR,
        auth_enabled=settings.auth_enabled,
    )
    if payload.runner_id:
        try:
            runner = storage.get_runner(payload.runner_id)
        except KeyError as exc:
            raise HTTPException(status_code=422, detail="Selected Local Runner not found") from exc
        if runner.project_id != payload.project_id:
            raise HTTPException(status_code=422, detail="Runner belongs to another project")
        payload.repository = payload.repository.strip()
    else:
        try:
            repository = Repository(payload.repository, settings.allowed_roots)
            payload.repository = str(repository.path)
        except RepositoryError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    task = storage.create_task(payload, settings.active_model)
    storage.add_event(task.id, "任务已创建，当前需求将作为本次执行边界")
    return task


@router.get("/tasks/{task_id}", response_model=TaskDetail)
def get_task(
    task_id: str,
    user: User = Depends(current_user),
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> TaskDetail:
    try:
        authorize_task(storage, settings, user, task_id, ProjectRole.VIEWER)
        return TaskDetail(
            task=storage.get_task(task_id),
            events=storage.list_events(task_id),
            artifacts=storage.list_artifacts(task_id),
            permissions=storage.list_permission_requests(task_id),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc


@router.get("/tasks/{task_id}/permissions", response_model=list[PermissionRequestRecord])
def list_task_permissions(
    task_id: str,
    user: User = Depends(current_user),
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> list[PermissionRequestRecord]:
    authorize_task(storage, settings, user, task_id, ProjectRole.VIEWER)
    return storage.list_permission_requests(task_id)


@router.post("/permissions/{permission_id}/decision", response_model=PermissionRequestRecord)
def decide_permission(
    permission_id: str,
    payload: PermissionDecision,
    user: User = Depends(current_user),
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> PermissionRequestRecord:
    try:
        permission = storage.get_permission_request(permission_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Permission request not found") from exc
    authorize_task(storage, settings, user, permission.task_id, ProjectRole.OWNER)
    try:
        resolved = storage.resolve_permission_request(
            permission_id, payload.allowed, user.id, payload.reason
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if payload.allowed:
        storage.update_task(permission.task_id, status=TaskStatus.RUNNING)
        storage.renew_job(permission.task_id, permission.runner_id, settings.job_lease_seconds)
    else:
        storage.update_task(
            permission.task_id,
            status=TaskStatus.FAILED,
            error=f"Permission denied: {permission.operation}",
        )
        storage.complete_job(permission.task_id, permission.runner_id, JobStatus.FAILED)
    storage.add_event(
        permission.task_id,
        f"权限请求已{'批准' if payload.allowed else '拒绝'}：{permission.operation}",
        level="info" if payload.allowed else "warning",
        data={"permission_id": permission.id, "reason": payload.reason},
    )
    return resolved


@router.get("/tasks/{task_id}/download")
def download_task_artifacts(
    task_id: str,
    user: User = Depends(current_user),
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> Response:
    try:
        task = authorize_task(storage, settings, user, task_id, ProjectRole.VIEWER)
        artifacts = storage.list_artifacts(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc
    if task.runner_id:
        raise HTTPException(
            status_code=409,
            detail="Remote Runner artifacts remain on the user's computer",
        )
    worktree = next((item.content for item in reversed(artifacts) if item.kind == "worktree"), None)
    if not worktree:
        raise HTTPException(status_code=409, detail="Task has no generated workspace")
    try:
        repository = Repository(worktree, settings.allowed_roots)
    except RepositoryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    delivery_raw = next(
        (item.content for item in reversed(artifacts) if item.kind == "delivery"), None
    )
    if delivery_raw:
        delivery = DeliveryOutput.model_validate_json(delivery_raw)
    else:
        acceptance_raw = next(
            (item.content for item in reversed(artifacts) if item.kind == "acceptance"), "{}"
        )
        acceptance = json.loads(acceptance_raw)
        delivery = DeliveryOutput(
            summary=task.title,
            accepted=acceptance.get("accepted", False),
            acceptance_score=acceptance.get("score", 0),
            branch=task.branch,
            changed_files=repository.delivery_files(),
            notes=["Legacy task: no structured run instructions were generated"],
        )

    diff = next((item.content for item in reversed(artifacts) if item.kind == "diff"), "")
    buffer = io.BytesIO()
    total_bytes = 0
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in delivery.changed_files:
            try:
                content = repository.read_delivery_file(path)
            except RepositoryError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if content is None:
                continue
            total_bytes += len(content)
            if total_bytes > settings.max_download_bytes:
                raise HTTPException(status_code=413, detail="Artifact bundle exceeds size limit")
            archive.writestr(path, content)
        archive.writestr("DELIVERY.md", _delivery_markdown(task.title, delivery))
        archive.writestr("changes.diff", diff)
    filename = f"autoflow-{task.id}.zip"
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/tasks/{task_id}/publish", response_model=PublishResult)
def publish_task(
    task_id: str,
    payload: PublishRequest,
    user: User = Depends(current_user),
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> PublishResult:
    task = authorize_task(storage, settings, user, task_id, ProjectRole.EDITOR)
    if task.runner_id:
        raise HTTPException(
            status_code=409,
            detail="Runner task publishing must be performed on the Runner host",
        )
    if task.status not in {TaskStatus.WAITING_APPROVAL, TaskStatus.COMPLETED}:
        raise HTTPException(status_code=409, detail="Task is not ready to publish")
    artifacts = storage.list_artifacts(task_id)
    artifact_values = {artifact.kind: artifact.content for artifact in artifacts}
    if artifact_values.get("merge_request"):
        return PublishResult.model_validate_json(artifact_values["merge_request"])
    acceptance = json.loads(artifact_values.get("acceptance") or "{}")
    if not acceptance.get("accepted"):
        raise HTTPException(status_code=409, detail="Product acceptance did not pass")
    worktree = artifact_values.get("worktree")
    if not worktree:
        raise HTTPException(status_code=409, detail="Task worktree is unavailable")
    try:
        integration, encrypted_token = storage.get_git_integration(task.project_id)
        token = CredentialVault(settings).decrypt(encrypted_token)
        repository = Repository(worktree, settings.allowed_roots)
        review = json.loads(artifact_values.get("review") or "{}")
        if repository.status().strip():
            repository.commit(str(review.get("mr_title") or task.title))
        result = GitPublisher().publish(
            repository,
            integration,
            token,
            task.branch,
            payload.base_branch or artifact_values.get("base_branch") or "main",
            str(review.get("mr_title") or task.title),
            str(review.get("mr_description") or task.requirement),
        )
    except KeyError as exc:
        raise HTTPException(status_code=409, detail="Git integration not configured") from exc
    except (RepositoryError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    storage.add_artifact(task_id, "merge_request", result.model_dump_json(indent=2))
    storage.add_event(
        task_id,
        f"已创建 {result.provider.value} 合并请求：{result.url}",
        stage=task.stage,
    )
    return result


def _delivery_markdown(title: str, delivery: DeliveryOutput) -> str:
    status = "ACCEPTED" if delivery.accepted else "NEEDS HUMAN REVIEW"
    files = "\n".join(f"- `{path}`" for path in delivery.changed_files) or "- None"
    run_command = delivery.run_command or "Not provided"
    run_output = delivery.run_output or "No output captured"
    notes = "\n".join(f"- {note}" for note in delivery.notes) or "- None"
    if delivery.source_sync_requested:
        source_sync = (
            f"Applied to `{delivery.source_repository}`"
            if delivery.source_applied
            else f"Not applied: {delivery.source_apply_error or 'Unknown error'}"
        )
    else:
        source_sync = "Not requested"
    return f"""# {title}

Status: **{status}**  
Acceptance score: **{delivery.acceptance_score}/100**  
Branch: `{delivery.branch}`

## Result

{delivery.summary}

## Changed Files

{files}

## Local Workspace

{source_sync}

## Run

```sh
{run_command}
```

## Actual Output

```text
{run_output}
```

## Build And Test

Build: `{delivery.build_command or "Not provided"}`  
Test: `{delivery.test_command or "Not provided"}`

```text
{delivery.validation or "No validation output captured"}
```

## Notes

{notes}
"""


@router.post("/tasks/{task_id}/start")
def start_task(
    task_id: str,
    user: User = Depends(current_user),
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    engine: WorkflowEngine = Depends(get_engine),
):
    try:
        authorize_task(storage, settings, user, task_id, ProjectRole.EDITOR)
        return engine.start(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc
    except (ValueError, RepositoryError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/cancel")
def cancel_task(
    task_id: str,
    user: User = Depends(current_user),
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    engine: WorkflowEngine = Depends(get_engine),
):
    try:
        authorize_task(storage, settings, user, task_id, ProjectRole.EDITOR)
        return engine.cancel(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc


@router.post("/tasks/{task_id}/approve")
def approve_task(
    task_id: str,
    payload: ApprovalRequest,
    user: User = Depends(current_user),
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    engine: WorkflowEngine = Depends(get_engine),
):
    try:
        authorize_task(storage, settings, user, task_id, ProjectRole.EDITOR)
        return engine.approve(task_id, payload.action)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc
    except (ValueError, RepositoryError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/tasks/{task_id}/events")
async def stream_events(
    task_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    user: User = Depends(current_user),
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
):
    try:
        authorize_task(storage, settings, user, task_id, ProjectRole.VIEWER)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc

    async def generate():
        cursor = after
        while not await request.is_disconnected():
            events = storage.list_events(task_id, cursor)
            for event in events:
                cursor = event.id
                yield f"id: {event.id}\nevent: workflow\ndata: {event.model_dump_json()}\n\n"
            task = storage.get_task(task_id)
            yield f"event: status\ndata: {task.model_dump_json()}\n\n"
            if task.status in {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
                TaskStatus.WAITING_APPROVAL,
            }:
                break
            await asyncio.sleep(1)

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post(
    "/runner/register",
    response_model=RunnerInfo,
)
def register_runner(
    payload: RunnerRegistration,
    principal: RunnerPrincipal = Depends(require_runner_token),
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> RunnerInfo:
    require_matching_runner(principal, payload.id)
    payload = payload.model_copy(update={"project_id": principal.project_id})
    return runner_is_online(storage.upsert_runner(payload), settings)


@router.post(
    "/runner/{runner_id}/heartbeat",
    response_model=RunnerInfo,
)
def heartbeat_runner(
    runner_id: str,
    principal: RunnerPrincipal = Depends(require_runner_token),
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> RunnerInfo:
    require_matching_runner(principal, runner_id)
    try:
        return runner_is_online(storage.touch_runner(runner_id), settings)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Runner not registered") from exc


@router.post(
    "/runner/{runner_id}/lease",
    response_model=RunnerLease,
)
def lease_runner_task(
    runner_id: str,
    principal: RunnerPrincipal = Depends(require_runner_token),
    storage: Storage = Depends(get_storage),
) -> RunnerLease:
    require_matching_runner(principal, runner_id)
    try:
        storage.touch_runner(runner_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Runner not registered") from exc
    return RunnerLease(task=storage.lease_runner_task(runner_id))


@router.get(
    "/runner/{runner_id}/tasks/{task_id}",
    response_model=Task,
)
def get_runner_task_for_execution(
    runner_id: str,
    task_id: str,
    principal: RunnerPrincipal = Depends(require_runner_token),
    storage: Storage = Depends(get_storage),
) -> Task:
    require_matching_runner(principal, runner_id)
    return get_runner_task(storage, runner_id, task_id)


@router.get(
    "/runner/{runner_id}/tasks/{task_id}/artifacts",
)
def get_runner_task_artifacts(
    runner_id: str,
    task_id: str,
    principal: RunnerPrincipal = Depends(require_runner_token),
    storage: Storage = Depends(get_storage),
):
    require_matching_runner(principal, runner_id)
    get_runner_task(storage, runner_id, task_id)
    return storage.list_artifacts(task_id)


@router.patch(
    "/runner/{runner_id}/tasks/{task_id}",
    response_model=Task,
)
def update_runner_task(
    runner_id: str,
    task_id: str,
    payload: RunnerTaskUpdate,
    principal: RunnerPrincipal = Depends(require_runner_token),
    storage: Storage = Depends(get_storage),
) -> Task:
    require_matching_runner(principal, runner_id)
    get_runner_task(storage, runner_id, task_id)
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        return storage.get_task(task_id)
    return storage.update_task(task_id, **fields)


@router.post(
    "/runner/{runner_id}/tasks/{task_id}/events",
)
def add_runner_event(
    runner_id: str,
    task_id: str,
    payload: RunnerEventCreate,
    principal: RunnerPrincipal = Depends(require_runner_token),
    storage: Storage = Depends(get_storage),
):
    require_matching_runner(principal, runner_id)
    get_runner_task(storage, runner_id, task_id)
    return storage.add_event(
        task_id,
        payload.message,
        stage=payload.stage,
        level=payload.level,
        data=payload.data,
    )


@router.post(
    "/runner/{runner_id}/tasks/{task_id}/artifacts",
)
def add_runner_artifact(
    runner_id: str,
    task_id: str,
    payload: RunnerArtifactCreate,
    principal: RunnerPrincipal = Depends(require_runner_token),
    storage: Storage = Depends(get_storage),
):
    require_matching_runner(principal, runner_id)
    get_runner_task(storage, runner_id, task_id)
    return storage.add_artifact(task_id, payload.kind, payload.content)
