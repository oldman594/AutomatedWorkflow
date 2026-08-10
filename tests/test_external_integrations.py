"""Opt-in tests that contact production-like external services.

Run only with AUTOFLOW_RUN_EXTERNAL_TESTS=1 and explicit disposable credentials/targets.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

import pytest

from app.agents import AgentClient
from app.config import Settings
from app.email_auth import SMTPVerificationSender
from app.git_platform import GitPublisher
from app.models import GitIntegrationInfo, GitProvider
from app.repository import Repository

pytestmark = pytest.mark.external


def external_enabled() -> bool:
    return os.getenv("AUTOFLOW_RUN_EXTERNAL_TESTS") == "1"


@pytest.mark.skipif(not external_enabled(), reason="external integration tests are opt-in")
def test_real_smtp_authentication_and_delivery() -> None:
    recipient = os.getenv("AUTOFLOW_TEST_SMTP_RECIPIENT")
    if not recipient:
        pytest.skip("AUTOFLOW_TEST_SMTP_RECIPIENT is not configured")
    sender = SMTPVerificationSender(Settings())

    sender.probe()
    sender.send_code(recipient, f"{secrets.randbelow(1_000_000):06d}", 600)


@pytest.mark.skipif(not external_enabled(), reason="external integration tests are opt-in")
def test_real_unique_llm_routes() -> None:
    settings = Settings()
    if settings.mock_llm:
        pytest.fail("AUTOFLOW_MOCK_LLM must be false for real LLM tests")
    errors = settings.route_errors()
    if errors:
        pytest.fail("; ".join(errors))

    client = AgentClient(settings)
    tested: set[tuple[str, str]] = set()
    for role, route in settings.agent_routes.items():
        identity = (route["provider"], route["model"])
        if identity in tested:
            continue
        provider, model, response = client.probe(role)
        assert (provider, model) == identity
        assert response
        tested.add(identity)


def git_test_configuration() -> tuple[GitProvider, str, str, str] | None:
    values = (
        os.getenv("AUTOFLOW_TEST_GIT_PROVIDER"),
        os.getenv("AUTOFLOW_TEST_GIT_BASE_URL"),
        os.getenv("AUTOFLOW_TEST_GIT_REPOSITORY"),
        os.getenv("AUTOFLOW_TEST_GIT_TOKEN"),
    )
    if not all(values):
        return None
    provider, base_url, repository, token = values
    return GitProvider(provider), str(base_url), str(repository), str(token)


@pytest.mark.skipif(not external_enabled(), reason="external integration tests are opt-in")
def test_real_git_account_repository_and_push_permission() -> None:
    configured = git_test_configuration()
    if not configured:
        pytest.skip("AUTOFLOW_TEST_GIT_* validation variables are not configured")
    provider, base_url, repository, token = configured

    result = GitPublisher().validate_credentials(provider, base_url, repository, token)

    assert result.ok is True
    assert result.can_push is True


@pytest.mark.skipif(
    not external_enabled() or os.getenv("AUTOFLOW_TEST_GIT_PUBLISH") != "1",
    reason="real PR/MR publishing requires an explicit disposable target",
)
def test_real_git_push_and_pull_or_merge_request() -> None:
    configured = git_test_configuration()
    worktree = os.getenv("AUTOFLOW_TEST_GIT_WORKTREE")
    if not configured or not worktree:
        pytest.skip("Git validation variables and AUTOFLOW_TEST_GIT_WORKTREE are required")
    provider, base_url, project_name, token = configured
    path = Path(worktree).expanduser().resolve()
    repository = Repository(path, [path.parent])
    branch = os.getenv("AUTOFLOW_TEST_GIT_BRANCH") or repository.current_branch()
    base_branch = os.getenv("AUTOFLOW_TEST_GIT_BASE_BRANCH", "main")
    info = GitIntegrationInfo(
        project_id="external-test",
        provider=provider,
        base_url=base_url,
        repository=project_name,
        created_at="external-test",
        updated_at="external-test",
    )

    result = GitPublisher().publish(
        repository,
        info,
        token,
        branch,
        base_branch,
        f"test: AutoFlow external integration {branch}",
        "Created by the opt-in AutoFlow external integration test.",
    )

    assert result.url.startswith("https://")
    assert result.external_id
