import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import app
from app.models import RunnerRegistration
from app.protocol import MessageEnvelope, MessageType
from app.runner import LocalRunner, RemoteStorage
from app.runner_transport import RunnerProtocolClient


class RecordingWebSocket:
    def __init__(self) -> None:
        self.messages: list[MessageEnvelope] = []

    def send(self, raw: str) -> None:
        self.messages.append(MessageEnvelope.model_validate_json(raw))


def test_protocol_client_queues_messages_until_handshake_is_ready(
    tmp_path: Path,
) -> None:
    registration = RunnerRegistration(
        id="local-test",
        name="Local Test Runner",
        platform="Linux test",
        roots=[str(tmp_path)],
    )
    protocol = RunnerProtocolClient(
        "http://localhost:8000",
        "test-token",
        registration,
        tmp_path / "runner-state.json",
        {"git": True},
    )
    websocket = RecordingWebSocket()
    protocol.connection = websocket  # type: ignore[assignment]

    queued = protocol.send(MessageType.TASK_LOG, "task-1", {"content": "queued"})
    assert websocket.messages == []
    assert protocol.state.pending_messages() == [queued]

    protocol.ready.set()
    protocol._flush_pending()
    assert websocket.messages == [queued]


def test_runner_executes_leased_task_and_reports_workflow(tmp_path: Path, monkeypatch) -> None:
    server_db = tmp_path / "server.db"
    token = "runner-integration-token"
    monkeypatch.setenv("AUTOFLOW_DATABASE_PATH", str(server_db))
    monkeypatch.setenv("AUTOFLOW_WORKTREE_ROOT", str(tmp_path / "server-worktrees"))
    monkeypatch.setenv("AUTOFLOW_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("AUTOFLOW_RUNNER_TOKEN", token)
    monkeypatch.setenv("AUTOFLOW_MOCK_LLM", "true")
    get_settings.cache_clear()

    repository = tmp_path / "local-repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repository, check=True)
    (repository / "README.md").write_text("local fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=repository, check=True, capture_output=True
    )

    with TestClient(app) as client:
        remote = RemoteStorage("http://testserver", token, "integration-runner", client=client)
        registration = RunnerRegistration(
            id="integration-runner",
            name="Integration Runner",
            platform="Linux test",
            roots=[str(tmp_path)],
            capabilities=["git", "shell", "ai:mock"],
        )
        remote.register(registration)
        created = client.post(
            "/api/tasks",
            json={
                "title": "Runner workflow",
                "requirement": "Inspect the repository through the Local Runner",
                "repository": str(repository),
                "runner_id": registration.id,
                "build_command": "true",
                "test_command": "true",
            },
        )
        task_id = created.json()["id"]
        assert client.post(f"/api/tasks/{task_id}/start").status_code == 200

        runner_settings = Settings(
            mock_llm=True,
            allowed_roots=[tmp_path],
            database_path=tmp_path / "unused-runner.db",
            worktree_root=tmp_path / "runner-worktrees",
        )
        runner = LocalRunner(runner_settings, remote, registration, 0.25, 10)
        assert runner.run_once() is True
        assert runner.run_once() is False

        detail = client.get(f"/api/tasks/{task_id}").json()
        assert detail["task"]["status"] == "waiting_approval"
        assert detail["task"]["runner_id"] == registration.id
        assert any(
            event["message"].startswith("Local Runner integration-runner")
            for event in detail["events"]
        )
        kinds = {artifact["kind"] for artifact in detail["artifacts"]}
        assert {"worktree", "plan", "reading", "validation", "delivery"} <= kinds

    get_settings.cache_clear()
