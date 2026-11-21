"""Home scenes — ``GET /api/scenes`` lists them, ``POST /api/scenes/{id}`` runs one.

A scene (``src.devices.home_scenes``) is a short list of chores. Running one calls the
same functions as the per-device controls, so it has their side effects too:
the blinds go through ``move_blinds`` plus the manual-move retry, the AC
units through ``set_device_state``, and the alarm through
``perform_manual_alarm_action``. That last call also writes the activity log
and the presence hand-off, exactly like a tap on the arm control. The blinds
and the AC run in parallel, and the alarm runs last so the house is closed
up before it arms.

A failed step does not stop the others. Each step reports its own outcome:
``ok`` is false only when the step took nowhere, and ``error`` is set when
any part of it failed (one blind out of five still counts as ``ok``). The
request is a 502 only when every step failed, the same rule as the blinds
group move, and its own ``ok`` is true only when no step has an error.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException

from app.webapp import read_snapshot
from app.webapp.alarm_notify import perform_manual_alarm_action
from app.webapp.blind_schedules import settle_manual_move
from app.webapp.routers.units import UNITS_SNAPSHOT
from src import telemetry
from src.devices.blind_automation import move_blinds
from src.devices.home_scenes import Scene, get_scene, load_scenes
from src.devices.melcloud_client import set_device_state

logger = logging.getLogger(__name__)

router = APIRouter()


def _scene_dict(scene: Scene) -> Dict[str, Any]:
    return {"id": scene.id, "label": scene.label, "steps": scene.step_labels()}


@router.get("/api/scenes")
async def list_scenes() -> Dict[str, Any]:
    return {"scenes": [_scene_dict(scene) for scene in load_scenes()]}


async def _run_blinds(action: str) -> Dict[str, Any]:
    step: Dict[str, Any] = {"kind": "blinds", "action": action}
    try:
        outcomes = await move_blinds(action)  # type: ignore[arg-type]
        await settle_manual_move(action, outcomes)
    except Exception as exc:  # noqa: BLE001 — reported as this step's outcome
        logger.warning("⚠️ Scene blinds %s failed: %s", action, exc)
        return {**step, "ok": False, "error": str(exc)}
    finally:
        read_snapshot.invalidate("tuya")
    failed = [o for o in outcomes if not o.ok and not o.superseded]
    step.update(ok=len(failed) < len(outcomes) or not outcomes, moved=len(outcomes) - len(failed), failed=len(failed))
    if failed:
        step["error"] = f"{len(failed)} of {len(outcomes)} blind(s) did not answer, retrying"
    return step


async def _run_ac_off() -> Dict[str, Any]:
    step: Dict[str, Any] = {"kind": "ac", "action": "off"}
    try:
        devices, _meta = await UNITS_SNAPSHOT.read()
    except Exception as exc:  # noqa: BLE001 — reported as this step's outcome
        logger.warning("⚠️ Scene AC off: could not read the units: %s", exc)
        return {**step, "ok": False, "error": str(exc)}
    running = [d.unit_id for d in devices if d.power]
    if not running:
        return {**step, "ok": True, "switched": 0, "failed": 0}
    results = await asyncio.gather(
        *(set_device_state(unit_id, power=False) for unit_id in running),
        return_exceptions=True,
    )
    errors = [str(r) for r in results if isinstance(r, BaseException)]
    UNITS_SNAPSHOT.invalidate()  # the next read shows what each unit took
    if errors:
        logger.warning("⚠️ Scene AC off: %d of %d unit(s) failed: %s", len(errors), len(running), "; ".join(errors))
    step.update(ok=len(errors) < len(running), switched=len(running) - len(errors), failed=len(errors))
    if errors:
        step["error"] = f"{len(errors)} of {len(running)} unit(s) did not switch off"
    return step


async def _run_alarm(action: str, scene_id: str) -> Dict[str, Any]:
    step: Dict[str, Any] = {"kind": "alarm", "action": action}
    try:
        state = await perform_manual_alarm_action(action, f"scene:{scene_id}")
    except Exception as exc:  # noqa: BLE001 — reported as this step's outcome
        logger.warning("⚠️ Scene alarm %s failed: %s", action, exc)
        return {**step, "ok": False, "error": str(exc)}
    finally:
        # The panel may have taken the command even when the read-back failed.
        read_snapshot.invalidate("security")
    return {**step, "ok": True, "mode": state.mode}


def _record_scene_event(scene_id: str, steps: List[Dict[str, Any]]) -> None:
    failed = [s["kind"] for s in steps if s.get("error")]
    telemetry.record_event_quietly(
        "scene",
        scene_id,
        entity_id=scene_id,
        source="manual",
        outcome="error" if failed else "ok",
        payload={"failed": failed} if failed else None,
    )


@router.post("/api/scenes/{scene_id}")
async def run_scene(scene_id: str) -> Dict[str, Any]:
    scene = get_scene(scene_id)
    if scene is None:
        raise HTTPException(status_code=404, detail=f"unknown scene '{scene_id}'")

    logger.info("ℹ️ Running scene %s: %s", scene.id, ", ".join(scene.step_labels()) or "no steps")
    parallel = []
    if scene.blinds:
        parallel.append(_run_blinds(scene.blinds))
    if scene.ac_off:
        parallel.append(_run_ac_off())
    steps: List[Dict[str, Any]] = list(await asyncio.gather(*parallel))
    if scene.alarm:
        steps.append(await _run_alarm(scene.alarm, scene.id))

    _record_scene_event(scene.id, steps)
    failed = [s for s in steps if not s["ok"]]
    if steps and len(failed) == len(steps):
        raise HTTPException(
            status_code=502,
            detail=f"{scene.label}: nothing took — " + "; ".join(f"{s['kind']}: {s['error']}" for s in failed),
        )
    partial = [s for s in steps if s.get("error")]
    if partial:
        logger.warning("⚠️ Scene %s finished with %d step(s) not fully done", scene.id, len(partial))
    else:
        logger.info("✅ Scene %s done", scene.id)
    return {"scene": scene.id, "label": scene.label, "ok": not partial, "steps": steps}
