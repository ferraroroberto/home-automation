"""Constant-time secret comparison that tolerates non-ASCII text.

``hmac.compare_digest(str, str)`` raises ``TypeError`` when either string holds
a non-ASCII character, which turned a password like ``contraseña`` into an
unloginable 500 and a non-ASCII token into a 500 instead of a 401. Comparing
the UTF-8 bytes keeps it constant-time and total.
"""

from __future__ import annotations

import hmac


def secrets_match(presented: str, expected: str) -> bool:
    """True iff the two strings are equal, compared as UTF-8 bytes."""
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))
