r"""The gate's runtime-write-ratchet stage: run the vendored script, map its exit code.

    & .\.venv\Scripts\python.exe -m scripts.runtime_write_ratchet_gate

`scripts/runtime_write_ratchet.py` (vendored with the resolver it imports,
`scripts/runtime_write_resolve.py`; never edited here) exits 0 on a pass, 1 when a
file's count of write targets resolving under a code folder rose (or the baseline is
missing), 2 on a bad table or baseline and 3 when a new write target cannot be
resolved, git is unusable or a tracked Python file does not parse. Exit 3 means a write
went unplaced, so it must fail the gate as its own state and never read as a pass or a
skip. The subprocess and verdict path is `static_ratchet_gate.run`; this module owns
only the runtime write labels. The gate calls the script itself only for its
--snapshot/--probe pair around the test runs.
"""

from __future__ import annotations

import sys
from pathlib import Path

from scripts import static_ratchet_gate as base

REPO_ROOT = base.REPO_ROOT
SCRIPT = REPO_ROOT / "scripts" / "runtime_write_ratchet.py"

_FAIL_LABELS = {
    base.EXIT_NEW: "a file gained a write target resolving under a code folder, or the baseline is missing",
    base.EXIT_CONFIG: "bad [runtime_write_ratchet] table, baseline or arguments",
    base.EXIT_NOT_CONFIRMED: "NOT CONFIRMED -- a new write target could not be resolved, so it was not checked",
}


def classify_exit(code: int) -> tuple[bool, str]:
    """`(ok, label)` for one script exit code. Only 0 passes."""
    if code == base.EXIT_PASS:
        return True, "no write under a code folder above its baseline"
    return False, _FAIL_LABELS.get(code, f"unexpected exit {code}")


def run(script: Path = SCRIPT) -> int:
    """Run the runtime write ratchet and return the gate's exit code (0 only on a pass)."""
    return base.run(script, stage="runtime write ratchet", classify=classify_exit)


if __name__ == "__main__":
    sys.exit(run())
