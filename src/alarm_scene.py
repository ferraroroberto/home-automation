"""Alarm-triggered scene capture + vision-LLM verdict (issue #162), UI-free core.

When the RISCO alarm trips, the orchestrator
(``app/webapp/alarm_scene_automation.py``) resolves the detector that fired to
its configured camera pairings (:mod:`src.alarm_scene_config`) and calls in here
to:

1. :func:`capture_scene` — drive each paired camera to its PTZ preset and grab a
   fresh JPEG, persisting it under the gitignored captures dir.
2. :func:`analyze_scene` — send each captured frame (plus the camera's last
   *calm* baseline frame, for contrast) to a vision-capable model and get back a
   structured verdict: real vs false alarm + a short natural-language reason.

The model call is routed through the **local hub** (``http://127.0.0.1:8000``,
Anthropic-shape ``/v1/messages``) per the fleet rule — never an inline
``claude -p`` wrapper. The hub's Anthropic-shape path forwards image blocks to a
vision-capable subscription backend; a probe confirmed the round-trip before this
landed. Any failure (hub down, parse error) degrades to a "verify manually"
verdict and never propagates into the alarm path.

Baselines (:func:`refresh_baselines`) are the most recent frame captured while no
alarm is active, kept per camera so the model can contrast "now" against
"normal". Both captures and baselines live under the already-gitignored
``webapp/camera_captures/`` tree (the repo is public — frames show the home).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.alarm_scene_config import ScenePairing
from src.camera_ffmpeg import CAPTURE_DIR

logger = logging.getLogger(__name__)

# Trigger frames and calm baselines both live under the gitignored captures dir.
TRIGGER_DIR = CAPTURE_DIR / "alarm"
BASELINE_DIR = CAPTURE_DIR / "baselines"

# Hub endpoint + default model. Overridable from .env by the orchestrator.
DEFAULT_HUB_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_MODEL = "claude-haiku-4-5"
# Verdict labels persisted to the audit log + shown in the alert copy.
VERDICT_REAL = "real"
VERDICT_FALSE = "false"
VERDICT_UNCERTAIN = "uncertain"
VERDICT_UNAVAILABLE = "unavailable"
_VALID_VERDICTS = frozenset({VERDICT_REAL, VERDICT_FALSE, VERDICT_UNCERTAIN})

# Seconds to let a PTZ camera settle on its preset before grabbing the frame. A
# full preset traverse plus autofocus takes a few seconds on the Reolink E1; a
# too-short wait snapshots mid-pan and the model rightly rejects a blurred frame
# (observed at 2 s during the #162 movement test), so default generously.
DEFAULT_PRESET_SETTLE_S = 4.0
# Cap how long the whole vision call may take so a wedged hub can't hang the
# fire-and-forget task forever.
_VISION_TIMEOUT_S = 120.0


@dataclass
class SceneCapture:
    """One captured frame for a paired camera (in memory + persisted)."""

    pairing: ScenePairing
    zone_name: str
    ok: bool
    frame: Optional[bytes] = None
    frame_path: Optional[Path] = None
    baseline: Optional[bytes] = None
    error: Optional[str] = None


@dataclass
class SceneVerdict:
    """The model's real/false assessment of a captured scene."""

    verdict: str
    summary: str
    raw_reply: str = ""
    model: str = ""
    per_camera: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# Baselines — last calm frame per camera                                      #
# --------------------------------------------------------------------------- #
def baseline_path(camera_id: str) -> Path:
    """On-disk path of a camera's last calm (no-alarm) baseline frame."""

    return BASELINE_DIR / f"{camera_id}.jpg"


def read_baseline(camera_id: str) -> Optional[bytes]:
    """Return a camera's stored baseline frame, or ``None`` if there is none."""

    try:
        return baseline_path(camera_id).read_bytes()
    except OSError:
        return None


