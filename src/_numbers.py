"""Single home for the "number or ``None``" coercion the device clients share.

Cloud and device payloads report a missing reading as ``None``, an empty field
or a string, and a reading that is absent must stay ``None`` — never ``0`` — so
downstream code can tell "not measured" from "measured zero".
``src.energy.athom_client`` keeps its own variant on purpose: ESPHome also
reports a not-yet-measured sensor as ``"nan"``, which it maps to ``None``.
"""

from __future__ import annotations

from typing import Any, Optional


def optional_float(value: Any) -> Optional[float]:
    """``float(value)``, or ``None`` when the value is missing or not numeric.

    A string is accepted with surrounding whitespace (``float`` strips it).
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
