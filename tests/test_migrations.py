from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.storage import Storage


def test_postgresql_offline_migrations_render_to_head() -> None:
    root = Path(__file__).resolve().parent.parent
    output = StringIO()
    config = Config(str(root / "alembic.ini"), output_buffer=output)
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://autoflow:placeholder@postgres:5432/autoflow",
    )

    command.upgrade(config, "head", sql=True)

    sql = output.getvalue()
    assert "CREATE TABLE users" in sql
    assert "CREATE TABLE email_challenges" in sql
    assert "UPDATE alembic_version SET version_num='0003'" in sql


def test_concurrent_storage_initialization_serializes_migrations(tmp_path: Path) -> None:
    database = tmp_path / "concurrent.db"

    def initialize(_: int) -> bool:
        storage = Storage(database)
        try:
            return storage.ping()
        finally:
            storage.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(initialize, range(2)))

    assert results == [True, True]
