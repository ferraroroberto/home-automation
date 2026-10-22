r"""The gate's docs-ratchet stage: run the vendored script, map its exit code.

    & .\.venv\Scripts\python.exe -m scripts.docs_ratchet_gate

`scripts/docs_ratchet.py` (vendored, never edited here) exits 0 on a pass, 1 on
a README over budget or a risen history-marker count, 2 on a bad table or
baseline and 3 when git or a doc could not be read, or a fenced block never
closes. Exit 3 means nothing was checked, so it must fail the gate as its own
state and never read as a pass or a skip. The subprocess and verdict path is
`static_ratchet_gate.run`; this module owns only the docs labels.
"""

from __future__ import annotations

import sys
from pathlib import Path

from scripts import static_ratchet_gate as base

REPO_ROOT = base.REPO_ROOT
SCRIPT = REPO_ROOT / "scripts" / "docs_ratchet.py"

_FAIL_LABELS = {
    base.EXIT_NEW: "README over budget or a history marker above the baseline",
    base.EXIT_CONFIG: "bad [docs_ratchet] table, baseline or arguments",
    base.EXIT_NOT_CONFIRMED: "NOT CONFIRMED -- git or a doc could not be read, so nothing was checked",
}


def classify_exit(code: int) -> tuple[bool, str]:
    """`(ok, label)` for one script exit code. Only 0 passes."""
    if code == base.EXIT_PASS:
        return True, "README within budget and no new history marker"
    return False, _FAIL_LABELS.get(code, f"unexpected exit {code}")


def run(script: Path = SCRIPT) -> int:
    """Run the docs ratchet and return the gate's exit code (0 only on a pass)."""
    return base.run(script, stage="docs ratchet", classify=classify_exit)


if __name__ == "__main__":
    sys.exit(run())
