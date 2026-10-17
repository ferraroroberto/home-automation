"""The e2e gate reports this run's wedged browser helpers as their own state (#971).

The browser sweep's ``wedged:out-of-scope`` count mixes every earlier run's
unkillable residue with this run's, so the conftest diffs the wedged set
against a session-start snapshot. Pinned here: a pre-existing wedge never
counts, a new one does, and an unreadable state reports ``UNKNOWN`` — never a
reassuring zero.
"""

from __future__ import annotations

from tests.e2e._browser_sweep import (
    STATE_RUNNING,
    STATE_WEDGED,
    VERDICT_PARENT_ALIVE,
    VERDICT_WEDGED_ELSEWHERE,
    HelperProcess,
    SweepEntry,
    SweepResult,
)
from tests.e2e.conftest import NEWLY_WEDGED_PREFIX, _newly_wedged_line


def _entry(pid: int, state: str, verdict: str) -> SweepEntry:
    process = HelperProcess(
        pid=pid, ppid=1, name="WebKitNetworkProcess.exe", state=state,
        parent_alive=state == STATE_RUNNING, cwd="C:/Temp",
    )
    return SweepEntry(process=process, verdict=verdict)


def _result(*entries: SweepEntry, supported: bool = True) -> SweepResult:
    return SweepResult(supported=supported, scope="E:/repo", entries=entries)


def test_only_wedges_absent_at_session_start_count() -> None:
    result = _result(
        _entry(10, STATE_WEDGED, VERDICT_WEDGED_ELSEWHERE),
        _entry(20, STATE_WEDGED, VERDICT_WEDGED_ELSEWHERE),
        _entry(30, STATE_WEDGED, VERDICT_WEDGED_ELSEWHERE),
        _entry(40, STATE_RUNNING, VERDICT_PARENT_ALIVE),
    )
    assert _newly_wedged_line(frozenset({10}), result) == f"{NEWLY_WEDGED_PREFIX} 2 (pids 20, 30)"


def test_no_new_wedge_reports_zero() -> None:
    result = _result(_entry(10, STATE_WEDGED, VERDICT_WEDGED_ELSEWHERE))
    assert _newly_wedged_line(frozenset({10}), result) == f"{NEWLY_WEDGED_PREFIX} 0"


def test_unreadable_state_is_unknown_not_zero() -> None:
    assert "UNKNOWN" in _newly_wedged_line(None, _result())
    assert "UNKNOWN" in _newly_wedged_line(frozenset(), _result(supported=False))
