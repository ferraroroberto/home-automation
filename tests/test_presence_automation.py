"""Unit tests for app.webapp.presence_automation.tick()'s arm/disarm apply path.

Covers routing the presence-triggered arm/disarm through the shared
``confirm_alarm_action`` retry helper (issue #390) - this is the actual gap
that caused a real false alarm: RISCO's WebUI call rejected a presence-
triggered "arm", but the panel confirmed armed shortly after, and this path
had zero retry/backoff before alerting (issue #388 only covered the schedule
engine). The retry/backoff mechanics themselves are covered in
``tests/test_alarm_notify.py``; these tests only confirm tick() routes
through the shared helper instead of calling ``control_system`` directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

import app.webapp.alarm_notify as AN
import app.webapp.presence_automation as PA
from src.activity_log import log_path_for
from src.presence_engine import ArmBlockObservation, PresenceDecision
from src.risco_client import RiscoCommandError


class _FakeSecurity:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.ongoing_alarm = False
        self.memory_alarm = False
        self.ac_lost = False


class _FakeConfirmState:
    def __init__(self, mode: str) -> None:
        self.mode = mode


class _Config:
    auto_arm_enabled = True
    auto_disarm_enabled = True
    arm_block_notify_after_s = 900


_DECISION = PresenceDecision(
    kind="arm",
    action="arm",
    key="arm:2026-07-06T15:00:00+00:00",
    reason="everyone away past grace",
    transition_at=datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc),
)


def _wire_common(monkeypatch) -> None:
    async def fake_fetch_security_state() -> _FakeSecurity:
        return _FakeSecurity("disarmed")

    async def fake_check_security_transitions(**kw) -> None:
        pass

    monkeypatch.setattr(PA, "fetch_security_state", fake_fetch_security_state)
    monkeypatch.setattr(PA, "check_security_transitions", fake_check_security_transitions)
    monkeypatch.setattr(PA, "consider_security_read", lambda security: None)
    monkeypatch.setattr(PA, "consider_security_override", lambda security: None)
    monkeypatch.setattr(PA, "load_automation_config", lambda: _Config())
    monkeypatch.setattr(PA, "load_people", lambda: {"p1": object()})
    monkeypatch.setattr(PA, "evaluate_alarm_decision", lambda *a, **k: _DECISION)
    monkeypatch.setattr(PA, "load_kids_home_override", lambda: False)
    monkeypatch.setattr(PA, "send_push", lambda *a, **k: None)
    monkeypatch.setattr(PA, "append_trigger_log", lambda event: None)

    async def fake_sync_arm_block_diagnostic(security_mode: str) -> None:
        pass

    monkeypatch.setattr(PA, "_sync_arm_block_diagnostic", fake_sync_arm_block_diagnostic)
    # Same treatment as the arm-block diagnostic above: a cheap local-only step
    # that is not what these tests are about. `_Config` and the `load_people`
    # sentinel are deliberately minimal, so the real helper (#598) can't read
    # them. Its own logic is covered in tests/test_presence_engine.py; that it
    # is wired into tick() at all is asserted separately below.
    monkeypatch.setattr(PA, "_consume_satisfied_disarm", lambda security_mode: None)


def test_presence_tick_applies_arm_via_confirm_helper_and_records_ok(monkeypatch) -> None:
    recorded: list[dict] = []
    applied: list[tuple] = []
    _wire_common(monkeypatch)

    async def fake_confirm(action: str) -> _FakeConfirmState:
        assert action == "arm"
        return _FakeConfirmState("armed")

    async def fake_record_alarm_action(**kw) -> None:
        recorded.append(kw)

    monkeypatch.setattr(PA, "confirm_alarm_action", fake_confirm)
    monkeypatch.setattr(PA, "mark_decision_applied", lambda d, o: applied.append((d, o)))
    monkeypatch.setattr(PA, "set_kids_home_override", lambda v: None)
    monkeypatch.setattr(PA, "record_alarm_action", fake_record_alarm_action)

    asyncio.run(PA.tick())

    assert recorded == [
        {
            "source": PA.SOURCE_PRESENCE,
            "action": "arm",
            "outcome": PA.OUTCOME_OK,
            "detail": _DECISION.reason,
        }
    ]
    assert applied == [(_DECISION, "armed")]


def test_presence_tick_alerts_only_after_confirm_helper_exhausts_retries(monkeypatch) -> None:
    """Regression for today's real false alarm: presence-triggered arm/disarm
    previously had zero retry and alerted on the very first raised exception.
    Now the failure only reaches here after ``confirm_alarm_action`` has
    exhausted its own read-only backoff retries."""

    recorded: list[dict] = []
    _wire_common(monkeypatch)

    async def fake_confirm(action: str) -> _FakeConfirmState:
        raise RiscoCommandError("RISCO rejected 'arm': D:")

    async def fake_record_alarm_action(**kw) -> None:
        recorded.append(kw)

    monkeypatch.setattr(PA, "confirm_alarm_action", fake_confirm)
    monkeypatch.setattr(PA, "record_alarm_action", fake_record_alarm_action)

    asyncio.run(PA.tick())

    assert recorded == [
        {
            "source": PA.SOURCE_PRESENCE,
            "action": "arm",
            "outcome": PA.OUTCOME_ERROR,
            "error": "RISCO rejected 'arm': D:",
            "detail": _DECISION.reason,
            "dedupe_key": "presence:arm",
        }
    ]


_BLOCK_S = 0.4


async def _run_watching_the_loop(coro) -> tuple[float, float]:
    """Await ``coro`` while heart-beating; return (worst wakeup gap, elapsed).

    A coroutine that does its blocking I/O inline pins the loop for the whole
    duration, so the heartbeat's worst gap ≈ the block; one that threads the
    call off keeps waking on schedule.
    """

    gaps: list[float] = []

    async def heartbeat() -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.005)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    started = time.monotonic()
    try:
        await coro
    finally:
        elapsed = time.monotonic() - started
        beat.cancel()
        try:
            await beat
        except asyncio.CancelledError:
            pass
    return max(gaps, default=elapsed), elapsed


def test_presence_tick_push_never_blocks_the_event_loop(monkeypatch) -> None:
    """Regression for #635: ``send_push`` is blocking network I/O (one
    pywebpush POST per subscription) and this tick shares uvicorn's single
    event loop, so calling it inline stalled the whole webapp on every
    auto-arm/disarm."""

    _wire_common(monkeypatch)
    monkeypatch.setattr(PA, "send_push", lambda *a, **k: time.sleep(_BLOCK_S))

    async def fake_confirm(action: str) -> _FakeConfirmState:
        return _FakeConfirmState("armed")

    async def fake_record_alarm_action(**kw) -> None:
        pass

    monkeypatch.setattr(PA, "confirm_alarm_action", fake_confirm)
    monkeypatch.setattr(PA, "mark_decision_applied", lambda d, o: None)
    monkeypatch.setattr(PA, "set_kids_home_override", lambda v: None)
    monkeypatch.setattr(PA, "record_alarm_action", fake_record_alarm_action)

    stall, elapsed = asyncio.run(_run_watching_the_loop(PA.tick()))

    assert elapsed >= _BLOCK_S, "the blocking push did not actually run"
    assert stall < _BLOCK_S / 2, f"event loop stalled {stall:.3f}s inside tick()"


def test_evaluate_current_decision_considers_every_tracked_person(monkeypatch) -> None:
    """Regression for #490: a person hidden from the Presence UI list (a
    display-only declutter toggle in ``config/presence_hidden.json``) must
    still be evaluated by the arm/disarm decision, not silently dropped -
    hiding one of two tracked people previously made the automation blind to
    them while still acting on the other.
    """

    captured: dict = {}

    def fake_evaluate(people, **kwargs):
        captured["people"] = list(people)
        return None

    monkeypatch.setattr(PA, "load_automation_config", lambda: _Config())
    monkeypatch.setattr(PA, "load_people", lambda: {"ana": object(), "roberto": object()})
    monkeypatch.setattr(PA, "evaluate_alarm_decision", fake_evaluate)
    monkeypatch.setattr(PA, "load_kids_home_override", lambda: False)

    PA._evaluate_current_decision("disarmed")

    assert len(captured["people"]) == 2


def test_evaluate_current_decision_warns_loudly_when_no_people_tracked(
    monkeypatch, caplog
) -> None:
    """Regression for #490: an empty people list after loading must not be a
    silent no-op - it needs a visible log line so it's diagnosable."""

    monkeypatch.setattr(PA, "load_automation_config", lambda: _Config())
    monkeypatch.setattr(PA, "load_people", lambda: {})

    with caplog.at_level(logging.WARNING, logger=PA.logger.name):
        result = PA._evaluate_current_decision("disarmed")

    assert result is None
    assert any("no tracked people" in record.message for record in caplog.records)


