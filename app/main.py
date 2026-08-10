from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.alerts import AlertDispatcher
from app.api import router
from app.auth import initialize_identity
from app.config import Settings, get_settings
from app.observability import (
    configure_logging,
    configure_tracing,
    metrics_response,
    observe_request,
)
from app.sandbox import sandbox_readiness
from app.storage import Storage
from app.worker import DurableTaskWorker
from app.workflow import WorkflowEngine

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    storage = Storage(settings.database_url or settings.database_path)
    initialize_identity(storage, settings)
    app.state.storage = storage
    engine = WorkflowEngine(settings, storage)
    alerts = AlertDispatcher(settings)
    worker = DurableTaskWorker(settings, storage, engine, alerts)
    app.state.engine = engine
    app.state.worker = worker
    app.state.alerts = alerts
    alerts.start()
    worker.start()
    try:
        yield
    finally:
        worker.stop()
        alerts.stop()
        storage.close()


app = FastAPI(title="AutoFlow", version=__version__, lifespan=lifespan)
configure_tracing(app, get_settings())
app.include_router(router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    return await observe_request(request, call_next)


@app.get("/metrics", include_in_schema=False)
def metrics(request: Request):
    settings = get_settings()
    if settings.metrics_token:
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        expected = settings.metrics_token.get_secret_value()
        if scheme.lower() != "bearer" or not secrets.compare_digest(token, expected):
            raise HTTPException(status_code=401, detail="Invalid metrics token")
    return metrics_response(request.app.state.storage)


@app.get("/api/ready", include_in_schema=False)
def ready(request: Request) -> dict[str, object]:
    settings = get_settings()
    database = request.app.state.storage.ping()
    worker = request.app.state.worker.is_healthy()
    sandbox = sandbox_readiness(request.app.state.engine.shell_executor)
    configuration = configuration_readiness(settings, request.app.state.storage)
    if not database or not worker or not sandbox["ready"] or not configuration["ready"]:
        raise HTTPException(
            status_code=503,
            detail={
                "database": database,
                "worker": worker,
                "sandbox": sandbox,
                "configuration": configuration,
            },
        )
    return {
        "status": "ready",
        "database": database,
        "worker": worker,
        "sandbox": sandbox,
        "configuration": configuration,
    }


def configuration_readiness(settings: Settings, storage: Storage) -> dict[str, object]:
    errors: list[str] = []
    if not settings.mock_llm:
        errors.extend(settings.route_errors())
    if settings.auth_enabled:
        if storage.count_users() == 0:
            errors.append("Authentication has no initialized user")
        required = {
            "AUTOFLOW_EMAIL_CODE_SECRET": settings.email_code_secret,
            "AUTOFLOW_SMTP_HOST": settings.smtp_host,
            "AUTOFLOW_SMTP_FROM_EMAIL": settings.smtp_from_email,
            "AUTOFLOW_CREDENTIAL_ENCRYPTION_KEY": settings.credential_encryption_key,
        }
        errors.extend(f"{name} is not configured" for name, value in required.items() if not value)
    return {"ready": not errors, "errors": errors}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
