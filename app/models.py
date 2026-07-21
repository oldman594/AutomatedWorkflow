from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class TaskStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    ASSIGNED = "assigned"
    ACCEPTED = "accepted"
    RUNNING = "running"
    WAIT_PERMISSION = "wait_permission"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Stage(StrEnum):
    PRODUCT = "product"
    PLANNER = "planner"
    READER = "reader"
    DESIGN = "design"
    CODER = "coder"
    BUILD = "build"
    TEST = "test"
    REVIEW = "review"
    ACCEPTANCE = "acceptance"
    DELIVERY = "delivery"


class TaskCreate(BaseModel):
    title: str = Field(min_length=2, max_length=160)
    requirement: str = Field(min_length=5, max_length=30_000)
    repository: str = Field(min_length=1)
    project_id: str | None = Field(default=None, max_length=100)
    runner_id: str | None = Field(default=None, max_length=100)
    branch: str = Field(default="", max_length=200)
    model: str | None = None
    build_command: str | None = Field(default=None, max_length=1000)
    test_command: str | None = Field(default=None, max_length=1000)
    include_local_changes: bool = True
    sync_to_source: bool = True
    auto_apply: bool = True
    auto_commit: bool = False

    @field_validator("branch", mode="before")
    @classmethod
    def normalize_empty_branch(cls, value: object) -> object:
        return "" if value is None else value

    @field_validator("runner_id", mode="before")
    @classmethod
    def normalize_empty_runner(cls, value: object) -> object:
        return None if value is None or value == "" else value


class Task(BaseModel):
    id: str
    title: str
    requirement: str
    repository: str
    project_id: str = "default"
    runner_id: str | None = None
    branch: str
    model: str
    build_command: str | None
    test_command: str | None
    include_local_changes: bool
    sync_to_source: bool
    auto_apply: bool
    auto_commit: bool
    status: TaskStatus
    stage: Stage | None = None
    progress: int = 0
    error: str | None = None
    cancel_requested: bool = False
    created_at: str
    updated_at: str


class Event(BaseModel):
    id: int
    task_id: str
    stage: Stage | None
    level: str
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class Artifact(BaseModel):
    id: int
    task_id: str
    kind: str
    content: str
    created_at: str


class TaskDetail(BaseModel):
    task: Task
    events: list[Event]
    artifacts: list[Artifact]


class RunnerRegistration(BaseModel):
    id: str = Field(min_length=2, max_length=100, pattern=r"^[a-zA-Z0-9._-]+$")
    name: str = Field(min_length=1, max_length=160)
    platform: str = Field(min_length=1, max_length=160)
    roots: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    project_id: str = "default"


class RunnerInfo(RunnerRegistration):
    online: bool = False
    status: str = "unknown"
    metrics: dict[str, Any] = Field(default_factory=dict)
    last_seen: str
    created_at: str


class ProjectRole(StrEnum):
    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


class User(BaseModel):
    id: str
    email: str
    display_name: str
    is_admin: bool = False
    disabled: bool = False
    created_at: str


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=1024)


class UserCreate(LoginRequest):
    display_name: str = Field(min_length=1, max_length=160)
    is_admin: bool = False


class ProjectCreate(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    slug: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")


class Project(BaseModel):
    id: str
    name: str
    slug: str
    created_by: str
    created_at: str


class ProjectAccess(Project):
    role: ProjectRole


class ProjectMemberCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: ProjectRole


class ProjectMember(BaseModel):
    project_id: str
    user_id: str
    email: str
    display_name: str
    role: ProjectRole


class RunnerTokenCreate(BaseModel):
    runner_id: str = Field(min_length=2, max_length=100, pattern=r"^[a-zA-Z0-9._-]+$")
    label: str = Field(default="Local Runner", min_length=1, max_length=160)


class RunnerTokenIssued(BaseModel):
    runner_id: str
    project_id: str
    token: str
    created_at: str


class RunnerLease(BaseModel):
    task: Task | None = None


class RunnerTaskUpdate(BaseModel):
    status: TaskStatus | None = None
    stage: Stage | None = None
    progress: int | None = Field(default=None, ge=0, le=100)
    error: str | None = None
    cancel_requested: bool | None = None
    branch: str | None = None


class RunnerEventCreate(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    stage: Stage | None = None
    level: str = Field(default="info", max_length=30)
    data: dict[str, Any] = Field(default_factory=dict)


class RunnerArtifactCreate(BaseModel):
    kind: str = Field(min_length=1, max_length=100)
    content: str = Field(max_length=100_000_000)


class ApprovalRequest(BaseModel):
    action: str = Field(pattern="^(complete|commit)$")


class PlanOutput(BaseModel):
    summary: str
    todos: list[str]
    likely_paths: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    build_command: str | None = None
    test_command: str | None = None
    run_command: str | None = None


class ProductSpec(BaseModel):
    objective: str
    refined_requirement: str
    user_value: str
    scope: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    ready: bool = False


class RequirementAssessment(BaseModel):
    approved: bool
    overall_score: int = Field(ge=0, le=100)
    clarity_score: int = Field(ge=0, le=100)
    completeness_score: int = Field(ge=0, le=100)
    testability_score: int = Field(ge=0, le=100)
    issues: list[str] = Field(default_factory=list)
    corrective_instructions: list[str] = Field(default_factory=list)


class ReadingOutput(BaseModel):
    summary: str
    relevant_files: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    conventions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class FileChange(BaseModel):
    path: str
    content: str
    reason: str = ""


class CodeOutput(BaseModel):
    summary: str
    changes: list[FileChange] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ReviewOutput(BaseModel):
    verdict: str
    findings: list[str] = Field(default_factory=list)
    test_gaps: list[str] = Field(default_factory=list)
    mr_title: str
    mr_description: str


class AcceptanceOutput(BaseModel):
    accepted: bool
    score: int = Field(ge=0, le=100)
    passed_criteria: list[str] = Field(default_factory=list)
    failed_criteria: list[str] = Field(default_factory=list)
    corrective_instructions: list[str] = Field(default_factory=list)
    rationale: str


class DeliveryOutput(BaseModel):
    summary: str
    accepted: bool
    acceptance_score: int = Field(ge=0, le=100)
    branch: str
    changed_files: list[str] = Field(default_factory=list)
    source_sync_requested: bool = False
    source_applied: bool = False
    source_repository: str | None = None
    source_apply_error: str | None = None
    run_command: str | None = None
    run_exit_code: int | None = None
    run_output: str = ""
    build_command: str | None = None
    test_command: str | None = None
    validation: str = ""
    notes: list[str] = Field(default_factory=list)
