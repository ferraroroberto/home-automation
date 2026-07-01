from __future__ import annotations

import asyncio

import app.webapp.alarm_scene_automation as auto
from src.alarm_scene import SceneCapture, SceneVerdict
from src.alarm_scene_config import ScenePairing
from src.risco_client import SecurityState, SecurityZone


def _security(*, ongoing=False, triggered_ids=()) -> SecurityState:
    zones = [
        SecurityZone(id=3, name="Garden", triggered=3 in triggered_ids),
        SecurityZone(id=5, name="Door", triggered=5 in triggered_ids),
    ]
    return SecurityState(
        reachable=True, label="home", mode="armed",
        zones=zones, ongoing_alarm=ongoing,
    )


def test_intrusion_rising_edge_only_fires_on_false_to_true() -> None:
    state: dict = {"intrusion": None}
    # First observation just sets the baseline — no fire even if already active.
    assert auto.intrusion_rising_edge(True, state) is False
    state["intrusion"] = False
    assert auto.intrusion_rising_edge(True, state) is True   # genuine onset
    assert auto.intrusion_rising_edge(True, state) is False  # still active
    assert auto.intrusion_rising_edge(False, state) is False  # cleared


def test_triggered_zones_reads_per_zone_flag() -> None:
    assert auto.triggered_zones(_security(triggered_ids=(3,))) == [(3, "Garden")]
    assert auto.triggered_zones(_security(triggered_ids=(3, 5))) == [(3, "Garden"), (5, "Door")]
    assert auto.triggered_zones(_security()) == []


def test_baseline_due_respects_interval() -> None:
    auto._state["last_baseline"] = None
    cfg = auto.AlarmSceneConfig(baseline_refresh_s=1800)
    assert auto._baseline_due(cfg, now=1000.0) is True       # first call
    assert auto._baseline_due(cfg, now=1500.0) is False      # within window
    assert auto._baseline_due(cfg, now=1000.0 + 1801) is True  # window elapsed


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
    asyncio.run(auto._run_onset(_security(ongoing=True, triggered_ids=(3,)), cfg))

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
    asyncio.run(auto._run_onset(_security(ongoing=True, triggered_ids=(3,)), cfg))

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
    auto.consider_security_read(_security(ongoing=True, triggered_ids=(3,)))


def test_consider_security_read_skips_edge_detection_on_unreadable_flags(monkeypatch) -> None:
    """Regression for #307: both flags ``None`` means the RISCO WebUI scrape came
    back unreadable this poll, not "no alarm" — treating it as a clear would let
    the next successful poll re-observe a still-latched, days-old
    ``memory_alarm`` and manufacture a bogus onset.
    """

    monkeypatch.setattr(auto, "load_alarm_scene_config",
                        lambda: auto.AlarmSceneConfig(enabled=True))
    auto._state["intrusion"] = True  # already latched from a prior real onset
    security = SecurityState(
        reachable=True, label="home", mode="armed", zones=[],
        ongoing_alarm=None, memory_alarm=None,
    )
    try:
        # Must not raise even with no running event loop (no task is created).
        auto.consider_security_read(security)
        assert auto._state["intrusion"] is True  # left untouched
    finally:
        auto._state["intrusion"] = None


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
