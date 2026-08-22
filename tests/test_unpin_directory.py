"""`scripts/unpin_directory.py` must never close a *live* process's handle (#681).

Closing a directory handle out from under a healthy process is the one genuinely
dangerous thing this script can do, so the guard that stops it is what gets
pinned here. The wedged branch cannot be tested deterministically — a helper
wedged inside termination is a load-sensitive teardown race that 12 deliberate
attempts failed to reproduce — so what is asserted is the classification and the
refusal, against a real live holder rather than a mock.

`is_delete_ready` is asserted separately from the holder list on purpose: after a
handle is closed the holder's PEB still carries the old cwd *string*, so the
holder list cannot be the success signal. Only the filesystem can answer that.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="unpin_directory is Windows-only"
)

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "unpin_directory.py"


@pytest.fixture(scope="module")
def unpin_module():
    spec = importlib.util.spec_from_file_location("unpin_directory", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def held_directory(unpin_module, tmp_path: Path) -> Iterator[Path]:
    """A directory pinned by a real live child process, torn down afterwards.

    Waits for the pin to actually exist rather than assuming `Popen` returning
    means it does: a child's cwd *handle* is opened during its own startup, a
    little after the parent gets the pid back, so probing immediately catches an
    unpinned directory and the test reads as a fix that isn't there. A pin that
    never appears is a hard failure — a skip here would report green on a guard
    nothing exercised.
    """
    target = tmp_path / "held"
    target.mkdir()
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        cwd=target,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if unpin_module.is_delete_ready(target) is False:
                break
            time.sleep(0.1)
        else:
            raise AssertionError(
                f"{target} never became pinned — the child process (pid {child.pid}) "
                f"did not take it as a working directory within 15s"
            )
        yield target
    finally:
        child.kill()
        child.wait(timeout=10)


def test_unheld_directory_is_delete_ready(unpin_module, tmp_path: Path) -> None:
    assert unpin_module.is_delete_ready(tmp_path) is True


def test_missing_directory_is_reported_not_crashed(unpin_module, tmp_path: Path) -> None:
    assert unpin_module.main([str(tmp_path / "nope")]) == 0


def test_live_holder_is_classified_live(unpin_module, held_directory: Path) -> None:
    holders = unpin_module.find_holders(held_directory)
    assert holders, "the live child should be found holding the directory"
    assert {h.state for h in holders} == {unpin_module.STATE_LIVE}


def test_close_refuses_to_touch_a_live_holder(unpin_module, held_directory: Path) -> None:
    assert unpin_module.main([str(held_directory), "--close"]) == 1
    # The refusal has to be real, not just a non-zero exit: the directory must
    # still be pinned, i.e. nothing was closed on the way out.
    assert unpin_module.is_delete_ready(held_directory) is False
