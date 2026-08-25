"""Per-channel preferences for the Athom CT-clamp meters (issue #25).

Maps ``"<meter_id>:<channel>"`` →
``{"display_name": str, "invert": bool, "hidden": bool}``, and is the reason a
clamp can be labelled, corrected and put away entirely from the card instead of
by editing a file or changing code.

Why this is not another ``display_names.py`` clone
--------------------------------------------------
``src/display_names.py`` and its two siblings (``tuya_display_names``,
``security_display_names``) hold a flat ``id → name`` mapping and reuse each
other verbatim. A circuit channel needs *further* per-channel facts — whether
its CT clamp was fitted backwards, and whether it is worth showing at all — so
the value here is an object, not a string. Bolting a parallel ``id → bool``
store alongside a ``id → name`` one would mean three files, three endpoints and
three ways for the set to drift apart for what is one row in one dialog. Same
atomic-write discipline, same "missing file is not an error" contract, richer
value. (The Tuya side did split them — ``tuya_display_names`` +
``tuya_hidden`` — which is exactly the shape this module exists to avoid.)

``invert`` exists because the BL0906 reports **signed** power, so a clamp
fitted with its arrow against the flow reads negative on an ordinary load. That
is an installation detail rather than a measurement, and the alternative to
correcting it in software is reopening a live consumer unit — so the card
offers the flip instead. It is applied in exactly one place
(:func:`src.athom_client._build_state`), which keeps both the raw and the
corrected value on the wire.

``hidden`` (issue #619) exists because a 6-channel meter in a consumer unit
with four breakers still reports six channels, and the two spare terminals are
noise on a card read at a glance. It is deliberately a **user** decision and
never inferred from a reading: the card's founding invariant is that a channel
is never hidden for measuring 0 W, so a clamp fitted next week starts reading
with nothing to reconfigure. Hiding is presentation only — the server keeps
returning every channel, and the flag rides along on the wire.

The real file is gitignored: it holds room and appliance names, and this is a
public repo. ``config/circuit_prefs.sample.json`` shows the shape.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

from src._atomic_json import write_json_atomic
from src._schedule_store import read_json

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "circuit_prefs.json"


def load_circuit_prefs(path: Optional[Path] = None) -> Dict[str, Dict[str, object]]:
    """Return ``{channel_key: {"display_name", "invert", "hidden"}}``, or ``{}``.

    A missing file is the normal first-run state, not an error. Malformed rows
    are dropped individually rather than discarding the whole file — one bad
    hand-edit must not silently un-label every other circuit. A row written by
    an older build simply has no ``hidden`` key and reads back as ``False``.

    Raises :class:`~src._schedule_store.StoreUnreadableError` when the file
    exists but can't be read (issue #692) — ``_set_field`` mutates this
    return value and saves it whole, so a transient failure here would
    otherwise get saved back over every other channel's prefs.
    """
    target = Path(path) if path is not None else DEFAULT_PATH
    raw = read_json(target, None)
    if not isinstance(raw, dict):
        return {}

    prefs: Dict[str, Dict[str, object]] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            logger.warning("⚠️ %s: entry %r is not an object; skipping", target, key)
            continue
        name = str(value.get("display_name") or "").strip()
        invert = bool(value.get("invert"))
        hidden = bool(value.get("hidden"))
        if not name and not invert and not hidden:
            continue  # a row with nothing set is the same as no row
        prefs[str(key)] = {"display_name": name, "invert": invert, "hidden": hidden}
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


def load_hidden_channels(path: Optional[Path] = None) -> Dict[str, bool]:
    """Just the hidden flags, as ``{key: True}`` for the put-away channels."""
    return {
        key: True for key, value in load_circuit_prefs(path).items() if value.get("hidden")
    }


#: One row's defaults — the shape ``_set_field`` starts from and the set of
#: fields it checks before pruning a row. Adding a fourth per-channel fact means
#: editing this one line, not three functions.
_DEFAULTS: Dict[str, object] = {"display_name": "", "invert": False, "hidden": False}


def _set_field(key: str, field: str, value: object, path: Optional[Path]) -> None:
    """Update one field of one channel's prefs, persisting immediately.

    A row whose every field is back at its default is removed rather than left
    as an empty object, so clearing a label leaves no residue on disk.
    """
    prefs = load_circuit_prefs(path)
    entry = dict(prefs.get(key) or _DEFAULTS)
    entry[field] = value
    if all(not entry.get(name) for name in _DEFAULTS):
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


def set_circuit_hidden(key: str, hidden: bool, path: Optional[Path] = None) -> None:
    """Put one channel away in the card, or bring it back."""
    _set_field(key, "hidden", bool(hidden), path)
