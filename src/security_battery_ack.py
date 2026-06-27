"""Acknowledgment watermark for the system-wide low-battery alert (issue #221).

The RISCO cloud exposes only an aggregate ``batteryLow`` flag — no per-detector
battery (see #220). To stop a known, un-fixable low cell from permanently lighting
the Home/Security tile, the user can *acknowledge* the alert: the badge hides until
a **new** ``Device Battery Low`` event appears (newer than the stored watermark) or
the aggregate flag clears and later re-raises.

The on-disk shape is a tiny JSON object — ``{"acknowledged": true,
"low_event_time": "<iso|null>"}`` — written atomically (same temp-then-replace
pattern as ``src.display_names``). It holds no detector names (just a timestamp),
but is gitignored for consistency with the other ``config/*.json`` stores; a
missing file simply means "never acknowledged".
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional, TypedDict

logger = logging.getLogger(__name__)

DEFAULT_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "security_battery_ack.json"
)


class BatteryAck(TypedDict):
    acknowledged: bool
    low_event_time: Optional[str]


def load_battery_ack(path: Optional[Path] = None) -> Optional[BatteryAck]:
    """Return the stored acknowledgment, or ``None`` if never acknowledged."""
    target = Path(path) if path is not None else DEFAULT_PATH
    if not target.exists():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("⚠️ Could not read %s (%s); treating as un-acknowledged", target, exc)
        return None
    if not isinstance(raw, dict) or not raw.get("acknowledged"):
        return None
    time = raw.get("low_event_time")
    return {"acknowledged": True, "low_event_time": str(time) if time else None}


def set_battery_ack(low_event_time: Optional[str], path: Optional[Path] = None) -> None:
    """Acknowledge the low-battery alert, watermarked at ``low_event_time``."""
    target = Path(path) if path is not None else DEFAULT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    payload: BatteryAck = {
        "acknowledged": True,
        "low_event_time": low_event_time or None,
    }
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)
    logger.info("💾 Acknowledged low-battery alert (watermark=%s)", low_event_time)


def clear_battery_ack(path: Optional[Path] = None) -> None:
    """Drop any stored acknowledgment (e.g. once the aggregate flag clears)."""
    target = Path(path) if path is not None else DEFAULT_PATH
    try:
        target.unlink()
        logger.info("🔄 Cleared low-battery acknowledgment")
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("⚠️ Could not clear %s (%s)", target, exc)
