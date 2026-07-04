r"""
Camera ffmpeg subsystem — snapshot / MJPEG stream / recording (issue #197 split).
================================================================================
Peeled out of ``camera_client`` (issue #197): the RTSP-over-ffmpeg capture paths
have nothing to do with the ONVIF/PTZ control surface that stays in
``camera_client``. This module owns the single-frame snapshot (+ last-known
persistence), the on-demand MJPEG transcode for the live view, and the
server-side clip recorder.

It depends on ``camera_client`` only for the config lookup, the authenticated
RTSP URL, and the shared :class:`CameraCommandError`; those are imported lazily
inside each function so the two modules never form an import cycle
(``camera_client`` imports the public capture functions from here at module load,
mirroring the lazy-import idiom this codebase already uses for ``camera_presets``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import AsyncIterator, Dict, Optional

from src._atomic_json import atomic_write_bytes

logger = logging.getLogger(__name__)

CAPTURE_DIR = Path(__file__).resolve().parent.parent / "webapp" / "camera_captures"
# The most recent snapshot per camera, persisted so the list can show a
# last-known frame without re-hitting the camera (issue #190). Lives under the
# already-gitignored captures dir.
LAST_SNAPSHOT_DIR = CAPTURE_DIR / "last"

_FFMPEG_SNAPSHOT_TIMEOUT_S = 15.0
# JPEG start-of-image marker — used to split ffmpeg's image2pipe MJPEG output
# into discrete frames for the live stream.
_JPEG_SOI = b"\xff\xd8"


# --------------------------------------------------------------------------- #
# Snapshot persistence                                                        #
# --------------------------------------------------------------------------- #
def last_snapshot_path(camera_id: str) -> Path:
    """On-disk path of a camera's most recent persisted snapshot."""
    return LAST_SNAPSHOT_DIR / f"{camera_id}.jpg"


def read_last_snapshot(camera_id: str) -> Optional[bytes]:
    """Return the persisted last snapshot for a camera, or None if there is none."""
    target = last_snapshot_path(camera_id)
    try:
        return target.read_bytes()
    except OSError:
        return None


def _save_last_snapshot(camera_id: str, data: bytes) -> None:
    """Atomically persist a camera's latest JPEG as its last-known frame."""
    target = last_snapshot_path(camera_id)
    try:
        atomic_write_bytes(target, data)
    except OSError as exc:  # noqa: BLE001 — persistence is best-effort, never fatal
        logger.warning("⚠️ camera %s could not persist last snapshot: %s", camera_id, exc)


# --------------------------------------------------------------------------- #
# ffmpeg capture (snapshot + live MJPEG)                                      #
# --------------------------------------------------------------------------- #
async def snapshot(camera_id: str, path: Optional[Path] = None) -> bytes:
    """Grab a single fresh JPEG off the substream via ffmpeg, returned as bytes.

    The grabbed frame is also persisted as the camera's last-known snapshot so
    the list can show it without re-hitting the camera (issue #190).
    """
    from src.camera_client import CameraCommandError, _stream_url_for, get_camera_config

    cfg = get_camera_config(camera_id, path)
    url = await _stream_url_for(cfg, main=False)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
        "-i", url, "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), _FFMPEG_SNAPSHOT_TIMEOUT_S)
    except asyncio.TimeoutError as exc:
        proc.kill()
        raise CameraCommandError(f"camera {camera_id} snapshot timed out") from exc
    if proc.returncode != 0 or not out:
        tail = (err or b"").decode("utf-8", "ignore").strip().splitlines()
        raise CameraCommandError(
            f"camera {camera_id} snapshot failed: {tail[-1] if tail else 'no frame'}"
        )
    _save_last_snapshot(camera_id, out)
    return out


async def mjpeg_frames(
    camera_id: str, fps: int = 8, path: Optional[Path] = None
) -> AsyncIterator[bytes]:
    """Yield JPEG frames off the substream as an MJPEG transcode (live view).

    ffmpeg runs only while this generator is consumed (i.e. while a viewer is
    connected); it is torn down when the consumer disconnects.
    """
    from src.camera_client import _stream_url_for, get_camera_config

    cfg = get_camera_config(camera_id, path)
    url = await _stream_url_for(cfg, main=False)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
        "-i", url, "-r", str(fps), "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    buf = b""
    try:
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            buf += chunk
            # Split on the next SOI marker: bytes up to it are a complete frame.
            while True:
                nxt = buf.find(_JPEG_SOI, 2)
                if nxt == -1:
                    break
                frame, buf = buf[:nxt], buf[nxt:]
                if frame.startswith(_JPEG_SOI):
                    yield frame
    finally:
        with_suppressed = (ProcessLookupError, PermissionError)
        try:
            proc.kill()
        except with_suppressed:
            pass
        try:
            await proc.wait()
        except with_suppressed:
            pass


# --------------------------------------------------------------------------- #
# Recording (server-side ffmpeg clip)                                         #
# --------------------------------------------------------------------------- #
_recordings: Dict[str, asyncio.subprocess.Process] = {}


def is_recording(camera_id: str) -> bool:
    proc = _recordings.get(camera_id)
    return proc is not None and proc.returncode is None


async def start_record(camera_id: str, path: Optional[Path] = None) -> str:
    """Start recording the main stream to ``webapp/camera_captures/``. Returns the filename."""
    from src.camera_client import CameraCommandError, _stream_url_for, get_camera_config

    if is_recording(camera_id):
        raise CameraCommandError(f"camera {camera_id} is already recording")
    cfg = get_camera_config(camera_id, path)
    url = await _stream_url_for(cfg, main=True)
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_path = CAPTURE_DIR / f"{cfg.id}-{stamp}.mp4"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
        "-i", url, "-c", "copy", str(out_path),
        stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    _recordings[camera_id] = proc
    logger.info("⏺️ camera %s recording to %s", camera_id, out_path.name)
    return out_path.name


async def stop_record(camera_id: str) -> None:
    """Finalize an in-progress recording (graceful ffmpeg quit)."""
    from src.camera_client import CameraCommandError

    proc = _recordings.pop(camera_id, None)
    if proc is None or proc.returncode is not None:
        raise CameraCommandError(f"camera {camera_id} is not recording")
    try:
        if proc.stdin is not None:
            proc.stdin.write(b"q")
            await proc.stdin.drain()
            proc.stdin.close()
        await asyncio.wait_for(proc.wait(), 5.0)
    except (asyncio.TimeoutError, ProcessLookupError, ConnectionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    logger.info("⏹️ camera %s recording stopped", camera_id)
