"""Add permission approvals, Git integrations and Runner versions.

Revision ID: 0002
Revises: 0001
"""

from alembic import op
from sqlalchemy import Column, String, inspect, text

from app.db_schema import git_integrations, permission_requests

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    permission_requests.create(bind=bind, checkfirst=True)
    git_integrations.create(bind=bind, checkfirst=True)
    columns = {column["name"] for column in inspect(bind).get_columns("runners")}
    if "version" not in columns:
        op.add_column(
            "runners",
            Column("version", String(40), nullable=False, server_default=text("'0.0.0'")),
        )


def downgrade() -> None:
    # Preserve credentials and approval audit records on rollback.
    pass
