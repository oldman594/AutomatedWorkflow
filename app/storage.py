from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any

from sqlalchemy.engine import RowMapping

from app.database import Database, DatabaseSession
from app.migrations import upgrade_database
from app.models import (
    Artifact,
    Event,
    GitIntegrationInfo,
    GitProvider,
    Job,
    JobStatus,
    PermissionRequestRecord,
    PermissionStatus,
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
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path) if "://" not in str(path) else None
        self._lock = Lock()
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.database = Database(path)
        upgrade_database(self.database.engine)

    @contextmanager
    def _connect(self) -> Iterator[DatabaseSession]:
        with self.database.connect() as session:
            yield session

    def close(self) -> None:
        self.database.dispose()

    def ping(self) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT 1 AS ok").fetchone()
        return row is not None and int(row["ok"]) == 1

    def job_status_counts(self) -> list[tuple[str, str, int]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT target, status, COUNT(*) AS count FROM jobs
                GROUP BY target, status"""
            ).fetchall()
        return [(str(row["target"]), str(row["status"]), int(row["count"])) for row in rows]

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

    def enqueue_job(self, task_id: str, target: str, max_attempts: int) -> Job:
        now = utc_now()
        job_id = uuid.uuid4().hex
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO jobs (
                    id, task_id, target, status, max_attempts, available_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    target = excluded.target,
                    status = excluded.status,
                    attempts = 0,
                    max_attempts = excluded.max_attempts,
                    available_at = excluded.available_at,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_error = NULL,
                    updated_at = excluded.updated_at""",
                (
                    job_id,
                    task_id,
                    target,
                    JobStatus.PENDING,
                    max_attempts,
                    now,
                    now,
                    now,
                ),
            )
        return self.get_job_for_task(task_id)

    def get_job_for_task(self, task_id: str) -> Job:
        with self._connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return Job(**dict(row))

    def recover_expired_jobs(self, target: str | None = None) -> int:
        now = utc_now()
        with self._lock, self._connect() as db:
            conditions = "status = ? AND lease_expires_at <= ?"
            values: list[Any] = [JobStatus.LEASED, now]
            if target is not None:
                conditions += " AND target = ?"
                values.append(target)
            rows = db.execute(f"SELECT task_id FROM jobs WHERE {conditions}", values).fetchall()
            if not rows:
                return 0
            task_ids = [str(row["task_id"]) for row in rows]
            placeholders = ",".join("?" for _ in task_ids)
            db.execute(
                f"""UPDATE jobs SET status = ?, lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = ? WHERE task_id IN ({placeholders})""",
                [JobStatus.PENDING, now, *task_ids],
            )
            db.execute(
                f"""UPDATE tasks SET status = ?, error = NULL, updated_at = ?
                    WHERE id IN ({placeholders})""",
                [TaskStatus.QUEUED, now, *task_ids],
            )
            return len(task_ids)

    def lease_job(self, target: str, owner: str, lease_seconds: int) -> Job | None:
        now = datetime.now(UTC)
        now_text = now.isoformat()
        expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self._lock, self._connect() as db:
            expired = db.execute(
                """SELECT task_id FROM jobs
                WHERE target = ? AND status = ? AND lease_expires_at <= ?""",
                (target, JobStatus.LEASED, now_text),
            ).fetchall()
            if expired:
                task_ids = [str(row["task_id"]) for row in expired]
                placeholders = ",".join("?" for _ in task_ids)
                db.execute(
                    f"""UPDATE jobs SET status = ?, lease_owner = NULL,
                        lease_expires_at = NULL, updated_at = ?
                        WHERE task_id IN ({placeholders})""",
                    [JobStatus.PENDING, now_text, *task_ids],
                )
                db.execute(
                    f"""UPDATE tasks SET status = ?, error = NULL, updated_at = ?
                        WHERE id IN ({placeholders})""",
                    [TaskStatus.QUEUED, now_text, *task_ids],
                )
            lock_clause = " FOR UPDATE SKIP LOCKED" if self.database.dialect == "postgresql" else ""
            row = db.execute(
                """SELECT * FROM jobs
                WHERE target = ? AND status = ? AND available_at <= ?
                ORDER BY created_at LIMIT 1"""
                + lock_clause,
                (target, JobStatus.PENDING, now_text),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                """UPDATE jobs SET status = ?, attempts = attempts + 1,
                    lease_owner = ?, lease_expires_at = ?, updated_at = ?
                WHERE id = ?""",
                (JobStatus.LEASED, owner, expires_at, now_text, row["id"]),
            )
            leased = db.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
            if leased is None:
                raise RuntimeError("Leased job disappeared")
        return Job(**dict(leased))

    def renew_job(self, task_id: str, owner: str, lease_seconds: int) -> bool:
        expires_at = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat()
        with self._lock, self._connect() as db:
            result = db.execute(
                """UPDATE jobs SET lease_expires_at = ?, updated_at = ?
                WHERE task_id = ? AND status = ? AND lease_owner = ?""",
                (expires_at, utc_now(), task_id, JobStatus.LEASED, owner),
            )
            return result.rowcount > 0

    def checkpoint_job(self, task_id: str, stage: Stage) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE jobs SET checkpoint = ?, updated_at = ? WHERE task_id = ?",
                (stage, utc_now(), task_id),
            )

    def complete_job(
        self, task_id: str, owner: str, status: JobStatus = JobStatus.COMPLETED
    ) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """UPDATE jobs SET status = ?, lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE task_id = ? AND lease_owner = ?""",
                (status, utc_now(), task_id, owner),
            )

    def fail_job(self, task_id: str, owner: str, error: str, retry_base_seconds: int) -> bool:
        now = datetime.now(UTC)
        with self._lock, self._connect() as db:
            row = db.execute(
                """SELECT attempts, max_attempts FROM jobs
                WHERE task_id = ? AND lease_owner = ?""",
                (task_id, owner),
            ).fetchone()
            if row is None:
                return False
            retry = int(row["attempts"]) < int(row["max_attempts"])
            if retry:
                delay = retry_base_seconds * (2 ** max(0, int(row["attempts"]) - 1))
                available_at = (now + timedelta(seconds=delay)).isoformat()
                status = JobStatus.PENDING
            else:
                available_at = now.isoformat()
                status = JobStatus.FAILED
            db.execute(
                """UPDATE jobs SET status = ?, available_at = ?, lease_owner = NULL,
                    lease_expires_at = NULL, last_error = ?, updated_at = ?
                WHERE task_id = ?""",
                (status, available_at, error[-8000:], now.isoformat(), task_id),
            )
            if retry:
                db.execute(
                    """UPDATE tasks SET status = ?, error = ?, updated_at = ? WHERE id = ?""",
                    (
                        TaskStatus.QUEUED,
                        f"任务将在 {delay} 秒后重试：{error}"[-8000:],
                        now.isoformat(),
                        task_id,
                    ),
                )
            return retry

    def cancel_job(self, task_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """UPDATE jobs SET status = ?, lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE task_id = ? AND status = ?""",
                (JobStatus.CANCELLED, utc_now(), task_id, JobStatus.PENDING),
            )

    def cleanup_expired_data(
        self,
        *,
        message_cutoff: str,
        execution_cutoff: str,
        session_cutoff: str,
    ) -> dict[str, int]:
        terminal = (
            TaskStatus.COMPLETED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
            TaskStatus.WAITING_APPROVAL,
        )
        with self._lock, self._connect() as db:
            sessions = db.execute(
                """DELETE FROM sessions
                WHERE expires_at < ? OR (revoked_at IS NOT NULL AND revoked_at < ?)""",
                (session_cutoff, session_cutoff),
            ).rowcount
            email_challenges = db.execute(
                "DELETE FROM email_challenges WHERE expires_at < ?", (session_cutoff,)
            ).rowcount
            messages = db.execute(
                "DELETE FROM runner_messages WHERE created_at < ?", (message_cutoff,)
            ).rowcount
            events = db.execute(
                """DELETE FROM events WHERE created_at < ? AND task_id IN (
                    SELECT id FROM tasks WHERE status IN (?, ?, ?, ?)
                )""",
                (execution_cutoff, *terminal),
            ).rowcount
            artifacts = db.execute(
                """DELETE FROM artifacts WHERE created_at < ?
                AND kind NOT IN ('delivery', 'commit', 'mr_description')
                AND task_id IN (
                    SELECT id FROM tasks WHERE status IN (?, ?, ?, ?)
                )""",
                (execution_cutoff, *terminal),
            ).rowcount
            jobs = db.execute(
                """DELETE FROM jobs WHERE updated_at < ?
                AND status IN (?, ?, ?)""",
                (
                    execution_cutoff,
                    JobStatus.COMPLETED,
                    JobStatus.CANCELLED,
                    JobStatus.FAILED,
                ),
            ).rowcount
        return {
            "sessions": max(0, sessions),
            "email_challenges": max(0, email_challenges),
            "runner_messages": max(0, messages),
            "events": max(0, events),
            "artifacts": max(0, artifacts),
            "jobs": max(0, jobs),
        }

    def list_stale_worktrees(self, cutoff: str) -> list[str]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT artifacts.content FROM artifacts
                JOIN tasks ON tasks.id = artifacts.task_id
                WHERE artifacts.kind = 'worktree' AND tasks.updated_at < ?
                AND tasks.status IN (?, ?, ?, ?)""",
                (
                    cutoff,
                    TaskStatus.COMPLETED,
                    TaskStatus.CANCELLED,
                    TaskStatus.FAILED,
                    TaskStatus.WAITING_APPROVAL,
                ),
            ).fetchall()
        return [str(row["content"]) for row in rows]

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
                    id, project_id, name, platform, version, roots, capabilities,
                    last_seen, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    project_id = excluded.project_id,
                    name = excluded.name,
                    platform = excluded.platform,
                    version = excluded.version,
                    roots = excluded.roots,
                    capabilities = excluded.capabilities,
                    last_seen = excluded.last_seen""",
                (
                    registration.id,
                    registration.project_id,
                    registration.name,
                    registration.platform,
                    registration.version,
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
        if row is None:
            return 0
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

    def latest_email_challenge_at(self, email: str) -> str | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT created_at FROM email_challenges
                WHERE email = ? ORDER BY created_at DESC LIMIT 1""",
                (email.strip().lower(),),
            ).fetchone()
        return str(row["created_at"]) if row else None

    def count_email_challenges_since(self, request_ip: str, cutoff: str) -> int:
        with self._connect() as db:
            row = db.execute(
                """SELECT COUNT(*) AS count FROM email_challenges
                WHERE request_ip = ? AND created_at > ?""",
                (request_ip, cutoff),
            ).fetchone()
        return int(row["count"]) if row else 0

    def create_email_challenge(
        self,
        challenge_id: str,
        email: str,
        request_ip: str,
        code_hash: str,
        expires_at: str,
        max_attempts: int,
    ) -> None:
        now = utc_now()
        normalized_email = email.strip().lower()
        with self._lock, self._connect() as db:
            db.execute(
                """UPDATE email_challenges SET consumed_at = ?
                WHERE email = ? AND consumed_at IS NULL""",
                (now, normalized_email),
            )
            db.execute(
                """INSERT INTO email_challenges (
                    id, email, request_ip, code_hash, attempts, max_attempts,
                    expires_at, created_at
                ) VALUES (?, ?, ?, ?, 0, ?, ?, ?)""",
                (
                    challenge_id,
                    normalized_email,
                    request_ip,
                    code_hash,
                    max_attempts,
                    expires_at,
                    now,
                ),
            )

    def consume_email_challenge(
        self,
        challenge_id: str,
        email: str,
        code_hash: str,
        now: str,
    ) -> bool:
        with self._lock, self._connect() as db:
            consumed = db.execute(
                """UPDATE email_challenges SET consumed_at = ?
                WHERE id = ? AND email = ? AND code_hash = ?
                  AND consumed_at IS NULL AND expires_at > ?
                  AND attempts < max_attempts""",
                (now, challenge_id, email.strip().lower(), code_hash, now),
            )
            if consumed.rowcount == 1:
                return True
            db.execute(
                """UPDATE email_challenges SET attempts = attempts + 1
                WHERE id = ? AND email = ? AND consumed_at IS NULL
                  AND expires_at > ? AND attempts < max_attempts""",
                (challenge_id, email.strip().lower(), now),
            )
        return False

    def delete_email_challenge(self, challenge_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM email_challenges WHERE id = ?", (challenge_id,))

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

    def save_git_integration(
        self,
        project_id: str,
        provider: GitProvider,
        base_url: str,
        repository: str,
        encrypted_token: str,
        created_by: str,
    ) -> GitIntegrationInfo:
        now = utc_now()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO git_integrations (
                    project_id, provider, base_url, repository, encrypted_token,
                    created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (project_id) DO UPDATE SET
                    provider = excluded.provider,
                    base_url = excluded.base_url,
                    repository = excluded.repository,
                    encrypted_token = excluded.encrypted_token,
                    created_by = excluded.created_by,
                    updated_at = excluded.updated_at""",
                (
                    project_id,
                    provider,
                    base_url.rstrip("/"),
                    repository.strip().removesuffix(".git"),
                    encrypted_token,
                    created_by,
                    now,
                    now,
                ),
            )
        info, _ = self.get_git_integration(project_id)
        return info

    def get_git_integration(self, project_id: str) -> tuple[GitIntegrationInfo, str]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM git_integrations WHERE project_id = ?", (project_id,)
            ).fetchone()
        if row is None:
            raise KeyError(project_id)
        data = dict(row)
        encrypted_token = str(data.pop("encrypted_token"))
        data.pop("created_by", None)
        return GitIntegrationInfo(**data), encrypted_token

    def lease_runner_task(self, runner_id: str, lease_seconds: int = 60) -> Task | None:
        job = self.lease_job(f"runner:{runner_id}", runner_id, lease_seconds)
        if job is None:
            return None
        return self.update_task(job.task_id, status=TaskStatus.RUNNING)

    def assign_runner_task(self, runner_id: str, lease_seconds: int = 60) -> Task | None:
        job = self.lease_job(f"runner:{runner_id}", runner_id, lease_seconds)
        if job is None:
            return None
        return self.update_task(job.task_id, status=TaskStatus.ASSIGNED)

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
        if row is None:
            return 1
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
                """INSERT INTO runner_messages (
                    runner_id, task_id, direction, seq, type, envelope, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (runner_id, direction, seq) DO NOTHING""",
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

    def create_permission_request(
        self,
        request_id: str,
        task_id: str,
        runner_id: str,
        operation: str,
        reason: str,
    ) -> PermissionRequestRecord:
        now = utc_now()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO permission_requests (
                    id, task_id, runner_id, operation, reason, status, requested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO NOTHING""",
                (
                    request_id,
                    task_id,
                    runner_id,
                    operation,
                    reason,
                    PermissionStatus.PENDING,
                    now,
                ),
            )
        return self.get_permission_request(request_id)

    def get_permission_request(self, request_id: str) -> PermissionRequestRecord:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM permission_requests WHERE id = ?", (request_id,)
            ).fetchone()
        if row is None:
            raise KeyError(request_id)
        return PermissionRequestRecord(**dict(row))

    def list_permission_requests(self, task_id: str) -> list[PermissionRequestRecord]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM permission_requests
                WHERE task_id = ? ORDER BY requested_at""",
                (task_id,),
            ).fetchall()
        return [PermissionRequestRecord(**dict(row)) for row in rows]

    def resolve_permission_request(
        self,
        request_id: str,
        allowed: bool,
        resolved_by: str,
        reason: str,
    ) -> PermissionRequestRecord:
        status = PermissionStatus.APPROVED if allowed else PermissionStatus.DENIED
        with self._lock, self._connect() as db:
            result = db.execute(
                """UPDATE permission_requests SET status = ?, resolved_by = ?,
                    resolved_at = ?, result_reason = ?, result_sent_at = NULL
                WHERE id = ? AND status = ?""",
                (
                    status,
                    resolved_by,
                    utc_now(),
                    reason,
                    request_id,
                    PermissionStatus.PENDING,
                ),
            )
            if result.rowcount == 0:
                raise ValueError("Permission request is already resolved or missing")
        return self.get_permission_request(request_id)

    def list_unsent_permission_results(
        self, runner_id: str
    ) -> list[tuple[PermissionRequestRecord, str]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM permission_requests WHERE runner_id = ?
                AND status IN (?, ?) AND result_sent_at IS NULL
                ORDER BY resolved_at""",
                (runner_id, PermissionStatus.APPROVED, PermissionStatus.DENIED),
            ).fetchall()
        return [
            (PermissionRequestRecord(**dict(row)), str(row["result_reason"] or "")) for row in rows
        ]

    def mark_permission_result_sent(self, request_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE permission_requests SET result_sent_at = ? WHERE id = ?",
                (utc_now(), request_id),
            )

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
                VALUES (?, ?, ?, ?, ?, ?) RETURNING id""",
                (task_id, stage, level, message, json.dumps(data or {}), now),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("SQLite did not return an event id")
            event_id = int(row["id"])
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
                """INSERT INTO artifacts (task_id, kind, content, created_at)
                VALUES (?, ?, ?, ?) RETURNING id""",
                (task_id, kind, content, now),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("SQLite did not return an artifact id")
            artifact_id = int(row["id"])
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
    def _row_to_task(row: RowMapping) -> Task:
        data = dict(row)
        data["include_local_changes"] = bool(data["include_local_changes"])
        data["sync_to_source"] = bool(data["sync_to_source"])
        data["auto_apply"] = bool(data["auto_apply"])
        data["auto_commit"] = bool(data["auto_commit"])
        data["cancel_requested"] = bool(data["cancel_requested"])
        return Task(**data)

    @staticmethod
    def _row_to_runner(row: RowMapping) -> RunnerInfo:
        data = dict(row)
        data["roots"] = json.loads(data["roots"])
        data["capabilities"] = json.loads(data["capabilities"])
        data["metrics"] = json.loads(data["metrics"])
        data["online"] = False
        return RunnerInfo(**data)

    @staticmethod
    def _row_to_user(row: RowMapping) -> User:
        data = dict(row)
        data["is_admin"] = bool(data["is_admin"])
        data["disabled"] = bool(data["disabled"])
        data.pop("password_hash", None)
        return User(**data)
