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
from datetime import datetime, timedelta, timezone

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
    # Issue #653: tick() builds an iCloud corroboration map every round. Not
    # what these tests are about, so keep it a cheap no-op ({}).
    monkeypatch.setattr(PA, "_build_icloud_corroboration", lambda cache: {})
    # Issue #689: tick() also refreshes the known-people roster every round.
    # Stubbed so these tests can never write the real config/presence_roster.json
    # from their `{"p1": ...}` sentinel household; the roster's own behaviour is
    # covered in tests/test_presence_roster.py.
    monkeypatch.setattr(PA, "remember_known_people", lambda ids: tuple(ids))

    async def fake_sync_arm_block_diagnostic(
        security_mode: str, corroboration=None, known_person_ids=()
    ) -> None:
        pass

    monkeypatch.setattr(PA, "_sync_arm_block_diagnostic", fake_sync_arm_block_diagnostic)

    async def fake_sync_staleness_block_diagnostic(
        corroboration=None, known_person_ids=()
    ) -> None:
        pass

    monkeypatch.setattr(PA, "_sync_staleness_block_diagnostic", fake_sync_staleness_block_diagnostic)
    # Same treatment as the arm-block diagnostic above: a cheap local-only step
    # that is not what these tests are about. `_Config` and the `load_people`
    # sentinel are deliberately minimal, so the real helper (#598) can't read
    # them. Its own logic is covered in tests/test_presence_engine.py; that it
    # is wired into tick() at all is asserted separately below.
    monkeypatch.setattr(
        PA,
        "_consume_satisfied_disarm",
        lambda security_mode, corroboration=None, known_person_ids=(): None,
    )


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
    seen: list[tuple] = []
    _wire_common(monkeypatch)
    monkeypatch.setattr(
        PA,
        "_consume_satisfied_disarm",
        lambda mode, corroboration=None, known_person_ids=(): seen.append(
            (mode, known_person_ids)
        ),
    )
    monkeypatch.setattr(PA, "evaluate_alarm_decision", lambda *a, **k: None)

    asyncio.run(PA.tick())

    # ...and the roster it must refuse to act on when incomplete (#689).
    assert seen == [("disarmed", ("p1",))]


def test_presence_tick_wires_staleness_block_diagnostic(monkeypatch) -> None:
    """tick() must call the staleness-block diagnostic every round (#653) -
    `_wire_common` stubs it out for the apply-path tests, so this is the one
    place that proves the call site exists."""
    seen: list = []
    _wire_common(monkeypatch)

    async def fake(corroboration=None, known_person_ids=()) -> None:
        seen.append((corroboration, known_person_ids))

    monkeypatch.setattr(PA, "_sync_staleness_block_diagnostic", fake)
    monkeypatch.setattr(PA, "evaluate_alarm_decision", lambda *a, **k: None)

    asyncio.run(PA.tick())

    # It is also the diagnostic that reports a *missing* roster member (#689),
    # so it has to be handed the roster.
    assert seen == [({}, ("p1",))]


def _entity(entity_id: str, name: str, *, last_seen=None, at_home=None):
    from src.presence_client import PresenceEntity

    return PresenceEntity(
        entity_id=entity_id,
        name=name,
        model=None,
        device_class=None,
        latitude=None,
        longitude=None,
        horizontal_accuracy_m=None,
        last_seen=last_seen,
        battery_level_pct=None,
        battery_status=None,
        at_home=at_home,
    )


def _cache(entities):
    from app.webapp.presence_refresher import PresenceDiagnosticsCache

    return PresenceDiagnosticsCache(entities=entities)


def test_build_icloud_corroboration_matches_by_effective_display_name(monkeypatch) -> None:
    """Person id "roberto" matches an iCloud entity displayed as "Roberto" -
    already the convention this household's config follows (issue #653)."""

    monkeypatch.setattr(PA, "load_presence_display_names", lambda: {"dev-1": "Roberto"})
    monkeypatch.setattr(PA, "load_people", lambda: {"roberto": object(), "ana": object()})

    seen = datetime(2026, 8, 11, 5, 0, tzinfo=timezone.utc)
    cache = _cache([_entity("dev-1", "Roberto's iPhone", last_seen=seen, at_home=False)])

    corroboration = PA._build_icloud_corroboration(cache)

    assert set(corroboration) == {"roberto"}
    assert corroboration["roberto"].last_seen == seen
    assert corroboration["roberto"].at_home is False


