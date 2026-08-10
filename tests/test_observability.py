import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.alerts import AlertDispatcher
from app.config import Settings, get_settings
from app.main import app, configuration_readiness
from app.observability import JsonFormatter
from app.storage import Storage


def test_request_id_readiness_and_protected_metrics(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AUTOFLOW_DATABASE_PATH", str(tmp_path / "observability.db"))
    monkeypatch.setenv("AUTOFLOW_METRICS_TOKEN", "metrics-secret")
    monkeypatch.setenv("AUTOFLOW_MOCK_LLM", "true")
    get_settings.cache_clear()
    with TestClient(app) as client:
        health = client.get("/api/health", headers={"X-Request-ID": "request-123"})
        assert health.headers["x-request-id"] == "request-123"
        assert client.get("/api/ready").json()["status"] == "ready"
        assert client.get("/metrics").status_code == 401
        metrics = client.get("/metrics", headers={"Authorization": "Bearer metrics-secret"})
        assert metrics.status_code == 200
        assert "autoflow_http_requests_total" in metrics.text
        assert "autoflow_job_queue_depth" in metrics.text
    get_settings.cache_clear()


def test_json_formatter_emits_machine_readable_context() -> None:
    import logging

    record = logging.LogRecord(
        "autoflow.test",
        logging.ERROR,
        __file__,
        1,
        "task.failed",
        (),
        None,
    )
    record.task_id = "task-123"
    record.error = "boom"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["event"] == "task.failed"
    assert payload["task_id"] == "task-123"
    assert payload["error"] == "boom"


def test_configuration_readiness_fails_closed_for_incomplete_auth(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "readiness.db")
    settings = Settings(
        auth_enabled=True,
        mock_llm=True,
        bootstrap_admin_email=None,
        email_code_secret=None,
        smtp_host=None,
        smtp_from_email=None,
        credential_encryption_key=None,
    )
    try:
        status = configuration_readiness(settings, storage)
    finally:
        storage.close()

    assert status["ready"] is False
    assert "Authentication has no initialized user" in status["errors"]
    assert "AUTOFLOW_EMAIL_CODE_SECRET is not configured" in status["errors"]


def test_alert_webhook_is_delivered_in_background(monkeypatch) -> None:
    delivered: list[dict] = []

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            pass

        def post(self, url: str, json: dict) -> httpx.Response:
            delivered.append({"url": url, "json": json})
            return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr("app.alerts.httpx.Client", FakeClient)
    dispatcher = AlertDispatcher(Settings(alert_webhook_url="https://alerts.example.test/hook"))
    dispatcher.start()
    dispatcher.notify("task.retry_exhausted", task_id="task-1")
    dispatcher.stop()

    assert delivered == [
        {
            "url": "https://alerts.example.test/hook",
            "json": {"event": "task.retry_exhausted", "task_id": "task-1"},
        }
    ]