def test_sync_arm_block_diagnostic_logs_once_per_episode(monkeypatch, caplog) -> None:
    """Regression for #531: a real-world case where one tracked person's
    presence stayed stuck 'home' overnight, silently blocking auto-arm with
    no trace anywhere. The diagnostic must persist the block and log it once
    when it appears - and not again on a repeat tick with unchanged state.
    """

    from src.presence_engine import PresenceBlock

    block = PresenceBlock(key="block:ana:t0", blocking_person_ids=("ana",), since=datetime(2026, 7, 25, 20, 21, tzinfo=timezone.utc))
    seen_keys: set[str] = set()

    def fake_set_arm_block(b: PresenceBlock, **kw) -> ArmBlockObservation:
        # Mirrors the real function's contract: `changed` only the first time a
        # given block key is observed. `notify` is the dwell gate (#599) and is
        # exercised separately - this test is about the log line.
        is_new = b.key not in seen_keys
        seen_keys.add(b.key)
        return ArmBlockObservation(changed=is_new, notify=False)

    async def fake_record_alarm_action(**kw) -> None:
        pass

    monkeypatch.setattr(PA, "load_automation_config", lambda: _Config())
    monkeypatch.setattr(PA, "load_people", lambda: {"ana": object(), "roberto": object()})
    monkeypatch.setattr(PA, "evaluate_arm_block", lambda people, **kw: block)
    monkeypatch.setattr(PA, "set_arm_block", fake_set_arm_block)
    monkeypatch.setattr(PA, "record_alarm_action", fake_record_alarm_action)

    with caplog.at_level(logging.INFO, logger=PA.logger.name):
        asyncio.run(PA._sync_arm_block_diagnostic("disarmed"))
        asyncio.run(PA._sync_arm_block_diagnostic("disarmed"))

    block_logs = [r.message for r in caplog.records if "Auto-arm blocked" in r.message]
    assert len(block_logs) == 1
    assert "ana" in block_logs[0]


