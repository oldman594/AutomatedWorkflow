import base64
import hashlib
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.updater import RunnerRelease, download_release, fetch_release, update_available


def test_signed_runner_manifest_and_package_checksum(tmp_path: Path) -> None:
    package = b"signed runner wheel"
    private_key = Ed25519PrivateKey.generate()
    public_key = base64.b64encode(private_key.public_key().public_bytes_raw()).decode()
    release = RunnerRelease(
        version="9.0.0",
        url="https://releases.example.test/runner.whl",
        sha256=hashlib.sha256(package).hexdigest(),
        signature="placeholder",
    )
    release.signature = base64.b64encode(private_key.sign(release.signed_payload())).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("manifest.json"):
            return httpx.Response(200, json=release.model_dump())
        return httpx.Response(200, content=package)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        verified = fetch_release(
            "https://releases.example.test/manifest.json",
            public_key,
            client=client,
        )
        target = download_release(verified, tmp_path, client=client)

    assert update_available(verified) is True
    assert target.read_bytes() == package
    assert target.stat().st_mode & 0o777 == 0o600


def test_runner_manifest_rejects_invalid_signature() -> None:
    private_key = Ed25519PrivateKey.generate()
    other_key = Ed25519PrivateKey.generate().public_key()
    release = RunnerRelease(
        version="9.0.0",
        url="https://releases.example.test/runner.whl",
        sha256="0" * 64,
        signature="placeholder",
    )
    release.signature = base64.b64encode(private_key.sign(release.signed_payload())).decode()
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=release.model_dump()))
    )

    with client, pytest.raises(RuntimeError, match="signature is invalid"):
        fetch_release(
            "https://releases.example.test/manifest.json",
            base64.b64encode(other_key.public_bytes_raw()).decode(),
            client=client,
        )
