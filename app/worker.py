from __future__ import annotations

import socket
import threading
import uuid

from app.config import Settings
from app.models import JobStatus, TaskStatus
from app.storage import Storage
from app.workflow import WorkflowEngine


class DurableTaskWorker:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        engine: WorkflowEngine,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.engine = engine
        self.worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        if self.threads:
            return
        self.storage.recover_expired_jobs("server")
        for index in range(self.settings.worker_concurrency):
            thread = threading.Thread(
                target=self._run_loop,
                args=(f"{self.worker_id}-{index}",),
                name=f"autoflow-worker-{index}",
                daemon=True,
            )
            thread.start()
            self.threads.append(thread)

    def stop(self) -> None:
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=5)
        self.threads.clear()

    def _run_loop(self, owner: str) -> None:
        while not self.stop_event.is_set():
            job = self.storage.lease_job("server", owner, self.settings.job_lease_seconds)
            if job is None:
                self.stop_event.wait(0.5)
                continue
            self._execute_job(job.task_id, owner)

    def _execute_job(self, task_id: str, owner: str) -> None:
        renew_stop = threading.Event()
        renewer = threading.Thread(
            target=self._renew_loop,
            args=(task_id, owner, renew_stop),
            name=f"autoflow-lease-{task_id}",
            daemon=True,
        )
        renewer.start()
        try:
            self.engine._run_guarded(task_id)
            task = self.storage.get_task(task_id)
            if task.status == TaskStatus.FAILED:
                retried = self.storage.fail_job(
                    task_id,
                    owner,
                    task.error or "Workflow failed",
                    self.settings.job_retry_base_seconds,
                )
                if retried:
                    self.storage.add_event(
                        task_id,
                        "持久化 Worker 已安排自动重试",
                        level="warning",
                    )
            elif task.status == TaskStatus.CANCELLED:
                self.storage.complete_job(task_id, owner, JobStatus.CANCELLED)
            elif task.status in {TaskStatus.WAITING_APPROVAL, TaskStatus.COMPLETED}:
                self.storage.complete_job(task_id, owner)
            else:
                self.storage.fail_job(
                    task_id,
                    owner,
                    f"Workflow stopped in unexpected state: {task.status}",
                    self.settings.job_retry_base_seconds,
                )
        finally:
            renew_stop.set()
            renewer.join(timeout=2)

    def _renew_loop(self, task_id: str, owner: str, stop_event: threading.Event) -> None:
        interval = max(1, self.settings.job_lease_seconds // 3)
        while not stop_event.wait(interval):
            if not self.storage.renew_job(task_id, owner, self.settings.job_lease_seconds):
                return
