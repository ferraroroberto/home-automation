r"""Gate stage for the usage track ratchet: run the vendored script, label its exit code.

    & .\.venv\Scripts\python.exe -m scripts.usage_track_ratchet_gate

Exit 3 from `scripts/usage_track_ratchet.py` (with its reader `usage_track_scan.py`) means a
file was unreadable or its JS never closed, so nothing was checked; it fails here as NOT
CONFIRMED. The subprocess and verdict path is `static_ratchet_gate.run`.
"""

from __future__ import annotations

import sys
from pathlib import Path

from scripts import static_ratchet_gate as base

REPO_ROOT = base.REPO_ROOT
SCRIPT = REPO_ROOT / "scripts" / "usage_track_ratchet.py"

_FAIL_LABELS = {
    base.EXIT_NEW: "a rule's hits rose above the baseline, the app does not reach its usage log, or the baseline is missing",
    base.EXIT_CONFIG: "bad [usage_track_ratchet] table, baseline or arguments",
    base.EXIT_NOT_CONFIRMED: "NOT CONFIRMED -- a file could not be read or closed, so nothing was checked",
}


def classify_exit(code: int) -> tuple[bool, str]:
    """`(ok, label)` for one script exit code. Only 0 passes."""
    if code == base.EXIT_PASS:
        return True, "no untracked control, sheet or built id beyond the baseline"
    return False, _FAIL_LABELS.get(code, f"unexpected exit {code}")


def run(script: Path = SCRIPT) -> int:
    """Run the usage track ratchet and return the gate's exit code (0 only on a pass)."""
    return base.run(script, stage="usage track ratchet", classify=classify_exit)


if __name__ == "__main__":
    sys.exit(run())