def test_build_icloud_corroboration_falls_back_to_raw_device_name(monkeypatch) -> None:
    monkeypatch.setattr(PA, "load_presence_display_names", lambda: {})
    monkeypatch.setattr(PA, "load_people", lambda: {"ana": object()})

    seen = datetime(2026, 8, 11, 5, 0, tzinfo=timezone.utc)
    cache = _cache([_entity("dev-2", "Ana", last_seen=seen, at_home=True)])

    corroboration = PA._build_icloud_corroboration(cache)

    assert corroboration["ana"].at_home is True


def test_build_icloud_corroboration_skips_unmatched_and_ambiguous(monkeypatch) -> None:
    monkeypatch.setattr(PA, "load_presence_display_names", lambda: {})
    monkeypatch.setattr(PA, "load_people", lambda: {"ana": object(), "roberto": object()})

    seen = datetime(2026, 8, 11, 5, 0, tzinfo=timezone.utc)
    cache = _cache([
        # Two entities both named "Ana" - ambiguous, must not corroborate.
        _entity("dev-1", "Ana", last_seen=seen, at_home=True),
        _entity("dev-2", "Ana", last_seen=seen, at_home=False),
        # No entity matches "roberto" at all.
    ])

    corroboration = PA._build_icloud_corroboration(cache)

    assert corroboration == {}


def test_build_icloud_corroboration_skips_entity_with_no_last_seen(monkeypatch) -> None:
    monkeypatch.setattr(PA, "load_presence_display_names", lambda: {})
    monkeypatch.setattr(PA, "load_people", lambda: {"ana": object()})

    cache = _cache([_entity("dev-1", "Ana", last_seen=None, at_home=True)])

    assert PA._build_icloud_corroboration(cache) == {}


def test_load_guardian_home_config_parses_env(monkeypatch) -> None:
    monkeypatch.setenv("PRESENCE_GUARDIAN_HOME_NAMES", "Nonna, Nonno")
    monkeypatch.setenv("PRESENCE_GUARDIAN_HOME_STALE_AFTER_S", "3600")

    cfg = PA._load_guardian_home_config()

    assert cfg.names == ("Nonna", "Nonno")
    assert cfg.stale_after_s == 3600


def test_load_guardian_home_config_defaults_to_disabled(monkeypatch) -> None:
    monkeypatch.delenv("PRESENCE_GUARDIAN_HOME_NAMES", raising=False)
    monkeypatch.delenv("PRESENCE_GUARDIAN_HOME_STALE_AFTER_S", raising=False)

    cfg = PA._load_guardian_home_config()

    assert cfg.names == ()
    assert cfg.stale_after_s == PA._GUARDIAN_HOME_STALE_AFTER_S_DEFAULT


def test_resolve_guardian_home_matches_fresh_at_home_entity(monkeypatch) -> None:
    monkeypatch.setattr(PA, "load_presence_display_names", lambda: {"dev-nonna": "Nonna"})

    at = datetime(2026, 8, 25, 6, 9, tzinfo=timezone.utc)
    seen = at - timedelta(hours=1)
    cache = _cache([_entity("dev-nonna", "Nonna's iPhone", last_seen=seen, at_home=True)])
    cfg = PA.GuardianHomeConfig(names=("Nonna",), stale_after_s=86400)

    assert PA._resolve_guardian_home(cache, cfg, at=at) == "Nonna"


def test_resolve_guardian_home_rejects_stale_reads(monkeypatch) -> None:
    monkeypatch.setattr(PA, "load_presence_display_names", lambda: {"dev-nonna": "Nonna"})

    at = datetime(2026, 8, 25, 6, 9, tzinfo=timezone.utc)
    seen = at - timedelta(hours=25)  # older than the 24h default bound
    cache = _cache([_entity("dev-nonna", "Nonna's iPhone", last_seen=seen, at_home=True)])
    cfg = PA.GuardianHomeConfig(names=("Nonna",), stale_after_s=86400)

    assert PA._resolve_guardian_home(cache, cfg, at=at) is None


