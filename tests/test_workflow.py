import json
import subprocess
from pathlib import Path

from app.config import Settings
from app.models import (
    AcceptanceOutput,
    CodeOutput,
    FileChange,
    PlanOutput,
    ProductSpec,
    ReadingOutput,
    RequirementAssessment,
    ReviewOutput,
    TaskCreate,
    TaskStatus,
)
from app.storage import Storage
from app.workflow import WorkflowEngine


class AlignmentAgents:
    def __init__(self) -> None:
        self.product_calls = 0

    def product_spec(self, requirement, model, prior=None, feedback=None):
        self.product_calls += 1
        return ProductSpec(
            objective="Deliver behavior",
            refined_requirement=requirement,
            user_value="User value",
            acceptance_criteria=["Observable result"],
            ready=self.product_calls > 1,
        )

    def assess_requirement(self, spec, inventory, model):
        score = 90 if self.product_calls > 1 else 60
        return RequirementAssessment(
            approved=score >= 85,
            overall_score=score,
            clarity_score=score,
            completeness_score=score,
            testability_score=score,
            issues=[] if score >= 85 else ["Criterion is ambiguous"],
            corrective_instructions=[] if score >= 85 else ["Make the result observable"],
        )


class SuccessfulLocalAgents:
    def product_spec(self, requirement, model, prior=None, feedback=None):
        return ProductSpec(
            objective="Complete the local draft",
            refined_requirement=requirement,
            user_value="Working local feature",
            acceptance_criteria=["result.py prints local feature ready"],
            ready=True,
        )

    def assess_requirement(self, spec, inventory, model):
        return RequirementAssessment(
            approved=True,
            overall_score=100,
            clarity_score=100,
            completeness_score=100,
            testability_score=100,
        )

    def plan(self, requirement, inventory, history, model):
        return PlanOutput(
            summary="Complete draft",
            todos=["Complete partial.py", "Add result.py"],
            likely_paths=["partial.py"],
            acceptance_criteria=["result.py prints local feature ready"],
            run_command="python3 result.py",
        )

    def read_code(self, requirement, plan, context, model):
        return ReadingOutput(
            summary="Found the local draft",
            relevant_files=["partial.py"],
        )

    def design(self, requirement, plan, context, model):
        return "Complete the draft and expose a runnable result."

    def code(self, requirement, plan, design, context, model, failure=None):
        return CodeOutput(
            summary="Completed local feature",
            changes=[
                FileChange(path="partial.py", content="STATUS = 'complete'\n"),
                FileChange(path="result.py", content="print('local feature ready')\n"),
            ],
        )

    def review(self, requirement, diff, validation, model):
        return ReviewOutput(
            verdict="approved",
            mr_title="Complete local feature",
            mr_description="Completes the local draft and adds a runnable result.",
        )

    def accept(self, product_spec, diff, validation, review, model):
        return AcceptanceOutput(
            accepted=True,
            score=100,
            passed_criteria=product_spec.acceptance_criteria,
            rationale="The runtime output and diff satisfy the requirement.",
        )


class ResumableAgents(SuccessfulLocalAgents):
    def __init__(self) -> None:
        self.calls = {name: 0 for name in ("product", "plan", "read", "design", "code", "review")}

    def product_spec(self, requirement, model, prior=None, feedback=None):
        self.calls["product"] += 1
        return super().product_spec(requirement, model, prior, feedback)

    def plan(self, requirement, inventory, history, model):
        self.calls["plan"] += 1
        return super().plan(requirement, inventory, history, model)

    def read_code(self, requirement, plan, context, model):
        self.calls["read"] += 1
        return super().read_code(requirement, plan, context, model)

    def design(self, requirement, plan, context, model):
        self.calls["design"] += 1
        return super().design(requirement, plan, context, model)

    def code(self, requirement, plan, design, context, model, failure=None):
        self.calls["code"] += 1
        return super().code(requirement, plan, design, context, model, failure)

    def review(self, requirement, diff, validation, model):
        self.calls["review"] += 1
        if self.calls["review"] == 1:
            raise RuntimeError("temporary reviewer outage")
        return super().review(requirement, diff, validation, model)


