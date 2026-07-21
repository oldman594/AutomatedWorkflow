from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    encoded_key = os.environ.get("RUNNER_RELEASE_PRIVATE_KEY", "")
    if not encoded_key:
        raise SystemExit("RUNNER_RELEASE_PRIVATE_KEY is required")
    private_key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(encoded_key))
    payload = {
        "sha256": hashlib.sha256(args.wheel.read_bytes()).hexdigest(),
        "url": args.url,
        "version": args.version,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["signature"] = base64.b64encode(private_key.sign(canonical)).decode()
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
