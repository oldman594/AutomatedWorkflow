from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import get_settings
from app.email_auth import SMTPVerificationSender
from app.main import app


def configure_auth(tmp_path: Path, monkeypatch) -> dict[str, str]:
    monkeypatch.setenv("AUTOFLOW_DATABASE_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setenv("AUTOFLOW_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    monkeypatch.setenv("AUTOFLOW_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("AUTOFLOW_AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTOFLOW_REGISTRATION_ENABLED", "true")
    monkeypatch.setenv("AUTOFLOW_AUTH_COOKIE_SECURE", "false")
    monkeypatch.setenv("AUTOFLOW_BOOTSTRAP_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("AUTOFLOW_EMAIL_CODE_SECRET", "test-email-code-secret")
    monkeypatch.setenv("AUTOFLOW_EMAIL_CODE_COOLDOWN_SECONDS", "60")
    monkeypatch.setenv("AUTOFLOW_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("AUTOFLOW_SMTP_FROM_EMAIL", "autoflow@example.com")
    monkeypatch.setenv("AUTOFLOW_MOCK_LLM", "true")
    monkeypatch.setenv("AUTOFLOW_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("AUTOFLOW_RUNNER_TOKEN", raising=False)
    sent_codes: dict[str, str] = {}

    def capture_code(
        _sender: SMTPVerificationSender,
        recipient: str,
        code: str,
        _expires_in_seconds: int,
    ) -> None:
        sent_codes[recipient] = code

    monkeypatch.setattr(SMTPVerificationSender, "send_code", capture_code)
    get_settings.cache_clear()
    return sent_codes


def email_login(client: TestClient, sent_codes: dict[str, str], email: str):
    requested = client.post("/api/auth/email/request", json={"email": email})
    assert requested.status_code == 202
    challenge_id = requested.json()["challenge_id"]
    return client.post(
        "/api/auth/email/verify",
        json={"email": email, "challenge_id": challenge_id, "code": sent_codes[email.lower()]},
    )


def test_email_code_creates_session_and_project_owner(tmp_path: Path, monkeypatch) -> None:
    sent_codes = configure_auth(tmp_path, monkeypatch)
    with TestClient(app) as client:
        page = client.get("/").text
        assert 'id="email-auth-form"' in page
        assert 'type="password"' not in page
        assert client.post("/api/auth/login", json={}).status_code == 404
        assert client.post("/api/auth/register", json={}).status_code == 404

        invalid = client.post("/api/auth/email/request", json={"email": "not-an-email"})
        assert invalid.status_code == 422
        assert "请输入有效的邮箱地址" in str(invalid.json())

        requested = client.post("/api/auth/email/request", json={"email": "Developer@Example.com"})
        assert requested.status_code == 202
        challenge_id = requested.json()["challenge_id"]
        assert requested.json()["expires_in_seconds"] == 600
        assert len(sent_codes["developer@example.com"]) == 6
        wrong_code = "000000" if sent_codes["developer@example.com"] != "000000" else "999999"

        wrong = client.post(
            "/api/auth/email/verify",
            json={
                "email": "Developer@Example.com",
                "challenge_id": challenge_id,
                "code": wrong_code,
            },
        )
        assert wrong.status_code == 401
        verified = client.post(
            "/api/auth/email/verify",
            json={
                "email": "Developer@Example.com",
                "challenge_id": challenge_id,
                "code": sent_codes["developer@example.com"],
            },
        )
        assert verified.status_code == 200
        assert verified.json()["email"] == "developer@example.com"
        assert verified.json()["is_admin"] is False
        assert "HttpOnly" in verified.headers["set-cookie"]
        assert client.get("/api/auth/me").json()["email"] == "developer@example.com"

        reused = client.post(
            "/api/auth/email/verify",
            json={
                "email": "Developer@Example.com",
                "challenge_id": challenge_id,
                "code": sent_codes["developer@example.com"],
            },
        )
        assert reused.status_code == 401
        project = client.post(
            "/api/projects", json={"name": "Developer Project", "slug": "developer-project"}
        )
        assert project.status_code == 201
        assert project.json()["role"] == "owner"

        limited_attempts = client.post(
            "/api/auth/email/request", json={"email": "attempts@example.com"}
        ).json()
        attempt_code = sent_codes["attempts@example.com"]
        wrong_attempt = "000000" if attempt_code != "000000" else "999999"
        for _ in range(5):
            rejected = client.post(
                "/api/auth/email/verify",
                json={
                    "email": "attempts@example.com",
                    "challenge_id": limited_attempts["challenge_id"],
                    "code": wrong_attempt,
                },
            )
            assert rejected.status_code == 401
        exhausted = client.post(
            "/api/auth/email/verify",
            json={
                "email": "attempts@example.com",
                "challenge_id": limited_attempts["challenge_id"],
                "code": attempt_code,
            },
        )
        assert exhausted.status_code == 401

        expired = client.post(
            "/api/auth/email/request", json={"email": "expired@example.com"}
        ).json()
        with app.state.storage._connect() as db:
            db.execute(
                "UPDATE email_challenges SET expires_at = ? WHERE id = ?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), expired["challenge_id"]),
            )
        expired_result = client.post(
            "/api/auth/email/verify",
            json={
                "email": "expired@example.com",
                "challenge_id": expired["challenge_id"],
                "code": sent_codes["expired@example.com"],
            },
        )
        assert expired_result.status_code == 401
    get_settings.cache_clear()


def test_email_code_is_rate_limited_and_registration_can_be_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    sent_codes = configure_auth(tmp_path, monkeypatch)
    with TestClient(app) as client:
        first = client.post("/api/auth/email/request", json={"email": "admin@example.com"})
        assert first.status_code == 202
        limited = client.post("/api/auth/email/request", json={"email": "admin@example.com"})
        assert limited.status_code == 429
        assert int(limited.headers["retry-after"]) > 0

    monkeypatch.setenv("AUTOFLOW_REGISTRATION_ENABLED", "false")
    get_settings.cache_clear()
    with TestClient(app) as client:
        unknown = client.post("/api/auth/email/request", json={"email": "unknown@example.com"})
        assert unknown.status_code == 202
        assert "unknown@example.com" not in sent_codes

    monkeypatch.setenv("AUTOFLOW_EMAIL_CODE_SECRET", "")
    get_settings.cache_clear()
    with TestClient(app) as client:
        unavailable = client.post("/api/auth/email/request", json={"email": "admin@example.com"})
        assert unavailable.status_code == 503
    get_settings.cache_clear()


def test_email_code_limits_requests_across_different_emails(tmp_path: Path, monkeypatch) -> None:
    configure_auth(tmp_path, monkeypatch)
    monkeypatch.setenv("AUTOFLOW_EMAIL_CODE_COOLDOWN_SECONDS", "0")
    monkeypatch.setenv("AUTOFLOW_EMAIL_CODE_IP_MAX_REQUESTS", "1")
    get_settings.cache_clear()
    with TestClient(app) as client:
        first = client.post("/api/auth/email/request", json={"email": "first@example.com"})
        second = client.post("/api/auth/email/request", json={"email": "second@example.com"})

        assert first.status_code == 202
        assert second.status_code == 429
        assert second.json()["detail"] == "验证码请求过于频繁，请稍后再试"
    get_settings.cache_clear()


def test_email_login_project_rbac_and_per_runner_token(tmp_path: Path, monkeypatch) -> None:
    sent_codes = configure_auth(tmp_path, monkeypatch)
    monkeypatch.setenv("AUTOFLOW_EMAIL_CODE_COOLDOWN_SECONDS", "0")
    get_settings.cache_clear()
    with TestClient(app) as client:
        assert client.get("/api/tasks").status_code == 401
        login = email_login(client, sent_codes, "admin@example.com")
        assert login.status_code == 200
        assert client.get("/api/auth/me").json()["is_admin"] is True

        project = client.post("/api/projects", json={"name": "Payments", "slug": "payments"}).json()
        project_id = project["id"]
        assert project["role"] == "owner"
        git_integration = client.put(
            f"/api/projects/{project_id}/git-integration",
            json={
                "provider": "github",
                "base_url": "https://api.github.com",
                "repository": "acme/payments",
                "token": "github-project-token",
            },
        )
        assert git_integration.status_code == 200
        assert "token" not in git_integration.json()

        created_user = client.post(
            "/api/users",
            json={"email": "viewer@example.com", "display_name": "Viewer"},
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
        viewer_login = email_login(client, sent_codes, "viewer@example.com")
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
            json={"id": "another-runner", "name": "Spoofed", "platform": "Linux"},
        )
        assert spoofed.status_code == 401
    get_settings.cache_clear()
