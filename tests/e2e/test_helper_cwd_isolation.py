"""No browser process may root itself inside this checkout (#681).

Windows refuses to delete a directory that is a live process's current working
directory. Everything Playwright spawns inherits one — the Node driver from
pytest, each browser from the driver, each WebKit helper from the browser — so
a suite run from `home-automation-wt-<N>` used to leave that whole tree rooted
in the worktree. Any of them outliving the run pins the worktree, and
`_bounded_teardown`'s force-kill (#440) makes exactly that outcome routine: a
helper killed with its browser can wedge *inside* termination, where nothing
can ever reap it. Six empty, undeletable worktrees accumulated on the fleet
host that way between 2026-08-19 and 2026-08-22.

`conftest._neutral_driver_cwd` fixes the cause by starting the driver from
`%TEMP%`. This pins that arrangement, and it is deliberately an assertion about
*working directories*, not about leaks: reproducing a wedge on demand is not
possible (it is a load-sensitive teardown race), but the rooting that turns one
into a pinned directory is deterministic and cheap to check while a browser is
live.

Both facts are asserted, because the two ends of the chain fail differently:
the driver is a `node.exe` that no image-name sweep would ever recognise, and
the helpers are the processes that actually wedge.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.e2e._browser_sweep import (
    HELPER_IMAGE_NAMES,
    _iter_process_table,
    _read_process_cwd,
    path_is_within,
)
from tests.e2e.conftest import _REPO_ROOT, _driver_pid


def test_driver_does_not_root_in_the_checkout(playwright, browser_name: str) -> None:
    """The Node driver — the ancestor every browser inherits its cwd from."""
    pid = _driver_pid(playwright)
    if pid is None:
        pytest.skip(
            "driver pid unreachable (Playwright internals changed) -- "
            "reporting unknown rather than a false pass"
        )

    cwd = _read_process_cwd(pid)
    assert cwd is not None, (
        f"could not read the {browser_name} driver's working directory (pid {pid}); "
        f"reporting unknown rather than a false pass"
    )
    assert not path_is_within(cwd, _REPO_ROOT), (
        f"the {browser_name} Playwright driver (pid {pid}) is running from {cwd}, "
        f"inside this checkout. Every browser and helper it spawns inherits that, "
        f"and a wedged one pins the directory permanently (#681)."
    )


def test_no_browser_helper_roots_in_the_checkout(page, browser_name: str) -> None:
    """No live WebKit/Playwright helper, from this run or any other."""
    page.goto("data:text/html,<h1>#681 helper cwd isolation</h1>")

    offenders = []
    for pid, _ppid, name in _iter_process_table():
        if name not in HELPER_IMAGE_NAMES:
            continue
        cwd = _read_process_cwd(pid)
        if cwd and path_is_within(cwd, _REPO_ROOT):
            offenders.append(f"{name}#{pid} cwd={cwd}")

    assert not offenders, (
        "browser helper process(es) are rooted inside this checkout and will pin "
        "it against deletion once they wedge (#681): " + ", ".join(offenders)
    )
