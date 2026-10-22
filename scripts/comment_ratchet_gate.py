r"""The gate's comment-ratchet stage: run the vendored script, map its exit code.

    & .\.venv\Scripts\python.exe -m scripts.comment_ratchet_gate

`scripts/comment_ratchet.py` (vendored, never edited here) exits 0 on a pass,
1 on a file's reference count rising (or a new file carrying references), 2 on
a bad table or baseline and 3 when git is unusable or a file could not be
scanned. Exit 3 means nothing was checked, so it must fail the gate as its own
state and never read as a pass or a skip. The subprocess and verdict path is
`static_ratchet_gate.run`; this module owns only the comment-ratchet labels.
"""

from __future__ import annotations

import sys
from pathlib import Path

from scripts import static_ratchet_gate as base

REPO_ROOT = base.REPO_ROOT
SCRIPT = REPO_ROOT / "scripts" / "comment_ratchet.py"

_FAIL_LABELS = {
    base.EXIT_NEW: "a file's #NNN reference count rose above the baseline",
    base.EXIT_CONFIG: "bad [comment_ratchet] table, baseline or arguments",
    base.EXIT_NOT_CONFIRMED: "NOT CONFIRMED -- git or a file could not be scanned, so nothing was checked",
}


def classify_exit(code: int) -> tuple[bool, str]:
    """`(ok, label)` for one script exit code. Only 0 passes."""
    if code == base.EXIT_PASS:
        return True, "no file's issue-reference comment count rose above the baseline"
    return False, _FAIL_LABELS.get(code, f"unexpected exit {code}")


def run(script: Path = SCRIPT) -> int:
    """Run the comment ratchet and return the gate's exit code (0 only on a pass)."""
    return base.run(script, stage="comment ratchet", classify=classify_exit)


if __name__ == "__main__":
    sys.exit(run())
