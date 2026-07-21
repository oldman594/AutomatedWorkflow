from pathlib import Path

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


def configure_auth(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AUTOFLOW_DATABASE_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setenv("AUTOFLOW_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    monkeypatch.setenv("AUTOFLOW_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("AUTOFLOW_AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTOFLOW_AUTH_COOKIE_SECURE", "false")
    monkeypatch.setenv("AUTOFLOW_BOOTSTRAP_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("AUTOFLOW_BOOTSTRAP_ADMIN_PASSWORD", "correct-horse-battery")
    monkeypatch.setenv("AUTOFLOW_MOCK_LLM", "true")
    monkeypatch.delenv("AUTOFLOW_RUNNER_TOKEN", raising=False)
    get_settings.cache_clear()


def test_login_project_rbac_and_per_runner_token(tmp_path: Path, monkeypatch) -> None:
    configure_auth(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.get("/api/tasks").status_code == 401
        assert (
            client.post(
                "/api/auth/login",
                json={"email": "admin@example.com", "password": "wrong-password"},
            ).status_code
            == 401
        )

        login = client.post(
            "/api/auth/login",
            json={
                "email": "admin@example.com",
                "password": "correct-horse-battery",
            },
        )
        assert login.status_code == 200
        assert "HttpOnly" in login.headers["set-cookie"]
        assert client.get("/api/auth/me").json()["is_admin"] is True

        project = client.post("/api/projects", json={"name": "Payments", "slug": "payments"}).json()
        project_id = project["id"]
        assert project["role"] == "owner"

        created_user = client.post(
            "/api/users",
            json={
                "email": "viewer@example.com",
                "display_name": "Viewer",
                "password": "viewer-password",
            },
        )
        assert created_user.status_code == 201
        member = client.put(
            f"/api/projects/{project_id}/members",
            json={"email": "viewer@example.com", "role": "viewer"},
        )
        assert member.json()["role"] == "viewer"

        issued = client.post(
            f"/api/projects/{project_id}/runner-tokens",
            json={"runner_id": "payments-runner", "label": "Payments Laptop"},
        )
        token = issued.json()["token"]
        assert token.startswith("afr_")

        runner_headers = {
            "Authorization": f"Bearer {token}",
            "X-Runner-ID": "payments-runner",
        }
        registered = client.post(
            "/api/runner/register",
            headers=runner_headers,
            json={
                "id": "payments-runner",
                "name": "Payments Laptop",
                "platform": "Linux",
                "roots": [str(tmp_path)],
            },
        )
        assert registered.status_code == 200
        assert registered.json()["project_id"] == project_id

        client.post("/api/auth/logout")
        viewer_login = client.post(
            "/api/auth/login",
            json={"email": "viewer@example.com", "password": "viewer-password"},
        )
        assert viewer_login.status_code == 200
        assert [item["id"] for item in client.get("/api/projects").json()] == [project_id]
        forbidden = client.post(
            "/api/tasks",
            json={
                "title": "Forbidden task",
                "requirement": "Viewer must not create a task",
                "repository": str(tmp_path / "repo"),
                "project_id": project_id,
            },
        )
        assert forbidden.status_code == 403

        spoofed = client.post(
            "/api/runner/register",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Runner-ID": "another-runner",
            },
            json={
                "id": "another-runner",
                "name": "Spoofed",
                "platform": "Linux",
            },
        )
        assert spoofed.status_code == 401
    get_settings.cache_clear()
