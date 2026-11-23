r"""Gate stage for the windows-only ratchet: run the vendored script, label its exit code.

    & .\.venv\Scripts\python.exe -m scripts.windows_only_ratchet_gate

Exit 3 from `scripts/windows_only_ratchet.py` means git could not list the tree, or a test
module could not be read or parsed, so nothing was checked; it fails here as NOT CONFIRMED.
The subprocess and verdict path is `static_ratchet_gate.run`.
"""

from __future__ import annotations

import sys
from pathlib import Path

from scripts import static_ratchet_gate as base

REPO_ROOT = base.REPO_ROOT
SCRIPT = REPO_ROOT / "scripts" / "windows_only_ratchet.py"

_FAIL_LABELS = {
    base.EXIT_NEW: "a test uses a Windows-only API without the windows_only marker, or the baseline is missing",
    base.EXIT_CONFIG: "bad [windows_only_ratchet] table, baseline or arguments",
    base.EXIT_NOT_CONFIRMED: "NOT CONFIRMED -- git or a test module could not be read or parsed, so nothing was checked",
}


def classify_exit(code: int) -> tuple[bool, str]:
    """`(ok, label)` for one script exit code. Only 0 passes."""
    if code == base.EXIT_PASS:
        return True, "no unmarked Windows-only test beyond the baseline"
    return False, _FAIL_LABELS.get(code, f"unexpected exit {code}")


def run(script: Path = SCRIPT) -> int:
    """Run the windows-only ratchet and return the gate's exit code (0 only on a pass)."""
    return base.run(script, stage="windows only ratchet", classify=classify_exit)


if __name__ == "__main__":
    sys.exit(run())
