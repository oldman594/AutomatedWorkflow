from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

from fastapi import HTTPException, WebSocket, WebSocketDisconnect

from app.auth import authenticate_runner
from app.config import Settings
from app.models import JobStatus, RunnerRegistration, Stage, TaskStatus
from app.observability import RUNNER_CONNECTIONS
from app.protocol import MessageEnvelope, MessageType
from app.storage import Storage

STEP_STAGES = {
    "clone": Stage.READER,
    "checkout": Stage.READER,
    "generate": Stage.CODER,
    "build": Stage.BUILD,
    "test": Stage.TEST,
    "commit": Stage.DELIVERY,
    "push": Stage.DELIVERY,
}


class RunnerGatewaySession:
    def __init__(
        self,
        websocket: WebSocket,
        storage: Storage,
        settings: Settings,
        expected_runner_id: str,
        project_id: str,
    ) -> None:
        self.websocket = websocket
        self.storage = storage
        self.settings = settings
        self.expected_runner_id = expected_runner_id
        self.project_id = project_id
        self.runner_id: str | None = None
        self.cancel_sent: set[str] = set()

    async def run(self) -> None:
        first = MessageEnvelope.model_validate_json(
            await asyncio.wait_for(self.websocket.receive_text(), timeout=15)
        )
        if first.type == MessageType.REGISTER:
            await self._register(first)
        elif first.type == MessageType.RECONNECT:
            await self._reconnect(first)
        else:
            await self.websocket.close(code=1003, reason="Register or Reconnect required")
            return

        while True:
            await self._dispatch_control_messages()
            try:
                raw = await asyncio.wait_for(self.websocket.receive_text(), timeout=1.0)
            except TimeoutError:
                continue
            envelope = MessageEnvelope.model_validate_json(raw)
            await self._handle(envelope)

    async def _register(self, envelope: MessageEnvelope) -> None:
        payload = envelope.payload
        runner_id = payload.get("runnerId") or envelope.runner_id
        if not runner_id:
            await self.websocket.close(code=1003, reason="runnerId is required")
            raise WebSocketDisconnect(code=1003)
        if runner_id != self.expected_runner_id:
            await self.websocket.close(code=1008, reason="runnerId mismatch")
            raise WebSocketDisconnect(code=1008)
        self.runner_id = runner_id
        registration = RunnerRegistration(
            id=runner_id,
            name=payload.get("hostname") or payload.get("name") or runner_id,
            platform=payload.get("platform") or "unknown",
            roots=payload.get("roots") or [],
            capabilities=[],
            project_id=self.project_id,
        )
        normalized = envelope.model_copy(update={"runner_id": runner_id})
        self.storage.record_runner_message(normalized, "runner")
        self.storage.upsert_runner(registration)
        await self._send(
            MessageType.REGISTER_ACK,
            payload={
                "accepted": True,
                "ackSeq": envelope.seq,
                "heartbeat": max(2, self.settings.runner_offline_seconds // 3),
            },
        )

    async def _reconnect(self, envelope: MessageEnvelope) -> None:
        if not envelope.runner_id:
            await self.websocket.close(code=1003, reason="runnerId is required")
            raise WebSocketDisconnect(code=1003)
        if envelope.runner_id != self.expected_runner_id:
            await self.websocket.close(code=1008, reason="runnerId mismatch")
            raise WebSocketDisconnect(code=1008)
        self.runner_id = envelope.runner_id
        try:
            runner = self.storage.get_runner(self.runner_id)
            if runner.project_id != self.project_id:
                runner = self.storage.upsert_runner(
                    RunnerRegistration(
                        id=runner.id,
                        name=runner.name,
                        platform=runner.platform,
                        roots=runner.roots,
                        capabilities=runner.capabilities,
                        project_id=self.project_id,
                    )
                )
            else:
                self.storage.touch_runner(self.runner_id)
        except KeyError as exc:
            await self.websocket.close(code=1008, reason="Runner is not registered")
            raise WebSocketDisconnect(code=1008) from exc
        self.storage.record_runner_message(envelope, "runner")
        last_seq = int(envelope.payload.get("lastSeq") or 0)
        for pending in self.storage.list_runner_messages(self.runner_id, "server", last_seq):
            await self.websocket.send_text(pending.to_json())
        await self._send(
            MessageType.ACK,
            task_id=envelope.task_id,
            payload={"ackSeq": envelope.seq, "reconnected": True},
        )

    async def _handle(self, envelope: MessageEnvelope) -> None:
        if not self.runner_id or envelope.runner_id != self.runner_id:
            await self.websocket.close(code=1008, reason="runnerId mismatch")
            raise WebSocketDisconnect(code=1008)
        is_new = self.storage.record_runner_message(envelope, "runner")
        if envelope.type == MessageType.ACK:
            return
        if not is_new:
            await self._ack(envelope, duplicate=True)
            return

        if envelope.type == MessageType.HEARTBEAT:
            payload = envelope.payload
            metrics = {key: payload[key] for key in ("cpu", "memory", "disk") if key in payload}
            self.storage.touch_runner(
                self.runner_id,
                status=str(payload.get("status") or "idle"),
                metrics=metrics,
            )
            active = self.storage.get_runner_active_task(self.runner_id)
            if active and payload.get("status") == "busy":
                self.storage.renew_job(active.id, self.runner_id, self.settings.job_lease_seconds)
            await self._send(
                MessageType.HEARTBEAT_ACK,
                payload={"ackSeq": envelope.seq},
            )
            return
        if envelope.type == MessageType.CAPABILITY:
            capabilities = [name for name, enabled in envelope.payload.items() if enabled]
            self.storage.update_runner_capabilities(self.runner_id, capabilities)
        elif envelope.type == MessageType.TASK_ACCEPTED:
            if envelope.task_id:
                self.storage.renew_job(
                    envelope.task_id, self.runner_id, self.settings.job_lease_seconds
                )
            self._update_task(
                envelope.task_id,
                status=TaskStatus.ACCEPTED,
            )
            if envelope.task_id:
                self.storage.add_event(
                    envelope.task_id,
                    f"Local Runner {self.runner_id} 已接受任务",
                )
        elif envelope.type == MessageType.TASK_PROGRESS:
            if envelope.task_id:
                self.storage.renew_job(
                    envelope.task_id, self.runner_id, self.settings.job_lease_seconds
                )
            if not envelope.payload.get("persisted"):
                step = str(envelope.payload.get("step") or "")
                fields: dict[str, Any] = {"status": TaskStatus.RUNNING}
                if step in STEP_STAGES:
                    fields["stage"] = STEP_STAGES[step]
                else:
                    with suppress(ValueError):
                        fields["stage"] = Stage(step)
                if "percent" in envelope.payload:
                    percent = envelope.payload["percent"]
                    if percent is not None:
                        fields["progress"] = int(percent)
                if envelope.payload.get("branch"):
                    fields["branch"] = str(envelope.payload["branch"])
                self._update_task(envelope.task_id, **fields)
        elif envelope.type == MessageType.TASK_LOG:
            if envelope.task_id and not envelope.payload.get("persisted"):
                self.storage.add_event(
                    envelope.task_id,
                    str(envelope.payload.get("content") or ""),
                    level=str(envelope.payload.get("level") or "INFO").lower(),
                )
        elif envelope.type == MessageType.TERMINAL_OUTPUT:
            if envelope.task_id and not envelope.payload.get("persisted"):
                output = envelope.payload.get("stdout") or envelope.payload.get("stderr") or ""
                self.storage.add_event(
                    envelope.task_id,
                    "Runner terminal output",
                    data={"output": output},
                )
        elif envelope.type == MessageType.AI_CHUNK:
            if envelope.task_id:
                self.storage.add_event(
                    envelope.task_id,
                    "AI stream",
                    data={"delta": envelope.payload.get("delta", "")},
                )
        elif envelope.type == MessageType.FILE_CHANGED:
            if envelope.task_id and not envelope.payload.get("persisted"):
                self.storage.add_event(
                    envelope.task_id,
                    f"文件变更：{envelope.payload.get('path', '')}",
                    data=envelope.payload,
                )
        elif envelope.type == MessageType.ARTIFACT:
            if envelope.task_id and not envelope.payload.get("persisted"):
                self.storage.add_artifact(
                    envelope.task_id,
                    str(envelope.payload.get("kind") or "runner_artifact"),
                    str(envelope.payload.get("content") or ""),
                )
        elif envelope.type == MessageType.PERMISSION_REQUEST:
            self._update_task(envelope.task_id, status=TaskStatus.WAIT_PERMISSION)
            await self._send(
                MessageType.PERMISSION_RESULT,
                task_id=envelope.task_id,
                payload={
                    "allowed": False,
                    "operation": envelope.payload.get("operation"),
                    "reason": "V1 requires explicit server-side permission integration",
                },
            )
        elif envelope.type == MessageType.TASK_COMPLETED:
            if not envelope.payload.get("persisted"):
                self._update_task(
                    envelope.task_id,
                    status=TaskStatus.WAITING_APPROVAL,
                    progress=100,
                )
                if envelope.task_id:
                    self.storage.complete_job(envelope.task_id, self.runner_id)
        elif envelope.type == MessageType.TASK_FAILED:
            if not envelope.payload.get("persisted"):
                self._update_task(
                    envelope.task_id,
                    status=TaskStatus.FAILED,
                    error=str(envelope.payload.get("reason") or "Runner task failed"),
                )
                if envelope.task_id:
                    self.storage.fail_job(
                        envelope.task_id,
                        self.runner_id,
                        str(envelope.payload.get("reason") or "Runner task failed"),
                        self.settings.job_retry_base_seconds,
                    )
        elif envelope.type == MessageType.TASK_CANCELLED:
            if not envelope.payload.get("persisted"):
                self._update_task(envelope.task_id, status=TaskStatus.CANCELLED)
                if envelope.task_id:
                    self.storage.complete_job(envelope.task_id, self.runner_id, JobStatus.CANCELLED)
        await self._ack(envelope)

    async def _dispatch_control_messages(self) -> None:
        if not self.runner_id:
            return
        self.storage.recover_expired_jobs(f"runner:{self.runner_id}")
        active = self.storage.get_runner_active_task(self.runner_id)
        if active and active.cancel_requested and active.id not in self.cancel_sent:
            await self._send(MessageType.CANCEL_TASK, task_id=active.id)
            self.cancel_sent.add(active.id)
            return
        if active:
            return
        task = self.storage.assign_runner_task(self.runner_id, self.settings.job_lease_seconds)
        if not task:
            return
        await self._send(
            MessageType.TASK_ASSIGN,
            task_id=task.id,
            payload={
                "repo": task.repository,
                "branch": task.branch,
                "prompt": task.requirement,
                "steps": ["checkout", "generate", "build", "test"],
                "task": task.model_dump(mode="json"),
            },
        )

    def _update_task(self, task_id: str | None, **fields: Any) -> None:
        if not task_id:
            return
        try:
            task = self.storage.get_task(task_id)
        except KeyError:
            return
        if task.runner_id != self.runner_id:
            return
        self.storage.update_task(task_id, **fields)

    async def _ack(self, envelope: MessageEnvelope, duplicate: bool = False) -> None:
        await self._send(
            MessageType.ACK,
            task_id=envelope.task_id,
            payload={"ackSeq": envelope.seq, "duplicate": duplicate},
        )

    async def _send(
        self,
        message_type: MessageType,
        *,
        task_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> MessageEnvelope:
        if not self.runner_id:
            raise RuntimeError("Runner is not registered")
        envelope = MessageEnvelope.create(
            message_type,
            seq=self.storage.next_runner_sequence(self.runner_id, "server"),
            runner_id=self.runner_id,
            task_id=task_id,
            payload=payload,
        )
        self.storage.record_runner_message(envelope, "server")
        await self.websocket.send_text(envelope.to_json())
        return envelope


async def run_runner_websocket(websocket: WebSocket, storage: Storage, settings: Settings) -> None:
    authorization = websocket.headers.get("authorization", "")
    runner_id = websocket.headers.get("x-runner-id", "")
    scheme, _, header_token = authorization.partition(" ")
    query_token = websocket.query_params.get("token", "")
    token = header_token if scheme.lower() == "bearer" else query_token
    if not runner_id or not token:
        await websocket.close(code=1008, reason="Invalid runner token")
        return
    try:
        project_id = authenticate_runner(storage, settings, runner_id, token)
    except HTTPException:
        await websocket.close(code=1008, reason="Invalid runner token")
        return
    await websocket.accept()
    RUNNER_CONNECTIONS.inc()
    logging.getLogger("autoflow.gateway").info("runner.connected", extra={"runner_id": runner_id})
    try:
        await RunnerGatewaySession(websocket, storage, settings, runner_id, project_id).run()
    except (TimeoutError, WebSocketDisconnect):
        return
    finally:
        RUNNER_CONNECTIONS.dec()
        logging.getLogger("autoflow.gateway").info(
            "runner.disconnected", extra={"runner_id": runner_id}
        )
