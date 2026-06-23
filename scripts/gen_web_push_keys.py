"""Generate local VAPID keys for browser Web Push.

Writes ``config/push_config.json`` with a private key (secret) and public key
used by the browser subscription call. The file is gitignored.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "config" / "push_config.json"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def main() -> int:
    subject = sys.argv[1] if len(sys.argv) > 1 else "mailto:admin@example.com"
    private = ec.generate_private_key(ec.SECP256R1())
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "public_key": _b64url(public),
                "private_key": private_pem,
                "subject": subject,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {OUT}")
    print("Restart the webapp, then tap Enable notifications in the Presence panel.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
