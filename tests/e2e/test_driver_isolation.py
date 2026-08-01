"""The Playwright driver must not be shared across browser projections (#584).

`browser_name` is session-parametrized by pytest-playwright, so the
session-scoped `browser` fixture is torn down and rebuilt at every projection
switch. The `playwright` fixture, however, took no `browser_name` dependency
upstream, so a single Node driver served *every* projection.

That matters because `_bounded_teardown` (#440) force-kills the driver process
when a teardown wedges -- the only way to unblock a greenlet-bound
`browser.close()` from another thread. With one shared driver, a hung Chromium
teardown killed the driver WebKit was about to use, and every subsequent launch
failed with "Connection closed while reading from the driver": one error per
remaining test, no summary line, pytest spinning at ~90% CPU (#584).

This test pins the topology rather than the symptom. The hang itself is
nondeterministic and load-sensitive -- it took 80+ minutes to surface and could
not be relied on to fail on demand -- but the shared-driver arrangement that
makes it fatal is deterministic and cheap to assert.

Deliberately not asserted here: that a kill happens at all. #440's guarantee is
that a wedged teardown is bounded, and that stays true either way; this test
only fixes the blast radius to one projection.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# One ledger per pytest session, keyed by the session's own temp root, so a
# rerun never reads a stale file and two concurrent runs cannot collide.
_LEDGER_NAME = "driver_pids_584.json"


@pytest.fixture(scope="session")
def _driver_ledger(tmp_path_factory: pytest.TempPathFactory) -> Path:
    # `getbasetemp().parent` would be shared across sessions; the session's own
    # basetemp is unique per run and survives across projections within it.
    return tmp_path_factory.getbasetemp() / _LEDGER_NAME


def test_driver_is_not_shared_across_projections(
    playwright, browser_name: str, _driver_ledger: Path
) -> None:
    """Each browser projection must get its own driver process."""
    from tests.e2e.conftest import _driver_pid

    pid = _driver_pid(playwright)
    if pid is None:
        pytest.skip(
            "driver pid unreachable (Playwright internals changed) -- "
            "reporting unknown rather than a false pass"
        )

    seen: dict[str, int] = {}
    if _driver_ledger.exists():
        seen = json.loads(_driver_ledger.read_text(encoding="utf-8"))

    clashes = [b for b, p in seen.items() if p == pid and b != browser_name]
    assert not clashes, (
        f"projection {browser_name!r} is reusing driver pid {pid}, already used by "
        f"{clashes!r}. A shared driver means a force-killed teardown in one "
        f"projection takes down every projection after it (#584)."
    )

    seen[browser_name] = pid
    _driver_ledger.write_text(json.dumps(seen), encoding="utf-8")
