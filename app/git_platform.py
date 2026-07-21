from __future__ import annotations

from urllib.parse import quote, urlsplit

import httpx

from app.models import GitIntegrationInfo, GitProvider, PublishResult
from app.repository import Repository, RepositoryError


class GitPublisher:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client

    def publish(
        self,
        repository: Repository,
        integration: GitIntegrationInfo,
        token: str,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> PublishResult:
        self._validate_remote(repository.remote_url(), integration)
        username = "x-access-token" if integration.provider == GitProvider.GITHUB else "oauth2"
        repository.push_with_token(branch, username, token)
        if integration.provider == GitProvider.GITHUB:
            return self._github(integration, token, branch, base_branch, title, body)
        return self._gitlab(integration, token, branch, base_branch, title, body)

    def _github(
        self,
        integration: GitIntegrationInfo,
        token: str,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> PublishResult:
        response = self._post(
            f"{integration.base_url}/repos/{integration.repository}/pulls",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"title": title, "head": branch, "base": base_branch, "body": body},
        )
        payload = response.json()
        return PublishResult(
            provider=integration.provider,
            branch=branch,
            url=str(payload["html_url"]),
            external_id=str(payload["number"]),
        )

    def _gitlab(
        self,
        integration: GitIntegrationInfo,
        token: str,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> PublishResult:
        project = quote(integration.repository, safe="")
        response = self._post(
            f"{integration.base_url}/projects/{project}/merge_requests",
            headers={"PRIVATE-TOKEN": token},
            json={
                "source_branch": branch,
                "target_branch": base_branch,
                "title": title,
                "description": body,
            },
        )
        payload = response.json()
        return PublishResult(
            provider=integration.provider,
            branch=branch,
            url=str(payload["web_url"]),
            external_id=str(payload["iid"]),
        )

    def _post(self, url: str, **kwargs) -> httpx.Response:
        if self.client:
            response = self.client.post(url, **kwargs)
        else:
            response = httpx.post(url, timeout=30, **kwargs)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RepositoryError(
                f"Git platform request failed ({response.status_code}): {response.text[-2000:]}"
            ) from exc
        return response

    @staticmethod
    def _validate_remote(remote_url: str, integration: GitIntegrationInfo) -> None:
        remote = urlsplit(remote_url)
        if remote.scheme != "https" or not remote.hostname:
            raise RepositoryError("Token publishing requires an HTTPS Git remote")
        expected_path = "/" + integration.repository.strip("/").removesuffix(".git")
        actual_path = remote.path.removesuffix(".git")
        if actual_path != expected_path:
            raise RepositoryError("Git remote does not match the configured project repository")
        api_host = urlsplit(integration.base_url).hostname or ""
        allowed_hosts = {api_host}
        if api_host == "api.github.com":
            allowed_hosts.add("github.com")
        if remote.hostname not in allowed_hosts:
            raise RepositoryError("Git remote host does not match the configured platform")
