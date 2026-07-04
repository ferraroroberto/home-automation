from __future__ import annotations

import asyncio

import app.webapp.alarm_scene_automation as auto
from src.alarm_scene import SceneCapture, SceneVerdict
from src.alarm_scene_config import ScenePairing
from src.risco_client import SecurityEvent, SecurityState, SecurityZone


def _security(*, ongoing=False, memory=False, zones=()) -> SecurityState:
    zone_objs = [SecurityZone(id=zid, name=name) for zid, name in zones]
    return SecurityState(
        reachable=True, label="home", mode="armed",
        zones=zone_objs, ongoing_alarm=ongoing, memory_alarm=memory,
    )


def _alarm_event(time: str, zone_id: int) -> SecurityEvent:
    return SecurityEvent(time=time, type="triggered", zone_id=zone_id)


def test_zone_name_for_looks_up_by_id() -> None:
    security = _security(zones=[(3, "Garden"), (5, "Door")])
    assert auto._zone_name_for(3, security) == "Garden"
    assert auto._zone_name_for(5, security) == "Door"
    assert auto._zone_name_for(9, security) == "9"  # unknown id falls back to str(id)


def test_baseline_due_respects_interval() -> None:
    auto._state["last_baseline"] = None
    cfg = auto.AlarmSceneConfig(baseline_refresh_s=1800)
    assert auto._baseline_due(cfg, now=1000.0) is True       # first call
    assert auto._baseline_due(cfg, now=1500.0) is False      # within window
    assert auto._baseline_due(cfg, now=1000.0 + 1801) is True  # window elapsed


def test_event_scan_due_respects_interval() -> None:
    auto._state["last_event_scan"] = None
    cfg = auto.AlarmSceneConfig(event_scan_interval_s=20)
    assert auto._event_scan_due(cfg, now=1000.0) is True     # first call
    assert auto._event_scan_due(cfg, now=1010.0) is False    # within window
    assert auto._event_scan_due(cfg, now=1000.0 + 21) is True  # window elapsed


def test_onset_skips_and_logs_when_no_pairing(monkeypatch) -> None:
    logged: list[dict] = []
    captured = {"called": False}

    monkeypatch.setattr(auto, "pairings_for_zone", lambda zid, path=None: [])
    monkeypatch.setattr(auto, "append_activity", lambda consumer, rec: logged.append(rec))

    async def fail_capture(*a, **k):
        captured["called"] = True
        return []

    monkeypatch.setattr(auto, "capture_scene", fail_capture)

    cfg = auto.AlarmSceneConfig()
    asyncio.run(auto._run_onset([(3, "Garden")], cfg))

    assert captured["called"] is False  # no random-detector capture
    assert len(logged) == 1
    assert logged[0]["event"] == "trigger_no_pairing"
    assert logged[0]["zones"] == [{"id": 3, "name": "Garden"}]


def test_onset_captures_analyses_delivers_and_logs(monkeypatch) -> None:
    logged: list[dict] = []
    pushes: list[tuple] = []

    pairing = ScenePairing(id="garden-bbq", zone_id=3, camera_id="garden", preset_name="Barbecue")
    monkeypatch.setattr(auto, "pairings_for_zone", lambda zid, path=None: [pairing] if zid == 3 else [])
    monkeypatch.setattr(auto, "append_activity", lambda consumer, rec: logged.append(rec))
    monkeypatch.setattr(auto, "send_push", lambda title, body, url="/": pushes.append((title, body)))
    monkeypatch.setattr(auto, "build_alarm_notifier", lambda: None)

    async def fake_capture(pairings, zone_names, *, settle_s):
        assert [p.id for p in pairings] == ["garden-bbq"]
        return [SceneCapture(pairing=pairing, zone_name="Garden", ok=True, frame=b"F",
                             frame_path=None, baseline=b"B")]

    async def fake_analyze(captures, *, model, base_url):
        return SceneVerdict(verdict="real", summary="person at the gate",
                            raw_reply='{"verdict":"real"}', model=model)

    monkeypatch.setattr(auto, "capture_scene", fake_capture)
    monkeypatch.setattr(auto, "analyze_scene", fake_analyze)

    cfg = auto.AlarmSceneConfig(model="claude-haiku-4-5")
    asyncio.run(auto._run_onset([(3, "Garden")], cfg))

    assert len(pushes) == 1
    assert "person at the gate" in pushes[0][1]
    rec = logged[0]
    assert rec["event"] == "scene_capture"
    assert rec["verdict"] == "real"
    assert rec["raw_reply"] == '{"verdict":"real"}'
    assert rec["pairings"] == ["garden-bbq"]
    assert rec["captures"][0]["camera_id"] == "garden"


