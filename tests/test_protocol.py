from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.protocol import MessageEnvelope, MessageType


def envelope(
    message_type: MessageType,
    seq: int,
    *,
    runner_id: str = "ws-runner",
    task_id: str | None = None,
    payload: dict | None = None,
) -> str:
    return MessageEnvelope.create(
        message_type,
        seq=seq,
        runner_id=runner_id,
        task_id=task_id,
        payload=payload,
    ).to_json()


def configure_server(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AUTOFLOW_DATABASE_PATH", str(tmp_path / "protocol.db"))
    monkeypatch.setenv("AUTOFLOW_WORKTREE_ROOT", str(tmp_path / "server-worktrees"))
    monkeypatch.setenv("AUTOFLOW_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("AUTOFLOW_RUNNER_TOKEN", "protocol-token")
    monkeypatch.setenv("AUTOFLOW_MOCK_LLM", "true")
    get_settings.cache_clear()


def register(websocket) -> MessageEnvelope:
    websocket.send_text(
        envelope(
            MessageType.REGISTER,
            1,
            payload={
                "runnerId": "ws-runner",
                "hostname": "WS Developer Laptop",
                "platform": "Linux test",
                "version": "1.0.0",
                "roots": ["/home/developer/projects"],
            },
        )
    )
    return MessageEnvelope.model_validate_json(websocket.receive_text())


def test_websocket_protocol_assigns_and_tracks_task(tmp_path, monkeypatch) -> None:
    configure_server(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer protocol-token"}
    with TestClient(app) as client:
        with client.websocket_connect("/api/runner/ws", headers=headers) as websocket:
            registered = register(websocket)
            assert registered.type == MessageType.REGISTER_ACK
            assert registered.payload["accepted"] is True
            assert registered.runner_id == "ws-runner"

            websocket.send_text(
                envelope(
                    MessageType.CAPABILITY,
                    2,
                    payload={
                        "git": True,
                        "docker": True,
                        "cargo": True,
                        "cmake": False,
                        "node": False,
                    },
                )
            )
            assert (
                MessageEnvelope.model_validate_json(websocket.receive_text()).type
                == MessageType.ACK
            )

            created = client.post(
                "/api/tasks",
                json={
                    "title": "WebSocket task",
                    "requirement": "Execute through the documented WebSocket protocol",
                    "repository": "/home/developer/projects/example",
                    "runner_id": "ws-runner",
                },
            ).json()
            task_id = created["id"]
            client.post(f"/api/tasks/{task_id}/start")
            assigned = MessageEnvelope.model_validate_json(websocket.receive_text())
            assert assigned.type == MessageType.TASK_ASSIGN
            assert assigned.task_id == task_id
            assert assigned.payload["repo"] == "/home/developer/projects/example"
            assert assigned.payload["prompt"].startswith("Execute through")
            assert assigned.payload["steps"] == ["checkout", "generate", "build", "test"]

            websocket.send_text(
                envelope(MessageType.ACK, 3, task_id=task_id, payload={"ackSeq": assigned.seq})
            )
            websocket.send_text(envelope(MessageType.TASK_ACCEPTED, 4, task_id=task_id))
            assert (
                MessageEnvelope.model_validate_json(websocket.receive_text()).type
                == MessageType.ACK
            )

            websocket.send_text(
                envelope(
                    MessageType.HEARTBEAT,
                    5,
                    payload={
                        "cpu": 20,
                        "memory": 55,
                        "disk": 70,
                        "status": "busy",
                    },
                )
            )
            assert (
                MessageEnvelope.model_validate_json(websocket.receive_text()).type
                == MessageType.HEARTBEAT_ACK
            )

            progress = envelope(
                MessageType.TASK_PROGRESS,
                6,
                task_id=task_id,
                payload={"step": "generate", "percent": 60},
            )
            websocket.send_text(progress)
            websocket.receive_text()
            log = envelope(
                MessageType.TASK_LOG,
                7,
                task_id=task_id,
                payload={"level": "INFO", "content": "Generating local code"},
            )
            websocket.send_text(log)
            websocket.receive_text()
            websocket.send_text(log)
            duplicate_ack = MessageEnvelope.model_validate_json(websocket.receive_text())
            assert duplicate_ack.payload["duplicate"] is True

            websocket.send_text(
                envelope(
                    MessageType.TERMINAL_OUTPUT,
                    8,
                    task_id=task_id,
                    payload={"stdout": "Compiling local project"},
                )
            )
            websocket.receive_text()
            websocket.send_text(
                envelope(
                    MessageType.FILE_CHANGED,
                    9,
                    task_id=task_id,
                    payload={"path": "src/main.rs", "operation": "modify"},
                )
            )
            websocket.receive_text()
            websocket.send_text(
                envelope(
                    MessageType.AI_CHUNK,
                    10,
                    task_id=task_id,
                    payload={"delta": "pub fn"},
                )
            )
            websocket.receive_text()
            websocket.send_text(
                envelope(
                    MessageType.PERMISSION_REQUEST,
                    11,
                    task_id=task_id,
                    payload={"operation": "git push"},
                )
            )
            permission = MessageEnvelope.model_validate_json(websocket.receive_text())
            assert permission.type == MessageType.PERMISSION_RESULT
            assert permission.payload["allowed"] is False
            websocket.receive_text()
            assert client.get(f"/api/tasks/{task_id}").json()["task"]["status"] == "wait_permission"

            websocket.send_text(
                envelope(
                    MessageType.TASK_COMPLETED,
                    12,
                    task_id=task_id,
                    payload={"commit": None, "mr": None},
                )
            )
            websocket.receive_text()

        detail = client.get(f"/api/tasks/{task_id}").json()
        assert detail["task"]["status"] == "waiting_approval"
        assert detail["task"]["stage"] == "coder"
        assert detail["task"]["progress"] == 100
        assert (
            len(
                [event for event in detail["events"] if event["message"] == "Generating local code"]
            )
            == 1
        )
        assert any(
            event["data"].get("output") == "Compiling local project" for event in detail["events"]
        )
        assert any(event["data"].get("path") == "src/main.rs" for event in detail["events"])
        assert any(event["data"].get("delta") == "pub fn" for event in detail["events"])
        runner = client.get("/api/runners").json()[0]
        assert runner["status"] == "busy"
        assert runner["metrics"] == {"cpu": 20, "memory": 55, "disk": 70}
        assert set(runner["capabilities"]) == {"git", "docker", "cargo"}
    get_settings.cache_clear()


def test_reconnect_replays_unacknowledged_server_messages(tmp_path, monkeypatch) -> None:
    configure_server(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer protocol-token"}
    with TestClient(app) as client:
        with client.websocket_connect("/api/runner/ws", headers=headers) as websocket:
            register_ack = register(websocket)
            created = client.post(
                "/api/tasks",
                json={
                    "title": "Reconnect task",
                    "requirement": "Recover the task assignment after disconnect",
                    "repository": "/home/developer/projects/example",
                    "runner_id": "ws-runner",
                },
            ).json()
            client.post(f"/api/tasks/{created['id']}/start")
            assigned = MessageEnvelope.model_validate_json(websocket.receive_text())
            assert assigned.type == MessageType.TASK_ASSIGN

        with client.websocket_connect("/api/runner/ws", headers=headers) as websocket:
            websocket.send_text(
                envelope(
                    MessageType.RECONNECT,
                    2,
                    task_id=created["id"],
                    payload={
                        "lastTask": created["id"],
                        "lastSeq": register_ack.seq,
                    },
                )
            )
            replayed = MessageEnvelope.model_validate_json(websocket.receive_text())
            assert replayed.id == assigned.id
            assert replayed.seq == assigned.seq
            reconnect_ack = MessageEnvelope.model_validate_json(websocket.receive_text())
            assert reconnect_ack.type == MessageType.ACK
            assert reconnect_ack.payload["reconnected"] is True
    get_settings.cache_clear()


def test_server_cancels_running_websocket_task(tmp_path, monkeypatch) -> None:
    configure_server(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer protocol-token"}
    with TestClient(app) as client:
        with client.websocket_connect("/api/runner/ws", headers=headers) as websocket:
            register(websocket)
            created = client.post(
                "/api/tasks",
                json={
                    "title": "Cancel task",
                    "requirement": "Cancel a running Local Runner task",
                    "repository": "/home/developer/projects/example",
                    "runner_id": "ws-runner",
                },
            ).json()
            task_id = created["id"]
            client.post(f"/api/tasks/{task_id}/start")
            assigned = MessageEnvelope.model_validate_json(websocket.receive_text())
            websocket.send_text(envelope(MessageType.TASK_ACCEPTED, 2, task_id=task_id))
            websocket.receive_text()

            client.post(f"/api/tasks/{task_id}/cancel")
            cancelled = MessageEnvelope.model_validate_json(websocket.receive_text())
            assert cancelled.type == MessageType.CANCEL_TASK
            assert cancelled.task_id == task_id
            websocket.send_text(
                envelope(
                    MessageType.ACK,
                    3,
                    task_id=task_id,
                    payload={"ackSeq": cancelled.seq},
                )
            )
            websocket.send_text(envelope(MessageType.TASK_CANCELLED, 4, task_id=task_id))
            websocket.receive_text()

        assert client.get(f"/api/tasks/{task_id}").json()["task"]["status"] == "cancelled"
        assert assigned.type == MessageType.TASK_ASSIGN
    get_settings.cache_clear()
