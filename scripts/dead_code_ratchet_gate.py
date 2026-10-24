r"""The gate's dead-code-ratchet stage: run the vendored script, map its exit code.

    & .\.venv\Scripts\python.exe -m scripts.dead_code_ratchet_gate

`scripts/dead_code_ratchet.py` (vendored, never edited here) exits 0 on a pass, 1 on an
unreferenced definition the baseline does not record (or a missing baseline), 2 on a bad
table or baseline and 3 when git is unusable or a tracked Python file cannot be read or
parsed. Exit 3 means nothing was checked, so it must fail the gate as its own state and
never read as a pass or a skip. The subprocess and verdict path is
`static_ratchet_gate.run`; this module owns only the dead-code labels.
"""

from __future__ import annotations

import sys
from pathlib import Path

from scripts import static_ratchet_gate as base

REPO_ROOT = base.REPO_ROOT
SCRIPT = REPO_ROOT / "scripts" / "dead_code_ratchet.py"

_FAIL_LABELS = {
    base.EXIT_NEW: "a function, class or method nothing references, or the baseline is missing",
    base.EXIT_CONFIG: "bad [dead_code_ratchet] table, baseline or arguments",
    base.EXIT_NOT_CONFIRMED: "NOT CONFIRMED -- git or a Python file could not be read or parsed, so nothing was checked",
}


def classify_exit(code: int) -> tuple[bool, str]:
    """`(ok, label)` for one script exit code. Only 0 passes."""
    if code == base.EXIT_PASS:
        return True, "no function, class or method nothing references beyond the baseline"
    return False, _FAIL_LABELS.get(code, f"unexpected exit {code}")


def run(script: Path = SCRIPT) -> int:
    """Run the dead-code ratchet and return the gate's exit code (0 only on a pass)."""
    return base.run(script, stage="dead code ratchet", classify=classify_exit)


if __name__ == "__main__":
    sys.exit(run())
