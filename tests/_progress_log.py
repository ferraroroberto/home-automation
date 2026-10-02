"""Gate progress log: per-test timings on disk (home-automation#778).

When the pre-ship gate (scripts/verify-before-ship.ps1) sets
``HA_VERIFY_PROGRESS_LOG``, every test's start and finish is appended — and
flushed — to that file as it happens. Each ``DONE`` line carries the test's
setup+call+teardown total, so fixture cost (an app boot, a page load) is
visible, and a failing setup or call leaves a bounded excerpt of its traceback
under its ``FAILED`` line. A gate that wedges or is killed leaves the active
node id (last ``START`` without a ``DONE``) on disk instead of a dead console.

The format is the one fleet-config's ``/e2e-audit`` parses
(``skills/_lib/e2e_value.py``: ``.fleet.toml`` ``[e2e] progress_log``),
adapted from app-launcher's ``tests/_progress_log.py`` so the two repos'
logs read the same way. Excerpt lines carry a ``    | `` prefix instead of
the timestamp, so ``grep -v '^    |'`` still yields one line per event.

Inert for normal pytest runs (env var absent → every hook no-ops). Loaded by
the gate with ``-p tests._progress_log``.
"""

from __future__ import annotations

import os
import time
from typing import Iterable, List

import pytest

PROGRESS_ENV = "HA_VERIFY_PROGRESS_LOG"
# Gate start (Unix ms), so the elapsed column runs across the gate's pytest
# processes instead of restarting at each one.
T0_ENV = "HA_VERIFY_PROGRESS_T0"
EXCERPT_PREFIX = "    | "
# Bounds, so a run with many reds cannot balloon the log: the tail of each
# traceback (where the crash line and its `E` lines sit), each line capped,
# and excerpts only for the first failures of a run — every later red still
# gets its FAILED line.
EXCERPT_MAX_LINES = 40
EXCERPT_MAX_LINE_CHARS = 240
EXCERPT_MAX_FAILURES = 30
SLOWEST_N = 15


def _gate_t0() -> float:
    try:
        return int(os.environ[T0_ENV]) / 1000.0
    except (KeyError, ValueError):
        return time.time()


_t0 = _gate_t0()
_node_totals: dict = {}
_durations: list = []
_excerpts_written = 0


def _write(line: str, detail: Iterable[str] = ()) -> None:
    path = os.environ.get(PROGRESS_ENV, "").strip()
    if not path:
        return
    stamp = time.strftime("%H:%M:%S")
    elapsed = time.time() - _t0
    block = f"[{stamp} +{elapsed:7.1f}s] {line}\n"
    block += "".join(f"{EXCERPT_PREFIX}{d}\n" for d in detail)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(block)
    except OSError:  # never let diagnostics fail the run
        pass


def failure_excerpt(report: pytest.TestReport) -> List[str]:
    """The last ``EXCERPT_MAX_LINES`` non-blank lines of a report's failure text."""
    lines = [ln.rstrip() for ln in report.longreprtext.splitlines() if ln.strip()]
    omitted = len(lines) - EXCERPT_MAX_LINES
    if omitted > 0:
        lines = [f"... {omitted} earlier line(s) omitted"] + lines[-EXCERPT_MAX_LINES:]
    return [
        ln if len(ln) <= EXCERPT_MAX_LINE_CHARS else ln[:EXCERPT_MAX_LINE_CHARS] + " ..."
        for ln in lines
    ]


def pytest_runtest_logstart(nodeid, location) -> None:
    _write(f"START {nodeid}")


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    global _excerpts_written
    total = _node_totals.get(report.nodeid, 0.0) + report.duration
    _node_totals[report.nodeid] = total
    if report.when in ("setup", "call") and report.outcome != "passed":
        # skipped / failed / errored — name the phase so a fixture skip is
        # distinguishable from an assertion failure.
        detail: List[str] = []
        if report.failed and os.environ.get(PROGRESS_ENV, "").strip():
            _excerpts_written += 1
            if _excerpts_written <= EXCERPT_MAX_FAILURES:
                detail = failure_excerpt(report)
            elif _excerpts_written == EXCERPT_MAX_FAILURES + 1:
                detail = [
                    f"(traceback excerpts stop after {EXCERPT_MAX_FAILURES} failures;"
                    " later ones are in the console output only)"
                ]
        _write(f"{report.outcome.upper()} ({report.when}) {report.nodeid}", detail)
    if report.when == "teardown":
        _node_totals.pop(report.nodeid, None)
        _durations.append((total, report.nodeid))
        _write(f"DONE  {report.nodeid} ({total:.1f}s)")


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Append the session's exit status and slowest tests to the log."""
    if not os.environ.get(PROGRESS_ENV, "").strip():
        return
    _write(f"pytest session finished (exit status {int(exitstatus)})")
    slowest = sorted(_durations, key=lambda t: t[0], reverse=True)[:SLOWEST_N]
    if slowest:
        _write("slowest tests (setup+call+teardown):")
        for duration, nodeid in slowest:
            _write(f"  {duration:7.1f}s  {nodeid}")
