"""Add persistent email authentication challenges.

Revision ID: 0003
Revises: 0002
"""

from alembic import op

from app.db_schema import email_challenges

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    email_challenges.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    # Preserve authentication audit data on rollback.
    pass
