from importlib.resources import files
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

BASE_DIR = Path(__file__).resolve().parent.parent


def migration_directory() -> Path:
    source_directory = BASE_DIR / "migrations"
    if source_directory.is_dir():
        return source_directory
    return Path(str(files("migrations")))


def upgrade_database(engine: Engine) -> None:
    config_path = BASE_DIR / "alembic.ini"
    config = Config(str(config_path)) if config_path.exists() else Config()
    config.set_main_option("script_location", str(migration_directory()))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
