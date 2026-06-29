"""Short-lived HMAC-signed scoped tokens for camera stream/snapshot URLs.

The long-lived bearer token must never appear in a URL (browser history, server
logs, Referer). These 60-second tokens are issued via ``POST /api/cameras/stream-token``
(which itself requires the bearer token as an Authorization header) and accepted by
the middleware for the specific paths that ``<img src>`` must reach without a header.

Token format: ``"{expiry_unix_ts}.{sha256_hmac_hex}"`` — stateless, no server storage.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Dict

_TOKEN_TTL = 60  # seconds


def _sign(bearer_token: str, expiry: int) -> str:
    msg = f"camera-stream:{expiry}".encode()
    return hmac.new(bearer_token.encode(), msg, hashlib.sha256).hexdigest()


def issue(bearer_token: str) -> Dict[str, Any]:
    """Return a fresh scoped token valid for ``_TOKEN_TTL`` seconds."""
    expiry = int(time.time()) + _TOKEN_TTL
    sig = _sign(bearer_token, expiry)
    return {"token": f"{expiry}.{sig}", "expires_in": _TOKEN_TTL}


def verify(camera_token: str, bearer_token: str) -> bool:
    """Return ``True`` iff ``camera_token`` is a valid, unexpired scoped token."""
    try:
        ts_str, sig = camera_token.split(".", 1)
        expiry = int(ts_str)
    except (ValueError, AttributeError):
        return False
    if int(time.time()) > expiry:
        return False
    expected = _sign(bearer_token, expiry)
    return hmac.compare_digest(sig, expected)
