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
from app.workflow import WorkflowEngine

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    storage = Storage(settings.database_path)
    initialize_identity(storage, settings)
    interrupted = storage.recover_interrupted_tasks()
    app.state.storage = storage
    app.state.engine = WorkflowEngine(settings, storage)
    if interrupted:
        for task in storage.list_tasks():
            if task.status.value == "failed" and task.error and "服务重启" in task.error:
                storage.add_event(task.id, task.error, level="error")
    yield


app = FastAPI(title="AutoFlow", version="0.1.0", lifespan=lifespan)
app.include_router(router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
