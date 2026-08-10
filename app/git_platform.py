from __future__ import annotations

from urllib.parse import quote, urlsplit

import httpx

from app.models import GitIntegrationInfo, GitProvider, IntegrationProbe, PublishResult
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

    def validate_credentials(
        self,
        provider: GitProvider,
        base_url: str,
        repository: str,
        token: str,
    ) -> IntegrationProbe:
        base_url = base_url.rstrip("/")
        repository = repository.strip().removesuffix(".git").strip("/")
        if provider == GitProvider.GITHUB:
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            account = self._get(f"{base_url}/user", headers=headers).json()
            project = self._get(f"{base_url}/repos/{repository}", headers=headers).json()
            can_push = bool((project.get("permissions") or {}).get("push"))
            account_name = str(account.get("login") or account.get("name") or "unknown")
        else:
            headers = {"PRIVATE-TOKEN": token}
            account = self._get(f"{base_url}/user", headers=headers).json()
            project_path = quote(repository, safe="")
            project = self._get(f"{base_url}/projects/{project_path}", headers=headers).json()
            account_name = str(account.get("username") or account.get("name") or "unknown")
            user_id = account.get("id")
            membership = self._get(
                f"{base_url}/projects/{project_path}/members/all/{user_id}", headers=headers
            ).json()
            can_push = int(membership.get("access_level") or 0) >= 30
        if not can_push:
            raise RepositoryError(
                f"Git token account {account_name} does not have push permission for {repository}"
            )
        return IntegrationProbe(
            service="git",
            ok=True,
            detail="Token, account and repository push permission verified",
            provider=provider.value,
            account=account_name,
            repository=str(
                project.get("path_with_namespace") or project.get("full_name") or repository
            ),
            default_branch=str(project.get("default_branch") or "") or None,
            can_push=True,
        )

    def _github(
        self,
        integration: GitIntegrationInfo,
        token: str,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> PublishResult:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            response = self._post(
                f"{integration.base_url}/repos/{integration.repository}/pulls",
                headers=headers,
                json={"title": title, "head": branch, "base": base_branch, "body": body},
            )
            payload = response.json()
        except RepositoryError as exc:
            if "(422)" not in str(exc):
                raise
            owner = integration.repository.split("/", 1)[0]
            existing = self._get(
                f"{integration.base_url}/repos/{integration.repository}/pulls",
                headers=headers,
                params={"state": "open", "head": f"{owner}:{branch}", "base": base_branch},
            ).json()
            if not existing:
                raise exc
            payload = existing[0]
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
        headers = {"PRIVATE-TOKEN": token}
        try:
            response = self._post(
                f"{integration.base_url}/projects/{project}/merge_requests",
                headers=headers,
                json={
                    "source_branch": branch,
                    "target_branch": base_branch,
                    "title": title,
                    "description": body,
                },
            )
            payload = response.json()
        except RepositoryError as exc:
            if "(409)" not in str(exc) and "(400)" not in str(exc):
                raise
            existing = self._get(
                f"{integration.base_url}/projects/{project}/merge_requests",
                headers=headers,
                params={
                    "state": "opened",
                    "source_branch": branch,
                    "target_branch": base_branch,
                },
            ).json()
            if not existing:
                raise exc
            payload = existing[0]
        return PublishResult(
            provider=integration.provider,
            branch=branch,
            url=str(payload["web_url"]),
            external_id=str(payload["iid"]),
        )

    def _post(self, url: str, **kwargs) -> httpx.Response:
        return self._request("POST", url, **kwargs)

    def _get(self, url: str, **kwargs) -> httpx.Response:
        return self._request("GET", url, **kwargs)

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            if self.client:
                response = self.client.request(method, url, **kwargs)
            else:
                response = httpx.request(method, url, timeout=30, **kwargs)
        except httpx.RequestError as exc:
            raise RepositoryError(
                f"Git platform connection failed: {exc.__class__.__name__}"
            ) from exc
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            headers = kwargs.get("headers") or {}
            secrets = [
                str(headers.get(name) or "").removeprefix("Bearer ")
                for name in ("Authorization", "PRIVATE-TOKEN")
            ]
            detail = self._error_detail(response, [value for value in secrets if value])
            raise RepositoryError(
                f"Git platform request failed ({response.status_code}): {detail}"
            ) from exc
        return response

    @staticmethod
    def _error_detail(response: httpx.Response, secrets: list[str]) -> str:
        try:
            payload = response.json()
        except ValueError:
            return response.reason_phrase or "request rejected"
        if isinstance(payload, dict):
            value = (
                payload.get("message") or payload.get("error_description") or payload.get("error")
            )
            if isinstance(value, str):
                for secret in secrets:
                    value = value.replace(secret, "[REDACTED]")
                return value[:500]
        return response.reason_phrase or "request rejected"

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
