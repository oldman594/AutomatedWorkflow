from pathlib import Path
import time
import io
import zipfile

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.models import RunnerRegistration, Stage, TaskStatus
from app.runner import RemoteStorage


def test_health_and_task_lifecycle(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "README.md").write_text("fixture", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)

    monkeypatch.setenv("AUTOFLOW_DATABASE_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("AUTOFLOW_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    monkeypatch.setenv("AUTOFLOW_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("AUTOFLOW_MOCK_LLM", "true")
    get_settings.cache_clear()

    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        response = client.post("/api/tasks", json={
            "title": "API task",
            "requirement": "Check the API task lifecycle",
            "repository": str(repo),
            "branch": None,
            "build_command": "true",
            "test_command": "true",
        })
        assert response.status_code == 201
        task_id = response.json()["id"]
        assert response.json()["branch"] == ""
        assert response.json()["include_local_changes"] is True
        assert response.json()["sync_to_source"] is True
        detail = client.get(f"/api/tasks/{task_id}")
        assert detail.json()["task"]["status"] == "draft"

        started = client.post(f"/api/tasks/{task_id}/start")
        assert started.status_code == 200
        for _ in range(100):
            detail = client.get(f"/api/tasks/{task_id}").json()
            if detail["task"]["status"] not in {"queued", "running"}:
                break
            time.sleep(0.02)
        assert detail["task"]["status"] == "waiting_approval"
        kinds = {artifact["kind"] for artifact in detail["artifacts"]}
        assert {
            "product_spec", "requirement_assessment", "plan", "reading", "design",
            "diff", "review", "acceptance", "mr_description", "local_snapshot",
        } <= kinds
        acceptance_rounds = [
            artifact for artifact in detail["artifacts"] if artifact["kind"] == "acceptance"
        ]
        assert len(acceptance_rounds) == 3
        assert any("纠偏 Agent" in event["message"] for event in detail["events"])
        delivery = next(
            artifact for artifact in detail["artifacts"] if artifact["kind"] == "delivery"
        )
        assert '"source_applied": false' in delivery["content"]

        download = client.get(f"/api/tasks/{task_id}/download")
        assert download.status_code == 200
        assert download.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            assert {"DELIVERY.md", "changes.diff"} <= set(archive.namelist())
            delivery_doc = archive.read("DELIVERY.md").decode("utf-8")
            assert "NEEDS HUMAN REVIEW" in delivery_doc
            assert "## Run" in delivery_doc

        rejected_commit = client.post(
            f"/api/tasks/{task_id}/approve", json={"action": "commit"}
        )
        assert rejected_commit.status_code == 409

        approved = client.post(f"/api/tasks/{task_id}/approve", json={"action": "complete"})
        assert approved.json()["status"] == "completed"

    get_settings.cache_clear()


def test_local_runner_registers_leases_and_reports_task(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AUTOFLOW_DATABASE_PATH", str(tmp_path / "runner-api.db"))
    monkeypatch.setenv("AUTOFLOW_WORKTREE_ROOT", str(tmp_path / "server-worktrees"))
    monkeypatch.setenv("AUTOFLOW_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("AUTOFLOW_RUNNER_TOKEN", "runner-test-token")
    monkeypatch.setenv("AUTOFLOW_MOCK_LLM", "true")
    get_settings.cache_clear()

    with TestClient(app) as client:
        registration = {
            "id": "devbox-01",
            "name": "Developer Laptop",
            "platform": "Linux test",
            "roots": ["/home/developer/projects"],
            "capabilities": ["git", "shell", "docker", "ai:mock"],
        }
        unauthorized = client.post("/api/runner/register", json=registration)
        assert unauthorized.status_code == 401

        remote = RemoteStorage(
            "http://testserver", "runner-test-token", "devbox-01", client=client
        )
        runner = remote.register(RunnerRegistration(**registration))
        assert runner.online is True
        assert remote.heartbeat().id == "devbox-01"
        assert client.get("/api/runners").json()[0]["online"] is True

        created = client.post(
            "/api/tasks",
            json={
                "title": "Remote local task",
                "requirement": "Continue the code on the developer laptop",
                "repository": "/home/developer/projects/example",
                "runner_id": "devbox-01",
            },
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        assert created.json()["runner_id"] == "devbox-01"
        assert client.post(f"/api/tasks/{task_id}/start").json()["status"] == "queued"

        leased = remote.lease()
        assert leased is not None
        assert leased.id == task_id
        assert leased.status == TaskStatus.RUNNING
        assert remote.lease() is None
        updated = remote.update_task(task_id, stage=Stage.READER, progress=24)
        assert updated.stage == Stage.READER
        remote.add_event(task_id, "Runner read local code", stage=Stage.READER)
        remote.add_artifact(task_id, "runner_test", "local artifact")
        assert remote.list_artifacts(task_id)[0].content == "local artifact"

        detail = client.get(f"/api/tasks/{task_id}").json()
        assert any(event["message"] == "Runner read local code" for event in detail["events"])
        assert client.get(f"/api/tasks/{task_id}/download").status_code == 409

    get_settings.cache_clear()
