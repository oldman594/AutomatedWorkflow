from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from threading import Lock
from typing import Any

from app.models import (
    Artifact,
    Event,
    RunnerInfo,
    RunnerRegistration,
    Stage,
    Task,
    TaskCreate,
    TaskStatus,
    utc_now,
)
from app.protocol import MessageEnvelope


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    requirement TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    runner_id TEXT,
                    branch TEXT NOT NULL,
                    model TEXT NOT NULL,
                    build_command TEXT,
                    test_command TEXT,
                    include_local_changes INTEGER NOT NULL DEFAULT 1,
                    sync_to_source INTEGER NOT NULL DEFAULT 1,
                    auto_apply INTEGER NOT NULL,
                    auto_commit INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT,
                    progress INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    stage TEXT,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    data TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runners (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    roots TEXT NOT NULL DEFAULT '[]',
                    capabilities TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'unknown',
                    metrics TEXT NOT NULL DEFAULT '{}',
                    last_seen TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runner_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    runner_id TEXT NOT NULL,
                    task_id TEXT,
                    direction TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    envelope TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(runner_id, direction, seq)
                );
                CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
                CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id, id);
                CREATE INDEX IF NOT EXISTS idx_runner_messages_replay
                    ON runner_messages(runner_id, direction, seq);
                """
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
            if "runner_id" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN runner_id TEXT")
            if "include_local_changes" not in columns:
                db.execute(
                    "ALTER TABLE tasks ADD COLUMN include_local_changes "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            if "sync_to_source" not in columns:
                db.execute(
                    "ALTER TABLE tasks ADD COLUMN sync_to_source "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            runner_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(runners)")
            }
            if "status" not in runner_columns:
                db.execute(
                    "ALTER TABLE runners ADD COLUMN status TEXT NOT NULL DEFAULT 'unknown'"
                )
            if "metrics" not in runner_columns:
                db.execute(
                    "ALTER TABLE runners ADD COLUMN metrics TEXT NOT NULL DEFAULT '{}'"
                )

    def create_task(self, request: TaskCreate, default_model: str) -> Task:
        task_id = uuid.uuid4().hex[:12]
        now = utc_now()
        values = (
            task_id,
            request.title,
            request.requirement,
            request.repository,
            request.runner_id,
            request.branch,
            request.model or default_model,
            request.build_command,
            request.test_command,
            int(request.include_local_changes),
            int(request.sync_to_source),
            int(request.auto_apply),
            int(request.auto_commit),
            TaskStatus.DRAFT,
            now,
            now,
        )
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO tasks (
                    id, title, requirement, repository, runner_id, branch, model,
                    build_command, test_command, include_local_changes,
                    sync_to_source, auto_apply, auto_commit,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> Task:
        with self._connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._row_to_task(row)

    def list_tasks(self, limit: int = 100) -> list[Task]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def recover_interrupted_tasks(self) -> int:
        now = utc_now()
        with self._lock, self._connect() as db:
            result = db.execute(
                """UPDATE tasks
                SET status = ?, error = ?, updated_at = ?
                WHERE status IN (?, ?, ?, ?, ?)""",
                (
                    TaskStatus.FAILED,
                    "服务重启中断了任务，请创建新任务重试",
                    now,
                    TaskStatus.QUEUED,
                    TaskStatus.ASSIGNED,
                    TaskStatus.ACCEPTED,
                    TaskStatus.RUNNING,
                    TaskStatus.WAIT_PERMISSION,
                ),
            )
            return result.rowcount

    def upsert_runner(self, registration: RunnerRegistration) -> RunnerInfo:
        now = utc_now()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO runners (
                    id, name, platform, roots, capabilities, last_seen, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    platform = excluded.platform,
                    roots = excluded.roots,
                    capabilities = excluded.capabilities,
                    last_seen = excluded.last_seen""",
                (
                    registration.id,
                    registration.name,
                    registration.platform,
                    json.dumps(registration.roots),
                    json.dumps(registration.capabilities),
                    now,
                    now,
                ),
            )
        return self.get_runner(registration.id)

    def touch_runner(
        self,
        runner_id: str,
        *,
        status: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> RunnerInfo:
        assignments = ["last_seen = ?"]
        values: list[Any] = [utc_now()]
        if status is not None:
            assignments.append("status = ?")
            values.append(status)
        if metrics is not None:
            assignments.append("metrics = ?")
            values.append(json.dumps(metrics))
        values.append(runner_id)
        with self._lock, self._connect() as db:
            result = db.execute(
                f"UPDATE runners SET {', '.join(assignments)} WHERE id = ?", values
            )
            if result.rowcount == 0:
                raise KeyError(runner_id)
        return self.get_runner(runner_id)

    def update_runner_capabilities(
        self, runner_id: str, capabilities: list[str]
    ) -> RunnerInfo:
        with self._lock, self._connect() as db:
            result = db.execute(
                "UPDATE runners SET capabilities = ?, last_seen = ? WHERE id = ?",
                (json.dumps(capabilities), utc_now(), runner_id),
            )
            if result.rowcount == 0:
                raise KeyError(runner_id)
        return self.get_runner(runner_id)

    def get_runner(self, runner_id: str) -> RunnerInfo:
        with self._connect() as db:
            row = db.execute("SELECT * FROM runners WHERE id = ?", (runner_id,)).fetchone()
        if row is None:
            raise KeyError(runner_id)
        return self._row_to_runner(row)

    def list_runners(self) -> list[RunnerInfo]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM runners ORDER BY name, id").fetchall()
        return [self._row_to_runner(row) for row in rows]

    def lease_runner_task(self, runner_id: str) -> Task | None:
        now = utc_now()
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT id FROM tasks
                WHERE runner_id = ? AND status = ?
                ORDER BY created_at LIMIT 1""",
                (runner_id, TaskStatus.QUEUED),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (TaskStatus.RUNNING, now, row["id"]),
            )
            task_id = row["id"]
        return self.get_task(task_id)

    def assign_runner_task(self, runner_id: str) -> Task | None:
        now = utc_now()
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(
                """SELECT id FROM tasks
                WHERE runner_id = ? AND status IN (?, ?, ?, ?)
                ORDER BY created_at LIMIT 1""",
                (
                    runner_id,
                    TaskStatus.ASSIGNED,
                    TaskStatus.ACCEPTED,
                    TaskStatus.RUNNING,
                    TaskStatus.WAIT_PERMISSION,
                ),
            ).fetchone()
            if active is not None:
                return None
            row = db.execute(
                """SELECT id FROM tasks
                WHERE runner_id = ? AND status = ?
                ORDER BY created_at LIMIT 1""",
                (runner_id, TaskStatus.QUEUED),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (TaskStatus.ASSIGNED, now, row["id"]),
            )
            task_id = row["id"]
        return self.get_task(task_id)

    def get_runner_active_task(self, runner_id: str) -> Task | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT * FROM tasks
                WHERE runner_id = ? AND status IN (?, ?, ?, ?)
                ORDER BY created_at LIMIT 1""",
                (
                    runner_id,
                    TaskStatus.ASSIGNED,
                    TaskStatus.ACCEPTED,
                    TaskStatus.RUNNING,
                    TaskStatus.WAIT_PERMISSION,
                ),
            ).fetchone()
        return self._row_to_task(row) if row is not None else None

    def next_runner_sequence(self, runner_id: str, direction: str) -> int:
        with self._connect() as db:
            row = db.execute(
                """SELECT COALESCE(MAX(seq), 0) AS seq FROM runner_messages
                WHERE runner_id = ? AND direction = ?""",
                (runner_id, direction),
            ).fetchone()
        return int(row["seq"]) + 1

    def record_runner_message(
        self, envelope: MessageEnvelope, direction: str
    ) -> bool:
        if not envelope.runner_id:
            raise ValueError("Runner message is missing runnerId")
        stored = envelope
        if envelope.type.value == "Artifact" and "content" in envelope.payload:
            payload = dict(envelope.payload)
            content = str(payload.pop("content"))
            payload["contentSize"] = len(content.encode("utf-8"))
            stored = envelope.model_copy(update={"payload": payload})
        with self._lock, self._connect() as db:
            result = db.execute(
                """INSERT OR IGNORE INTO runner_messages (
                    runner_id, task_id, direction, seq, type, envelope, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    envelope.runner_id,
                    envelope.task_id,
                    direction,
                    envelope.seq,
                    envelope.type,
                    stored.to_json(),
                    utc_now(),
                ),
            )
            return result.rowcount > 0

    def list_runner_messages(
        self, runner_id: str, direction: str, after_seq: int = 0
    ) -> list[MessageEnvelope]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT envelope FROM runner_messages
                WHERE runner_id = ? AND direction = ? AND seq > ?
                ORDER BY seq""",
                (runner_id, direction, after_seq),
            ).fetchall()
        return [MessageEnvelope.model_validate_json(row["envelope"]) for row in rows]

    def update_task(self, task_id: str, **fields: Any) -> Task:
        allowed = {
            "status", "stage", "progress", "error", "cancel_requested", "branch"
        }
        invalid = set(fields) - allowed
        if invalid:
            raise ValueError(f"Unsupported fields: {sorted(invalid)}")
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = [self._db_value(value) for value in fields.values()]
        with self._lock, self._connect() as db:
            result = db.execute(
                f"UPDATE tasks SET {assignments} WHERE id = ?", values + [task_id]
            )
            if result.rowcount == 0:
                raise KeyError(task_id)
        return self.get_task(task_id)

    def add_event(
        self,
        task_id: str,
        message: str,
        *,
        stage: Stage | None = None,
        level: str = "info",
        data: dict[str, Any] | None = None,
    ) -> Event:
        now = utc_now()
        with self._lock, self._connect() as db:
            cursor = db.execute(
                """INSERT INTO events (task_id, stage, level, message, data, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (task_id, stage, level, message, json.dumps(data or {}), now),
            )
            event_id = cursor.lastrowid
        return Event(
            id=event_id,
            task_id=task_id,
            stage=stage,
            level=level,
            message=message,
            data=data or {},
            created_at=now,
        )

    def list_events(self, task_id: str, after: int = 0) -> list[Event]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE task_id = ? AND id > ? ORDER BY id",
                (task_id, after),
            ).fetchall()
        return [
            Event(
                id=row["id"],
                task_id=row["task_id"],
                stage=row["stage"],
                level=row["level"],
                message=row["message"],
                data=json.loads(row["data"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def add_artifact(self, task_id: str, kind: str, content: str) -> Artifact:
        now = utc_now()
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "INSERT INTO artifacts (task_id, kind, content, created_at) VALUES (?, ?, ?, ?)",
                (task_id, kind, content, now),
            )
            artifact_id = cursor.lastrowid
        return Artifact(
            id=artifact_id,
            task_id=task_id,
            kind=kind,
            content=content,
            created_at=now,
        )

    def list_artifacts(self, task_id: str) -> list[Artifact]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM artifacts WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
        return [Artifact(**dict(row)) for row in rows]

    @staticmethod
    def _db_value(value: Any) -> Any:
        if hasattr(value, "value"):
            return value.value
        if isinstance(value, bool):
            return int(value)
        return value

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        data = dict(row)
        data["include_local_changes"] = bool(data["include_local_changes"])
        data["sync_to_source"] = bool(data["sync_to_source"])
        data["auto_apply"] = bool(data["auto_apply"])
        data["auto_commit"] = bool(data["auto_commit"])
        data["cancel_requested"] = bool(data["cancel_requested"])
        return Task(**data)

    @staticmethod
    def _row_to_runner(row: sqlite3.Row) -> RunnerInfo:
        data = dict(row)
        data["roots"] = json.loads(data["roots"])
        data["capabilities"] = json.loads(data["capabilities"])
        data["metrics"] = json.loads(data["metrics"])
        data["online"] = False
        return RunnerInfo(**data)