def test_resolve_guardian_home_ignores_not_at_home(monkeypatch) -> None:
    monkeypatch.setattr(PA, "load_presence_display_names", lambda: {"dev-nonna": "Nonna"})

    at = datetime(2026, 8, 25, 6, 9, tzinfo=timezone.utc)
    cache = _cache([_entity("dev-nonna", "Nonna's iPhone", last_seen=at, at_home=False)])
    cfg = PA.GuardianHomeConfig(names=("Nonna",), stale_after_s=86400)

    assert PA._resolve_guardian_home(cache, cfg, at=at) is None


def test_resolve_guardian_home_no_names_configured_is_a_noop(monkeypatch) -> None:
    at = datetime(2026, 8, 25, 6, 9, tzinfo=timezone.utc)
    cache = _cache([_entity("dev-nonna", "Nonna", last_seen=at, at_home=True)])
    cfg = PA.GuardianHomeConfig(names=(), stale_after_s=86400)

    assert PA._resolve_guardian_home(cache, cfg, at=at) is None


def test_presence_tick_holds_arm_and_notifies_on_guardian_home(monkeypatch) -> None:
    """Issue #693: tick() must route a guardian_hold decision to a direct
    Telegram notification, never through confirm_alarm_action/RISCO, and mark
    it applied so it doesn't re-notify on the very next poll."""

    _wire_common(monkeypatch)
    hold_decision = PresenceDecision(
        kind="guardian_hold",
        action="hold",
        key="guardian_hold:Nonna:2026-08-25T06:08:42+00:00",
        reason="everyone tracked away, but Nonna is home",
        transition_at=datetime(2026, 8, 25, 6, 8, 42, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(PA, "evaluate_alarm_decision", lambda *a, **k: hold_decision)
    monkeypatch.setattr(PA, "_resolve_guardian_home", lambda cache, cfg, at: "Nonna")

    applied: list[tuple] = []
    notified: list[str] = []
    monkeypatch.setattr(PA, "mark_decision_applied", lambda d, o: applied.append((d, o)))

    async def fake_notify(name: str) -> None:
        notified.append(name)

    monkeypatch.setattr(PA, "_notify_guardian_hold", fake_notify)

    def boom_confirm(action: str):
        raise AssertionError("guardian_hold must never call confirm_alarm_action")

    monkeypatch.setattr(PA, "confirm_alarm_action", boom_confirm)

    asyncio.run(PA.tick())

    assert applied == [(hold_decision, "held")]
    assert notified == ["Nonna"]


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


def test_a_successful_action_is_not_reported_as_failed_when_bookkeeping_read_fails(
    monkeypatch,
) -> None:
    """Issue #689: `mark_decision_applied` reads `presence_state.json`. Since an
    unreadable store now raises instead of degrading to empty, a hiccup there
    used to land inside the `confirm_alarm_action` try and fire a Telegram alert
    claiming an arm failed that the panel had actually accepted."""

    from src._schedule_store import StoreUnreadableError

    recorded: list[dict] = []
    _wire_common(monkeypatch)

    async def fake_confirm(action: str) -> _FakeConfirmState:
        return _FakeConfirmState("armed")

    def exploding_mark(decision, outcome):
        raise StoreUnreadableError("could not read presence_state.json")

    async def fake_record_alarm_action(**kw) -> None:
        recorded.append(kw)

    monkeypatch.setattr(PA, "confirm_alarm_action", fake_confirm)
    monkeypatch.setattr(PA, "mark_decision_applied", exploding_mark)
    monkeypatch.setattr(PA, "set_kids_home_override", lambda v: None)
    monkeypatch.setattr(PA, "record_alarm_action", fake_record_alarm_action)

    asyncio.run(PA.tick())

    assert [r["outcome"] for r in recorded] == [PA.OUTCOME_OK]
    assert "error" not in recorded[0]
