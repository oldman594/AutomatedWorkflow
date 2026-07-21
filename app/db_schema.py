from sqlalchemy import (
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)

metadata = MetaData()

users = Table(
    "users",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("email", String(320), nullable=False, unique=True),
    Column("display_name", String(160), nullable=False),
    Column("password_hash", Text, nullable=False),
    Column("is_admin", Integer, nullable=False, server_default=text("0")),
    Column("disabled", Integer, nullable=False, server_default=text("0")),
    Column("created_at", String(40), nullable=False),
)

projects = Table(
    "projects",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("name", String(160), nullable=False),
    Column("slug", String(80), nullable=False, unique=True),
    Column("created_by", String(64), nullable=False),
    Column("created_at", String(40), nullable=False),
)

tasks = Table(
    "tasks",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("title", String(160), nullable=False),
    Column("requirement", Text, nullable=False),
    Column("repository", Text, nullable=False),
    Column("project_id", String(64), nullable=False, server_default=text("'default'")),
    Column("runner_id", String(100)),
    Column("branch", String(200), nullable=False),
    Column("model", String(200), nullable=False),
    Column("build_command", Text),
    Column("test_command", Text),
    Column("include_local_changes", Integer, nullable=False, server_default=text("1")),
    Column("sync_to_source", Integer, nullable=False, server_default=text("1")),
    Column("auto_apply", Integer, nullable=False),
    Column("auto_commit", Integer, nullable=False),
    Column("status", String(40), nullable=False),
    Column("stage", String(40)),
    Column("progress", Integer, nullable=False, server_default=text("0")),
    Column("error", Text),
    Column("cancel_requested", Integer, nullable=False, server_default=text("0")),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
)

events = Table(
    "events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("task_id", String(64), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
    Column("stage", String(40)),
    Column("level", String(30), nullable=False),
    Column("message", Text, nullable=False),
    Column("data", Text, nullable=False, server_default=text("'{}'")),
    Column("created_at", String(40), nullable=False),
)

artifacts = Table(
    "artifacts",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("task_id", String(64), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
    Column("kind", String(100), nullable=False),
    Column("content", Text, nullable=False),
    Column("created_at", String(40), nullable=False),
)

runners = Table(
    "runners",
    metadata,
    Column("id", String(100), primary_key=True),
    Column("project_id", String(64), nullable=False, server_default=text("'default'")),
    Column("name", String(160), nullable=False),
    Column("platform", String(160), nullable=False),
    Column("version", String(40), nullable=False, server_default=text("'0.0.0'")),
    Column("roots", Text, nullable=False, server_default=text("'[]'")),
    Column("capabilities", Text, nullable=False, server_default=text("'[]'")),
    Column("status", String(40), nullable=False, server_default=text("'unknown'")),
    Column("metrics", Text, nullable=False, server_default=text("'{}'")),
    Column("last_seen", String(40), nullable=False),
    Column("created_at", String(40), nullable=False),
)

runner_messages = Table(
    "runner_messages",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("runner_id", String(100), nullable=False),
    Column("task_id", String(64)),
    Column("direction", String(20), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("type", String(60), nullable=False),
    Column("envelope", Text, nullable=False),
    Column("created_at", String(40), nullable=False),
    UniqueConstraint("runner_id", "direction", "seq"),
)

project_members = Table(
    "project_members",
    metadata,
    Column(
        "project_id", String(64), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    ),
    Column("user_id", String(64), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("role", String(20), nullable=False),
    Column("created_at", String(40), nullable=False),
)

sessions = Table(
    "sessions",
    metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("user_id", String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("expires_at", String(40), nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("revoked_at", String(40)),
)

email_challenges = Table(
    "email_challenges",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("email", String(320), nullable=False),
    Column("request_ip", String(64), nullable=False),
    Column("code_hash", String(64), nullable=False),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("max_attempts", Integer, nullable=False),
    Column("expires_at", String(40), nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("consumed_at", String(40)),
)

runner_tokens = Table(
    "runner_tokens",
    metadata,
    Column("runner_id", String(100), primary_key=True),
    Column("project_id", String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("label", String(160), nullable=False),
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("created_by", String(64), ForeignKey("users.id"), nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("revoked_at", String(40)),
)

jobs = Table(
    "jobs",
    metadata,
    Column("id", String(64), primary_key=True),
    Column(
        "task_id",
        String(64),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    ),
    Column("target", String(120), nullable=False),
    Column("status", String(30), nullable=False),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("max_attempts", Integer, nullable=False),
    Column("available_at", String(40), nullable=False),
    Column("lease_owner", String(160)),
    Column("lease_expires_at", String(40)),
    Column("checkpoint", String(40)),
    Column("last_error", Text),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
)

permission_requests = Table(
    "permission_requests",
    metadata,
    Column("id", String(80), primary_key=True),
    Column("task_id", String(64), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
    Column("runner_id", String(100), nullable=False),
    Column("operation", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("status", String(20), nullable=False),
    Column("requested_at", String(40), nullable=False),
    Column("resolved_by", String(64)),
    Column("resolved_at", String(40)),
    Column("result_reason", Text),
    Column("result_sent_at", String(40)),
)

git_integrations = Table(
    "git_integrations",
    metadata,
    Column(
        "project_id",
        String(64),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("provider", String(20), nullable=False),
    Column("base_url", Text, nullable=False),
    Column("repository", String(500), nullable=False),
    Column("encrypted_token", Text, nullable=False),
    Column("created_by", String(64), nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
)

Index("idx_events_task", events.c.task_id, events.c.id)
Index("idx_artifacts_task", artifacts.c.task_id, artifacts.c.id)
Index(
    "idx_runner_messages_replay",
    runner_messages.c.runner_id,
    runner_messages.c.direction,
    runner_messages.c.seq,
)
Index("idx_sessions_user", sessions.c.user_id, sessions.c.expires_at)
Index("idx_email_challenges_email", email_challenges.c.email, email_challenges.c.created_at)
Index("idx_email_challenges_expiry", email_challenges.c.expires_at)
Index("idx_email_challenges_ip", email_challenges.c.request_ip, email_challenges.c.created_at)
Index("idx_project_members_user", project_members.c.user_id, project_members.c.project_id)
Index("idx_jobs_claim", jobs.c.target, jobs.c.status, jobs.c.available_at, jobs.c.created_at)
Index("idx_permissions_runner", permission_requests.c.runner_id, permission_requests.c.status)