def test_consider_security_read_disabled_is_noop(monkeypatch) -> None:
    monkeypatch.setattr(auto, "load_alarm_scene_config",
                        lambda: auto.AlarmSceneConfig(enabled=False))
    # Must not raise even with no running event loop (no task is created).
    auto.consider_security_read(_security(ongoing=True, zones=[(3, "Garden")]))


def test_consider_security_read_skips_scan_on_unreadable_flags(monkeypatch) -> None:
    """Regression for #307: both flags ``None`` means the RISCO WebUI scrape came
    back unreadable this poll, not "no alarm" — must not schedule any work
    (an unreadable poll manufacturing a bogus scan is as wrong as it
    manufacturing a bogus onset).
    """

    monkeypatch.setattr(auto, "load_alarm_scene_config",
                        lambda: auto.AlarmSceneConfig(enabled=True))
    scheduled: list = []
    monkeypatch.setattr(
        auto.asyncio, "create_task",
        lambda coro, name=None: (scheduled.append(name), coro.close())[0],
    )
    security = SecurityState(
        reachable=True, label="home", mode="armed", zones=[],
        ongoing_alarm=None, memory_alarm=None,
    )
    auto.consider_security_read(security)
    assert scheduled == []


def test_consider_security_read_schedules_event_scan_when_intruding(monkeypatch) -> None:
    monkeypatch.setattr(auto, "load_alarm_scene_config",
                        lambda: auto.AlarmSceneConfig(enabled=True, event_scan_interval_s=20))
    auto._state["last_event_scan"] = None
    scheduled: list = []
    monkeypatch.setattr(
        auto.asyncio, "create_task",
        lambda coro, name=None: (scheduled.append(name), coro.close())[0],
    )
    security = _security(ongoing=True, zones=[(3, "Garden")])
    auto.consider_security_read(security)
    assert scheduled == ["alarm-scene-event-scan"]


def test_run_event_scan_fires_onset_for_each_new_alarm(monkeypatch) -> None:
    """The core of #325: two alarms on the same zone within one still-latched
    session (RISCO's ongoing_alarm/memory_alarm never toggling back to False
    between them) must each produce their own onset, not collapse into one.
    """

    events = [
        _alarm_event("2026-07-02T13:14:00Z", 12),
        SecurityEvent(time="2026-07-02T13:15:00Z", type="zone bypassed", zone_id=12),  # not an alarm
        _alarm_event("2026-07-02T13:05:00Z", 12),
        _alarm_event("2026-07-02T12:37:00Z", 12),  # at-or-before cursor - already processed
    ]

    async def fake_fetch_events(*a, **k):
        return events

    monkeypatch.setattr(auto, "fetch_events", fake_fetch_events)
    monkeypatch.setattr(auto, "load_last_alarm_event_time", lambda: "2026-07-02T12:37:00Z")

    saved: list[str] = []
    monkeypatch.setattr(auto, "save_last_alarm_event_time", lambda ts: saved.append(ts))

    fired: list[tuple] = []

    async def fake_run_onset(zones, config):
        fired.append(tuple(zones))

    monkeypatch.setattr(auto, "_run_onset", fake_run_onset)

    security = _security(ongoing=True, zones=[(12, "PUERTA JARDIN")])
    asyncio.run(auto._run_event_scan(security, auto.AlarmSceneConfig()))

    # Both alarms newer than the cursor fire, oldest first; the omit event and
    # the alarm at-or-before the cursor are excluded.
    assert fired == [
        ((12, "PUERTA JARDIN"),),
        ((12, "PUERTA JARDIN"),),
    ]
    assert saved == ["2026-07-02T13:05:00Z", "2026-07-02T13:14:00Z"]


def test_run_event_scan_skips_event_already_claimed_by_concurrent_scan(monkeypatch) -> None:
    """#339: a concurrent scan (e.g. an orphaned webapp process still alive
    after a tray restart) that read the same stale cursor and already claimed
    this event must not be redelivered — the live cursor re-check right
    before firing must see the concurrent scan's already-saved claim and skip.
    """

    events = [_alarm_event("2026-07-03T14:16:00Z", 12)]

    async def fake_fetch_events(*a, **k):
        return events

    monkeypatch.setattr(auto, "fetch_events", fake_fetch_events)

    calls = {"n": 0}

    def fake_load():
        calls["n"] += 1
        # First call is the initial (stale) cursor snapshot; every later call
        # is the per-event reclaim check, which observes a concurrent scan's
        # already-saved claim for this exact event.
        return "2026-07-03T14:15:00Z" if calls["n"] == 1 else "2026-07-03T14:16:00Z"

    monkeypatch.setattr(auto, "load_last_alarm_event_time", fake_load)
    saved: list[str] = []
    monkeypatch.setattr(auto, "save_last_alarm_event_time", lambda ts: saved.append(ts))

    fired: list = []

    async def fake_run_onset(zones, config):
        fired.append(zones)

    monkeypatch.setattr(auto, "_run_onset", fake_run_onset)

    security = _security(ongoing=True, zones=[(12, "PUERTA JARDIN")])
    asyncio.run(auto._run_event_scan(security, auto.AlarmSceneConfig()))

    assert fired == []
    assert saved == []