def test_sync_arm_block_diagnostic_alerts_via_telegram_once_per_episode(monkeypatch, tmp_path) -> None:
    """Regression for #533: a UI note alone wasn't enough - the user wants a
    proactive Telegram ping through the SAME alert path already used for a
    presence-triggered arm that failed to confirm (``record_alarm_action``
    with ``SOURCE_PRESENCE``/``OUTCOME_ERROR``), fired once per new blocking
    episode - never on every ~10s poll while the same block persists.
    """

    from src.presence_engine import PresenceBlock

    monkeypatch.setattr("src.presence_engine.STATE_PATH", tmp_path / "presence_state.json")

    block = PresenceBlock(
        key="block:ana:2026-07-25T20:21:21+00:00",
        blocking_person_ids=("ana",),
        since=datetime(2026, 7, 25, 20, 21, 21, tzinfo=timezone.utc),
    )
    recorded: list[dict] = []

    async def fake_record_alarm_action(**kw) -> bool:
        recorded.append(kw)
        return True  # a confirmed send - the second call must see it as notified

    monkeypatch.setattr(PA, "load_automation_config", lambda: _Config())
    monkeypatch.setattr(PA, "load_people", lambda: {"ana": object(), "roberto": object()})
    monkeypatch.setattr(PA, "evaluate_arm_block", lambda people, **kw: block)
    # First call observes a new episode past its dwell; the second (unchanged
    # block, already notified) does not.
    monkeypatch.setattr(
        PA, "set_arm_block",
        lambda b, **kw: ArmBlockObservation(changed=not recorded, notify=not recorded),
    )
    monkeypatch.setattr(PA, "record_alarm_action", fake_record_alarm_action)

    asyncio.run(PA._sync_arm_block_diagnostic("disarmed"))
    asyncio.run(PA._sync_arm_block_diagnostic("disarmed"))

    assert len(recorded) == 1
    call = recorded[0]
    assert call["source"] == PA.SOURCE_PRESENCE
    assert call["action"] == "arm"
    assert call["outcome"] == PA.OUTCOME_BLOCKED
    assert "ana" in call["error"]
    assert call["dedupe_key"] == f"presence:blocked:{block.key}"