def _save_baseline(camera_id: str, data: bytes) -> None:
    """Atomically persist a camera's latest calm frame as its baseline."""

    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    target = baseline_path(camera_id)
    tmp = target.with_suffix(".jpg.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except OSError as exc:  # noqa: BLE001 — baselines are best-effort, never fatal
        logger.warning("⚠️ camera %s baseline persist failed: %s", camera_id, exc)


async def refresh_baselines(camera_ids: List[str]) -> int:
    """Grab a fresh calm frame for each camera and store it as its baseline.

    Best-effort: an unreachable camera is skipped, never raised. Returns the
    number of baselines refreshed. The orchestrator only calls this while no
    alarm is active, so the stored frame is a genuine "normal" reference.
    """

    from src.camera_client import snapshot

    async def _one(camera_id: str) -> bool:
        try:
            data = await snapshot(camera_id)
        except Exception as exc:  # noqa: BLE001
            logger.info("ℹ️ camera %s baseline refresh skipped: %s", camera_id, exc)
            return False
        _save_baseline(camera_id, data)
        return True

    results = await asyncio.gather(*(_one(cid) for cid in camera_ids))
    return sum(1 for ok in results if ok)


# --------------------------------------------------------------------------- #
# Capture — drive each pairing's camera to its preset and snapshot            #
# --------------------------------------------------------------------------- #
def _save_trigger_frame(camera_id: str, zone_id: int, data: bytes, stamp: str) -> Path:
    TRIGGER_DIR.mkdir(parents=True, exist_ok=True)
    target = TRIGGER_DIR / f"{stamp}-z{zone_id}-{camera_id}.jpg"
    try:
        target.write_bytes(data)
    except OSError as exc:  # noqa: BLE001 — keep the in-memory frame even if disk fails
        logger.warning("⚠️ could not persist trigger frame %s: %s", target.name, exc)
    return target


async def _capture_one(
    pairing: ScenePairing, zone_name: str, *, settle_s: float, stamp: str
) -> SceneCapture:
    from src.camera_client import goto_preset, snapshot

    if pairing.preset_token:
        try:
            await goto_preset(pairing.camera_id, pairing.preset_token)
            await asyncio.sleep(settle_s)
        except Exception as exc:  # noqa: BLE001 — still try to snapshot where it points
            logger.info(
                "ℹ️ camera %s preset %s recall failed (%s); snapshotting anyway",
                pairing.camera_id, pairing.preset_token, exc,
            )
    try:
        frame = await snapshot(pairing.camera_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ camera %s alarm snapshot failed: %s", pairing.camera_id, exc)
        return SceneCapture(pairing=pairing, zone_name=zone_name, ok=False, error=str(exc))
    frame_path = _save_trigger_frame(pairing.camera_id, pairing.zone_id, frame, stamp)
    return SceneCapture(
        pairing=pairing,
        zone_name=zone_name,
        ok=True,
        frame=frame,
        frame_path=frame_path,
        baseline=read_baseline(pairing.camera_id),
    )


async def capture_scene(
    pairings: List[ScenePairing],
    zone_names: Dict[int, str],
    *,
    settle_s: float = DEFAULT_PRESET_SETTLE_S,
    now: Optional[datetime] = None,
) -> List[SceneCapture]:
    """Capture a frame for every pairing, in parallel. Failures are flagged, not raised."""

    if not pairings:
        return []
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return list(
        await asyncio.gather(
            *(
                _capture_one(
                    p, zone_names.get(p.zone_id, str(p.zone_id)),
                    settle_s=settle_s, stamp=stamp,
                )
                for p in pairings
            )
        )
    )


# --------------------------------------------------------------------------- #
# Analysis — vision LLM via the local hub                                     #
# --------------------------------------------------------------------------- #
_SYSTEM_PROMPT = (
    "You are a home-security analyst. The house alarm has triggered. For each "
    "camera you are shown its current frame and (when available) a recent CALM "
    "baseline frame for contrast. Decide whether this is a REAL intrusion or a "
    "likely FALSE alarm (pet, vehicle, wind/foliage, lighting change, or nothing "
    "moved). Be concise and practical — the owner needs a fast verdict."
)

_RESPONSE_INSTRUCTION = (
    "Reply with ONLY a JSON object, no prose, no markdown fence:\n"
    '{"verdict": "real|false|uncertain", '
    '"summary": "<one short sentence the owner reads on their phone>", '
    '"per_camera": [{"camera": "<id>", "assessment": "<what you see>"}]}'
)


def _image_block(data: bytes) -> Dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.b64encode(data).decode("ascii"),
        },
    }


