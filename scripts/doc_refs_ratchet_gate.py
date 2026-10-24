r"""The gate's doc-refs-ratchet stage: run the vendored script, map its exit code.

    & .\.venv\Scripts\python.exe -m scripts.doc_refs_ratchet_gate

`scripts/doc_refs_ratchet.py` (vendored, never edited here) exits 0 on a pass, 1 on a
dangling reference the baseline does not record (or a missing baseline), 2 on a bad
table or baseline and 3 when git is unusable, a doc is not UTF-8 or a fenced block or
HTML comment never closes. Exit 3 means nothing was checked, so it must fail the gate
as its own state and never read as a pass or a skip. The subprocess and verdict path
is `static_ratchet_gate.run`; this module owns only the doc-refs labels.
"""

from __future__ import annotations

import sys
from pathlib import Path

from scripts import static_ratchet_gate as base

REPO_ROOT = base.REPO_ROOT
SCRIPT = REPO_ROOT / "scripts" / "doc_refs_ratchet.py"

_FAIL_LABELS = {
    base.EXIT_NEW: "a doc names a path, module, link or port that does not exist, or the baseline is missing",
    base.EXIT_CONFIG: "bad [doc_refs_ratchet] table, baseline or arguments",
    base.EXIT_NOT_CONFIRMED: "NOT CONFIRMED -- git or a doc could not be read, so nothing was checked",
}


def classify_exit(code: int) -> tuple[bool, str]:
    """`(ok, label)` for one script exit code. Only 0 passes."""
    if code == base.EXIT_PASS:
        return True, "no doc names a repo path, module, link or port that does not exist"
    return False, _FAIL_LABELS.get(code, f"unexpected exit {code}")


def run(script: Path = SCRIPT) -> int:
    """Run the doc-refs ratchet and return the gate's exit code (0 only on a pass)."""
    return base.run(script, stage="doc refs ratchet", classify=classify_exit)


if __name__ == "__main__":
    sys.exit(run())
