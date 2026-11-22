r"""Gate stage for the request budget ratchet: run the vendored script, label its exit code.

    & .\.venv\Scripts\python.exe -m scripts.request_budget_ratchet_gate

Exit 3 from `scripts/request_budget_ratchet.py` (with its reader `request_budget_scan.py`) means
git was unusable, a file could not be read, JS never closed or Python did not parse, so nothing
was checked; it fails here as NOT CONFIRMED. The subprocess and verdict path is
`static_ratchet_gate.run`.
"""

from __future__ import annotations

import sys
from pathlib import Path

from scripts import static_ratchet_gate as base

REPO_ROOT = base.REPO_ROOT
SCRIPT = REPO_ROOT / "scripts" / "request_budget_ratchet.py"

_FAIL_LABELS = {
    base.EXIT_NEW: "a rule's hits rose above the baseline, or the baseline is missing",
    base.EXIT_CONFIG: "bad [request_budget_ratchet] or [request_budget] table, baseline or arguments",
    base.EXIT_NOT_CONFIRMED: "NOT CONFIRMED -- a file could not be read or parsed, so nothing was checked",
}


def classify_exit(code: int) -> tuple[bool, str]:
    """`(ok, label)` for one script exit code. Only 0 passes."""
    if code == base.EXIT_PASS:
        return True, "no request-budget hit beyond the baseline"
    return False, _FAIL_LABELS.get(code, f"unexpected exit {code}")


def run(script: Path = SCRIPT) -> int:
    """Run the request budget ratchet and return the gate's exit code (0 only on a pass)."""
    return base.run(script, stage="request budget ratchet", classify=classify_exit)


if __name__ == "__main__":
    sys.exit(run())
