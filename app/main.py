from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.alerts import AlertDispatcher
from app.api import router
from app.auth import initialize_identity
from app.config import get_settings
from app.observability import (
    configure_logging,
    configure_tracing,
    metrics_response,
    observe_request,
)
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


app = FastAPI(title="AutoFlow", version="0.1.0", lifespan=lifespan)
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
    database = request.app.state.storage.ping()
    worker = request.app.state.worker.is_healthy()
    if not database or not worker:
        raise HTTPException(status_code=503, detail={"database": database, "worker": worker})
    return {"status": "ready", "database": database, "worker": worker}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