def test_sync_arm_block_diagnostic_wires_attempt_and_notified_markers(monkeypatch) -> None:
    """#601: the call site itself must stamp every attempt regardless of
    outcome, and only mark the episode notified once the send is confirmed -
    this exercises the wiring in ``_sync_arm_block_diagnostic``, not just the
    ``presence_engine`` state machine underneath it."""

    from src.presence_engine import PresenceBlock

    block = PresenceBlock(
        key="block:ana:2026-07-25T20:21:21+00:00",
        blocking_person_ids=("ana",),
        since=datetime(2026, 7, 25, 20, 21, 21, tzinfo=timezone.utc),
    )
    attempted: list[str] = []
    notified: list[str] = []

    monkeypatch.setattr(PA, "load_automation_config", lambda: _Config())
    monkeypatch.setattr(PA, "load_people", lambda: {"ana": object(), "roberto": object()})
    monkeypatch.setattr(PA, "evaluate_arm_block", lambda people, **kw: block)
    monkeypatch.setattr(
        PA, "set_arm_block",
        lambda b, **kw: ArmBlockObservation(changed=True, notify=True),
    )
    monkeypatch.setattr(PA, "mark_arm_block_attempted", lambda key, **kw: attempted.append(key))
    monkeypatch.setattr(PA, "mark_arm_block_notified", lambda key: notified.append(key))

    # A declined/failed send: the attempt is stamped, but never marked notified.
    async def fake_declined(**kw) -> bool:
        return False

    monkeypatch.setattr(PA, "record_alarm_action", fake_declined)
    asyncio.run(PA._sync_arm_block_diagnostic("disarmed"))
    assert attempted == [block.key]
    assert notified == []

    # A confirmed send: also marked notified.
    async def fake_sent(**kw) -> bool:
        return True

    monkeypatch.setattr(PA, "record_alarm_action", fake_sent)
    asyncio.run(PA._sync_arm_block_diagnostic("disarmed"))
    assert attempted == [block.key, block.key]
    assert notified == [block.key]


def test_sync_arm_block_diagnostic_does_not_alert_when_block_clears(monkeypatch) -> None:
    """The clearing transition (block -> None) must not itself page Telegram -
    only a newly-appearing block should."""

    recorded: list[dict] = []

    async def fake_record_alarm_action(**kw) -> None:
        recorded.append(kw)

    monkeypatch.setattr(PA, "load_automation_config", lambda: _Config())
    monkeypatch.setattr(PA, "load_people", lambda: {"ana": object(), "roberto": object()})
    monkeypatch.setattr(PA, "evaluate_arm_block", lambda people, **kw: None)
    monkeypatch.setattr(
        PA, "set_arm_block",
        lambda b, **kw: ArmBlockObservation(changed=True, notify=False),
    )  # clearing is itself "new"
    monkeypatch.setattr(PA, "record_alarm_action", fake_record_alarm_action)

    asyncio.run(PA._sync_arm_block_diagnostic("disarmed"))

    assert recorded == []