def _build_content(captures: List[SceneCapture]) -> List[Dict[str, Any]]:
    """Assemble the Anthropic-shape user content: per-camera baseline + live frame."""

    content: List[Dict[str, Any]] = []
    for cap in captures:
        if not cap.ok or cap.frame is None:
            continue
        where = cap.pairing.preset_name or cap.pairing.camera_id
        content.append({
            "type": "text",
            "text": f"Camera '{cap.pairing.camera_id}' (detector: {cap.zone_name}, view: {where}).",
        })
        if cap.baseline:
            content.append({"type": "text", "text": "Calm baseline frame:"})
            content.append(_image_block(cap.baseline))
        content.append({"type": "text", "text": "Current frame (alarm just triggered):"})
        content.append(_image_block(cap.frame))
    content.append({"type": "text", "text": _RESPONSE_INSTRUCTION})
    return content


async def _call_vision(
    content: List[Dict[str, Any]], *, model: str, base_url: str
) -> str:
    """POST the assembled content to the hub and return the model's raw text.

    Isolated as the single network seam so tests monkeypatch exactly this and
    never touch the hub. The ``anthropic`` import is lazy so the module imports
    cleanly when the SDK isn't installed (py_compile / unrelated tests).
    """

    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key="local-dummy", base_url=base_url)
    try:
        message = await asyncio.wait_for(
            client.messages.create(
                model=model,
                max_tokens=512,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
            ),
            _VISION_TIMEOUT_S,
        )
    finally:
        await client.close()
    return "".join(
        getattr(block, "text", "") for block in message.content
        if getattr(block, "type", None) == "text"
    ).strip()


def _parse_verdict(raw: str, model: str) -> SceneVerdict:
    """Coerce the model's reply into a :class:`SceneVerdict`.

    A clean JSON object is used directly; anything else degrades to an
    ``uncertain`` verdict that still carries the raw reply, so a chatty or
    malformed answer never throws and is always inspectable in the audit log.
    """

    text = raw.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            verdict = str(data.get("verdict", "")).strip().lower()
            if verdict not in _VALID_VERDICTS:
                verdict = VERDICT_UNCERTAIN
            summary = str(data.get("summary") or "").strip() or text[:200]
            per_camera = data.get("per_camera")
            return SceneVerdict(
                verdict=verdict,
                summary=summary,
                raw_reply=raw,
                model=model,
                per_camera=per_camera if isinstance(per_camera, list) else [],
            )
        except (ValueError, TypeError):
            pass
    return SceneVerdict(
        verdict=VERDICT_UNCERTAIN,
        summary=text[:200] or "model returned no readable verdict",
        raw_reply=raw,
        model=model,
    )


async def analyze_scene(
    captures: List[SceneCapture],
    *,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_HUB_BASE_URL,
) -> SceneVerdict:
    """Send the captured frames to the hub and return a structured verdict.

    Never raises: a hub/network failure degrades to an ``unavailable`` verdict
    telling the owner to verify manually, so the alarm-scene task can still log
    and notify rather than dying.
    """

    usable = [c for c in captures if c.ok and c.frame is not None]
    if not usable:
        return SceneVerdict(
            verdict=VERDICT_UNAVAILABLE,
            summary="no camera frame could be captured — verify manually",
            model=model,
            error="no usable captures",
        )
    content = _build_content(usable)
    try:
        raw = await _call_vision(content, model=model, base_url=base_url)
    except Exception as exc:  # noqa: BLE001 — analysis must never break the alarm path
        logger.warning("⚠️ alarm scene vision call failed: %s", exc)
        return SceneVerdict(
            verdict=VERDICT_UNAVAILABLE,
            summary="AI analysis unavailable — verify manually",
            model=model,
            error=str(exc),
        )
    return _parse_verdict(raw, model)
