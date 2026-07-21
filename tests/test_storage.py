import sqlite3
from pathlib import Path

from app.models import Stage, TaskCreate, TaskStatus
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
    updated = storage.update_task(task.id, status=TaskStatus.RUNNING, stage=Stage.PLANNER, progress=10)
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
        TaskCreate(title="Interrupted", requirement="Test restart recovery", repository="/tmp/repo"),
        "gpt-test",
    )
    storage.update_task(task.id, status=TaskStatus.RUNNING, stage=Stage.CODER)

    assert storage.recover_interrupted_tasks() == 1
    recovered = storage.get_task(task.id)
    assert recovered.status == TaskStatus.FAILED
    assert "服务重启" in recovered.error


def test_existing_database_adds_local_changes_column(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
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

    with sqlite3.connect(path) as db:
        columns = {row[1]: row for row in db.execute("PRAGMA table_info(tasks)")}
    assert columns["include_local_changes"][4] == "0"
    assert columns["sync_to_source"][4] == "0"
    assert "runner_id" in columns