def test_sync_arm_block_diagnostic_end_to_end_writes_activity_log(monkeypatch, tmp_path) -> None:
    """Integration check for #533: runs the REAL ``record_alarm_action`` (not
    mocked, unlike the tests above) so the full chain - diagnostic -> Telegram
    call site -> activity log - is exercised together, not just each piece in
    isolation. Safe to run for real: ``build_alarm_notifier()`` has its own
    hard safety net that returns ``None`` whenever ``pytest`` is loaded (see
    ``src/notify_config.py``), so no real Telegram send is attempted here -
    only the ``logs/alarm.jsonl`` side effect is observed.
    """

    from src.presence_engine import PresenceBlock

    monkeypatch.setattr(AN, "_DEDUPE_PATH", tmp_path / "alarm_notify_dedupe.json")
    AN._last_error_notify.clear()
    monkeypatch.setattr("src.presence_engine.STATE_PATH", tmp_path / "presence_state.json")

    block = PresenceBlock(
        key="block:ana:2026-07-25T20:21:21+00:00",
        blocking_person_ids=("ana",),
        since=datetime(2026, 7, 25, 20, 21, 21, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(PA, "load_automation_config", lambda: _Config())
    monkeypatch.setattr(PA, "load_people", lambda: {"ana": object(), "roberto": object()})
    monkeypatch.setattr(PA, "evaluate_arm_block", lambda people, **kw: block)
    monkeypatch.setattr(
        PA, "set_arm_block",
        lambda b, **kw: ArmBlockObservation(changed=True, notify=True),
    )  # first observation, dwell already elapsed

    asyncio.run(PA._sync_arm_block_diagnostic("disarmed"))

    lines = log_path_for("alarm").read_text(encoding="utf-8").strip().splitlines()
    entries = [json.loads(line) for line in lines]
    assert entries == [
        {
            "source": PA.SOURCE_PRESENCE,
            "action": "arm",
            "event": "set",
            "outcome": PA.OUTCOME_BLOCKED,
            "error": "ana still reported home since 2026-07-25T20:21:21+00:00",
            "ts": entries[0]["ts"],
            "consumer": "alarm",
        }
    ]


def test_presence_tick_consumes_satisfied_disarm(monkeypatch) -> None:
    """tick() must retire an already-satisfied disarm arrival every poll (#598).

    `_wire_common` stubs the helper out for the apply-path tests, so this is the
    one place that proves the call site exists and gets the panel's real mode.
    """
    seen: list[str] = []
    _wire_common(monkeypatch)
    monkeypatch.setattr(PA, "_consume_satisfied_disarm", lambda mode: seen.append(mode))
    monkeypatch.setattr(PA, "evaluate_alarm_decision", lambda *a, **k: None)

    asyncio.run(PA.tick())

    assert seen == ["disarmed"]


def test_consume_satisfied_disarm_marks_and_is_idempotent(monkeypatch, tmp_path) -> None:
    """The helper itself, against the real engine rather than the stubs."""
    import src.presence_engine as P

    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    arrival = datetime(2026, 8, 1, 17, 51, 39, tzinfo=timezone.utc)
    person = P.PersonPresence(
        person_id="roberto", state="home", updated_at=arrival, state_since=arrival,
    )
    monkeypatch.setattr(PA, "load_people", lambda: {"roberto": person})
    monkeypatch.setattr(
        PA, "load_automation_config",
        lambda: P.PresenceAutomationConfig(auto_disarm_enabled=True, stale_after_s=10**9),
    )

    PA._consume_satisfied_disarm("disarmed")
    assert P._last_key("disarm") == f"disarm:{arrival.isoformat()}"

    # Second pass has nothing left to retire.
    marked: list[str] = []
    monkeypatch.setattr(PA, "mark_disarm_satisfied", lambda key: marked.append(key))
    PA._consume_satisfied_disarm("disarmed")
    assert marked == []
