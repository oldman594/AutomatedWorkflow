from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MessageType(StrEnum):
    REGISTER = "Register"
    REGISTER_ACK = "RegisterAck"
    HEARTBEAT = "Heartbeat"
    HEARTBEAT_ACK = "HeartbeatAck"
    CAPABILITY = "Capability"
    TASK_ASSIGN = "TaskAssign"
    TASK_ACCEPTED = "TaskAccepted"
    TASK_PROGRESS = "TaskProgress"
    TASK_LOG = "TaskLog"
    TERMINAL_OUTPUT = "TerminalOutput"
    AI_CHUNK = "AIChunk"
    FILE_CHANGED = "FileChanged"
    PERMISSION_REQUEST = "PermissionRequest"
    PERMISSION_RESULT = "PermissionResult"
    CANCEL_TASK = "CancelTask"
    TASK_COMPLETED = "TaskCompleted"
    TASK_FAILED = "TaskFailed"
    TASK_CANCELLED = "TaskCancelled"
    RECONNECT = "Reconnect"
    ACK = "Ack"
    ARTIFACT = "Artifact"


class MessageEnvelope(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default_factory=lambda: f"msg-{uuid.uuid4().hex}")
    type: MessageType
    timestamp: int = Field(default_factory=lambda: int(time.time()))
    seq: int = Field(default=0, ge=0)
    runner_id: str | None = Field(default=None, alias="runnerId")
    task_id: str | None = Field(default=None, alias="taskId")
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_json(self) -> str:
        return self.model_dump_json(by_alias=True, exclude_none=True)

    @classmethod
    def create(
        cls,
        message_type: MessageType,
        *,
        seq: int,
        runner_id: str | None = None,
        task_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> MessageEnvelope:
        return cls(
            type=message_type,
            seq=seq,
            runnerId=runner_id,
            taskId=task_id,
            payload=payload or {},
        )
