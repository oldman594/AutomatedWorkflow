from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

BASE_DIR = Path(__file__).resolve().parent.parent


def upgrade_database(engine: Engine) -> None:
    config = Config(str(BASE_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BASE_DIR / "migrations"))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
