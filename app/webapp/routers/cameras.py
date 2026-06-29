"""Camera API over ``src.camera_client`` (issue #161).

Read + control for the open RTSP/ONVIF cameras: list, fresh JPEG snapshot, live
MJPEG stream, PTZ, server-side recording, and the display-name override. Auth is
handled globally by the bearer/token middleware — the live ``<img>`` stream is
reachable from the PWA via the same ``?token=`` the rest of the app uses.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from src.camera_client import (
    CameraCommandError,
    CameraConfigError,
    fetch_cameras,
    get_ptz_status,
    goto_preset,
    is_recording,
    list_presets,
    mjpeg_frames,
    ptz_absolute,
    ptz_start,
    ptz_step,
    ptz_stop,
    read_last_snapshot,
    remove_preset,
    set_preset,
    snapshot,
    start_record,
    stop_record,
)
from src.camera_display_names import load_camera_display_names, set_camera_display_name
from src.camera_token import issue as _issue_camera_token
from src.camera_preset_names import apply_overrides as apply_preset_overrides
from src.camera_preset_names import set_preset_name

logger = logging.getLogger(__name__)

router = APIRouter()

# Continuous-move velocity for a directional press (0..1). The fixed-step nudge
# uses a gentler speed so one click maps to a small, predictable increment.
_PTZ_SPEED = 0.6
_PTZ_STEP_SPEED = 0.4
_MJPEG_BOUNDARY = "frame"


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, CameraConfigError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, CameraCommandError):
        return HTTPException(status_code=502, detail=str(exc))
    logger.warning("⚠️ Failed to call cameras: %s", exc)
    return HTTPException(status_code=502, detail=f"failed to call cameras: {exc}")


@router.post("/api/cameras/stream-token")
async def camera_stream_token(request: Request) -> Dict[str, Any]:
    """Issue a short-lived scoped token for camera stream/snapshot img-src URLs.

    The caller must already be authenticated (bearer header or loopback).  The
    returned token is valid for 60 s and accepted by the middleware only on the
    ``/stream`` and ``/last_snapshot`` paths — the long-lived bearer never needs
    to appear in a URL (issue #261).
    """
    cfg = getattr(request.app.state, "webapp_config", None)
    bearer = ((cfg.auth_token if cfg else "") or "").strip()
    if not bearer:
        return {"token": "", "expires_in": 0}
    return _issue_camera_token(bearer)


@router.get("/api/cameras")
async def list_cameras() -> Dict[str, Any]:
    try:
        display_names = load_camera_display_names()
        cameras = []
        for cam in await fetch_cameras():
            data = asdict(cam)
            data["display_name"] = display_names.get(cam.id)
            data["recording"] = is_recording(cam.id)
            cameras.append(data)
        return {"cameras": cameras}
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


@router.get("/api/cameras/{camera_id}/snapshot")
async def camera_snapshot(camera_id: str) -> Response:
    try:
        jpeg = await snapshot(camera_id)
        return Response(content=jpeg, media_type="image/jpeg")
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


@router.get("/api/cameras/{camera_id}/last_snapshot")
async def camera_last_snapshot(camera_id: str) -> Response:
    """Serve the persisted last-known frame (cheap; never hits the camera)."""
    jpeg = read_last_snapshot(camera_id)
    if jpeg is None:
        raise HTTPException(status_code=404, detail="no snapshot yet")
    return Response(content=jpeg, media_type="image/jpeg")


@router.get("/api/cameras/{camera_id}/stream")
async def camera_stream(camera_id: str) -> StreamingResponse:
    try:
        async def _multipart() -> AsyncIterator[bytes]:
            async for jpeg in mjpeg_frames(camera_id):
                yield (
                    b"--" + _MJPEG_BOUNDARY.encode() + b"\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                    + jpeg + b"\r\n"
                )

        return StreamingResponse(
            _multipart(),
            media_type=f"multipart/x-mixed-replace; boundary={_MJPEG_BOUNDARY}",
        )
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


class PtzPayload(BaseModel):
    action: str = "stop"  # "start" | "stop" | "step"
    direction: Optional[str] = None  # up | down | left | right
    zoom: Optional[str] = None  # in | out


def _velocity(
    direction: Optional[str], zoom: Optional[str], speed: float = _PTZ_SPEED
) -> Dict[str, float]:
    pan = tilt = z = 0.0
    if direction == "left":
        pan = -speed
    elif direction == "right":
        pan = speed
    elif direction == "up":
        tilt = speed
    elif direction == "down":
        tilt = -speed
    if zoom == "in":
        z = speed
    elif zoom == "out":
        z = -speed
    return {"pan": pan, "tilt": tilt, "zoom": z}


@router.post("/api/cameras/{camera_id}/ptz")
async def camera_ptz(camera_id: str, payload: PtzPayload) -> Dict[str, Any]:
    try:
        if payload.action == "start":
            v = _velocity(payload.direction, payload.zoom)
            await ptz_start(camera_id, pan=v["pan"], tilt=v["tilt"], zoom=v["zoom"])
        elif payload.action == "step":
            v = _velocity(payload.direction, payload.zoom, _PTZ_STEP_SPEED)
            await ptz_step(camera_id, pan=v["pan"], tilt=v["tilt"], zoom=v["zoom"])
        else:
            await ptz_stop(camera_id)
        return {"camera_id": camera_id, "action": payload.action}
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


@router.get("/api/cameras/{camera_id}/ptz/status")
async def camera_ptz_status(camera_id: str) -> Dict[str, Any]:
    """Live pan/tilt/zoom + absolute-move bounds for the manual-coordinate UI."""
    try:
        return await get_ptz_status(camera_id)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


class PtzAbsolutePayload(BaseModel):
    pan: float
    tilt: float
    zoom: Optional[float] = None


@router.post("/api/cameras/{camera_id}/ptz/absolute")
async def camera_ptz_absolute(
    camera_id: str, payload: PtzAbsolutePayload
) -> Dict[str, Any]:
    try:
        await ptz_absolute(
            camera_id, pan=payload.pan, tilt=payload.tilt, zoom=payload.zoom
        )
        return {"camera_id": camera_id, "pan": payload.pan, "tilt": payload.tilt}
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


@router.get("/api/cameras/{camera_id}/presets")
async def camera_presets(camera_id: str) -> Dict[str, Any]:
    try:
        presets = await list_presets(camera_id)
        return {"presets": apply_preset_overrides(camera_id, presets)}
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


class PresetSavePayload(BaseModel):
    name: str = ""


@router.post("/api/cameras/{camera_id}/presets")
async def camera_preset_save(
    camera_id: str, payload: PresetSavePayload
) -> Dict[str, Any]:
    try:
        return await set_preset(camera_id, payload.name.strip())
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


class PresetRenamePayload(BaseModel):
    name: str = ""


@router.put("/api/cameras/{camera_id}/presets/{token}/name")
async def camera_preset_rename(
    camera_id: str, token: str, payload: PresetRenamePayload
) -> Dict[str, Any]:
    """Rename a preset via a local override (keeps the saved lens position)."""
    name = payload.name.strip()
    set_preset_name(camera_id, token, name)
    return {"camera_id": camera_id, "token": token, "name": name}


@router.post("/api/cameras/{camera_id}/presets/{token}/goto")
async def camera_preset_goto(camera_id: str, token: str) -> Dict[str, Any]:
    try:
        await goto_preset(camera_id, token)
        return {"camera_id": camera_id, "token": token}
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


@router.delete("/api/cameras/{camera_id}/presets/{token}")
async def camera_preset_remove(camera_id: str, token: str) -> Dict[str, Any]:
    try:
        await remove_preset(camera_id, token)
        set_preset_name(camera_id, token, "")  # drop any stale name override
        return {"camera_id": camera_id, "token": token, "removed": True}
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


class RecordPayload(BaseModel):
    action: str = "stop"  # "start" | "stop"


@router.post("/api/cameras/{camera_id}/record")
async def camera_record(camera_id: str, payload: RecordPayload) -> Dict[str, Any]:
    try:
        if payload.action == "start":
            filename = await start_record(camera_id)
            return {"camera_id": camera_id, "recording": True, "file": filename}
        await stop_record(camera_id)
        return {"camera_id": camera_id, "recording": False}
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


class CameraDisplayNamePayload(BaseModel):
    display_name: str = ""


@router.put("/api/cameras/{camera_id}/display_name")
async def set_camera_name(
    camera_id: str, payload: CameraDisplayNamePayload
) -> Dict[str, Any]:
    try:
        display_name = payload.display_name.strip()
        set_camera_display_name(camera_id, display_name)
        return {"camera_id": camera_id, "display_name": display_name or None}
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)
