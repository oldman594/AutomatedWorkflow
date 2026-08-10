"""Create the AutoFlow platform schema.

Revision ID: 0001
Revises: None
"""

from alembic import context, op
from sqlalchemy import Column, Integer, String, inspect, text

from app.db_schema import metadata

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _add_legacy_columns() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    inspector = inspect(bind)
    if "tasks" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("tasks")}
        additions = {
            "runner_id": Column("runner_id", String(100)),
            "project_id": Column(
                "project_id", String(64), nullable=False, server_default=text("'default'")
            ),
            "include_local_changes": Column(
                "include_local_changes", Integer, nullable=False, server_default=text("0")
            ),
            "sync_to_source": Column(
                "sync_to_source", Integer, nullable=False, server_default=text("0")
            ),
        }
        for name, column in additions.items():
            if name not in columns:
                op.add_column("tasks", column)
    if "runners" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("runners")}
        additions = {
            "project_id": Column(
                "project_id", String(64), nullable=False, server_default=text("'default'")
            ),
            "status": Column(
                "status", String(40), nullable=False, server_default=text("'unknown'")
            ),
            "metrics": Column("metrics", String(), nullable=False, server_default=text("'{}'")),
        }
        for name, column in additions.items():
            if name not in columns:
                op.add_column("runners", column)


def upgrade() -> None:
    bind = op.get_bind()
    metadata.create_all(bind=bind)
    _add_legacy_columns()
    bind.execute(
        text(
            """INSERT INTO projects (id, name, slug, created_by, created_at)
            VALUES ('default', 'Default Project', 'default', 'system', :created_at)
            ON CONFLICT (id) DO NOTHING"""
        ),
        {"created_at": "1970-01-01T00:00:00+00:00"},
    )


def downgrade() -> None:
    # The baseline migration intentionally preserves existing user data.
    pass
