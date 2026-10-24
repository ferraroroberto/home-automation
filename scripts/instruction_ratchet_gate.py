r"""The gate's instruction-ratchet stage: run the vendored script, map its exit code.

    & .\.venv\Scripts\python.exe -m scripts.instruction_ratchet_gate

`scripts/instruction_ratchet.py` (vendored, never edited here) exits 0 on a pass,
1 on a count that rose (or a file past its size cap, or a missing baseline), 2 on a
bad table or baseline and 3 when git is unusable, an instruction file cannot be
read, a fenced block never closes or a skill description cannot be read. Exit 3
means nothing was checked, so it must fail the gate as its own state and never read
as a pass or a skip. The subprocess and verdict path is `static_ratchet_gate.run`;
this module owns only the instruction-ratchet labels.
"""

from __future__ import annotations

import sys
from pathlib import Path

from scripts import static_ratchet_gate as base

REPO_ROOT = base.REPO_ROOT
SCRIPT = REPO_ROOT / "scripts" / "instruction_ratchet.py"

_FAIL_LABELS = {
    base.EXIT_NEW: "an instruction file gained a rule hit or passed its size cap, or the baseline is missing",
    base.EXIT_CONFIG: "bad [instruction_ratchet] table, baseline or arguments",
    base.EXIT_NOT_CONFIRMED: "NOT CONFIRMED -- git or an instruction file could not be read, so nothing was checked",
}


def classify_exit(code: int) -> tuple[bool, str]:
    """`(ok, label)` for one script exit code. Only 0 passes."""
    if code == base.EXIT_PASS:
        return True, "no instruction file gained a rule hit or passed its size cap"
    return False, _FAIL_LABELS.get(code, f"unexpected exit {code}")


def run(script: Path = SCRIPT) -> int:
    """Run the instruction ratchet and return the gate's exit code (0 only on a pass)."""
    return base.run(script, stage="instruction ratchet", classify=classify_exit)


if __name__ == "__main__":
    sys.exit(run())
