r"""The gate's structure-ratchet stage: run the vendored script, map its exit code.

    & .\.venv\Scripts\python.exe -m scripts.structure_ratchet_gate

`scripts/structure_ratchet.py` (vendored, never edited here) exits 0 on a pass,
1 on a finding above its baseline (or a root folder that breaks `shape`), 2 on a
bad table or baseline and 3 when git is unusable, a file could not be read or
parsed, or `shape` is unknown. Exit 3 means nothing was checked, so it must fail
the gate as its own state and never read as a pass or a skip. The subprocess and
verdict path is `static_ratchet_gate.run`; this module owns only the
structure-ratchet labels.
"""

from __future__ import annotations

import sys
from pathlib import Path

from scripts import static_ratchet_gate as base

REPO_ROOT = base.REPO_ROOT
SCRIPT = REPO_ROOT / "scripts" / "structure_ratchet.py"

_FAIL_LABELS = {
    base.EXIT_NEW: "a file, function or directory grew past the baseline, or a root folder breaks the shape",
    base.EXIT_CONFIG: "bad [structure_ratchet] table, shape, baseline or arguments",
    base.EXIT_NOT_CONFIRMED: "NOT CONFIRMED -- git, a file or the shape could not be read, so nothing was checked",
}


def classify_exit(code: int) -> tuple[bool, str]:
    """`(ok, label)` for one script exit code. Only 0 passes."""
    if code == base.EXIT_PASS:
        return True, "no file, function or directory grew past the baseline and the root layout matches the shape"
    return False, _FAIL_LABELS.get(code, f"unexpected exit {code}")


def run(script: Path = SCRIPT) -> int:
    """Run the structure ratchet and return the gate's exit code (0 only on a pass)."""
    return base.run(script, stage="structure ratchet", classify=classify_exit)


if __name__ == "__main__":
    sys.exit(run())