def test_run_event_scan_claims_cursor_before_delivering(monkeypatch) -> None:
    """The cursor must be saved before `_run_onset` runs, not after — that's
    what shrinks the cross-process race window from the whole capture+vision+
    deliver pipeline down to a couple of local file reads (#339).
    """

    events = [_alarm_event("2026-07-02T13:14:00Z", 12)]

    async def fake_fetch_events(*a, **k):
        return events

    monkeypatch.setattr(auto, "fetch_events", fake_fetch_events)
    monkeypatch.setattr(auto, "load_last_alarm_event_time", lambda: "2026-07-02T12:37:00Z")

    order: list[str] = []
    monkeypatch.setattr(auto, "save_last_alarm_event_time", lambda ts: order.append("save"))

    async def fake_run_onset(zones, config):
        order.append("onset")

    monkeypatch.setattr(auto, "_run_onset", fake_run_onset)

    security = _security(ongoing=True, zones=[(12, "PUERTA JARDIN")])
    asyncio.run(auto._run_event_scan(security, auto.AlarmSceneConfig()))

    assert order == ["save", "onset"]


def test_two_concurrent_scans_only_deliver_once(monkeypatch, tmp_path) -> None:
    """Integration-level repro of #339: two independent scans (simulating two
    separate webapp processes, each with its own in-memory ``_state``, hence
    the shared in-process overlap guard is reset between them below) racing on
    the same real on-disk cursor file must only deliver the alarm once.
    """

    import functools

    from src import alarm_scene_cursor as cursor_mod

    cursor_path = tmp_path / "cursor.json"
    monkeypatch.setattr(
        auto, "load_last_alarm_event_time",
        functools.partial(cursor_mod.load_last_alarm_event_time, path=cursor_path),
    )
    monkeypatch.setattr(
        auto, "save_last_alarm_event_time",
        functools.partial(cursor_mod.save_last_alarm_event_time, path=cursor_path),
    )
    auto.save_last_alarm_event_time("2026-07-03T14:15:00Z")  # seed cursor before the alarm

    events = [_alarm_event("2026-07-03T14:16:00Z", 12)]

    async def fake_fetch_events(*a, **k):
        return events

    monkeypatch.setattr(auto, "fetch_events", fake_fetch_events)

    delivered: list = []

    async def fake_run_onset(zones, config):
        await asyncio.sleep(0.05)  # simulate the capture+vision pipeline taking a moment
        delivered.append(zones)

    monkeypatch.setattr(auto, "_run_onset", fake_run_onset)

    security = _security(ongoing=True, zones=[(12, "PUERTA JARDIN")])
    cfg = auto.AlarmSceneConfig()

    async def scan():
        auto._state["event_scan_running"] = False  # a second process shares no in-memory state
        await auto._run_event_scan(security, cfg)

    async def run_both():
        await asyncio.gather(scan(), scan())

    asyncio.run(run_both())

    assert len(delivered) == 1


def test_run_event_scan_first_run_sets_baseline_without_firing(monkeypatch) -> None:
    """No prior cursor (fresh deploy) must not replay history as new onsets."""

    events = [_alarm_event("2026-06-28T12:52:01Z", 12), _alarm_event("2026-07-02T09:40:00Z", 12)]

    async def fake_fetch_events(*a, **k):
        return events

    monkeypatch.setattr(auto, "fetch_events", fake_fetch_events)
    monkeypatch.setattr(auto, "load_last_alarm_event_time", lambda: None)
    saved: list[str] = []
    monkeypatch.setattr(auto, "save_last_alarm_event_time", lambda ts: saved.append(ts))

    fired: list = []

    async def fake_run_onset(zones, config):
        fired.append(zones)

    monkeypatch.setattr(auto, "_run_onset", fake_run_onset)

    security = _security(ongoing=True, zones=[(12, "PUERTA JARDIN")])
    asyncio.run(auto._run_event_scan(security, auto.AlarmSceneConfig()))

    assert fired == []
    assert saved == ["2026-07-02T09:40:00Z"]


