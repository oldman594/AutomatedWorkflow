from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.repository import CommandResult, RepositoryError, ShellExecutor


@dataclass(frozen=True, slots=True)
class DockerSandboxConfig:
    runtime: str
    image: str
    network: str
    memory: str
    cpus: float
    pids_limit: int


class HostShellExecutor:
    def run(self, command: str, cwd: Path, timeout: int, task_id: str | None) -> CommandResult:
        completed = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            executable="/bin/bash",
            text=True,
            capture_output=True,
            timeout=timeout,
            env={**os.environ, "CI": "1"},
        )
        return CommandResult(command, completed.returncode, completed.stdout, completed.stderr)


class DockerShellExecutor:
    def __init__(self, config: DockerSandboxConfig) -> None:
        self.config = config

    def run(self, command: str, cwd: Path, timeout: int, task_id: str | None) -> CommandResult:
        runtime = self.config.runtime
        if shutil.which(runtime) is None:
            raise RepositoryError(
                f"Container sandbox is required but the {runtime} CLI is unavailable. "
                f"Install {runtime} or explicitly set AUTOFLOW_SANDBOX_MODE=host for trusted development."
            )
        container_name = self._container_name(task_id)
        docker_command = self._docker_command(container_name, cwd, command)
        try:
            completed = subprocess.run(
                docker_command,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            subprocess.run(
                [runtime, "rm", "-f", container_name],
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            raise
        return CommandResult(command, completed.returncode, completed.stdout, completed.stderr)

    def _docker_command(self, container_name: str, cwd: Path, command: str) -> list[str]:
        runtime_options = ["--userns", "keep-id"] if self.config.runtime == "podman" else []
        return [
            self.config.runtime,
            "run",
            "--rm",
            "--name",
            container_name,
            *runtime_options,
            "--read-only",
            "--network",
            self.config.network,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self.config.pids_limit),
            "--memory",
            self.config.memory,
            "--cpus",
            str(self.config.cpus),
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=512m",
            "--env",
            "CI=1",
            "--env",
            "HOME=/tmp/home",
            "--mount",
            f"type=bind,src={cwd.resolve()},dst=/workspace",
            "--workdir",
            "/workspace",
            self.config.image,
            "/bin/bash",
            "-lc",
            "mkdir -p /tmp/home && " + command,
        ]

    @staticmethod
    def _container_name(task_id: str | None) -> str:
        normalized = re.sub(r"[^a-zA-Z0-9_.-]+", "-", task_id or "task")[:40]
        return f"autoflow-{normalized}-{uuid.uuid4().hex[:8]}"


def create_shell_executor(settings: Settings) -> ShellExecutor:
    if settings.sandbox_mode == "host":
        return HostShellExecutor()
    runtime: str = settings.container_runtime
    if runtime == "auto":
        runtime = next(
            (candidate for candidate in ("docker", "podman") if shutil.which(candidate)),
            "docker",
        )
    return DockerShellExecutor(
        DockerSandboxConfig(
            runtime=runtime,
            image=settings.sandbox_image,
            network=settings.sandbox_network,
            memory=settings.sandbox_memory,
            cpus=settings.sandbox_cpus,
            pids_limit=settings.sandbox_pids_limit,
        )
    )


def sandbox_readiness(executor: ShellExecutor) -> dict[str, object]:
    if isinstance(executor, HostShellExecutor):
        return {"ready": True, "mode": "host", "isolated": False}
    if not isinstance(executor, DockerShellExecutor):
        return {"ready": False, "mode": "unknown", "error": "Unsupported shell executor"}
    runtime = executor.config.runtime
    if shutil.which(runtime) is None:
        return {
            "ready": False,
            "mode": "container",
            "runtime": runtime,
            "error": f"{runtime} CLI is unavailable",
        }
    try:
        result = subprocess.run(
            [runtime, "image", "inspect", executor.config.image],
            text=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ready": False,
            "mode": "container",
            "runtime": runtime,
            "error": str(exc),
        }
    return {
        "ready": result.returncode == 0,
        "mode": "container",
        "runtime": runtime,
        "image": executor.config.image,
        "error": result.stderr[-1000:] if result.returncode else None,
    }
