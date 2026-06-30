"""Alarm-triggered scene capture + AI verdict orchestrator (issue #162).

This is the glue between the RISCO alarm and the camera/vision core
(:mod:`src.alarm_scene`). It does **not** poll RISCO itself — the presence loop
(:mod:`app.webapp.presence_automation`) is the single interval reader of the
alarm state (a second poller would risk the cloud's third-party rate limit), so
it hands its one ``SecurityState`` read to :func:`consider_security_read` every
tick. We do the cheap work inline (edge detection) and fire the expensive work
(camera capture + vision call) as a detached task so the presence poll never
blocks.

On the **intrusion rising edge** we resolve which detector(s) tripped from the
same read's per-zone ``triggered`` flags, look up the configured camera pairings
(:mod:`src.alarm_scene_config`), and:

* capture a frame from each *paired* camera at its PTZ preset,
* send the frames (+ each camera's calm baseline) to the vision model,
* deliver the verdict via Web Push **and** the Telegram alarm notifier, and
* append the trigger + full model reply to the gitignored
  ``logs/alarm_scene.jsonl`` activity log.

A tripped detector with **no** pairing is logged and skipped — a random detector
firing must not photograph the house. While no alarm is active, the same entry
point opportunistically refreshes each camera's calm baseline.

Logging goes through :func:`src.activity_log.append_activity` — the same JSONL
substrate the telemetry-unification work (issue #283) catalogs and intends to
unify — rather than a bespoke writer, so this stays a registered producer rather
than a fourth divergent mechanism.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from app.webapp._env import _env_bool, _env_int
from src.activity_log import append_activity
from src.alarm_scene import (
    DEFAULT_HUB_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_PRESET_SETTLE_S,
    VERDICT_FALSE,
    VERDICT_REAL,
    VERDICT_UNAVAILABLE,
    SceneCapture,
    analyze_scene,
    capture_scene,
    refresh_baselines,
)
from src.alarm_scene_config import ScenePairing, pairings_for_zone
from src.notify import NotifierError
from src.notify_config import build_alarm_notifier, load_notify_config
from src.push_notifications import send_push

logger = logging.getLogger(__name__)

_LOG_CONSUMER = "alarm_scene"

# Telegram's caption hard limit; the verdict copy is short but truncate defensively.
_TELEGRAM_CAPTION_MAX = 1024
_TELEGRAM_PHOTO_API = "https://api.telegram.org/bot{token}/sendPhoto"
_TELEGRAM_TIMEOUT_S = 20

# Verdict → alert glyph, so the push/Telegram copy reads at a glance.
_VERDICT_EMOJI = {
    VERDICT_REAL: "🚨",
    VERDICT_FALSE: "✅",
    VERDICT_UNAVAILABLE: "⚠️",
}
_DEFAULT_EMOJI = "❓"


@dataclass(frozen=True)
class AlarmSceneConfig:
    """Alarm-scene engine knobs read from ``.env``."""

    enabled: bool = True
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_HUB_BASE_URL
    preset_settle_s: float = DEFAULT_PRESET_SETTLE_S
    baseline_refresh_s: int = 1800


def load_alarm_scene_config() -> AlarmSceneConfig:
    """Read alarm-scene settings from the already-loaded process env.

    Unlike the ``start_*`` entry points, this is called every presence tick, so
    it deliberately does **not** ``load_dotenv`` again — the presence engine's
    own ``load_dotenv(override=True)`` at startup has already populated the env,
    and re-reading the file 6×/minute would be pointless disk I/O.
    """

    import os

    return AlarmSceneConfig(
        enabled=_env_bool("ALARM_SCENE_ENABLED", True),
        model=(os.getenv("ALARM_SCENE_MODEL") or DEFAULT_MODEL).strip(),
        base_url=(os.getenv("ALARM_SCENE_HUB_URL") or DEFAULT_HUB_BASE_URL).strip(),
        preset_settle_s=max(0.0, _env_int("ALARM_SCENE_PRESET_SETTLE_MS", 4000) / 1000),
        baseline_refresh_s=max(60, _env_int("ALARM_SCENE_BASELINE_REFRESH_S", 1800)),
    )


# Process-lifetime edge + baseline-cadence state. A condition already active at
# startup sets the baseline (no fire), mirroring ``check_security_transitions``.
_state: Dict[str, object] = {"intrusion": None, "last_baseline": None}


def intrusion_rising_edge(intrusion: bool, state: Dict[str, object]) -> bool:
    """True only on a genuine no-alarm → alarm transition.

    First observation just records the baseline (returns False), so an alarm
    already ongoing when the app starts doesn't re-fire a capture on every boot.
    """

    last = state.get("intrusion")
    state["intrusion"] = intrusion
    return last is not None and last is False and intrusion is True


def triggered_zones(security: object) -> List[Tuple[int, str]]:
    """Return ``(zone_id, name)`` for every detector reporting ``triggered``."""

    out: List[Tuple[int, str]] = []
    for zone in getattr(security, "zones", None) or []:
        if getattr(zone, "triggered", False):
            out.append((int(getattr(zone, "id")), str(getattr(zone, "name", "") or getattr(zone, "id"))))
    return out


def _resolve_pairings(
    zones: List[Tuple[int, str]],
) -> Tuple[List[ScenePairing], Dict[int, str]]:
    """Collect the enabled pairings for the tripped detectors + a zone-name map."""

    pairings: List[ScenePairing] = []
    zone_names: Dict[int, str] = {}
    for zone_id, name in zones:
        zone_names[zone_id] = name
        pairings.extend(pairings_for_zone(zone_id))
    return pairings, zone_names


def _multipart_photo(
    *, chat_id: str, caption: str, image: bytes
) -> Tuple[bytes, str]:
    """Encode a Telegram ``sendPhoto`` multipart/form-data body (stdlib only)."""

    boundary = "----alarmscene" + uuid.uuid4().hex
    nl = b"\r\n"
    buf = bytearray()
    for name, value in (("chat_id", chat_id), ("caption", caption)):
        buf += b"--" + boundary.encode() + nl
        buf += f'Content-Disposition: form-data; name="{name}"'.encode() + nl + nl
        buf += value.encode("utf-8") + nl
    buf += b"--" + boundary.encode() + nl
    buf += b'Content-Disposition: form-data; name="photo"; filename="scene.jpg"' + nl
    buf += b"Content-Type: image/jpeg" + nl + nl
    buf += image + nl
    buf += b"--" + boundary.encode() + b"--" + nl
    return bytes(buf), boundary


def _send_telegram_photo(image: bytes, caption: str) -> bool:
    """Send a captured frame to the Telegram chat as a photo. Returns success.

    App-layer delivery built directly on the Bot API ``sendPhoto`` endpoint — the
    vendored ``src.notify`` primitive is intentionally text-only and stays
    copy-verbatim across the fleet, so attaching an image lives here, not there.
    Best-effort: any failure is logged and reported as ``False`` so the caller
    can fall back to a plain-text notification.
    """

    cfg = load_notify_config()
    if not (cfg.bot_token and cfg.chat_id):
        return False
    body, boundary = _multipart_photo(
        chat_id=cfg.chat_id, caption=caption[:_TELEGRAM_CAPTION_MAX], image=image
    )
    request = urllib.request.Request(
        _TELEGRAM_PHOTO_API.format(token=cfg.bot_token),
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TELEGRAM_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if not payload.get("ok"):
            logger.warning("⚠️ Telegram sendPhoto rejected: %s", payload.get("description"))
            return False
        return True
    except (urllib.error.URLError, ValueError) as exc:
        logger.warning("⚠️ Telegram sendPhoto failed: %s", exc)
        return False


def _deliver(
    zones: List[Tuple[int, str]], verdict: object, captures: List[SceneCapture]
) -> None:
    """Push + Telegram the verdict (with the captured frame attached). Never raises."""

    emoji = _VERDICT_EMOJI.get(getattr(verdict, "verdict", ""), _DEFAULT_EMOJI)
    where = ", ".join(name for _, name in zones) or "alarm"
    summary = getattr(verdict, "summary", "") or "scene captured"
    title = f"{emoji} Alarm scene — {where}"
    body = summary
    try:
        send_push(title, body, url="/")
    except Exception as exc:  # noqa: BLE001 — push is best-effort
        logger.warning("⚠️ alarm scene push failed: %s", exc)

    notifier = build_alarm_notifier()
    if notifier is None:  # unconfigured, or under pytest (send-proof guard)
        return
    caption = f"{title}: {body}"
    # Attach the first usable frame so the verdict arrives with the image; fall
    # back to a plain-text message if there's no frame or the photo upload fails.
    frame = next((c.frame for c in captures if c.ok and c.frame), None)
    if frame is not None and _send_telegram_photo(frame, caption):
        return
    try:
        notifier.send_text(caption)
    except NotifierError as exc:
        logger.warning("⚠️ alarm scene Telegram notify failed: %s", exc)


def _capture_record(cap: SceneCapture) -> Dict[str, object]:
    return {
        "camera_id": cap.pairing.camera_id,
        "zone_id": cap.pairing.zone_id,
        "zone": cap.zone_name,
        "preset": cap.pairing.preset_name or cap.pairing.preset_token,
        "ok": cap.ok,
        "frame": str(cap.frame_path) if cap.frame_path else None,
        "had_baseline": bool(cap.baseline),
        "error": cap.error,
    }


async def _run_onset(security: object, config: AlarmSceneConfig) -> None:
    """Capture, analyse, deliver and log one alarm trigger. Never raises."""

    try:
        zones = triggered_zones(security)
        pairings, zone_names = _resolve_pairings(zones)
        if not pairings:
            # Either no detector reported triggered, or none of the tripped ones
            # have a camera pairing — log and skip (no random-detector photos).
            logger.info("ℹ️ Alarm trigger with no camera pairing; scene capture skipped")
            append_activity(_LOG_CONSUMER, {
                "event": "trigger_no_pairing",
                "zones": [{"id": zid, "name": name} for zid, name in zones],
                "note": "no camera pairing for tripped detector(s); capture skipped",
            })
            return

        logger.info("📸 Alarm scene capture for detector(s): %s", zone_names)
        captures = await capture_scene(
            pairings, zone_names, settle_s=config.preset_settle_s
        )
        verdict = await analyze_scene(
            captures, model=config.model, base_url=config.base_url
        )
        _deliver(zones, verdict, captures)
        append_activity(_LOG_CONSUMER, {
            "event": "scene_capture",
            "zones": [{"id": zid, "name": name} for zid, name in zones],
            "pairings": [p.id for p in pairings],
            "captures": [_capture_record(c) for c in captures],
            "verdict": verdict.verdict,
            "summary": verdict.summary,
            "model": verdict.model,
            "raw_reply": verdict.raw_reply,
            "analysis_error": verdict.error,
        })
        logger.info(
            "✅ Alarm scene verdict: %s — %s", verdict.verdict, verdict.summary
        )
    except Exception as exc:  # noqa: BLE001 — a detached task must never crash silently
        logger.warning("⚠️ Alarm scene onset handler failed: %s", exc)
        append_activity(_LOG_CONSUMER, {"event": "error", "error": str(exc)})


async def _run_baseline_refresh(config: AlarmSceneConfig) -> None:
    """Refresh calm baselines for every configured camera. Never raises."""

    try:
        from src.camera_client import load_cameras

        camera_ids = [cam.id for cam in load_cameras()]
        if not camera_ids:
            return
        count = await refresh_baselines(camera_ids)
        if count:
            logger.info("🖼️ Refreshed %d alarm-scene baseline(s)", count)
    except Exception as exc:  # noqa: BLE001
        logger.info("ℹ️ Alarm scene baseline refresh skipped: %s", exc)


def _baseline_due(config: AlarmSceneConfig, *, now: Optional[float] = None) -> bool:
    """True when enough time has elapsed since the last calm-baseline refresh."""

    instant = time.monotonic() if now is None else now
    last = _state.get("last_baseline")
    if last is not None and (instant - float(last)) < config.baseline_refresh_s:
        return False
    _state["last_baseline"] = instant
    return True


def consider_security_read(security: object) -> None:
    """Entry point called from the presence loop with its one RISCO read.

    Cheap and synchronous: detects the intrusion rising edge and, when calm,
    decides whether a baseline refresh is due. The actual camera/vision work is
    dispatched as a detached task so the presence poll is never blocked.
    """

    config = load_alarm_scene_config()
    if not config.enabled:
        return
    intrusion = bool(
        getattr(security, "ongoing_alarm", None) or getattr(security, "memory_alarm", None)
    )
    if intrusion_rising_edge(intrusion, _state):
        asyncio.create_task(_run_onset(security, config), name="alarm-scene-onset")
    elif not intrusion and _baseline_due(config):
        asyncio.create_task(_run_baseline_refresh(config), name="alarm-scene-baseline")
