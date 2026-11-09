"""Browser process isolation: no browser process roots in this checkout, and no
Playwright driver is shared between browser projections.

Three facts about the processes the suite spawns, asserted in one test because
none of them is a user flow and each costs only a process-table read once a
browser is live.

Working directories. Windows refuses to delete a directory that is a live
process's current working directory. Everything Playwright spawns inherits one
(the Node driver from pytest, each browser from the driver, each WebKit helper
from the browser), so a suite run from `home-automation-wt-<N>` could leave
that whole tree rooted in the worktree, and any of them outliving the run pins
it. `_bounded_teardown`'s force-kill makes that outcome routine: a helper
killed with its browser can wedge *inside* termination, where nothing can ever
reap it. `conftest._neutral_driver_cwd` starts the driver from `%TEMP%`; the
test checks both ends of the chain, because they fail differently (the driver is
a `node.exe` no image-name sweep would recognise; the helpers are what wedge).
It asserts *working directories*, not leaks: a wedge is a load-sensitive
teardown race that cannot be reproduced on demand, while the rooting that turns
one into a pinned directory is deterministic.

Driver topology. `browser_name` is session-parametrized by pytest-playwright, so
the session-scoped `browser` fixture is rebuilt at every projection switch, but
the `playwright` fixture takes no `browser_name` dependency upstream, so one
Node driver could serve every projection. `_bounded_teardown` force-kills the
driver when a teardown wedges, so with one shared driver a hung Chromium
teardown would kill the driver WebKit is about to use and every later launch
fails with "Connection closed while reading from the driver". The test fixes the
blast radius to one projection; it does not assert that a kill happens at all.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.e2e._browser_sweep import (
    HELPER_IMAGE_NAMES,
    _iter_process_table,
    _read_process_cwd,
    path_is_within,
)
from tests.e2e.conftest import _REPO_ROOT, _driver_pid

# One ledger per pytest session, keyed by the session's own temp root, so a
# rerun never reads a stale file and two concurrent runs cannot collide.
_LEDGER_NAME = "driver_pids_584.json"

# The cwd probes read another process's PEB through kernel32, which exists only
# on Windows; the pinned-directory failure they guard is Windows-only too.
_WIN32 = sys.platform == "win32"


@pytest.fixture(scope="session")
def _driver_ledger(tmp_path_factory: pytest.TempPathFactory) -> Path:
    # `getbasetemp().parent` would be shared across sessions; the session's own
    # basetemp is unique per run and survives across projections within it.
    return tmp_path_factory.getbasetemp() / _LEDGER_NAME


def _no_browser_helper_roots_in_the_checkout() -> None:
    """No live WebKit/Playwright helper, from this run or any other."""
    offenders = []
    for pid, _ppid, name in _iter_process_table():
        if name not in HELPER_IMAGE_NAMES:
            continue
        cwd = _read_process_cwd(pid)
        if cwd and path_is_within(cwd, _REPO_ROOT):
            offenders.append(f"{name}#{pid} cwd={cwd}")

    assert not offenders, (
        "browser helper process(es) are rooted inside this checkout and will pin "
        "it against deletion once they wedge: " + ", ".join(offenders)
    )


def _driver_does_not_root_in_the_checkout(pid: int, browser_name: str) -> None:
    """The Node driver is the ancestor every browser inherits its cwd from."""
    cwd = _read_process_cwd(pid)
    assert cwd is not None, (
        f"could not read the {browser_name} driver's working directory (pid {pid}); "
        f"reporting unknown rather than a false pass"
    )
    assert not path_is_within(cwd, _REPO_ROOT), (
        f"the {browser_name} Playwright driver (pid {pid}) is running from {cwd}, "
        f"inside this checkout. Every browser and helper it spawns inherits that, "
        f"and a wedged one pins the directory permanently."
    )


def _driver_is_not_shared_across_projections(
    pid: int, browser_name: str, ledger: Path,
) -> None:
    seen: dict[str, int] = {}
    if ledger.exists():
        seen = json.loads(ledger.read_text(encoding="utf-8"))

    clashes = [b for b, p in seen.items() if p == pid and b != browser_name]
    assert not clashes, (
        f"projection {browser_name!r} is reusing driver pid {pid}, already used by "
        f"{clashes!r}. A shared driver means a force-killed teardown in one "
        f"projection takes down every projection after it."
    )

    seen[browser_name] = pid
    ledger.write_text(json.dumps(seen), encoding="utf-8")


def test_browser_process_isolation(
    playwright, page, browser_name: str, _driver_ledger: Path,
) -> None:
    """Browser helpers and the driver stay out of the checkout; each projection has its own driver."""
    page.goto("data:text/html,<h1>browser process isolation</h1>")
    if _WIN32:
        _no_browser_helper_roots_in_the_checkout()

    pid = _driver_pid(playwright)
    if pid is None:
        # Every check above has run; only the pid-keyed ones cannot.
        pytest.skip(
            "driver pid unreachable (Playwright internals changed) -- "
            "reporting unknown rather than a false pass"
        )
    if _WIN32:
        _driver_does_not_root_in_the_checkout(pid, browser_name)
    _driver_is_not_shared_across_projections(pid, browser_name, _driver_ledger)
