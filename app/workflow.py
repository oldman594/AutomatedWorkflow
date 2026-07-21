from __future__ import annotations

import json
import re
import traceback
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from app.agents import AgentClient
from app.config import Settings
from app.models import (
    AcceptanceOutput,
    CodeOutput,
    DeliveryOutput,
    PlanOutput,
    ProductSpec,
    RequirementAssessment,
    ReviewOutput,
    Stage,
    Task,
    TaskStatus,
)
from app.repository import CommandResult, Repository, RepositoryError
from app.storage import Storage

STAGE_PROGRESS = {
    Stage.PRODUCT: 6,
    Stage.PLANNER: 14,
    Stage.READER: 24,
    Stage.DESIGN: 34,
    Stage.CODER: 48,
    Stage.BUILD: 60,
    Stage.TEST: 72,
    Stage.REVIEW: 84,
    Stage.ACCEPTANCE: 94,
    Stage.DELIVERY: 98,
}


class TaskCancelled(RuntimeError):
    pass


class WorkflowEngine:
    def __init__(self, settings: Settings, storage: Storage) -> None:
        self.settings = settings
        self.storage = storage
        self.agents = AgentClient(settings)
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="autoflow")
        self._active: set[str] = set()
        self._lock = Lock()

    def start(self, task_id: str) -> Task:
        task = self.storage.get_task(task_id)
        if task.status not in {TaskStatus.DRAFT, TaskStatus.FAILED}:
            raise ValueError(f"Task cannot start from {task.status}")
        if task.status == TaskStatus.FAILED:
            has_worktree = any(
                artifact.kind == "worktree" for artifact in self.storage.list_artifacts(task_id)
            )
            worktree_path = (self.settings.worktree_root / task.id).resolve()
            if has_worktree or worktree_path.exists():
                raise ValueError("任务已生成工作区，不能从头重试；请审查现有变更或创建新任务")
        if task.runner_id:
            try:
                self.storage.get_runner(task.runner_id)
            except KeyError as exc:
                raise ValueError("Task Local Runner is not registered") from exc
            self.storage.update_task(
                task_id,
                status=TaskStatus.QUEUED,
                progress=1,
                error=None,
                cancel_requested=False,
            )
            self.storage.add_event(task_id, f"任务已进入 Local Runner 队列：{task.runner_id}")
            return self.storage.get_task(task_id)
        if not self.settings.mock_llm:
            route_errors = self.settings.route_errors()
            if route_errors:
                raise ValueError("Agent 路由配置无效：" + "；".join(route_errors))
        with self._lock:
            if task_id in self._active:
                raise ValueError("Task is already running")
            self._active.add(task_id)
        self.storage.update_task(
            task_id, status=TaskStatus.QUEUED, progress=1, error=None, cancel_requested=False
        )
        message = (
            "失败任务已重新进入执行队列"
            if task.status == TaskStatus.FAILED
            else "任务已进入执行队列"
        )
        self.storage.add_event(task_id, message)
        self._executor.submit(self._run_guarded, task_id)
        return self.storage.get_task(task_id)

    def cancel(self, task_id: str) -> Task:
        task = self.storage.get_task(task_id)
        if task.status in {TaskStatus.COMPLETED, TaskStatus.CANCELLED}:
            return task
        self.storage.add_event(task_id, "已请求取消任务", level="warning")
        return self.storage.update_task(task_id, cancel_requested=True)

    def approve(self, task_id: str, action: str) -> Task:
        task = self.storage.get_task(task_id)
        if task.status != TaskStatus.WAITING_APPROVAL:
            raise ValueError("Task is not waiting for approval")
        artifacts = self.storage.list_artifacts(task_id)
        worktree = next(
            (item.content for item in reversed(artifacts) if item.kind == "worktree"), None
        )
        if action == "commit":
            if task.runner_id:
                raise ValueError("Local Runner 任务请在用户电脑的原仓库中手动 Commit")
            if task.sync_to_source:
                raise ValueError("本地同步模式不会自动提交代码，请在原仓库审查后手动 Commit")
            if not worktree:
                raise ValueError("Task has no worktree")
            acceptance_raw = next(
                (item.content for item in reversed(artifacts) if item.kind == "acceptance"),
                None,
            )
            if acceptance_raw and not json.loads(acceptance_raw).get("accepted", False):
                raise ValueError("产品验收未通过，不能自动创建 Commit")
            review_raw = next(
                (item.content for item in reversed(artifacts) if item.kind == "review"), "{}"
            )
            review = json.loads(review_raw)
            if review.get("findings"):
                raise ValueError("代码审查仍有 findings，不能自动创建 Commit")
            existing_commit = next(
                (item.content for item in reversed(artifacts) if item.kind == "commit"), None
            )
            if not existing_commit:
                repository = Repository(worktree, self.settings.allowed_roots)
                commit_hash = repository.commit(review.get("mr_title", task.title))
                self.storage.add_artifact(task_id, "commit", commit_hash)
                self.storage.add_event(
                    task_id, f"已创建 Commit：{commit_hash[:12]}", stage=Stage.DELIVERY
                )
        self.storage.add_event(task_id, "人工审批已通过", stage=Stage.DELIVERY)
        return self.storage.update_task(
            task_id, status=TaskStatus.COMPLETED, stage=Stage.DELIVERY, progress=100
        )

    def _run_guarded(self, task_id: str) -> None:
        try:
            self._run(task_id)
        except TaskCancelled:
            self.storage.update_task(task_id, status=TaskStatus.CANCELLED, error=None)
            self.storage.add_event(task_id, "任务已取消", level="warning")
        except Exception as exc:
            self.storage.update_task(task_id, status=TaskStatus.FAILED, error=str(exc))
            self.storage.add_event(
                task_id,
                f"工作流失败：{exc}",
                level="error",
                data={"trace": traceback.format_exc()[-8000:]},
            )
        finally:
            with self._lock:
                self._active.discard(task_id)

    def _run(self, task_id: str) -> None:
        task = self.storage.update_task(task_id, status=TaskStatus.RUNNING)
        source = Repository(task.repository, self.settings.allowed_roots)
        source_head = source.head_oid()
        branch = task.branch or f"autoflow/{self._slug(task.title)}-{task.id[:6]}"
        worktree_path = (self.settings.worktree_root / task.id).resolve()
        repository = source.create_worktree(worktree_path, branch)
        self.storage.update_task(task_id, branch=branch)
        self.storage.add_artifact(task_id, "worktree", str(worktree_path))
        self.storage.add_event(task_id, f"隔离 worktree 已就绪，分支：{branch}")
        if task.sync_to_source and repository.head_oid() != source_head:
            raise RepositoryError(
                "直接同步要求目标分支从源仓库当前 HEAD 创建；请留空目标分支或使用新分支"
            )

        if task.include_local_changes:
            snapshot = source.copy_local_changes_to(repository, self.settings.max_download_bytes)
            self.storage.add_artifact(
                task_id,
                "local_snapshot",
                json.dumps(
                    {
                        "files": snapshot.files,
                        "baseline_commit": snapshot.baseline_commit,
                        "source_status": snapshot.source_status,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            if snapshot.baseline_commit:
                self.storage.add_event(
                    task_id,
                    f"已将 {len(snapshot.files)} 个本地未提交文件纳入开发基线",
                    data={
                        "files": snapshot.files,
                        "baseline_commit": snapshot.baseline_commit,
                    },
                )
            else:
                self.storage.add_event(task_id, "源仓库没有需要纳入的本地未提交代码")

        inventory = repository.inventory()
        product_spec, assessment = self._align_product_requirement(task, inventory)
        requirement = product_spec.refined_requirement

        self._enter(task_id, Stage.PLANNER, "Planner 正在拆分已对齐的产品需求", "planner")
        plan = self.agents.plan(requirement, inventory, repository.recent_history(), task.model)
        if not plan.acceptance_criteria:
            plan.acceptance_criteria = product_spec.acceptance_criteria
        self.storage.add_artifact(task_id, "plan", plan.model_dump_json(indent=2))
        self.storage.add_event(
            task_id,
            f"计划已生成，共 {len(plan.todos)} 个 Todo",
            stage=Stage.PLANNER,
            data={"todos": plan.todos, "questions": plan.questions},
        )
        self._check_cancelled(task_id)

        self._enter(task_id, Stage.READER, "Reader 正在阅读相关代码上下文", "reader")
        context = repository.collect_context(
            plan.likely_paths, requirement, self.settings.max_context_chars
        )
        context_manifest = self._context_manifest(context)
        reading = self.agents.read_code(requirement, plan, context, task.model)
        self.storage.add_artifact(task_id, "context_manifest", context_manifest)
        self.storage.add_artifact(task_id, "reading", reading.model_dump_json(indent=2))
        self.storage.add_event(
            task_id,
            "仓库上下文阅读完成",
            stage=Stage.READER,
            data={
                "characters": len(context),
                "files": context_manifest.splitlines(),
                "route": self.settings.agent_routes["reader"],
            },
        )
        self._check_cancelled(task_id)

        enriched_context = (
            f"{context}\n\n--- READER ANALYSIS ---\n{reading.model_dump_json(indent=2)}"
        )
        self._enter(task_id, Stage.DESIGN, "Architecture Agent 正在设计变更", "architecture")
        design = self.agents.design(requirement, plan, enriched_context, task.model)
        self.storage.add_artifact(task_id, "design", design)
        self.storage.add_event(task_id, "设计方案已完成", stage=Stage.DESIGN)

        self._enter(task_id, Stage.CODER, "Coder 正在实现计划中的变更", "coder")
        code = self.agents.code(requirement, plan, design, enriched_context, task.model)
        written = self._apply_code(repository, code, task.auto_apply)
        self.storage.add_artifact(task_id, "code_summary", code.model_dump_json(indent=2))
        self.storage.add_event(
            task_id,
            f"Coder 生成了 {len(code.changes)} 个文件变更",
            stage=Stage.CODER,
            data={"files": written},
        )
        self._check_cancelled(task_id)

        detected_build, detected_test = repository.detect_commands()
        build_command = task.build_command or plan.build_command or detected_build
        test_command = task.test_command or plan.test_command or detected_test
        run_command = plan.run_command
        review, acceptance, accepted, validation, run_result = self._acceptance_loop(
            task=task,
            repository=repository,
            product_spec=product_spec,
            plan=plan,
            design=design,
            context=enriched_context,
            build_command=build_command,
            test_command=test_command,
            run_command=run_command,
        )

        changed_files = repository.delivery_files()
        source_applied = False
        source_apply_error: str | None = None
        if task.sync_to_source:
            if accepted:
                try:
                    source.apply_patch(repository.diff(), source_head)
                    source_applied = True
                    self.storage.add_event(
                        task_id,
                        f"已将 {len(changed_files)} 个 AI 变更文件同步到本地仓库",
                        stage=Stage.DELIVERY,
                        data={"files": changed_files, "repository": str(source.path)},
                    )
                except RepositoryError as exc:
                    source_apply_error = str(exc)
                    self.storage.add_event(
                        task_id,
                        f"本地仓库同步失败：{exc}",
                        stage=Stage.DELIVERY,
                        level="warning",
                    )
            else:
                source_apply_error = "产品验收未通过，未修改本地仓库"

        delivery_notes = (
            [] if run_command else ["Planner 未提供功能运行命令，请人工补充运行方式"]
        ) + ([] if accepted else ["自动产品验收未通过，产物仅供人工检查"])
        if task.sync_to_source:
            delivery_notes.append(
                f"AI 变更已直接同步到本地仓库：{source.path}"
                if source_applied
                else f"本地仓库未更新：{source_apply_error}"
            )

        delivery = DeliveryOutput(
            summary=review.mr_description or review.mr_title,
            accepted=accepted,
            acceptance_score=acceptance.score,
            branch=branch,
            changed_files=changed_files,
            source_sync_requested=task.sync_to_source,
            source_applied=source_applied,
            source_repository=str(source.path) if task.sync_to_source else None,
            source_apply_error=source_apply_error,
            run_command=run_command,
            run_exit_code=run_result.returncode if run_result else None,
            run_output=(run_result.stdout + run_result.stderr).strip() if run_result else "",
            build_command=build_command,
            test_command=test_command,
            validation=validation,
            notes=delivery_notes,
        )
        self.storage.add_artifact(task_id, "delivery", delivery.model_dump_json(indent=2))
        self.storage.add_event(
            task_id,
            f"交付清单已生成，包含 {len(delivery.changed_files)} 个变更文件",
            stage=Stage.DELIVERY,
            data={
                "files": delivery.changed_files,
                "run_command": run_command,
                "run_output": delivery.run_output[-4000:],
                "source_applied": source_applied,
            },
        )

        self._enter(task_id, Stage.DELIVERY, "交付产物已就绪，等待人工审批")
        if task.auto_commit and accepted and not task.sync_to_source:
            commit_hash = repository.commit(review.mr_title)
            self.storage.add_artifact(task_id, "commit", commit_hash)
            self.storage.add_event(
                task_id, f"已创建 Commit：{commit_hash[:12]}", stage=Stage.DELIVERY
            )
        self.storage.update_task(
            task_id, status=TaskStatus.WAITING_APPROVAL, stage=Stage.DELIVERY, progress=100
        )
        self.storage.add_event(
            task_id,
            (
                "产品验收通过，AI 变更已写入本地仓库，等待最终人工确认"
                if source_applied
                else "产品验收通过，但本地同步失败，已保留隔离产物供人工处理"
            )
            if accepted and task.sync_to_source
            else (
                "产品验收通过，等待最终人工审查；push 和创建 MR 保持为手动操作"
                if accepted
                else "自动纠偏达到上限，已携带产品验收差距转人工处理"
            ),
            stage=Stage.DELIVERY,
            level=(
                "warning"
                if not accepted or (task.sync_to_source and not source_applied)
                else "info"
            ),
            data={
                "accepted": accepted,
                "score": acceptance.score,
                "source_applied": source_applied,
            },
        )

    def _align_product_requirement(
        self, task: Task, inventory: list[str]
    ) -> tuple[ProductSpec, RequirementAssessment]:
        spec: ProductSpec | None = None
        assessment: RequirementAssessment | None = None
        for iteration in range(1, self.settings.max_product_iterations + 1):
            self._enter(
                task.id,
                Stage.PRODUCT,
                f"Product Manager 正在进行第 {iteration} 轮需求对齐",
                "product",
            )
            spec = self.agents.product_spec(
                task.requirement, task.model, prior=spec, feedback=assessment
            )
            self.storage.add_artifact(task.id, "product_spec", spec.model_dump_json(indent=2))
            assessment = self.agents.assess_requirement(spec, inventory, task.model)
            self.storage.add_artifact(
                task.id, "requirement_assessment", assessment.model_dump_json(indent=2)
            )
            aligned = (
                spec.ready
                and assessment.approved
                and assessment.overall_score >= self.settings.product_quality_threshold
            )
            self.storage.add_event(
                task.id,
                f"需求对齐第 {iteration} 轮：{assessment.overall_score}/100",
                stage=Stage.PRODUCT,
                level="info" if aligned else "warning",
                data={
                    "iteration": iteration,
                    "aligned": aligned,
                    "issues": assessment.issues,
                    "product_route": self.settings.agent_routes["product"],
                    "reader_route": self.settings.agent_routes["reader"],
                },
            )
            if aligned:
                return spec, assessment
            self._check_cancelled(task.id)
        assert spec is not None and assessment is not None
        raise RepositoryError(
            "产品需求对齐未达到质量门槛："
            f"{assessment.overall_score}/{self.settings.product_quality_threshold}；"
            + "；".join(assessment.corrective_instructions or assessment.issues)
        )

    def _acceptance_loop(
        self,
        *,
        task: Task,
        repository: Repository,
        product_spec: ProductSpec,
        plan: PlanOutput,
        design: str,
        context: str,
        build_command: str | None,
        test_command: str | None,
        run_command: str | None,
    ) -> tuple[ReviewOutput, AcceptanceOutput, bool, str, CommandResult | None]:
        review: ReviewOutput | None = None
        acceptance: AcceptanceOutput | None = None
        for iteration in range(self.settings.max_acceptance_iterations + 1):
            validation, run_result = self._run_validation_cycle(
                task,
                repository,
                plan,
                design,
                context,
                product_spec.refined_requirement,
                build_command,
                test_command,
                run_command,
            )
            hygiene_findings = repository.hygiene_findings()
            if hygiene_findings:
                validation += "\n\n$ deterministic repository hygiene check\nexit=1\n" + "\n".join(
                    hygiene_findings
                )
            else:
                validation += "\n\n$ deterministic repository hygiene check\nexit=0"
            diff = repository.diff()
            self.storage.add_artifact(task.id, "diff", diff)
            self.storage.add_artifact(task.id, "validation", validation)

            self._enter(
                task.id,
                Stage.REVIEW,
                f"Reviewer 正在执行第 {iteration + 1} 轮代码审查",
                "reviewer",
            )
            review = self.agents.review(
                product_spec.refined_requirement, diff, validation, task.model
            )
            self.storage.add_artifact(task.id, "review", review.model_dump_json(indent=2))
            self.storage.add_artifact(task.id, "mr_description", review.mr_description)
            self.storage.add_event(
                task.id,
                f"审查结论：{review.verdict}",
                stage=Stage.REVIEW,
                level="warning" if review.findings else "info",
                data={
                    "iteration": iteration + 1,
                    "findings": review.findings,
                    "route": self.settings.agent_routes["reviewer"],
                },
            )

            self._enter(
                task.id,
                Stage.ACCEPTANCE,
                f"Product Manager 正在执行第 {iteration + 1} 轮交付验收",
                "acceptance",
            )
            acceptance = self.agents.accept(product_spec, diff, validation, review, task.model)
            accepted = (
                acceptance.accepted
                and acceptance.score >= self.settings.product_quality_threshold
                and not acceptance.failed_criteria
                and not acceptance.corrective_instructions
                and not review.findings
                and not hygiene_findings
                and review.verdict.lower() in {"approved", "approve", "pass", "passed"}
            )
            self.storage.add_artifact(task.id, "acceptance", acceptance.model_dump_json(indent=2))
            self.storage.add_event(
                task.id,
                f"产品验收第 {iteration + 1} 轮：{acceptance.score}/100",
                stage=Stage.ACCEPTANCE,
                level="info" if accepted else "warning",
                data={
                    "iteration": iteration + 1,
                    "accepted": accepted,
                    "failed_criteria": acceptance.failed_criteria,
                    "hygiene_findings": hygiene_findings,
                    "route": self.settings.agent_routes["acceptance"],
                },
            )
            if accepted:
                return review, acceptance, True, validation, run_result
            if iteration >= self.settings.max_acceptance_iterations:
                return review, acceptance, False, validation, run_result

            corrections = [
                *acceptance.corrective_instructions,
                *review.findings,
                *review.test_gaps,
                *hygiene_findings,
            ]
            self._enter(
                task.id,
                Stage.CODER,
                f"产品验收未通过，Coder 正在执行第 {iteration + 1} 轮纠偏",
                "coder",
            )
            refreshed = repository.collect_context(
                plan.likely_paths,
                product_spec.refined_requirement,
                self.settings.max_context_chars,
            )
            fix = self.agents.code(
                product_spec.refined_requirement,
                plan,
                design,
                refreshed or context,
                task.model,
                failure="Product acceptance corrections:\n" + "\n".join(corrections),
            )
            written = self._apply_code(repository, fix, task.auto_apply)
            self.storage.add_artifact(task.id, "code_summary", fix.model_dump_json(indent=2))
            self.storage.add_event(
                task.id,
                f"纠偏 Agent 更新了 {len(written)} 个文件",
                stage=Stage.CODER,
                data={"files": written, "corrections": corrections},
            )
        raise AssertionError("unreachable")

    def _run_validation_cycle(
        self,
        task: Task,
        repository: Repository,
        plan: PlanOutput,
        design: str,
        context: str,
        requirement: str,
        build_command: str | None,
        test_command: str | None,
        run_command: str | None,
    ) -> tuple[str, CommandResult | None]:
        logs: list[str] = []
        run_result: CommandResult | None = None
        if build_command:
            self._enter(task.id, Stage.BUILD, f"执行构建：{build_command}")
            result = self._validate_with_fixes(
                task, repository, plan, design, build_command, "build", context, requirement
            )
            logs.append(self._format_result(result))
        else:
            self._enter(task.id, Stage.BUILD, "未检测到构建命令，已跳过")
        if test_command:
            self._enter(task.id, Stage.TEST, f"执行测试：{test_command}")
            result = self._validate_with_fixes(
                task, repository, plan, design, test_command, "test", context, requirement
            )
            logs.append(self._format_result(result))
        else:
            self._enter(task.id, Stage.TEST, "未检测到测试命令，已跳过")
        if run_command:
            self._enter(task.id, Stage.TEST, f"执行功能运行命令：{run_command}")
            run_result = self._validate_with_fixes(
                task, repository, plan, design, run_command, "run", context, requirement
            )
            logs.append(self._format_result(run_result))
        else:
            self.storage.add_event(
                task.id,
                "Planner 未提供功能运行命令，无法采集用户可见输出",
                stage=Stage.TEST,
                level="warning",
            )
        return "\n\n".join(logs), run_result

    def _validate_with_fixes(
        self,
        task: Task,
        repository: Repository,
        plan: PlanOutput,
        design: str,
        command: str,
        kind: str,
        context: str,
        requirement: str,
    ) -> CommandResult:
        stage = Stage.BUILD if kind == "build" else Stage.TEST
        for attempt in range(self.settings.max_fix_attempts + 1):
            self._check_cancelled(task.id)
            result = repository.run_shell(command, self.settings.command_timeout_seconds)
            self.storage.add_event(
                task.id,
                f"{kind.title()} attempt {attempt + 1} {'passed' if result.returncode == 0 else 'failed'}",
                stage=stage,
                level="info" if result.returncode == 0 else "warning",
                data={"returncode": result.returncode, "output": result.combined[-6000:]},
            )
            if result.returncode == 0:
                return result
            if attempt >= self.settings.max_fix_attempts:
                raise RepositoryError(
                    f"{kind.title()} failed after {attempt + 1} attempts:\n{result.combined[-8000:]}"
                )
            refreshed = repository.collect_context(
                plan.likely_paths, requirement, self.settings.max_context_chars
            )
            fix = self.agents.code(
                requirement,
                plan,
                design,
                refreshed or context,
                task.model,
                failure=f"Command: {command}\n{result.combined}",
            )
            written = self._apply_code(repository, fix, task.auto_apply)
            self.storage.add_event(
                task.id,
                f"Fix agent updated {len(written)} files",
                stage=stage,
                data={"files": written},
            )
        raise AssertionError("unreachable")

    @staticmethod
    def _apply_code(repository: Repository, code: CodeOutput, auto_apply: bool) -> list[str]:
        changes = code.changes
        if not auto_apply:
            return [change.path for change in changes]
        return repository.write_changes([(change.path, change.content) for change in changes])

    def _enter(self, task_id: str, stage: Stage, message: str, role: str | None = None) -> None:
        self._check_cancelled(task_id)
        self.storage.update_task(
            task_id, status=TaskStatus.RUNNING, stage=stage, progress=STAGE_PROGRESS[stage]
        )
        data = {"route": self.settings.agent_routes[role]} if role else None
        self.storage.add_event(task_id, message, stage=stage, data=data)

    def _check_cancelled(self, task_id: str) -> None:
        if self.storage.get_task(task_id).cancel_requested:
            raise TaskCancelled()

    @staticmethod
    def _context_manifest(context: str) -> str:
        return "\n".join(re.findall(r"^--- FILE: (.+) ---$", context, flags=re.MULTILINE))

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
        return slug[:48] or "task"

    @staticmethod
    def _format_result(result: CommandResult) -> str:
        return f"$ {result.command}\nexit={result.returncode}\n{result.combined[-12000:]}"
