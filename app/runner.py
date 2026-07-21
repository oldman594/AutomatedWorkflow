from __future__ import annotations

import argparse
import platform
import re
import socket
import sys
from collections.abc import Callable
from pathlib import Path
from threading import Event as ThreadEvent
from threading import Thread
from typing import Any

import httpx

from app.config import Settings
from app.models import (
    Artifact,
    Event,
    RunnerInfo,
    RunnerLease,
    RunnerRegistration,
    Stage,
    Task,
    TaskStatus,
    utc_now,
)
from app.protocol import MessageType
from app.runner_transport import RunnerProtocolClient, capability_flags
from app.workflow import WorkflowEngine


class RunnerConnectionError(RuntimeError):
    pass


class RemoteStorage:
    def __init__(
        self,
        server: str,
        token: str,
        runner_id: str,
        client: httpx.Client | None = None,
    ) -> None:
        self.runner_id = runner_id
        self._owns_client = client is None
        self._path_prefix = "" if client is None else "/api"
        self.client = client or httpx.Client(base_url=server.rstrip("/") + "/api", timeout=30)
        self.client.headers["Authorization"] = f"Bearer {token}"
        self.message_sender: Callable[[MessageType, str | None, dict | None], object] | None = None
        self.task_cache: dict[str, Task] = {}
        self.artifact_cache: dict[str, list[Artifact]] = {}
        self.local_event_id = 0

    def set_message_sender(
        self,
        sender: Callable[[MessageType, str | None, dict | None], object],
    ) -> None:
        self.message_sender = sender

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def register(self, registration: RunnerRegistration) -> RunnerInfo:
        response = self._request(
            "POST", "/runner/register", json=registration.model_dump(mode="json")
        )
        return RunnerInfo.model_validate(response.json())

    def heartbeat(self) -> RunnerInfo:
        response = self._request("POST", f"/runner/{self.runner_id}/heartbeat")
        return RunnerInfo.model_validate(response.json())

    def lease(self) -> Task | None:
        response = self._request("POST", f"/runner/{self.runner_id}/lease")
        return RunnerLease.model_validate(response.json()).task

    def get_task(self, task_id: str) -> Task:
        if self.message_sender and task_id in self.task_cache:
            return self.task_cache[task_id]
        response = self._request("GET", f"/runner/{self.runner_id}/tasks/{task_id}")
        return Task.model_validate(response.json())

    def update_task(self, task_id: str, **fields: Any) -> Task:
        if self.message_sender and task_id in self.task_cache:
            task = self.task_cache[task_id].model_copy(update=fields)
            self.task_cache[task_id] = task
            self._emit_task_update(task_id, fields)
            return task
        payload = {
            key: value.value if hasattr(value, "value") else value for key, value in fields.items()
        }
        response = self._request(
            "PATCH",
            f"/runner/{self.runner_id}/tasks/{task_id}",
            json=payload,
        )
        task = Task.model_validate(response.json())
        self._emit_task_update(task_id, fields)
        return task

    def add_event(
        self,
        task_id: str,
        message: str,
        *,
        stage: Stage | None = None,
        level: str = "info",
        data: dict[str, Any] | None = None,
    ) -> Event:
        if self.message_sender:
            self.local_event_id += 1
            event = Event(
                id=self.local_event_id,
                task_id=task_id,
                stage=stage,
                level=level,
                message=message,
                data=data or {},
                created_at=utc_now(),
            )
            self._emit_event(event)
            return event
        response = self._request(
            "POST",
            f"/runner/{self.runner_id}/tasks/{task_id}/events",
            json={
                "message": message,
                "stage": stage.value if stage else None,
                "level": level,
                "data": data or {},
            },
        )
        event = Event.model_validate(response.json())
        return event

    def add_artifact(self, task_id: str, kind: str, content: str) -> Artifact:
        content_size = len(content.encode("utf-8"))
        if self.message_sender and content_size <= 12 * 1024 * 1024:
            artifacts = self.artifact_cache.setdefault(task_id, [])
            artifact = Artifact(
                id=len(artifacts) + 1,
                task_id=task_id,
                kind=kind,
                content=content,
                created_at=utc_now(),
            )
            artifacts.append(artifact)
            self._emit(
                MessageType.ARTIFACT,
                task_id,
                {"kind": kind, "content": content},
            )
            return artifact
        response = self._request(
            "POST",
            f"/runner/{self.runner_id}/tasks/{task_id}/artifacts",
            json={"kind": kind, "content": content},
        )
        artifact = Artifact.model_validate(response.json())
        if self.message_sender:
            self._emit(
                MessageType.ARTIFACT,
                task_id,
                {
                    "kind": kind,
                    "size": content_size,
                    "persisted": True,
                    "transport": "rest",
                },
            )
        return artifact

    def list_artifacts(self, task_id: str) -> list[Artifact]:
        if self.message_sender:
            return list(self.artifact_cache.get(task_id, []))
        response = self._request("GET", f"/runner/{self.runner_id}/tasks/{task_id}/artifacts")
        return [Artifact.model_validate(item) for item in response.json()]

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self.client.request(method, self._path_prefix + path, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPError as exc:
            detail = ""
            if isinstance(exc, httpx.HTTPStatusError):
                detail = exc.response.text[-2000:]
            raise RunnerConnectionError(f"Runner request failed: {method} {path} {detail}") from exc

    def _emit_task_update(self, task_id: str, fields: dict[str, Any]) -> None:
        status = fields.get("status")
        if isinstance(status, TaskStatus):
            status = status.value
        if status == TaskStatus.WAITING_APPROVAL:
            self._emit(
                MessageType.TASK_COMPLETED,
                task_id,
                {"awaitingApproval": True},
            )
            return
        if status == TaskStatus.FAILED:
            self._emit(
                MessageType.TASK_FAILED,
                task_id,
                {"reason": fields.get("error") or "Runner task failed"},
            )
            return
        if status == TaskStatus.CANCELLED:
            self._emit(MessageType.TASK_CANCELLED, task_id, {})
            return
        stage = fields.get("stage")
        if isinstance(stage, Stage):
            stage = stage.value
        if stage is not None or "progress" in fields or status == TaskStatus.RUNNING:
            self._emit(
                MessageType.TASK_PROGRESS,
                task_id,
                {
                    "step": stage or "running",
                    "percent": fields.get("progress"),
                    "branch": fields.get("branch"),
                },
            )

    def cache_task(self, task: Task) -> None:
        self.task_cache[task.id] = task

    def handle_protocol_message(self, envelope: object) -> None:
        message_type = getattr(envelope, "type", None)
        task_id = getattr(envelope, "task_id", None)
        if message_type == MessageType.CANCEL_TASK and task_id in self.task_cache:
            self.task_cache[task_id] = self.task_cache[task_id].model_copy(
                update={"cancel_requested": True}
            )

    def _emit_event(self, event: Event) -> None:
        self._emit(
            MessageType.TASK_LOG,
            event.task_id,
            {"level": event.level.upper(), "content": event.message},
        )
        if event.data.get("output"):
            self._emit(
                MessageType.TERMINAL_OUTPUT,
                event.task_id,
                {"stdout": str(event.data["output"])},
            )
        if event.data.get("files"):
            for path in event.data["files"]:
                self._emit(
                    MessageType.FILE_CHANGED,
                    event.task_id,
                    {"path": str(path), "operation": "modify"},
                )

    def _emit(
        self,
        message_type: MessageType,
        task_id: str | None,
        payload: dict | None,
    ) -> None:
        if self.message_sender:
            self.message_sender(message_type, task_id, payload)


class LocalRunner:
    def __init__(
        self,
        settings: Settings,
        storage: RemoteStorage,
        registration: RunnerRegistration,
        poll_seconds: float,
        heartbeat_seconds: float,
        protocol: RunnerProtocolClient | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.registration = registration
        self.poll_seconds = poll_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.stop_event = ThreadEvent()
        self.protocol = protocol
        self.engine = WorkflowEngine(settings, storage)  # type: ignore[arg-type]

    def run(self) -> None:
        if self.protocol:
            self._run_websocket()
            return
        runner = self.storage.register(self.registration)
        print(f"Local Runner registered: {runner.id} ({runner.name})", flush=True)
        heartbeat = Thread(
            target=self._heartbeat_loop,
            name="autoflow-runner-heartbeat",
            daemon=True,
        )
        heartbeat.start()
        try:
            while not self.stop_event.is_set():
                try:
                    executed = self.run_once()
                except RunnerConnectionError as exc:
                    print(str(exc), file=sys.stderr, flush=True)
                    self.stop_event.wait(self.poll_seconds)
                    continue
                if not executed:
                    self.stop_event.wait(self.poll_seconds)
        finally:
            self.stop_event.set()
            heartbeat.join(timeout=self.heartbeat_seconds + 1)
            self.storage.close()

    def stop(self) -> None:
        self.stop_event.set()
        if self.protocol:
            self.protocol.stop()

    def run_once(self) -> bool:
        task = self.storage.lease()
        if task is None:
            return False
        print(f"Leased task {task.id}: {task.title}", flush=True)
        self.storage.add_event(
            task.id,
            f"Local Runner {self.registration.id} 已领取任务并开始本地执行",
        )
        self.engine._run_guarded(task.id)
        return True

    def _run_websocket(self) -> None:
        assert self.protocol is not None
        self.protocol.start()
        while not self.stop_event.is_set() and not self.protocol.wait_until_ready(2):
            if self.protocol.thread and not self.protocol.thread.is_alive():
                raise RunnerConnectionError("WebSocket protocol thread stopped")
        print(
            f"Local Runner connected through WebSocket: {self.registration.id}",
            flush=True,
        )
        try:
            while not self.stop_event.is_set():
                task = self.protocol.next_task(timeout=1)
                if task is None:
                    continue
                print(f"Assigned task {task.id}: {task.title}", flush=True)
                self.storage.cache_task(task)
                try:
                    self.engine._run_guarded(task.id)
                finally:
                    self.protocol.task_finished(task.id)
        finally:
            self.protocol.stop()
            self.storage.close()

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.wait(self.heartbeat_seconds):
            try:
                self.storage.heartbeat()
            except RunnerConnectionError as exc:
                print(str(exc), file=sys.stderr, flush=True)


def default_runner_id() -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", socket.gethostname()).strip("-")
    return value[:100] or "local-runner"


def capabilities(settings: Settings) -> list[str]:
    values = [name for name, enabled in capability_flags().items() if enabled]
    values.extend(["shell", f"ai:{settings.provider}"])
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an AutoFlow Local Runner")
    parser.add_argument("--server", default="http://127.0.0.1:8765")
    parser.add_argument("--token", default=None)
    parser.add_argument("--id", default=default_runner_id())
    parser.add_argument("--name", default=socket.gethostname())
    parser.add_argument("--root", action="append", type=Path)
    parser.add_argument("--worktree-root", type=Path, default=Path("~/.autoflow/worktrees"))
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--heartbeat-seconds", type=float, default=10.0)
    parser.add_argument("--state-file", type=Path, default=None)
    args = parser.parse_args()

    settings = Settings()
    token = args.token or settings.runner_token
    if not token:
        raise SystemExit("AUTOFLOW_RUNNER_TOKEN or --token is required")
    roots = [path.expanduser().resolve() for path in (args.root or settings.allowed_roots)]
    settings.allowed_roots = roots
    settings.worktree_root = args.worktree_root.expanduser().resolve()
    settings.ensure_directories()
    if not settings.mock_llm and settings.route_errors():
        raise SystemExit("Invalid AI routes: " + "; ".join(settings.route_errors()))

    registration = RunnerRegistration(
        id=args.id,
        name=args.name,
        platform=platform.platform(),
        roots=[str(path) for path in roots],
        capabilities=capabilities(settings),
    )
    storage = RemoteStorage(args.server, token, registration.id)
    protocol = RunnerProtocolClient(
        args.server,
        token,
        registration,
        args.state_file or settings.worktree_root.parent / f"runner-{registration.id}-state.json",
        capability_flags(),
        max(2.0, args.heartbeat_seconds),
    )
    storage.set_message_sender(protocol.send)
    protocol.on_message = storage.handle_protocol_message
    runner = LocalRunner(
        settings,
        storage,
        registration,
        max(0.25, args.poll_seconds),
        max(2.0, args.heartbeat_seconds),
        protocol,
    )
    try:
        runner.run()
    except KeyboardInterrupt:
        runner.stop()


if __name__ == "__main__":
    main()
