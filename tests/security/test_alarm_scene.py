from __future__ import annotations

import asyncio

import src.alarm_scene as scene
from src.alarm_scene import (
    VERDICT_REAL,
    VERDICT_UNAVAILABLE,
    VERDICT_UNCERTAIN,
    SceneCapture,
    analyze_scene,
    capture_scene,
)
from src.alarm_scene_config import ScenePairing


def _capture(camera_id="garden", *, ok=True, baseline=b"BASE") -> SceneCapture:
    return SceneCapture(
        pairing=ScenePairing(id="p", zone_id=3, camera_id=camera_id, preset_name="Barbecue"),
        zone_name="Garden",
        ok=ok,
        frame=b"FRAME" if ok else None,
        baseline=baseline,
    )


def test_parse_verdict_clean_json() -> None:
    raw = '{"verdict": "real", "summary": "person at the gate", "per_camera": [{"camera": "garden"}]}'
    v = scene._parse_verdict(raw, "m")
    assert v.verdict == VERDICT_REAL
    assert v.summary == "person at the gate"
    assert v.per_camera == [{"camera": "garden"}]
    assert v.raw_reply == raw


def test_parse_verdict_chatty_and_invalid_label_degrade_to_uncertain() -> None:
    # Wrapped in prose + a bogus verdict value -> uncertain, but raw kept.
    raw = 'Sure!\n```json\n{"verdict": "maybe", "summary": "a cat"}\n```'
    v = scene._parse_verdict(raw, "m")
    assert v.verdict == VERDICT_UNCERTAIN
    assert v.summary == "a cat"
    assert v.raw_reply == raw


def test_parse_verdict_garbage_keeps_raw() -> None:
    v = scene._parse_verdict("totally not json", "m")
    assert v.verdict == VERDICT_UNCERTAIN
    assert "totally not json" in v.summary
    assert v.raw_reply == "totally not json"


def test_build_content_includes_baseline_and_live_frame() -> None:
    content = scene._build_content([_capture(baseline=b"BASE")])
    images = [b for b in content if b.get("type") == "image"]
    assert len(images) == 2  # baseline + live
    # The final block is always the JSON response instruction.
    assert content[-1]["type"] == "text"
    assert "verdict" in content[-1]["text"]


def test_build_content_skips_baseline_when_absent() -> None:
    content = scene._build_content([_capture(baseline=None)])
    images = [b for b in content if b.get("type") == "image"]
    assert len(images) == 1  # live only


def test_analyze_scene_parses_hub_reply(monkeypatch) -> None:
    async def fake_call(content, *, model, base_url):
        assert any(b.get("type") == "image" for b in content)
        return '{"verdict": "false", "summary": "just the cat"}'

    monkeypatch.setattr(scene, "_call_vision", fake_call)
    verdict = asyncio.run(analyze_scene([_capture()], model="claude-haiku-4-5"))
    assert verdict.verdict == "false"
    assert verdict.summary == "just the cat"
    assert verdict.model == "claude-haiku-4-5"


def test_analyze_scene_degrades_when_hub_fails(monkeypatch) -> None:
    async def boom(content, *, model, base_url):
        raise RuntimeError("hub down")

    monkeypatch.setattr(scene, "_call_vision", boom)
    verdict = asyncio.run(analyze_scene([_capture()]))
    assert verdict.verdict == VERDICT_UNAVAILABLE
    assert "verify manually" in verdict.summary
    assert verdict.error == "hub down"


def test_analyze_scene_unavailable_with_no_usable_captures() -> None:
    verdict = asyncio.run(analyze_scene([_capture(ok=False)]))
    assert verdict.verdict == VERDICT_UNAVAILABLE
    assert verdict.error == "no usable captures"


def test_capture_scene_drives_preset_then_snapshots(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(scene, "TRIGGER_DIR", tmp_path)
    moved: list = []

    async def fake_goto(camera_id, token):
        moved.append((camera_id, token))

    async def fake_snapshot(camera_id, path=None):
        return b"JPEGDATA-" + camera_id.encode()

    import src.camera_client as cc

    monkeypatch.setattr(cc, "goto_preset", fake_goto)
    monkeypatch.setattr(cc, "snapshot", fake_snapshot)

    pairings = [ScenePairing(id="p", zone_id=3, camera_id="garden", preset_token="1")]
    caps = asyncio.run(capture_scene(pairings, {3: "Garden"}, settle_s=0, now=None))

    assert moved == [("garden", "1")]
    assert len(caps) == 1
    assert caps[0].ok is True
    assert caps[0].frame == b"JPEGDATA-garden"
    assert caps[0].frame_path is not None and caps[0].frame_path.exists()


def test_capture_scene_flags_snapshot_failure(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(scene, "TRIGGER_DIR", tmp_path)

    async def fail_snapshot(camera_id, path=None):
        raise RuntimeError("camera offline")

    import src.camera_client as cc

    monkeypatch.setattr(cc, "snapshot", fail_snapshot)

    pairings = [ScenePairing(id="p", zone_id=3, camera_id="garden")]
    caps = asyncio.run(capture_scene(pairings, {3: "Garden"}, settle_s=0))
    assert caps[0].ok is False
    assert "camera offline" in caps[0].error
