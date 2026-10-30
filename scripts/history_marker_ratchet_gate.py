r"""The gate's history-marker-ratchet stage: run the script, map its exit code.

    & .\.venv\Scripts\python.exe -m scripts.history_marker_ratchet_gate

`scripts/history_marker_ratchet.py` (repo-specific, not vendored) exits 0 on a
pass, 1 on a file's marker count rising (or a new file carrying markers), 2 on
a bad table or baseline and 3 when git is unusable or a file could not be
read. Exit 3 means nothing was checked, so it must fail the gate as its own
state and never read as a pass or a skip. The subprocess and verdict path is
`static_ratchet_gate.run`; this module owns only the history-marker labels.
"""

from __future__ import annotations

import sys
from pathlib import Path

from scripts import static_ratchet_gate as base

REPO_ROOT = base.REPO_ROOT
SCRIPT = REPO_ROOT / "scripts" / "history_marker_ratchet.py"

_FAIL_LABELS = {
    base.EXIT_NEW: "a file's history-marker count rose above the baseline",
    base.EXIT_CONFIG: "bad [history_marker_ratchet] table, baseline or arguments",
    base.EXIT_NOT_CONFIRMED: "NOT CONFIRMED -- git or a file could not be read, so nothing was checked",
}


def classify_exit(code: int) -> tuple[bool, str]:
    """`(ok, label)` for one script exit code. Only 0 passes."""
    if code == base.EXIT_PASS:
        return True, "no file's history-narration marker count rose above the baseline"
    return False, _FAIL_LABELS.get(code, f"unexpected exit {code}")


def run(script: Path = SCRIPT) -> int:
    """Run the history marker ratchet and return the gate's exit code (0 only on a pass)."""
    return base.run(script, stage="history marker ratchet", classify=classify_exit)


if __name__ == "__main__":
    sys.exit(run())
