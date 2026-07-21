import sqlite3
from contextlib import closing
from pathlib import Path

from app.models import JobStatus, Stage, TaskCreate, TaskStatus
from app.storage import Storage


def test_task_event_and_artifact_round_trip(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "test.db")
    task = storage.create_task(
        TaskCreate(
            title="Test task",
            requirement="Implement test behavior",
            repository="/tmp/repo",
            include_local_changes=False,
            sync_to_source=False,
        ),
        "gpt-test",
    )

    assert task.status == TaskStatus.DRAFT
    assert task.include_local_changes is False
    assert task.sync_to_source is False
    updated = storage.update_task(
        task.id, status=TaskStatus.RUNNING, stage=Stage.PLANNER, progress=10
    )
    event = storage.add_event(task.id, "Planning", stage=Stage.PLANNER, data={"count": 3})
    artifact = storage.add_artifact(task.id, "plan", "{}")

    assert updated.stage == Stage.PLANNER
    assert storage.list_events(task.id)[0] == event
    assert storage.list_artifacts(task.id)[0] == artifact


def test_unknown_task_raises_key_error(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "test.db")
    try:
        storage.get_task("missing")
    except KeyError as exc:
        assert exc.args == ("missing",)
    else:
        raise AssertionError("Expected KeyError")


def test_recover_interrupted_tasks(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "test.db")
    task = storage.create_task(
        TaskCreate(
            title="Interrupted", requirement="Test restart recovery", repository="/tmp/repo"
        ),
        "gpt-test",
    )
    storage.update_task(task.id, status=TaskStatus.RUNNING, stage=Stage.CODER)

    assert storage.recover_interrupted_tasks() == 1
    recovered = storage.get_task(task.id)
    assert recovered.status == TaskStatus.FAILED
    assert "服务重启" in recovered.error


def test_durable_job_lease_recovery_checkpoint_and_retry(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "jobs.db")
    task = storage.create_task(
        TaskCreate(title="Durable", requirement="Recover leased job", repository="/tmp/repo"),
        "gpt-test",
    )
    storage.update_task(task.id, status=TaskStatus.QUEUED)
    storage.enqueue_job(task.id, "server", max_attempts=3)

    first = storage.lease_job("server", "worker-1", lease_seconds=60)
    assert first is not None
    assert first.attempts == 1
    assert storage.lease_job("server", "worker-2", lease_seconds=60) is None
    storage.checkpoint_job(task.id, Stage.CODER)
    assert storage.get_job_for_task(task.id).checkpoint == Stage.CODER

    with storage._connect() as db:
        db.execute(
            "UPDATE jobs SET lease_expires_at = ? WHERE task_id = ?",
            ("2000-01-01T00:00:00+00:00", task.id),
        )
    assert storage.recover_expired_jobs("server") == 1
    second = storage.lease_job("server", "worker-2", lease_seconds=60)
    assert second is not None
    assert second.attempts == 2
    storage.complete_job(task.id, "worker-2")
    assert storage.get_job_for_task(task.id).status == JobStatus.COMPLETED

    retry_task = storage.create_task(
        TaskCreate(title="Retry", requirement="Retry failed job", repository="/tmp/repo"),
        "gpt-test",
    )
    storage.update_task(retry_task.id, status=TaskStatus.FAILED, error="temporary")
    storage.enqueue_job(retry_task.id, "server", max_attempts=2)
    assert storage.lease_job("server", "worker-1", lease_seconds=60) is not None
    assert storage.fail_job(retry_task.id, "worker-1", "temporary", 1) is True
    assert storage.get_job_for_task(retry_task.id).status == JobStatus.PENDING
    assert storage.get_task(retry_task.id).status == TaskStatus.QUEUED

    with storage._connect() as db:
        db.execute(
            "UPDATE jobs SET available_at = ? WHERE task_id = ?",
            ("2000-01-01T00:00:00+00:00", retry_task.id),
        )
    assert storage.lease_job("server", "worker-2", lease_seconds=60) is not None
    assert storage.fail_job(retry_task.id, "worker-2", "permanent", 1) is False
    assert storage.get_job_for_task(retry_task.id).status == JobStatus.FAILED


def test_existing_database_adds_local_changes_column(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            """CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                requirement TEXT NOT NULL,
                repository TEXT NOT NULL,
                branch TEXT NOT NULL,
                model TEXT NOT NULL,
                build_command TEXT,
                test_command TEXT,
                auto_apply INTEGER NOT NULL,
                auto_commit INTEGER NOT NULL,
                status TEXT NOT NULL,
                stage TEXT,
                progress INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )

    Storage(path)

    with closing(sqlite3.connect(path)) as db:
        columns = {row[1]: row for row in db.execute("PRAGMA table_info(tasks)")}
    assert columns["include_local_changes"][4] == "0"
    assert columns["sync_to_source"][4] == "0"
    assert "runner_id" in columns
