"""Behavioral tests for the alarm-override auto-bypass automation (issue #341).

Drives ``_run_event_scan`` directly (bypassing the tick-throttle that
``consider_security_read`` applies) against a fake RISCO event log, proving:
a repeated "triggered" event for an overridden zone is counted across scans and
only bypasses the detector once the configured ``max_retries`` is reached, and
the next arming event restores it and resets the session — without ever
touching a real RISCO connection or the real on-disk config.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import app.webapp.security_override_automation as engine
import src.security_override as override_cfg
import src.security_override_session as session_cfg
from src.security_override import set_overrides


class _FakeEvent:
    def __init__(self, time, name=None, type=None, zone_id=None):
        self.time = time
        self.name = name
        self.type = type
        self.zone_id = zone_id


class _FakeState:
    zones = []


def _async_return(value):
    async def _f(*args, **kwargs):
        return value

    return _f


def test_override_bypasses_after_max_retries_and_restores_on_rearm(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(override_cfg, "OVERRIDES_PATH", tmp_path / "security_override.json")
    monkeypatch.setattr(session_cfg, "SESSION_PATH", tmp_path / "security_override_session.json")
    set_overrides([{"id": "jardin", "zone_id": 12, "max_retries": 2}], path=override_cfg.OVERRIDES_PATH)

    bypass_calls: list[tuple[int, bool]] = []

    async def fake_bypass(zone_id: int, bypass: bool):
        bypass_calls.append((zone_id, bypass))
        return _FakeState()

    monkeypatch.setattr(engine, "set_zone_bypass", fake_bypass)

    telemetry_calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        engine.telemetry, "record_event", lambda *a, **kw: telemetry_calls.append((a, kw))
    )

    config = engine.OverrideAutomationConfig(enabled=True, event_scan_interval_s=20)
    events: list[_FakeEvent] = [_FakeEvent("2026-07-04T10:00:00Z", name="Full Set - 'USER 1 MASTER', WEB", type="armed")]

    # First scan ever: no cursor yet -> establishes the baseline, no replay.
    monkeypatch.setattr(engine, "fetch_events", _async_return(events))
    asyncio.run(engine._run_event_scan(config))
    assert bypass_calls == []

    # First "Alarm" trigger for zone 12 -> count 1, still below max_retries=2.
    events = events + [
        _FakeEvent("2026-07-04T10:05:00Z", name="Alarm - 'PUERTA JARDIN'", type="triggered", zone_id=12)
    ]
    monkeypatch.setattr(engine, "fetch_events", _async_return(events))
    asyncio.run(engine._run_event_scan(config))
    assert bypass_calls == []

    # Second trigger -> count reaches max_retries=2 -> zone gets bypassed.
    events = events + [
        _FakeEvent("2026-07-04T10:06:00Z", name="Alarm - 'PUERTA JARDIN'", type="triggered", zone_id=12)
    ]
    monkeypatch.setattr(engine, "fetch_events", _async_return(events))
    asyncio.run(engine._run_event_scan(config))
    assert bypass_calls == [(12, True)]
    assert telemetry_calls[-1][0][:2] == ("security", "auto_bypass")
    assert telemetry_calls[-1][1]["entity_id"] == "12"
    assert telemetry_calls[-1][1]["payload"]["trigger_count"] == 2

    session = session_cfg.load_override_session()
    assert session.auto_bypassed_zones == [12]
    assert session.session_counts.get("12", 0) == 0

    # A third trigger before the next arm must not re-bypass (already bypassed;
    # RISCO itself won't re-trigger an omitted zone, but the count still tracks).
    events = events + [
        _FakeEvent("2026-07-04T10:07:00Z", name="Alarm - 'PUERTA JARDIN'", type="triggered", zone_id=12)
    ]
    monkeypatch.setattr(engine, "fetch_events", _async_return(events))
    asyncio.run(engine._run_event_scan(config))
    assert bypass_calls == [(12, True)]  # unchanged - only one bypass call so far

    # The next arm event restores the zone and resets the session.
    events = events + [
        _FakeEvent("2026-07-04T11:00:00Z", name="Full Set - 'USER 1 MASTER', WEB", type="armed")
    ]
    monkeypatch.setattr(engine, "fetch_events", _async_return(events))
    asyncio.run(engine._run_event_scan(config))
    assert bypass_calls[-1] == (12, False)
    assert telemetry_calls[-1][0][:2] == ("security", "auto_unbypass")

    session = session_cfg.load_override_session()
    assert session.auto_bypassed_zones == []
    assert session.session_counts == {}


def test_restore_after_rearm_keeps_failed_zone_bypassed_for_retry(monkeypatch, tmp_path) -> None:
    """Issue #745: a zone whose un-bypass call raises (e.g. a transient RISCO
    Cloud error at the arm event) must stay in ``auto_bypassed_zones`` so the
    next arm retries it — clearing it unconditionally would forget it forever
    and leave it bypassed through every later armed session.
    """

    monkeypatch.setattr(session_cfg, "SESSION_PATH", tmp_path / "security_override_session.json")
    session = session_cfg.load_override_session()
    session.auto_bypassed_zones = [12, 34]
    session.session_counts = {"12": 1, "34": 1}
    session_cfg.save_override_session(session)

    async def fake_bypass(zone_id: int, bypass: bool):
        if zone_id == 12:
            raise RuntimeError("transient RISCO Cloud error")
        return _FakeState()

    monkeypatch.setattr(engine, "set_zone_bypass", fake_bypass)
    monkeypatch.setattr(engine.telemetry, "record_event", lambda *a, **kw: None)

    asyncio.run(engine._restore_after_rearm())

    session = session_cfg.load_override_session()
    assert session.auto_bypassed_zones == [12]
    assert session.session_counts == {}


def test_unreadable_session_store_does_not_clear_auto_bypassed_zones(
    monkeypatch, tmp_path
) -> None:
    """Issue #692: a transient read failure on the session store — the file is
    right there, only the read glitches — must never get folded into a fresh
    default and saved back over the real session. That is exactly how a zone
    this automation owed a restore would be silently forgotten forever.
    """

    monkeypatch.setattr(override_cfg, "OVERRIDES_PATH", tmp_path / "security_override.json")
    monkeypatch.setattr(session_cfg, "SESSION_PATH", tmp_path / "security_override_session.json")
    set_overrides([{"id": "jardin", "zone_id": 12, "max_retries": 2}], path=override_cfg.OVERRIDES_PATH)

    real_session = {
        "last_event_time": "2026-07-04T10:00:00Z",
        "session_counts": {"12": 1},
        "auto_bypassed_zones": [12],
    }
    session_cfg.SESSION_PATH.write_text(json.dumps(real_session), encoding="utf-8")

    real_read_text = Path.read_text

    def _fail_reading_the_session_store(self, *args, **kwargs):
        if self == session_cfg.SESSION_PATH:
            raise PermissionError(13, "Permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _fail_reading_the_session_store)
    import src._schedule_store as store_mod

    monkeypatch.setattr(store_mod.time, "sleep", lambda _s: None)

    events = [
        _FakeEvent("2026-07-04T10:05:00Z", name="Alarm - 'PUERTA JARDIN'", type="triggered", zone_id=12)
    ]
    monkeypatch.setattr(engine, "fetch_events", _async_return(events))

    config = engine.OverrideAutomationConfig(enabled=True, event_scan_interval_s=20)
    asyncio.run(engine._run_event_scan(config))  # must honor the "never raises" contract

    monkeypatch.setattr(Path, "read_text", real_read_text)
    on_disk = json.loads(session_cfg.SESSION_PATH.read_text(encoding="utf-8"))
    assert on_disk == real_session


def test_scan_skips_fetch_when_nothing_configured_or_pending(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(override_cfg, "OVERRIDES_PATH", tmp_path / "security_override.json")
    monkeypatch.setattr(session_cfg, "SESSION_PATH", tmp_path / "security_override_session.json")

    called = {"n": 0}

    async def fake_fetch_events():
        called["n"] += 1
        return []

    monkeypatch.setattr(engine, "fetch_events", fake_fetch_events)

    config = engine.OverrideAutomationConfig(enabled=True, event_scan_interval_s=20)
    asyncio.run(engine._run_event_scan(config))

    assert called["n"] == 0  # no overrides configured, nothing pending restoration


# ---------------------------------------------------------------------------
# Event-log scan contract (#912): pinned against the pre-refactor engine so
# the shared scanner provably keeps it. Throttle, overlap guard, cursor
# baseline / re-check / claim and failure handling, each through the
# ``_run_event_scan`` / ``consider_security_read`` seams only.

_CURSOR = "2026-07-04T10:00:00Z"
_SCAN_CONFIG = engine.OverrideAutomationConfig(enabled=True, event_scan_interval_s=20)


def _alarm(time: str) -> _FakeEvent:
    return _FakeEvent(time, name="Alarm - 'PUERTA JARDIN'", type="triggered", zone_id=12)


def _store(monkeypatch, tmp_path, *, cursor=_CURSOR) -> None:
    """Real on-disk override + session stores under ``tmp_path``, one override configured."""

    monkeypatch.setattr(override_cfg, "OVERRIDES_PATH", tmp_path / "security_override.json")
    monkeypatch.setattr(session_cfg, "SESSION_PATH", tmp_path / "security_override_session.json")
    set_overrides([{"id": "jardin", "zone_id": 12, "max_retries": 3}], path=override_cfg.OVERRIDES_PATH)
    if cursor is not None:
        session_cfg.save_override_session(session_cfg.OverrideSession(last_event_time=cursor))


def _cursor():
    return session_cfg.load_override_session().last_event_time


def _record_handled(monkeypatch) -> list:
    """Replace the per-event action; record (event time, cursor seen while handling)."""

    handled: list = []

    async def fake_handle(event, overrides_by_zone):
        handled.append((event.time, _cursor()))

    monkeypatch.setattr(engine, "_handle_event", fake_handle)
    return handled


def test_scan_handles_each_new_event_once_oldest_first_claiming_after(monkeypatch, tmp_path) -> None:
    """Every event type counts (arm events too); an event at or before the
    cursor, or with no time, is skipped. Each new event is handled before its
    claim is saved, so the handler still sees the previous cursor."""

    _store(monkeypatch, tmp_path)
    handled = _record_handled(monkeypatch)
    events = [
        _alarm("2026-07-04T10:07:00Z"),
        _FakeEvent("2026-07-04T10:05:00Z", name="Full Set - 'USER 1 MASTER', WEB", type="armed"),
        _alarm(_CURSOR),  # at the cursor: already processed
        _alarm("2026-07-04T09:00:00Z"),  # before the cursor
        _FakeEvent(None, type="triggered", zone_id=12),  # no time: ignored
    ]
    monkeypatch.setattr(engine, "fetch_events", _async_return(events))

    asyncio.run(engine._run_event_scan(_SCAN_CONFIG))
    assert handled == [
        ("2026-07-04T10:05:00Z", _CURSOR),
        ("2026-07-04T10:07:00Z", "2026-07-04T10:05:00Z"),
    ]
    assert _cursor() == "2026-07-04T10:07:00Z"

    asyncio.run(engine._run_event_scan(_SCAN_CONFIG))  # same log again: nothing new
    assert len(handled) == 2


def test_scan_rechecks_cursor_before_each_event(monkeypatch, tmp_path) -> None:
    """#339 shape: a concurrent scan claiming a later event between this scan's
    claims must make this scan skip it, not act on it a second time."""

    _store(monkeypatch, tmp_path)
    handled = _record_handled(monkeypatch)
    first, second = "2026-07-04T10:05:00Z", "2026-07-04T10:06:00Z"
    monkeypatch.setattr(engine, "fetch_events", _async_return([_alarm(first), _alarm(second)]))

    real_save = session_cfg.save_override_session

    def save_then_race(session, path=None):
        real_save(session, path)
        if session.last_event_time == first:  # another process claims `second` right after
            real_save(session_cfg.OverrideSession(last_event_time=second), path)

    monkeypatch.setattr(engine, "save_override_session", save_then_race)

    asyncio.run(engine._run_event_scan(_SCAN_CONFIG))
    assert [t for t, _ in handled] == [first]
    assert _cursor() == second


def test_scan_first_run_sets_baseline_without_handling(monkeypatch, tmp_path) -> None:
    _store(monkeypatch, tmp_path, cursor=None)
    handled = _record_handled(monkeypatch)
    events = [
        _FakeEvent("2026-07-04T11:00:00Z", name="Full Set - 'USER 1 MASTER', WEB", type="armed"),
        _alarm("2026-07-04T10:05:00Z"),
    ]
    monkeypatch.setattr(engine, "fetch_events", _async_return(events))

    asyncio.run(engine._run_event_scan(_SCAN_CONFIG))
    assert handled == []
    assert _cursor() == "2026-07-04T11:00:00Z"


def test_scan_handler_failure_leaves_cursor_and_retries_next_scan(monkeypatch, tmp_path, caplog) -> None:
    """A handler that raises stops the scan before that event's claim: the
    cursor stays put, later events wait, nothing escapes the detached task,
    and the overlap guard is released so the next scan retries the event."""

    _store(monkeypatch, tmp_path)
    first, second = "2026-07-04T10:05:00Z", "2026-07-04T10:06:00Z"
    monkeypatch.setattr(engine, "fetch_events", _async_return([_alarm(first), _alarm(second)]))

    attempts: list = []

    async def failing_handle(event, overrides_by_zone):
        attempts.append(event.time)
        raise RuntimeError("RISCO Cloud hiccup")

    monkeypatch.setattr(engine, "_handle_event", failing_handle)
    with caplog.at_level("WARNING", logger=engine.logger.name):
        asyncio.run(engine._run_event_scan(_SCAN_CONFIG))
    assert attempts == [first]
    assert _cursor() == _CURSOR
    assert "Security override event scan failed: RISCO Cloud hiccup" in caplog.text

    handled = _record_handled(monkeypatch)
    asyncio.run(engine._run_event_scan(_SCAN_CONFIG))
    assert [t for t, _ in handled] == [first, second]
    assert _cursor() == second


def test_scan_fetch_failure_leaves_cursor_and_releases_guard(monkeypatch, tmp_path, caplog) -> None:
    _store(monkeypatch, tmp_path)
    handled = _record_handled(monkeypatch)

    async def failing_fetch():
        raise RuntimeError("cloud down")

    monkeypatch.setattr(engine, "fetch_events", failing_fetch)
    with caplog.at_level("WARNING", logger=engine.logger.name):
        asyncio.run(engine._run_event_scan(_SCAN_CONFIG))
    assert handled == [] and _cursor() == _CURSOR
    assert "Alarm-override event-log read failed: cloud down" in caplog.text

    monkeypatch.setattr(engine, "fetch_events", _async_return([_alarm("2026-07-04T10:05:00Z")]))
    asyncio.run(engine._run_event_scan(_SCAN_CONFIG))
    assert [t for t, _ in handled] == ["2026-07-04T10:05:00Z"]


def test_scan_guards_against_overlap(monkeypatch, tmp_path) -> None:
    """A scan still in flight (a slow RISCO read) makes a second one a no-op;
    once it finishes the guard is released for the next."""

    _store(monkeypatch, tmp_path)
    _record_handled(monkeypatch)
    fetches = {"n": 0}

    async def run() -> None:
        release = asyncio.Event()

        async def slow_fetch():
            fetches["n"] += 1
            await release.wait()
            return []

        monkeypatch.setattr(engine, "fetch_events", slow_fetch)
        first = asyncio.create_task(engine._run_event_scan(_SCAN_CONFIG))
        await asyncio.sleep(0)
        await engine._run_event_scan(_SCAN_CONFIG)  # overlaps the first: skipped
        assert fetches["n"] == 1
        release.set()
        await first
        await engine._run_event_scan(_SCAN_CONFIG)
        assert fetches["n"] == 2

    asyncio.run(run())


def test_consider_security_read_throttles_scans(monkeypatch) -> None:
    """A scan is spawned at most once per ``event_scan_interval_s`` of
    monotonic time; exactly one interval later is due again."""

    monkeypatch.setattr(engine, "load_override_automation_config", lambda: _SCAN_CONFIG)
    scans: list = []

    async def fake_scan(config):
        scans.append(config)

    monkeypatch.setattr(engine, "_run_event_scan", fake_scan)
    base = 1e12  # far past any real monotonic reading an earlier test left behind
    due: list = []

    async def run() -> None:
        for offset in (0, 10, 19.9, 20, 25, 39.9, 40):
            before = len(scans)
            with monkeypatch.context() as m:
                m.setattr(time, "monotonic", lambda: base + offset)
                engine.consider_security_read(object())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            if len(scans) > before:
                due.append(offset)

    asyncio.run(run())
    assert due == [0, 20, 40]
