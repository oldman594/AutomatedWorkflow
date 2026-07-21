from __future__ import annotations

import json
import secrets
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any

from app.models import (
    Artifact,
    Event,
    Project,
    ProjectAccess,
    ProjectMember,
    ProjectRole,
    RunnerInfo,
    RunnerRegistration,
    Stage,
    Task,
    TaskCreate,
    TaskStatus,
    User,
    utc_now,
)
from app.protocol import MessageEnvelope


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    requirement TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    project_id TEXT NOT NULL DEFAULT 'default',
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
                    project_id TEXT NOT NULL DEFAULT 'default',
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
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    disabled INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    slug TEXT NOT NULL UNIQUE,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_members (
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS runner_tokens (
                    runner_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    label TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
                CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id, id);
                CREATE INDEX IF NOT EXISTS idx_runner_messages_replay
                    ON runner_messages(runner_id, direction, seq);
                CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, expires_at);
                CREATE INDEX IF NOT EXISTS idx_project_members_user
                    ON project_members(user_id, project_id);
                """
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
            if "runner_id" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN runner_id TEXT")
            if "project_id" not in columns:
                db.execute(
                    "ALTER TABLE tasks ADD COLUMN project_id TEXT NOT NULL DEFAULT 'default'"
                )
            if "include_local_changes" not in columns:
                db.execute(
                    "ALTER TABLE tasks ADD COLUMN include_local_changes INTEGER NOT NULL DEFAULT 0"
                )
            if "sync_to_source" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN sync_to_source INTEGER NOT NULL DEFAULT 0")
            runner_columns = {row["name"] for row in db.execute("PRAGMA table_info(runners)")}
            if "status" not in runner_columns:
                db.execute("ALTER TABLE runners ADD COLUMN status TEXT NOT NULL DEFAULT 'unknown'")
            if "metrics" not in runner_columns:
                db.execute("ALTER TABLE runners ADD COLUMN metrics TEXT NOT NULL DEFAULT '{}'")
            if "project_id" not in runner_columns:
                db.execute(
                    "ALTER TABLE runners ADD COLUMN project_id TEXT NOT NULL DEFAULT 'default'"
                )
            db.execute(
                """INSERT OR IGNORE INTO projects (id, name, slug, created_by, created_at)
                VALUES ('default', 'Default Project', 'default', 'system', ?)""",
                (utc_now(),),
            )

    def create_task(self, request: TaskCreate, default_model: str) -> Task:
        task_id = uuid.uuid4().hex[:12]
        now = utc_now()
        values = (
            task_id,
            request.title,
            request.requirement,
            request.repository,
            request.project_id or "default",
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
                    id, title, requirement, repository, project_id, runner_id, branch, model,
                    build_command, test_command, include_local_changes,
                    sync_to_source, auto_apply, auto_commit,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> Task:
        with self._connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._row_to_task(row)

    def list_tasks(self, limit: int = 100, project_ids: list[str] | None = None) -> list[Task]:
        with self._connect() as db:
            if project_ids is None:
                rows = db.execute(
                    "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            elif not project_ids:
                rows = []
            else:
                placeholders = ",".join("?" for _ in project_ids)
                rows = db.execute(
                    f"""SELECT * FROM tasks WHERE project_id IN ({placeholders})
                    ORDER BY created_at DESC LIMIT ?""",
                    [*project_ids, limit],
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
                    id, project_id, name, platform, roots, capabilities, last_seen, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    project_id = excluded.project_id,
                    name = excluded.name,
                    platform = excluded.platform,
                    roots = excluded.roots,
                    capabilities = excluded.capabilities,
                    last_seen = excluded.last_seen""",
                (
                    registration.id,
                    registration.project_id,
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
            result = db.execute(f"UPDATE runners SET {', '.join(assignments)} WHERE id = ?", values)
            if result.rowcount == 0:
                raise KeyError(runner_id)
        return self.get_runner(runner_id)

    def update_runner_capabilities(self, runner_id: str, capabilities: list[str]) -> RunnerInfo:
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

    def list_runners(self, project_ids: list[str] | None = None) -> list[RunnerInfo]:
        with self._connect() as db:
            if project_ids is None:
                rows = db.execute("SELECT * FROM runners ORDER BY name, id").fetchall()
            elif not project_ids:
                rows = []
            else:
                placeholders = ",".join("?" for _ in project_ids)
                rows = db.execute(
                    f"SELECT * FROM runners WHERE project_id IN ({placeholders}) ORDER BY name, id",
                    project_ids,
                ).fetchall()
        return [self._row_to_runner(row) for row in rows]

    def count_users(self) -> int:
        with self._connect() as db:
            row = db.execute("SELECT COUNT(*) AS count FROM users").fetchone()
        return int(row["count"])

    def create_user(
        self,
        email: str,
        display_name: str,
        password_hash: str,
        *,
        is_admin: bool = False,
    ) -> User:
        user_id = uuid.uuid4().hex
        now = utc_now()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO users (
                    id, email, display_name, password_hash, is_admin, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    email.strip().lower(),
                    display_name.strip(),
                    password_hash,
                    int(is_admin),
                    now,
                ),
            )
        return self.get_user(user_id)

    def get_user(self, user_id: str) -> User:
        with self._connect() as db:
            row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise KeyError(user_id)
        return self._row_to_user(row)

    def get_user_by_email(self, email: str) -> tuple[User, str]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM users WHERE email = ?", (email.strip().lower(),)
            ).fetchone()
        if row is None:
            raise KeyError(email)
        return self._row_to_user(row), str(row["password_hash"])

    def create_session(self, token_hash: str, user_id: str, expires_at: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO sessions (
                    token_hash, user_id, expires_at, created_at
                ) VALUES (?, ?, ?, ?)""",
                (token_hash, user_id, expires_at, utc_now()),
            )

    def get_session_user(self, token_hash: str, now: str) -> User:
        with self._connect() as db:
            row = db.execute(
                """SELECT users.* FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ? AND sessions.revoked_at IS NULL
                  AND sessions.expires_at > ? AND users.disabled = 0""",
                (token_hash, now),
            ).fetchone()
        if row is None:
            raise KeyError("session")
        return self._row_to_user(row)

    def revoke_session(self, token_hash: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE sessions SET revoked_at = ? WHERE token_hash = ?",
                (utc_now(), token_hash),
            )

    def create_project(self, name: str, slug: str, owner_id: str) -> ProjectAccess:
        project_id = uuid.uuid4().hex[:12]
        now = utc_now()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO projects (id, name, slug, created_by, created_at)
                VALUES (?, ?, ?, ?, ?)""",
                (project_id, name.strip(), slug.strip(), owner_id, now),
            )
            db.execute(
                """INSERT INTO project_members (project_id, user_id, role, created_at)
                VALUES (?, ?, ?, ?)""",
                (project_id, owner_id, ProjectRole.OWNER, now),
            )
        return ProjectAccess(**self.get_project(project_id).model_dump(), role=ProjectRole.OWNER)

    def get_project(self, project_id: str) -> Project:
        with self._connect() as db:
            row = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise KeyError(project_id)
        return Project(**dict(row))

    def list_user_projects(self, user_id: str) -> list[ProjectAccess]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT projects.*, project_members.role FROM project_members
                JOIN projects ON projects.id = project_members.project_id
                WHERE project_members.user_id = ? ORDER BY projects.name""",
                (user_id,),
            ).fetchall()
        return [ProjectAccess(**dict(row)) for row in rows]

    def get_project_role(self, project_id: str, user_id: str) -> ProjectRole:
        with self._connect() as db:
            row = db.execute(
                """SELECT role FROM project_members
                WHERE project_id = ? AND user_id = ?""",
                (project_id, user_id),
            ).fetchone()
        if row is None:
            raise KeyError(project_id)
        return ProjectRole(row["role"])

    def upsert_project_member(
        self, project_id: str, user_id: str, role: ProjectRole
    ) -> ProjectMember:
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO project_members (project_id, user_id, role, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id, user_id) DO UPDATE SET role = excluded.role""",
                (project_id, user_id, role, utc_now()),
            )
        return next(
            member for member in self.list_project_members(project_id) if member.user_id == user_id
        )

    def list_project_members(self, project_id: str) -> list[ProjectMember]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT project_members.project_id, project_members.user_id,
                    users.email, users.display_name, project_members.role
                FROM project_members JOIN users ON users.id = project_members.user_id
                WHERE project_members.project_id = ? ORDER BY users.email""",
                (project_id,),
            ).fetchall()
        return [ProjectMember(**dict(row)) for row in rows]

    def save_runner_token(
        self,
        runner_id: str,
        project_id: str,
        label: str,
        token_hash: str,
        created_by: str,
    ) -> str:
        now = utc_now()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO runner_tokens (
                    runner_id, project_id, label, token_hash, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(runner_id) DO UPDATE SET
                    project_id = excluded.project_id,
                    label = excluded.label,
                    token_hash = excluded.token_hash,
                    created_by = excluded.created_by,
                    created_at = excluded.created_at,
                    revoked_at = NULL""",
                (runner_id, project_id, label, token_hash, created_by, now),
            )
        return now

    def verify_runner_token(self, runner_id: str, token_hash: str) -> str:
        with self._connect() as db:
            row = db.execute(
                """SELECT project_id, token_hash FROM runner_tokens
                WHERE runner_id = ? AND revoked_at IS NULL""",
                (runner_id,),
            ).fetchone()
        if row is None or not secrets.compare_digest(str(row["token_hash"]), token_hash):
            raise KeyError(runner_id)
        return str(row["project_id"])

    def has_active_runner_token(self, runner_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                """SELECT 1 FROM runner_tokens
                WHERE runner_id = ? AND revoked_at IS NULL""",
                (runner_id,),
            ).fetchone()
        return row is not None

    def revoke_runner_token(self, runner_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE runner_tokens SET revoked_at = ? WHERE runner_id = ?",
                (utc_now(), runner_id),
            )

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

    def record_runner_message(self, envelope: MessageEnvelope, direction: str) -> bool:
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
        allowed = {"status", "stage", "progress", "error", "cancel_requested", "branch"}
        invalid = set(fields) - allowed
        if invalid:
            raise ValueError(f"Unsupported fields: {sorted(invalid)}")
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = [self._db_value(value) for value in fields.values()]
        with self._lock, self._connect() as db:
            result = db.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", values + [task_id])
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
            if event_id is None:
                raise RuntimeError("SQLite did not return an event id")
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
            if artifact_id is None:
                raise RuntimeError("SQLite did not return an artifact id")
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

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> User:
        data = dict(row)
        data["is_admin"] = bool(data["is_admin"])
        data["disabled"] = bool(data["disabled"])
        data.pop("password_hash", None)
        return User(**data)