def test_product_manager_refines_until_reader_approves(tmp_path: Path) -> None:
    settings = Settings(
        mock_llm=True,
        database_path=tmp_path / "workflow.db",
        worktree_root=tmp_path / "worktrees",
        max_product_iterations=3,
        product_quality_threshold=85,
    )
    storage = Storage(settings.database_path)
    task = storage.create_task(
        TaskCreate(
            title="Alignment",
            requirement="Implement an observable behavior",
            repository=str(tmp_path),
        ),
        "mock",
    )
    engine = WorkflowEngine(settings, storage)
    agents = AlignmentAgents()
    engine.agents = agents

    spec, assessment = engine._align_product_requirement(task, ["app.py"])

    assert agents.product_calls == 2
    assert spec.ready is True
    assert assessment.overall_score == 90
    artifacts = storage.list_artifacts(task.id)
    assert len([item for item in artifacts if item.kind == "product_spec"]) == 2
    assert len([item for item in artifacts if item.kind == "requirement_assessment"]) == 2


def test_successful_workflow_syncs_ai_changes_to_local_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repository, check=True)
    (repository / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=repository, check=True, capture_output=True
    )
    (repository / "partial.py").write_text("STATUS = 'draft'\n", encoding="utf-8")

    settings = Settings(
        mock_llm=True,
        allowed_roots=[tmp_path],
        database_path=tmp_path / "sync.db",
        worktree_root=tmp_path / "worktrees",
    )
    storage = Storage(settings.database_path)
    task = storage.create_task(
        TaskCreate(
            title="Local sync",
            requirement="Complete the existing local draft and make it runnable",
            repository=str(repository),
        ),
        "mock",
    )
    engine = WorkflowEngine(settings, storage)
    engine.agents = SuccessfulLocalAgents()

    engine._run(task.id)

    completed = storage.get_task(task.id)
    assert completed.status == TaskStatus.WAITING_APPROVAL
    assert (repository / "partial.py").read_text(encoding="utf-8") == "STATUS = 'complete'\n"
    assert (repository / "result.py").read_text(
        encoding="utf-8"
    ) == "print('local feature ready')\n"
    delivery = next(item for item in storage.list_artifacts(task.id) if item.kind == "delivery")
    manifest = json.loads(delivery.content)
    assert manifest["source_applied"] is True
    assert manifest["run_output"] == "local feature ready"


def test_failed_workflow_resumes_from_persisted_stage_artifacts(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repository, check=True)
    (repository / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=repository, check=True, capture_output=True
    )
    (repository / "partial.py").write_text("STATUS = 'draft'\n", encoding="utf-8")

    settings = Settings(
        mock_llm=True,
        sandbox_mode="host",
        allowed_roots=[tmp_path],
        database_path=tmp_path / "resume.db",
        worktree_root=tmp_path / "worktrees",
    )
    storage = Storage(settings.database_path)
    task = storage.create_task(
        TaskCreate(
            title="Resume local sync",
            requirement="Resume after a temporary reviewer outage",
            repository=str(repository),
        ),
        "mock",
    )
    agents = ResumableAgents()
    engine = WorkflowEngine(settings, storage)
    engine.agents = agents

    engine._run_guarded(task.id)
    assert storage.get_task(task.id).status == TaskStatus.FAILED
    engine._run(task.id)

    assert storage.get_task(task.id).status == TaskStatus.WAITING_APPROVAL
    assert agents.calls == {
        "product": 1,
        "plan": 1,
        "read": 1,
        "design": 1,
        "code": 1,
        "review": 2,
    }
    assert len([item for item in storage.list_artifacts(task.id) if item.kind == "worktree"]) == 1
