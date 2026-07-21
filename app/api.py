from __future__ import annotations

import asyncio
import io
import json
import zipfile
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, WebSocket
from fastapi.responses import Response, StreamingResponse

from app.auth import (
    PASSWORD_HASH,
    SESSION_COOKIE,
    RunnerPrincipal,
    authenticate_runner,
    authenticate_user,
    create_user_session,
    current_user,
    hash_token,
    new_runner_token,
    require_project_role,
    visible_project_ids,
)
from app.config import Settings, get_settings
from app.gateway import run_runner_websocket
from app.models import (
    ApprovalRequest,
    DeliveryOutput,
    LoginRequest,
    ProjectAccess,
    ProjectCreate,
    ProjectMember,
    ProjectMemberCreate,
    ProjectRole,
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
        "agent_routes": settings.agent_routes,
        "quality_gate": {
            "threshold": settings.product_quality_threshold,
            "product_iterations": settings.max_product_iterations,
            "acceptance_iterations": settings.max_acceptance_iterations,
        },
    }


@router.post("/auth/login", response_model=User)
def login(
    payload: LoginRequest,
    response: Response,
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> User:
    if not settings.auth_enabled:
        raise HTTPException(status_code=409, detail="Authentication is disabled")
    user = authenticate_user(storage, payload.email, payload.password)
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
    return user


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
            PASSWORD_HASH.hash(payload.password),
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
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc


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
