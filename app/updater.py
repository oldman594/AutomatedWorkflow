from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from packaging.version import Version
from pydantic import BaseModel, Field

from app import __version__


class RunnerRelease(BaseModel):
    version: str
    url: str
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    signature: str

    def signed_payload(self) -> bytes:
        return json.dumps(
            {"sha256": self.sha256, "url": self.url, "version": self.version},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


def fetch_release(
    manifest_url: str,
    public_key: str,
    *,
    client: httpx.Client | None = None,
) -> RunnerRelease:
    owns_client = client is None
    http = client or httpx.Client(timeout=30, follow_redirects=True)
    try:
        response = http.get(manifest_url)
        response.raise_for_status()
        release = RunnerRelease.model_validate(response.json())
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key))
        key.verify(base64.b64decode(release.signature), release.signed_payload())
        return release
    except (ValueError, InvalidSignature) as exc:
        raise RuntimeError("Runner release manifest signature is invalid") from exc
    finally:
        if owns_client:
            http.close()


def update_available(release: RunnerRelease) -> bool:
    return Version(release.version) > Version(__version__)


def download_release(
    release: RunnerRelease,
    destination: Path,
    *,
    client: httpx.Client | None = None,
) -> Path:
    owns_client = client is None
    http = client or httpx.Client(timeout=120, follow_redirects=True)
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f"autoflow-runner-{release.version}.whl"
    temporary = target.with_suffix(".part")
    try:
        response = http.get(release.url)
        response.raise_for_status()
        content = response.content
        digest = hashlib.sha256(content).hexdigest()
        if digest.lower() != release.sha256.lower():
            raise RuntimeError("Runner release checksum does not match the signed manifest")
        temporary.write_bytes(content)
        temporary.chmod(0o600)
        temporary.replace(target)
        return target
    finally:
        if temporary.exists():
            temporary.unlink()
        if owns_client:
            http.close()
