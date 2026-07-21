from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import router
from app.auth import initialize_identity
from app.config import get_settings
from app.storage import Storage
from app.worker import DurableTaskWorker
from app.workflow import WorkflowEngine

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    storage = Storage(settings.database_url or settings.database_path)
    initialize_identity(storage, settings)
    app.state.storage = storage
    engine = WorkflowEngine(settings, storage)
    worker = DurableTaskWorker(settings, storage, engine)
    app.state.engine = engine
    app.state.worker = worker
    worker.start()
    try:
        yield
    finally:
        worker.stop()
        storage.close()


app = FastAPI(title="AutoFlow", version="0.1.0", lifespan=lifespan)
app.include_router(router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
