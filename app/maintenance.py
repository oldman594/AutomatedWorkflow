from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.engine import make_url

from app.config import Settings
from app.database import normalize_database_url
from app.storage import Storage


def backup_database(database: str | Path, destination: Path) -> Path:
    url = make_url(normalize_database_url(database))
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if url.get_backend_name() == "sqlite":
        source_path = Path(url.database or "").resolve()
        with (
            closing(sqlite3.connect(source_path)) as source,
            closing(sqlite3.connect(destination)) as target,
        ):
            source.backup(target)
        return destination

    if url.get_backend_name() != "postgresql":
        raise RuntimeError(f"Unsupported backup database: {url.get_backend_name()}")
    if shutil.which("pg_dump") is None:
        raise RuntimeError("pg_dump is required for PostgreSQL backups")
    command = ["pg_dump", "--format=custom", "--file", str(destination)]
    if url.host:
        command.extend(["--host", url.host])
    if url.port:
        command.extend(["--port", str(url.port)])
    if url.username:
        command.extend(["--username", url.username])
    if url.database:
        command.extend(["--dbname", url.database])
    environment = dict(os.environ)
    if url.password:
        environment["PGPASSWORD"] = url.password
    subprocess.run(command, check=True, env=environment, timeout=3600)
    return destination


def cleanup(settings: Settings) -> dict[str, int]:
    now = datetime.now(UTC)
    storage = Storage(settings.database_url or settings.database_path)
    worktree_cutoff = now - timedelta(days=settings.worktree_retention_days)
    stale_worktrees = storage.list_stale_worktrees(worktree_cutoff.isoformat())
    removed = 0
    root = settings.worktree_root.expanduser().resolve()
    for raw_path in stale_worktrees:
        path = Path(raw_path).expanduser().resolve()
        if path != root and path.is_relative_to(root) and path.is_dir():
            shutil.rmtree(path)
            removed += 1
    try:
        counts = storage.cleanup_expired_data(
            message_cutoff=(now - timedelta(days=settings.message_retention_days)).isoformat(),
            execution_cutoff=(now - timedelta(days=settings.execution_retention_days)).isoformat(),
            session_cutoff=(now - timedelta(hours=settings.session_cleanup_hours)).isoformat(),
        )
    finally:
        storage.close()
    counts["worktrees"] = removed
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Maintain AutoFlow data")
    subcommands = parser.add_subparsers(dest="command", required=True)
    backup = subcommands.add_parser("backup")
    backup.add_argument("destination", type=Path)
    subcommands.add_parser("cleanup")
    args = parser.parse_args()
    settings = Settings()
    if args.command == "backup":
        path = backup_database(settings.database_url or settings.database_path, args.destination)
        print(path)
    else:
        print(json.dumps(cleanup(settings), ensure_ascii=False))


if __name__ == "__main__":
    main()
