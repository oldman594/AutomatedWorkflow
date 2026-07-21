from __future__ import annotations

import asyncio
import io
import json
import secrets
import zipfile
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, WebSocket
from fastapi.responses import Response, StreamingResponse

from app.config import Settings, get_settings
from app.gateway import run_runner_websocket
from app.models import (
    ApprovalRequest,
    DeliveryOutput,
    RunnerArtifactCreate,
    RunnerEventCreate,
    RunnerInfo,
    RunnerLease,
    RunnerRegistration,
    RunnerTaskUpdate,
    Task,
    TaskCreate,
    TaskDetail,
    TaskStatus,
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
    settings: Settings = Depends(get_settings),
) -> None:
    if not settings.runner_token:
        raise HTTPException(status_code=503, detail="Local Runner is not configured")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(
        token, settings.runner_token
    ):
        raise HTTPException(status_code=401, detail="Invalid runner token")


def runner_is_online(runner: RunnerInfo, settings: Settings) -> RunnerInfo:
    last_seen = datetime.fromisoformat(runner.last_seen)
    age = (datetime.now(timezone.utc) - last_seen).total_seconds()
    return runner.model_copy(update={"online": age <= settings.runner_offline_seconds})


def get_runner_task(storage: Storage, runner_id: str, task_id: str) -> Task:
    try:
        task = storage.get_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc
    if task.runner_id != runner_id:
        raise HTTPException(status_code=404, detail="Task not assigned to this runner")
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
        "agent_routes": settings.agent_routes,
        "quality_gate": {
            "threshold": settings.product_quality_threshold,
            "product_iterations": settings.max_product_iterations,
            "acceptance_iterations": settings.max_acceptance_iterations,
        },
    }


@router.get("/tasks")
def list_tasks(storage: Storage = Depends(get_storage)):
    return storage.list_tasks()


@router.get("/runners", response_model=list[RunnerInfo])
def list_runners(
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> list[RunnerInfo]:
    return [runner_is_online(item, settings) for item in storage.list_runners()]


@router.post("/tasks", status_code=201)
def create_task(
    payload: TaskCreate,
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
):
    if payload.runner_id:
        try:
            storage.get_runner(payload.runner_id)
        except KeyError as exc:
            raise HTTPException(status_code=422, detail="Selected Local Runner not found") from exc
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
def get_task(task_id: str, storage: Storage = Depends(get_storage)) -> TaskDetail:
    try:
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
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> Response:
    try:
        task = storage.get_task(task_id)
        artifacts = storage.list_artifacts(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc
    if task.runner_id:
        raise HTTPException(
            status_code=409,
            detail="Remote Runner artifacts remain on the user's computer",
        )
    worktree = next(
        (item.content for item in reversed(artifacts) if item.kind == "worktree"), None
    )
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

    diff = next(
        (item.content for item in reversed(artifacts) if item.kind == "diff"), ""
    )
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
def start_task(task_id: str, engine: WorkflowEngine = Depends(get_engine)):
    try:
        return engine.start(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc
    except (ValueError, RepositoryError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str, engine: WorkflowEngine = Depends(get_engine)):
    try:
        return engine.cancel(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc


@router.post("/tasks/{task_id}/approve")
def approve_task(
    task_id: str,
    payload: ApprovalRequest,
    engine: WorkflowEngine = Depends(get_engine),
):
    try:
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
    storage: Storage = Depends(get_storage),
):
    try:
        storage.get_task(task_id)
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
                TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED,
                TaskStatus.WAITING_APPROVAL,
            }:
                break
            await asyncio.sleep(1)

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post(
    "/runner/register",
    response_model=RunnerInfo,
    dependencies=[Depends(require_runner_token)],
)
def register_runner(
    payload: RunnerRegistration,
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> RunnerInfo:
    return runner_is_online(storage.upsert_runner(payload), settings)


@router.post(
    "/runner/{runner_id}/heartbeat",
    response_model=RunnerInfo,
    dependencies=[Depends(require_runner_token)],
)
def heartbeat_runner(
    runner_id: str,
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> RunnerInfo:
    try:
        return runner_is_online(storage.touch_runner(runner_id), settings)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Runner not registered") from exc


@router.post(
    "/runner/{runner_id}/lease",
    response_model=RunnerLease,
    dependencies=[Depends(require_runner_token)],
)
def lease_runner_task(
    runner_id: str,
    storage: Storage = Depends(get_storage),
) -> RunnerLease:
    try:
        storage.touch_runner(runner_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Runner not registered") from exc
    return RunnerLease(task=storage.lease_runner_task(runner_id))


@router.get(
    "/runner/{runner_id}/tasks/{task_id}",
    response_model=Task,
    dependencies=[Depends(require_runner_token)],
)
def get_runner_task_for_execution(
    runner_id: str,
    task_id: str,
    storage: Storage = Depends(get_storage),
) -> Task:
    return get_runner_task(storage, runner_id, task_id)


@router.get(
    "/runner/{runner_id}/tasks/{task_id}/artifacts",
    dependencies=[Depends(require_runner_token)],
)
def get_runner_task_artifacts(
    runner_id: str,
    task_id: str,
    storage: Storage = Depends(get_storage),
):
    get_runner_task(storage, runner_id, task_id)
    return storage.list_artifacts(task_id)


@router.patch(
    "/runner/{runner_id}/tasks/{task_id}",
    response_model=Task,
    dependencies=[Depends(require_runner_token)],
)
def update_runner_task(
    runner_id: str,
    task_id: str,
    payload: RunnerTaskUpdate,
    storage: Storage = Depends(get_storage),
) -> Task:
    get_runner_task(storage, runner_id, task_id)
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        return storage.get_task(task_id)
    return storage.update_task(task_id, **fields)


@router.post(
    "/runner/{runner_id}/tasks/{task_id}/events",
    dependencies=[Depends(require_runner_token)],
)
def add_runner_event(
    runner_id: str,
    task_id: str,
    payload: RunnerEventCreate,
    storage: Storage = Depends(get_storage),
):
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
    dependencies=[Depends(require_runner_token)],
)
def add_runner_artifact(
    runner_id: str,
    task_id: str,
    payload: RunnerArtifactCreate,
    storage: Storage = Depends(get_storage),
):
    get_runner_task(storage, runner_id, task_id)
    return storage.add_artifact(task_id, payload.kind, payload.content)
