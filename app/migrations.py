from importlib.resources import files
from pathlib import Path
from threading import Lock

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

BASE_DIR = Path(__file__).resolve().parent.parent
MIGRATION_LOCK_ID = int.from_bytes(b"autoflow", "big", signed=True)
_migration_lock = Lock()


def migration_directory() -> Path:
    source_directory = BASE_DIR / "migrations"
    if source_directory.is_dir():
        return source_directory
    return Path(str(files("migrations")))


def upgrade_database(engine: Engine) -> None:
    with _migration_lock:
        if engine.dialect.name == "postgresql":
            with engine.begin() as connection:
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(:lock_id)"),
                    {"lock_id": MIGRATION_LOCK_ID},
                )
                _upgrade_with_connection(connection)
            return
        with engine.connect() as connection:
            _upgrade_with_connection(connection)


def _upgrade_with_connection(connection: Connection) -> None:
    config_path = BASE_DIR / "alembic.ini"
    config = Config(str(config_path)) if config_path.exists() else Config()
    config.set_main_option("script_location", str(migration_directory()))
    config.attributes["connection"] = connection
    command.upgrade(config, "head")
