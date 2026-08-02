"""Per-channel preferences for the Athom CT-clamp meters (issue #25).

Maps ``"<meter_id>:<channel>"`` → ``{"display_name": str, "invert": bool}``, and
is the reason a clamp can be labelled and corrected entirely from the card
instead of by editing a file or changing code.

Why this is not another ``display_names.py`` clone
--------------------------------------------------
``src/display_names.py`` and its two siblings (``tuya_display_names``,
``security_display_names``) hold a flat ``id → name`` mapping and reuse each
other verbatim. A circuit channel needs a *second* per-channel fact — whether
its CT clamp was fitted backwards — so the value here is an object, not a
string. Bolting a parallel ``id → bool`` store alongside a ``id → name`` one
would mean two files, two endpoints and two ways for the pair to drift apart
for what is one row in one dialog. Same atomic-write discipline, same
"missing file is not an error" contract, richer value.

``invert`` exists because the BL0906 reports **signed** power, so a clamp
fitted with its arrow against the flow reads negative on an ordinary load. That
is an installation detail rather than a measurement, and the alternative to
correcting it in software is reopening a live consumer unit — so the card
offers the flip instead. It is applied in exactly one place
(:func:`src.athom_client._build_state`), which keeps both the raw and the
corrected value on the wire.

The real file is gitignored: it holds room and appliance names, and this is a
public repo. ``config/circuit_prefs.sample.json`` shows the shape.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Optional

from src._atomic_json import write_json_atomic

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "circuit_prefs.json"


def load_circuit_prefs(path: Optional[Path] = None) -> Dict[str, Dict[str, object]]:
    """Return ``{channel_key: {"display_name": str, "invert": bool}}``, or ``{}``.

    A missing file is the normal first-run state, not an error. Malformed rows
    are dropped individually rather than discarding the whole file — one bad
    hand-edit must not silently un-label every other circuit.
    """
    target = Path(path) if path is not None else DEFAULT_PATH
    if not target.exists():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("⚠️ Could not read %s (%s); returning empty prefs", target, exc)
        return {}
    if not isinstance(raw, dict):
        logger.warning("⚠️ %s is not a JSON object; returning empty prefs", target)
        return {}

    prefs: Dict[str, Dict[str, object]] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            logger.warning("⚠️ %s: entry %r is not an object; skipping", target, key)
            continue
        name = str(value.get("display_name") or "").strip()
        invert = bool(value.get("invert"))
        if not name and not invert:
            continue  # a row with nothing set is the same as no row
        prefs[str(key)] = {"display_name": name, "invert": invert}
    return prefs


def save_circuit_prefs(
    prefs: Dict[str, Dict[str, object]], path: Optional[Path] = None
) -> None:
    """Atomically write the per-channel prefs map to disk."""
    target = Path(path) if path is not None else DEFAULT_PATH
    write_json_atomic(target, prefs)
    logger.info("💾 Saved circuit_prefs to %s", target)


def load_circuit_display_names(path: Optional[Path] = None) -> Dict[str, str]:
    """Just the labels, in the flat ``{key: name}`` shape the routers merge."""
    return {
        key: str(value.get("display_name") or "")
        for key, value in load_circuit_prefs(path).items()
        if value.get("display_name")
    }


def load_inverted_channels(path: Optional[Path] = None) -> Dict[str, bool]:
    """Just the sign corrections, as ``{key: True}`` for the flipped channels."""
    return {
        key: True for key, value in load_circuit_prefs(path).items() if value.get("invert")
    }


def _set_field(key: str, field: str, value: object, path: Optional[Path]) -> None:
    """Update one field of one channel's prefs, persisting immediately.

    A row whose every field is back at its default is removed rather than left
    as an empty object, so clearing a label leaves no residue on disk.
    """
    prefs = load_circuit_prefs(path)
    entry = dict(prefs.get(key) or {"display_name": "", "invert": False})
    entry[field] = value
    if not entry.get("display_name") and not entry.get("invert"):
        prefs.pop(key, None)
    else:
        prefs[key] = entry
    save_circuit_prefs(prefs, path)


def set_circuit_display_name(
    key: str, display_name: str, path: Optional[Path] = None
) -> None:
    """Set or clear one channel's label (``""`` clears it)."""
    _set_field(key, "display_name", (display_name or "").strip(), path)


def set_circuit_inverted(key: str, invert: bool, path: Optional[Path] = None) -> None:
    """Flip or unflip the sign of one channel's power reading."""
    _set_field(key, "invert", bool(invert), path)
