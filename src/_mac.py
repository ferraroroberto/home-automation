"""Single home for MAC-address key normalisation.

Every network module keys devices, overrides and rename stores by the same
canonical MAC form — upper-case, separators left as reported, whitespace
trimmed. This used to be copy-pasted as a private ``normalize_mac`` in each of
``dhcp_plan`` / ``network_oui`` / ``network_display_names`` / ``dhcp_overrides``;
they now all import it from here so the key form can never drift between them.

Note: ``src.network_types._normalise_mac`` is a *different*, richer helper
(``Optional[str]`` in/out, re-joins to colon-separated hex pairs) used by the
live network clients — it is intentionally not folded in here.
"""

from __future__ import annotations


def normalize_mac(mac: str) -> str:
    """Canonical key form: upper-case, separators as reported, whitespace-trimmed."""
    return (mac or "").strip().upper()
