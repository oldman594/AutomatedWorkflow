import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import Settings
from app.maintenance import backup_database, cleanup
from app.models import TaskCreate, TaskStatus
from app.storage import Storage


def test_sqlite_online_backup_contains_current_data(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    storage = Storage(database)
    task = storage.create_task(
        TaskCreate(title="Backup", requirement="Back up this task", repository="/tmp/repo"),
        "mock",
    )
    destination = backup_database(database, tmp_path / "backups" / "snapshot.db")
    restored = Storage(destination)

    assert restored.get_task(task.id).title == "Backup"
    storage.close()
    restored.close()


def test_cleanup_removes_expired_execution_data_and_safe_worktree(tmp_path: Path) -> None:
    worktree_root = tmp_path / "worktrees"
    worktree = worktree_root / "stale-task"
    worktree.mkdir(parents=True)
    (worktree / "generated.txt").write_text("stale", encoding="utf-8")
    settings = Settings(
        database_path=tmp_path / "cleanup.db",
        worktree_root=worktree_root,
        worktree_retention_days=30,
        execution_retention_days=90,
        message_retention_days=14,
    )
    storage = Storage(settings.database_path)
    task = storage.create_task(
        TaskCreate(title="Cleanup", requirement="Clean old execution data", repository="/tmp/repo"),
        "mock",
    )
    storage.add_event(task.id, "old event")
    storage.add_artifact(task.id, "worktree", str(worktree))
    storage.add_artifact(task.id, "validation", "large output")
    storage.add_artifact(task.id, "delivery", json.dumps({"accepted": True}))
    storage.update_task(task.id, status=TaskStatus.COMPLETED)
    old = (datetime.now(UTC) - timedelta(days=365)).isoformat()
    with storage._connect() as db:
        db.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (old, task.id))
        db.execute("UPDATE events SET created_at = ? WHERE task_id = ?", (old, task.id))
        db.execute("UPDATE artifacts SET created_at = ? WHERE task_id = ?", (old, task.id))
    storage.close()

    counts = cleanup(settings)

    assert counts["worktrees"] == 1
    assert counts["events"] == 1
    assert counts["artifacts"] == 2
    assert not worktree.exists()
    remaining = Storage(settings.database_path).list_artifacts(task.id)
    assert [artifact.kind for artifact in remaining] == ["delivery"]
