"""Generate local VAPID keys for browser Web Push.

Writes ``config/push_config.json`` with a private key (secret) and public key
used by the browser subscription call. The file is gitignored.

The private key is stored as the base64url-encoded **raw** 32-byte EC private
scalar (no PEM armor). This is the format ``pywebpush``/``py_vapid`` expect
when ``vapid_private_key`` is passed as a plain string rather than a file
path (`py_vapid.Vapid.from_string`): it strips newlines and base64url-decodes
the whole value, then dispatches on decoded length — 32 bytes means "raw
scalar", anything else is parsed as DER. A full PEM string (with
``-----BEGIN/END-----`` markers) fails that path: the header/footer text gets
base64url-"decoded" along with the key material, corrupting the DER bytes and
producing a "ASN.1 parsing error: invalid length" at send time (see #284).
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
    private_raw = private.private_numbers().private_value.to_bytes(32, "big")
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "public_key": _b64url(public),
                "private_key": _b64url(private_raw),
                "subject": subject,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {OUT}")
    print("Existing browser subscriptions are tied to the old public key and")
    print("will need to re-subscribe (Enable notifications) after this change.")
    print("Restart the webapp, then tap Enable notifications in the Presence panel.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
