import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from app.agents import AgentClient
from app.config import Settings
from app.models import CodeOutput, PlanOutput, ReadingOutput


def test_codex_cli_uses_official_provider_and_read_only_sandbox(
    tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["prompt"] = kwargs["input"]
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "summary": "Plan",
                    "todos": ["Implement"],
                    "likely_paths": [],
                    "risks": [],
                    "questions": [],
                    "acceptance_criteria": ["Tests pass"],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    settings = Settings(
        provider="codex_cli",
        codex_model="gpt-test",
        agent_planner_provider="codex_cli",
        agent_planner_model="gpt-test",
        worktree_root=tmp_path,
        database_path=tmp_path / "test.db",
    )
    client = AgentClient(settings)

    result = client._structured("gpt-test", "planner", "Plan this", PlanOutput)

    command = captured["command"]
    assert result.todos == ["Implement"]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert 'model_provider="openai"' in command
    assert "--ephemeral" in command
    assert "planning agent" in captured["prompt"]


def test_deepseek_routes_coder_to_pro_with_json_mode(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        message = SimpleNamespace(content='{"summary":"done","changes":[],"notes":[]}')
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    settings = Settings(
        provider="deepseek",
        deepseek_api_key="test-key",
        deepseek_model="deepseek-v4-flash",
        deepseek_coding_model="deepseek-v4-pro",
        worktree_root=tmp_path,
        database_path=tmp_path / "test.db",
    )
    client = AgentClient(settings)
    client._deepseek_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
    )

    result = client._structured("ignored", "coder", "Implement this", CodeOutput)

    assert result.summary == "done"
    assert captured["model"] == "deepseek-v4-pro"
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["extra_body"] == {"thinking": {"type": "enabled"}}
    assert "JSON Schema" in captured["messages"][1]["content"]


def test_reader_can_route_to_doubao_independently(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        content = '{"summary":"read","relevant_files":[],"symbols":[],"dependencies":[],"conventions":[],"risks":[]}'
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    settings = Settings(
        provider="deepseek",
        deepseek_api_key="deepseek-key",
        agent_reader_provider="doubao",
        agent_reader_model="ep-reader",
        doubao_api_key="ark-key",
        worktree_root=tmp_path,
        database_path=tmp_path / "test.db",
    )
    client = AgentClient(settings)
    client._doubao_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
    )

    result = client._structured("fallback", "reader", "Read this", ReadingOutput)

    assert result.summary == "read"
    assert captured["model"] == "ep-reader"
    assert captured["response_format"] == {"type": "json_object"}
    assert settings.agent_routes["coder"]["provider"] == "deepseek"


def test_coder_can_route_to_qwen_independently(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        content = '{"summary":"coded","changes":[],"notes":[]}'
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    settings = Settings(
        provider="deepseek",
        deepseek_api_key="deepseek-key",
        agent_coder_provider="qwen",
        agent_coder_model="qwen-coder-test",
        qwen_api_key="qwen-key",
        worktree_root=tmp_path,
        database_path=tmp_path / "test.db",
    )
    client = AgentClient(settings)
    client._qwen_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
    )

    result = client._structured("fallback", "coder", "Write code", CodeOutput)

    assert result.summary == "coded"
    assert captured["model"] == "qwen-coder-test"
    assert captured["response_format"] == {"type": "json_object"}