def test_run_event_scan_survives_restart_by_resuming_from_persisted_cursor(monkeypatch) -> None:
    """A webapp restart clears in-memory ``_state`` but must not clear the
    on-disk cursor — an alarm from just before/after the restart still fires,
    unlike the old in-memory-only edge detector it replaces.
    """

    auto._state["last_event_scan"] = None  # simulate a fresh process

    events = [_alarm_event("2026-07-02T09:40:00Z", 12)]

    async def fake_fetch_events(*a, **k):
        return events

    monkeypatch.setattr(auto, "fetch_events", fake_fetch_events)
    # Cursor from before the restart, persisted to disk, still older than the alarm.
    monkeypatch.setattr(auto, "load_last_alarm_event_time", lambda: "2026-07-02T03:01:01Z")
    saved: list[str] = []
    monkeypatch.setattr(auto, "save_last_alarm_event_time", lambda ts: saved.append(ts))

    fired: list = []

    async def fake_run_onset(zones, config):
        fired.append(zones)

    monkeypatch.setattr(auto, "_run_onset", fake_run_onset)

    security = _security(ongoing=True, zones=[(12, "PUERTA JARDIN")])
    asyncio.run(auto._run_event_scan(security, auto.AlarmSceneConfig()))

    assert fired == [[(12, "PUERTA JARDIN")]]
    assert saved == ["2026-07-02T09:40:00Z"]


def test_run_event_scan_guards_against_overlap(monkeypatch) -> None:
    calls = {"fetch": 0}

    async def fake_fetch_events(*a, **k):
        calls["fetch"] += 1
        return []

    monkeypatch.setattr(auto, "fetch_events", fake_fetch_events)
    auto._state["event_scan_running"] = True
    try:
        asyncio.run(auto._run_event_scan(_security(), auto.AlarmSceneConfig()))
    finally:
        auto._state["event_scan_running"] = False
    assert calls["fetch"] == 0


class _FakeNotifier:
    def __init__(self) -> None:
        self.text: str | None = None

    def send_text(self, text: str) -> None:
        self.text = text


def _deliver_caps(*, ok: bool, frame: bytes | None) -> list[SceneCapture]:
    return [SceneCapture(
        pairing=ScenePairing(id="p", zone_id=3, camera_id="garden"),
        zone_name="Garden", ok=ok, frame=frame,
    )]


def test_deliver_attaches_photo_when_frame_present(monkeypatch) -> None:
    notifier = _FakeNotifier()
    photo: list = []
    monkeypatch.setattr(auto, "send_push", lambda *a, **k: 0)
    monkeypatch.setattr(auto, "build_alarm_notifier", lambda: notifier)
    monkeypatch.setattr(auto, "_send_telegram_photo",
                        lambda img, cap: (photo.append((img, cap)), True)[1])

    verdict = SceneVerdict(verdict="real", summary="person at the gate")
    auto._deliver([(3, "Garden")], verdict, _deliver_caps(ok=True, frame=b"IMG"))

    assert photo and photo[0][0] == b"IMG"
    assert "person at the gate" in photo[0][1]
    assert notifier.text is None  # photo path used — no text fallback


def test_deliver_falls_back_to_text_without_frame(monkeypatch) -> None:
    notifier = _FakeNotifier()
    called = {"photo": False}
    monkeypatch.setattr(auto, "send_push", lambda *a, **k: 0)
    monkeypatch.setattr(auto, "build_alarm_notifier", lambda: notifier)
    monkeypatch.setattr(auto, "_send_telegram_photo",
                        lambda img, cap: called.__setitem__("photo", True) or True)

    auto._deliver([(3, "Garden")], SceneVerdict(verdict="false", summary="just a cat"),
                  _deliver_caps(ok=False, frame=None))

    assert called["photo"] is False
    assert "just a cat" in notifier.text


def test_deliver_photo_failure_falls_back_to_text(monkeypatch) -> None:
    notifier = _FakeNotifier()
    monkeypatch.setattr(auto, "send_push", lambda *a, **k: 0)
    monkeypatch.setattr(auto, "build_alarm_notifier", lambda: notifier)
    monkeypatch.setattr(auto, "_send_telegram_photo", lambda img, cap: False)

    auto._deliver([(3, "Garden")], SceneVerdict(verdict="real", summary="someone"),
                  _deliver_caps(ok=True, frame=b"IMG"))

    assert "someone" in notifier.text  # photo upload failed -> text fallback fired


def test_multipart_photo_encodes_fields_and_image() -> None:
    body, boundary = auto._multipart_photo(chat_id="123", caption="hi there", image=b"\xff\xd8JPEG")
    assert boundary.encode() in body
    assert b'name="chat_id"' in body and b"123" in body
    assert b'name="caption"' in body and b"hi there" in body
    assert b'name="photo"; filename="scene.jpg"' in body
    assert b"image/jpeg" in body
    assert b"\xff\xd8JPEG" in body
