import httpx
import pytest
from cryptography.fernet import Fernet

from app.config import Settings
from app.credentials import CredentialVault
from app.git_platform import GitPublisher
from app.models import GitIntegrationInfo, GitProvider
from app.repository import RepositoryError


class FakeRepository:
    def __init__(self, remote: str) -> None:
        self.remote = remote
        self.pushes: list[tuple[str, str, str]] = []

    def remote_url(self) -> str:
        return self.remote

    def push_with_token(self, branch: str, username: str, token: str) -> None:
        self.pushes.append((branch, username, token))


def integration(provider: GitProvider = GitProvider.GITHUB) -> GitIntegrationInfo:
    return GitIntegrationInfo(
        project_id="project-1",
        provider=provider,
        base_url="https://api.github.com",
        repository="acme/payments",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def test_credential_vault_encrypts_project_token() -> None:
    key = Fernet.generate_key().decode()
    vault = CredentialVault(Settings(credential_encryption_key=key))
    encrypted = vault.encrypt("github-secret-token")

    assert encrypted != "github-secret-token"
    assert vault.decrypt(encrypted) == "github-secret-token"


def test_github_publisher_pushes_branch_and_creates_pull_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            201,
            json={"html_url": "https://github.com/acme/payments/pull/42", "number": 42},
        )

    repository = FakeRepository("https://github.com/acme/payments.git")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = GitPublisher(client).publish(
            repository,  # type: ignore[arg-type]
            integration(),
            "github-secret-token",
            "autoflow/payment",
            "main",
            "Implement payment",
            "Validated change",
        )

    assert repository.pushes == [("autoflow/payment", "x-access-token", "github-secret-token")]
    assert requests[0].url.path == "/repos/acme/payments/pulls"
    assert requests[0].headers["authorization"] == "Bearer github-secret-token"
    assert result.external_id == "42"


def test_gitlab_publisher_pushes_branch_and_creates_merge_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            201,
            json={
                "web_url": "https://gitlab.example.com/acme/payments/-/merge_requests/7",
                "iid": 7,
            },
        )

    repository = FakeRepository("https://gitlab.example.com/acme/payments.git")
    gitlab = integration(GitProvider.GITLAB).model_copy(
        update={"base_url": "https://gitlab.example.com/api/v4"}
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = GitPublisher(client).publish(
            repository,  # type: ignore[arg-type]
            gitlab,
            "gitlab-secret-token",
            "autoflow/payment",
            "main",
            "Implement payment",
            "Validated change",
        )

    assert repository.pushes == [("autoflow/payment", "oauth2", "gitlab-secret-token")]
    assert requests[0].url.raw_path == b"/api/v4/projects/acme%2Fpayments/merge_requests"
    assert requests[0].headers["private-token"] == "gitlab-secret-token"
    assert result.external_id == "7"


def test_git_publisher_rejects_mismatched_remote() -> None:
    repository = FakeRepository("https://github.com/other/repository.git")
    with pytest.raises(RepositoryError, match="does not match"):
        GitPublisher().publish(
            repository,  # type: ignore[arg-type]
            integration(),
            "token-value",
            "feature",
            "main",
            "Title",
            "Body",
        )
