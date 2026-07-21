import subprocess
from pathlib import Path

import pytest

from app.repository import RepositoryError
from app.sandbox import DockerSandboxConfig, DockerShellExecutor


def sandbox() -> DockerShellExecutor:
    return DockerShellExecutor(
        DockerSandboxConfig(
            image="autoflow-sandbox:test",
            network="none",
            memory="2g",
            cpus=2,
            pids_limit=128,
        )
    )


def test_docker_sandbox_applies_security_and_resource_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[str] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.extend(command)
        return subprocess.CompletedProcess(command, 0, "passed\n", "")

    monkeypatch.setattr("app.sandbox.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr("app.sandbox.subprocess.run", fake_run)

    result = sandbox().run("pytest -q", tmp_path, 60, "task/unsafe id")

    assert result.returncode == 0
    assert captured[:2] == ["docker", "run"]
    assert "--read-only" in captured
    assert captured[captured.index("--network") + 1] == "none"
    assert captured[captured.index("--cap-drop") + 1] == "ALL"
    assert captured[captured.index("--memory") + 1] == "2g"
    assert captured[captured.index("--pids-limit") + 1] == "128"
    assert f"type=bind,src={tmp_path},dst=/workspace,rw" in captured
    assert captured[-1].endswith("pytest -q")


def test_docker_sandbox_fails_closed_when_docker_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.sandbox.shutil.which", lambda _: None)

    with pytest.raises(RepositoryError, match="Docker sandbox is required"):
        sandbox().run("pytest -q", tmp_path, 60, "task-1")


def test_docker_sandbox_removes_timed_out_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[:2] == ["docker", "run"]:
            raise subprocess.TimeoutExpired(command, 1)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("app.sandbox.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr("app.sandbox.subprocess.run", fake_run)

    with pytest.raises(subprocess.TimeoutExpired):
        sandbox().run("sleep 5", tmp_path, 1, "task-1")

    assert commands[1][:3] == ["docker", "rm", "-f"]
