"""Automation-ownership lock (#690): exactly one process may run the
write-side automation loops when two webapp instances share config/.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from src._no_window import NO_WINDOW
from src.automation_owner import AutomationOwnership

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_second_instance_does_not_acquire_while_first_holds_it(tmp_path):
    lock_path = tmp_path / ".automation-owner.lock"

    first = AutomationOwnership(lock_path=lock_path, port=8447)
    second = AutomationOwnership(lock_path=lock_path, port=8448)

    assert first.held is True
    assert second.held is False

    first.release()
    second.release()


def test_owner_info_names_the_holder_pid_and_port(tmp_path):
    lock_path = tmp_path / ".automation-owner.lock"

    first = AutomationOwnership(lock_path=lock_path, port=8447)
    second = AutomationOwnership(lock_path=lock_path, port=8448)

    assert "8447" in second.owner_info

    first.release()
    second.release()


def test_release_lets_a_new_instance_claim_ownership(tmp_path):
    lock_path = tmp_path / ".automation-owner.lock"

    first = AutomationOwnership(lock_path=lock_path, port=8447)
    assert first.held is True
    first.release()

    second = AutomationOwnership(lock_path=lock_path, port=8448)
    assert second.held is True
    second.release()


def test_lock_file_is_created_under_a_nonexistent_parent(tmp_path):
    lock_path = tmp_path / "nested" / ".automation-owner.lock"

    owner = AutomationOwnership(lock_path=lock_path, port=8447)

    assert owner.held is True
    assert lock_path.exists()
    owner.release()


def test_context_manager_releases_on_exit(tmp_path):
    lock_path = tmp_path / ".automation-owner.lock"

    with AutomationOwnership(lock_path=lock_path, port=8447) as owner:
        assert owner.held is True

    second = AutomationOwnership(lock_path=lock_path, port=8448)
    assert second.held is True
    second.release()


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="unclean-kill lock release is empirically verified on Windows, the deployed platform (#690)",
)
def test_lock_releases_when_holder_is_killed_uncleanly(tmp_path):
    """The OS releases the file lock when the holder is torn down — even by
    a hard kill with no chance to run cleanup code. Verified live against
    this exact primitive before writing this test (see PR description)."""
    lock_path = tmp_path / ".automation-owner.lock"
    code = (
        "from src.automation_owner import AutomationOwnership\n"
        "import time\n"
        f"o = AutomationOwnership(lock_path=r'{lock_path}', port=9999)\n"
        "print('locked' if o.held else 'not-locked', flush=True)\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        text=True,
        creationflags=NO_WINDOW,
    )
    try:
        line = proc.stdout.readline().strip()
        assert line == "locked"

        denied = AutomationOwnership(lock_path=lock_path, port=8447)
        assert denied.held is False
        denied.release()
    finally:
        proc.kill()  # simulate an unclean death — no chance to release explicitly
        proc.wait(timeout=5)

    deadline = time.time() + 5
    acquired = False
    while time.time() < deadline:
        owner = AutomationOwnership(lock_path=lock_path, port=8447)
        if owner.held:
            acquired = True
            owner.release()
            break
        time.sleep(0.1)
    assert acquired, "lock was not released after the holder was killed uncleanly"
