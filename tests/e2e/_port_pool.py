"""Name a drained loopback port pool instead of reporting it as a page failure.

Every closed TCP connection to a local server leaves an (ephemeral port, server
port) pair in TIME_WAIT for minutes, and Windows answers a ``connect()`` that
lands on such a pair with ``net::ERR_ADDRESS_IN_USE`` rather than trying another
port. So a run that makes many connections, or a second lane gating on the same
host, fails a page load that has nothing wrong with it. Playwright's own
Node-side client fails the same way as ``connect ENOBUFS``.

Importable from a conftest: ``pytest_runtest_makereport`` relabels a failed
report whose text carries either marker, and ``write_summary`` prints the
affected tests in a section of their own, so a gate reader sees a host-capacity
problem to wait out and re-run, not a defect to debug.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest

PORT_POOL_MARKERS = ("ERR_ADDRESS_IN_USE", "connect ENOBUFS")
PORT_POOL_HEADLINE = (
    "PORT POOL EXHAUSTED — this is the host running out of loopback ephemeral ports "
    f"({' / '.join(PORT_POOL_MARKERS)}), not a page failure. Another gate or a recent "
    "run is holding ports in TIME_WAIT; wait a few minutes and re-run rather than "
    "debugging the page."
)

_FAILED: list[str] = []


def is_port_pool_failure(text: str) -> bool:
    return any(marker in text for marker in PORT_POOL_MARKERS)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Lead a failed report that names a drained port pool with that cause."""
    report = yield
    if report.failed and is_port_pool_failure(report.longreprtext):
        report.longrepr = f"{PORT_POOL_HEADLINE}\n\n{report.longreprtext}"
        _FAILED.append(report.nodeid)
    return report


def write_summary(terminalreporter: Any) -> None:
    """Print the section that lists every test this session failed on the port pool."""
    if not _FAILED:
        return
    terminalreporter.write_sep("=", "PORT POOL EXHAUSTED", red=True, bold=True)
    terminalreporter.write_line(PORT_POOL_HEADLINE)
    for nodeid in dict.fromkeys(_FAILED):
        terminalreporter.write_line(f"  {nodeid}")
