from __future__ import annotations

import json
import os
import queue
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from threading import Event, Lock, Thread
from typing import cast
from urllib.parse import urlsplit, urlunsplit

from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import ClientConnection, connect

from app import __version__
from app.models import RunnerRegistration, Task
from app.protocol import MessageEnvelope, MessageType


class ProtocolState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = Lock()
        self.next_seq = 1
        self.last_server_seq = 0
        self.last_task: str | None = None
        self.registered = False
        self.pending: dict[int, MessageEnvelope] = {}
        self._load()

    def new_envelope(
        self,
        message_type: MessageType,
        runner_id: str,
        *,
        task_id: str | None = None,
        payload: dict | None = None,
    ) -> MessageEnvelope:
        with self.lock:
            envelope = MessageEnvelope.create(
                message_type,
                seq=self.next_seq,
                runner_id=runner_id,
                task_id=task_id,
                payload=payload,
            )
            self.next_seq += 1
            self.pending[envelope.seq] = envelope
            self._save_locked()
            return envelope

    def acknowledge(self, seq: int) -> None:
        with self.lock:
            self.pending.pop(seq, None)
            self._save_locked()

    def mark_server_message(self, envelope: MessageEnvelope) -> None:
        with self.lock:
            self.last_server_seq = max(self.last_server_seq, envelope.seq)
            if envelope.task_id:
                self.last_task = envelope.task_id
            self._save_locked()

    def mark_registered(self) -> None:
        with self.lock:
            self.registered = True
            self._save_locked()

    def discard_types(self, types: set[MessageType]) -> None:
        with self.lock:
            self.pending = {
                seq: item for seq, item in self.pending.items() if item.type not in types
            }
            self._save_locked()

    def pending_messages(self) -> list[MessageEnvelope]:
        with self.lock:
            return [self.pending[seq] for seq in sorted(self.pending)]

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.next_seq = max(1, int(data.get("nextSeq", 1)))
            self.last_server_seq = max(0, int(data.get("lastServerSeq", 0)))
            self.last_task = data.get("lastTask")
            self.registered = bool(data.get("registered", False))
            self.pending = {
                item.seq: item
                for item in (MessageEnvelope.model_validate(raw) for raw in data.get("pending", []))
            }
        except (OSError, ValueError, TypeError):
            self.pending = {}

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "nextSeq": self.next_seq,
                    "lastServerSeq": self.last_server_seq,
                    "lastTask": self.last_task,
                    "registered": self.registered,
                    "pending": [
                        item.model_dump(mode="json", by_alias=True, exclude_none=True)
                        for item in self.pending_messages_unlocked()
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def pending_messages_unlocked(self) -> list[MessageEnvelope]:
        return [self.pending[seq] for seq in sorted(self.pending)]


class RunnerProtocolClient:
    def __init__(
        self,
        server: str,
        token: str,
        registration: RunnerRegistration,
        state_path: Path,
        capability_payload: dict[str, bool],
        heartbeat_seconds: float = 10,
    ) -> None:
        self.uri = websocket_uri(server)
        self.token = token
        self.registration = registration
        self.state = ProtocolState(state_path)
        self.capability_payload = capability_payload
        self.heartbeat_seconds = heartbeat_seconds
        self.tasks: queue.Queue[Task] = queue.Queue()
        self.stop_event = Event()
        self.ready = Event()
        self.thread: Thread | None = None
        self.connection: ClientConnection | None = None
        self.connection_lock = Lock()
        self.send_lock = Lock()
        self.message_lock = Lock()
        self.active_task_id: str | None = None
        self.queued_task_ids: set[str] = set()
        self.on_message: Callable[[MessageEnvelope], None] | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = Thread(
            target=self._connection_loop,
            name="autoflow-runner-websocket",
            daemon=True,
        )
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        with self.connection_lock:
            if self.connection:
                self.connection.close()
        if self.thread:
            self.thread.join(timeout=5)

    def wait_until_ready(self, timeout: float = 15) -> bool:
        return self.ready.wait(timeout)

    def next_task(self, timeout: float = 1) -> Task | None:
        try:
            task = self.tasks.get(timeout=timeout)
            self.queued_task_ids.discard(task.id)
            self.active_task_id = task.id
            return task
        except queue.Empty:
            return None

    def task_finished(self, task_id: str) -> None:
        if self.active_task_id == task_id:
            self.active_task_id = None

    def send(
        self,
        message_type: MessageType,
        task_id: str | None = None,
        payload: dict | None = None,
    ) -> MessageEnvelope:
        with self.message_lock:
            envelope = self.state.new_envelope(
                message_type,
                self.registration.id,
                task_id=task_id,
                payload=payload,
            )
            self._try_send(envelope)
            if message_type == MessageType.ACK:
                self.state.acknowledge(envelope.seq)
            return envelope

    def _connection_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                with connect(
                    self.uri,
                    additional_headers={
                        "Authorization": f"Bearer {self.token}",
                        "X-Runner-ID": self.registration.id,
                    },
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=16 * 1024 * 1024,
                ) as opened:
                    websocket = cast(ClientConnection, opened)
                    with self.connection_lock:
                        self.connection = websocket
                    self.ready.clear()
                    if self.state.registered:
                        self._send_reconnect(websocket)
                    else:
                        self._send_register(websocket)
                    self._receive_loop(websocket)
            except (WebSocketException, OSError, TimeoutError) as exc:
                if not self.stop_event.is_set():
                    print(f"Runner WebSocket disconnected: {exc}", flush=True)
            finally:
                self.ready.clear()
                with self.connection_lock:
                    self.connection = None
            self.stop_event.wait(2)

    def _receive_loop(self, websocket: ClientConnection) -> None:
        last_heartbeat = 0.0
        while not self.stop_event.is_set():
            now = time.monotonic()
            if now - last_heartbeat >= self.heartbeat_seconds:
                metrics = system_metrics(self.registration.roots)
                metrics["status"] = "busy" if self.active_task_id else "idle"
                self.send(MessageType.HEARTBEAT, payload=metrics)
                last_heartbeat = now
            try:
                raw = websocket.recv(timeout=1)
            except TimeoutError:
                continue
            if not isinstance(raw, str):
                continue
            envelope = MessageEnvelope.model_validate_json(raw)
            self._handle_server_message(envelope)
            self.state.mark_server_message(envelope)
            if self.on_message:
                self.on_message(envelope)

    def _handle_server_message(self, envelope: MessageEnvelope) -> None:
        ack_seq = envelope.payload.get("ackSeq")
        if ack_seq is not None:
            self.state.acknowledge(int(ack_seq))
        if envelope.type == MessageType.REGISTER_ACK:
            if not envelope.payload.get("accepted"):
                self._report_release(envelope.payload)
                print(
                    f"Runner registration rejected: {envelope.payload.get('reason', 'unknown reason')}",
                    flush=True,
                )
                self.stop_event.set()
                return
            self.state.mark_registered()
            self._report_release(envelope.payload)
            self.state.discard_types({MessageType.REGISTER})
            self.heartbeat_seconds = max(
                2.0, float(envelope.payload.get("heartbeat") or self.heartbeat_seconds)
            )
            self.ready.set()
            self._flush_pending()
            self.send(MessageType.CAPABILITY, payload=self.capability_payload)
            return
        if envelope.type == MessageType.ACK and envelope.payload.get("reconnected"):
            self._report_release(envelope.payload)
            self.state.discard_types({MessageType.RECONNECT})
            self.ready.set()
            self._flush_pending()
            return
        if envelope.type == MessageType.TASK_ASSIGN:
            self._send_ack(envelope)
            task = Task.model_validate(envelope.payload["task"])
            self.send(MessageType.TASK_ACCEPTED, task.id)
            if task.id != self.active_task_id and task.id not in self.queued_task_ids:
                self.queued_task_ids.add(task.id)
                self.tasks.put(task)
            return
        if envelope.type in {MessageType.CANCEL_TASK, MessageType.PERMISSION_RESULT}:
            self._send_ack(envelope)

    @staticmethod
    def _report_release(payload: dict) -> None:
        if payload.get("updateAvailable"):
            version = payload.get("recommendedVersion")
            manifest = payload.get("releaseManifestUrl")
            print(
                f"Runner update available: {version}; manifest: {manifest or 'not configured'}",
                flush=True,
            )

    def _send_register(self, websocket: ClientConnection) -> None:
        envelope = self.state.new_envelope(
            MessageType.REGISTER,
            self.registration.id,
            payload={
                "runnerId": self.registration.id,
                "hostname": self.registration.name,
                "platform": self.registration.platform,
                "version": __version__,
                "roots": self.registration.roots,
            },
        )
        self._send_on(websocket, envelope)

    def _send_reconnect(self, websocket: ClientConnection) -> None:
        envelope = self.state.new_envelope(
            MessageType.RECONNECT,
            self.registration.id,
            task_id=self.state.last_task,
            payload={
                "lastTask": self.state.last_task,
                "lastSeq": self.state.last_server_seq,
                "version": __version__,
            },
        )
        self._send_on(websocket, envelope)

    def _send_ack(self, envelope: MessageEnvelope) -> None:
        self.send(
            MessageType.ACK,
            envelope.task_id,
            {"ackSeq": envelope.seq},
        )

    def _flush_pending(self) -> None:
        with self.message_lock:
            for envelope in self.state.pending_messages():
                if envelope.type not in {MessageType.REGISTER, MessageType.RECONNECT}:
                    self._try_send(envelope)

    def _try_send(self, envelope: MessageEnvelope) -> None:
        # Keep application messages queued until Register/Reconnect is acknowledged.
        if not self.ready.is_set():
            return
        with self.connection_lock:
            websocket = self.connection
        if websocket is None:
            return
        try:
            self._send_on(websocket, envelope)
        except (ConnectionClosed, OSError):
            return

    def _send_on(self, websocket: ClientConnection, envelope: MessageEnvelope) -> None:
        with self.send_lock:
            websocket.send(envelope.to_json())


def websocket_uri(server: str) -> str:
    parsed = urlsplit(server)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = parsed.path.rstrip("/") + "/api/runner/ws"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def capability_flags() -> dict[str, bool]:
    return {
        "git": shutil.which("git") is not None,
        "docker": shutil.which("docker") is not None,
        "cargo": shutil.which("cargo") is not None,
        "cmake": shutil.which("cmake") is not None,
        "node": shutil.which("node") is not None,
    }


def system_metrics(roots: list[str]) -> dict[str, float | str]:
    cpu_count = max(1, os.cpu_count() or 1)
    try:
        cpu = min(100.0, os.getloadavg()[0] / cpu_count * 100)
    except (AttributeError, OSError):
        cpu = 0.0
    disk_root = roots[0] if roots else str(Path.home())
    usage = shutil.disk_usage(disk_root)
    disk = usage.used / usage.total * 100 if usage.total else 0.0
    memory = linux_memory_percent()
    return {
        "cpu": round(cpu, 1),
        "memory": round(memory, 1),
        "disk": round(disk, 1),
        "status": "busy",
    }


def linux_memory_percent() -> float:
    try:
        values: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            name, value = line.split(":", 1)
            values[name] = int(value.strip().split()[0])
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", 0)
        return (total - available) / total * 100 if total else 0.0
    except (OSError, ValueError, IndexError):
        return 0.0
