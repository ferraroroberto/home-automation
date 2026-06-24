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

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from src.camera_client import (
    CameraCommandError,
    CameraConfigError,
    fetch_cameras,
    is_recording,
    mjpeg_frames,
    ptz_start,
    ptz_stop,
    snapshot,
    start_record,
    stop_record,
)
from src.camera_display_names import load_camera_display_names, set_camera_display_name

logger = logging.getLogger(__name__)

router = APIRouter()

# Continuous-move velocity for a directional press (0..1).
_PTZ_SPEED = 0.6
_MJPEG_BOUNDARY = "frame"


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, CameraConfigError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, CameraCommandError):
        return HTTPException(status_code=502, detail=str(exc))
    logger.warning("⚠️ Failed to call cameras: %s", exc)
    return HTTPException(status_code=502, detail=f"failed to call cameras: {exc}")


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
    action: str = "stop"  # "start" | "stop"
    direction: Optional[str] = None  # up | down | left | right
    zoom: Optional[str] = None  # in | out


def _velocity(direction: Optional[str], zoom: Optional[str]) -> Dict[str, float]:
    pan = tilt = z = 0.0
    if direction == "left":
        pan = -_PTZ_SPEED
    elif direction == "right":
        pan = _PTZ_SPEED
    elif direction == "up":
        tilt = _PTZ_SPEED
    elif direction == "down":
        tilt = -_PTZ_SPEED
    if zoom == "in":
        z = _PTZ_SPEED
    elif zoom == "out":
        z = -_PTZ_SPEED
    return {"pan": pan, "tilt": tilt, "zoom": z}


@router.post("/api/cameras/{camera_id}/ptz")
async def camera_ptz(camera_id: str, payload: PtzPayload) -> Dict[str, Any]:
    try:
        if payload.action == "start":
            v = _velocity(payload.direction, payload.zoom)
            await ptz_start(camera_id, pan=v["pan"], tilt=v["tilt"], zoom=v["zoom"])
        else:
            await ptz_stop(camera_id)
        return {"camera_id": camera_id, "action": payload.action}
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
