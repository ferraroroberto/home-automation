"""
Camera LAN client (async, UI-free)
==================================
Product core for the open RTSP/ONVIF cameras (issue #161), built on the path
proven by the #89 spike. Mirrors the other domain cores (``elgato_client`` /
``melcloud_client`` / ``risco_client``): no Streamlit, no FastAPI — config in →
flattened :class:`CameraInfo` out, plus snapshot / MJPEG-stream / PTZ / record.

The generic, vendor-neutral path keeps the fleet swappable:

* **ONVIF** for device info, RTSP stream URIs (``GetStreamUri``), and PTZ
  (``ContinuousMove`` / ``Stop``).
* **RTSP + ffmpeg** for a still snapshot, the live MJPEG stream (transcoded on
  demand, only while a viewer is connected), and server-side clip recording.

Cameras are declared in gitignored ``config/cameras.json`` (the repo is public —
hosts/credentials/locations never enter git). Copy ``config/cameras.sample.json``::

    [
      {"id": "garden", "host": "192.168.0.x", "onvif_port": 8000,
       "rtsp_port": 554, "username": "admin", "password": "…"}
    ]

ONVIF connections are cached per camera id so list/PTZ calls don't re-handshake
on every poll. Captures land in gitignored ``webapp/camera_captures/``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional
from urllib.parse import quote, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "cameras.json"
CAPTURE_DIR = Path(__file__).resolve().parent.parent / "webapp" / "camera_captures"

# A continuous PTZ move is auto-stopped after this long even if the client never
# sends an explicit stop (a dropped pointerup on mobile must not run the motor
# into its hard stop).
_PTZ_SAFETY_STOP_S = 4.0
_FFMPEG_SNAPSHOT_TIMEOUT_S = 15.0
# JPEG start-of-image marker — used to split ffmpeg's image2pipe MJPEG output
# into discrete frames for the live stream.
_JPEG_SOI = b"\xff\xd8"


class CameraConfigError(RuntimeError):
    """No cameras are configured, or ``config/cameras.json`` is unreadable."""


class CameraCommandError(RuntimeError):
    """A camera rejected a command or returned an unusable response."""


@dataclass(frozen=True)
class CameraConfig:
    """One camera's connection details, from ``config/cameras.json``."""

    id: str
    host: str
    username: str
    password: str
    onvif_port: int = 8000
    rtsp_port: int = 554


@dataclass(frozen=True)
class CameraInfo:
    """Flattened camera state safe for CLI/API/UI callers."""

    id: str
    host: str
    reachable: bool
    display_name: Optional[str] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    firmware: Optional[str] = None
    ptz_capable: bool = False
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# Config                                                                      #
# --------------------------------------------------------------------------- #
def load_cameras(path: Optional[Path] = None) -> List[CameraConfig]:
    """Read camera definitions from ``config/cameras.json``. Empty list if absent."""
    target = Path(path) if path is not None else _CONFIG_PATH
    if not target.exists():
        return []
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CameraConfigError(f"could not read {target}: {exc}") from exc
    items = raw.get("cameras", raw) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise CameraConfigError(f"{target} must be a JSON list of cameras")
    cameras: List[CameraConfig] = []
    for entry in items:
        if not isinstance(entry, dict) or not entry.get("host"):
            continue
        cam_id = str(entry.get("id") or entry["host"])
        cameras.append(
            CameraConfig(
                id=cam_id,
                host=str(entry["host"]),
                username=str(entry.get("username", "admin")),
                password=str(entry.get("password", "")),
                onvif_port=int(entry.get("onvif_port", 8000)),
                rtsp_port=int(entry.get("rtsp_port", 554)),
            )
        )
    return cameras


def get_camera_config(camera_id: str, path: Optional[Path] = None) -> CameraConfig:
    """Look up one camera by id, or raise if it isn't configured."""
    for cam in load_cameras(path):
        if cam.id == camera_id:
            return cam
    raise CameraCommandError(f"camera {camera_id} is not configured")


# --------------------------------------------------------------------------- #
# Cached ONVIF connection per camera                                          #
# --------------------------------------------------------------------------- #
@dataclass
class _CameraConn:
    """A live ONVIF connection plus the static facts derived from it."""

    cam: object  # onvif.ONVIFCamera
    devmgmt: object  # device-management service (for liveness pings)
    ptz: object  # ptz service (or None)
    profile_token: str
    main_uri: str
    sub_uri: str
    manufacturer: Optional[str]
    model: Optional[str]
    firmware: Optional[str]
    ptz_capable: bool


_conns: Dict[str, _CameraConn] = {}
_conn_locks: Dict[str, asyncio.Lock] = {}
_ptz_watchdogs: Dict[str, asyncio.Task] = {}


def _lock_for(camera_id: str) -> asyncio.Lock:
    lock = _conn_locks.get(camera_id)
    if lock is None:
        lock = asyncio.Lock()
        _conn_locks[camera_id] = lock
    return lock


async def _stream_uri(media, token: str) -> str:
    req = media.create_type("GetStreamUri")
    req.ProfileToken = token
    req.StreamSetup = {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}}
    resp = await media.GetStreamUri(req)
    return resp.Uri


async def _connect(cfg: CameraConfig) -> _CameraConn:
    """Open an ONVIF session and read the static profile/PTZ facts once."""
    try:
        from onvif import ONVIFCamera
    except Exception as exc:  # pragma: no cover - import guard
        raise CameraCommandError(f"onvif client unavailable: {exc}") from exc

    cam = ONVIFCamera(
        cfg.host, cfg.onvif_port, cfg.username, cfg.password, adjust_time=True
    )
    await cam.update_xaddrs()
    devmgmt = await cam.create_devicemgmt_service()
    info = await devmgmt.GetDeviceInformation()
    media = await cam.create_media_service()
    profiles = await media.GetProfiles()
    if not profiles:
        raise CameraCommandError(f"camera {cfg.id} exposes no media profiles")
    token = profiles[0].token
    main_uri = await _stream_uri(media, token)
    sub_uri = main_uri
    if len(profiles) > 1:
        try:
            sub_uri = await _stream_uri(media, profiles[-1].token)
        except Exception as exc:  # noqa: BLE001
            logger.info("ℹ️ camera %s substream URI unavailable: %s", cfg.id, exc)

    ptz = None
    ptz_capable = False
    try:
        ptz = await cam.create_ptz_service()
        ptz_capable = bool(await ptz.GetConfigurations())
    except Exception as exc:  # noqa: BLE001
        logger.info("ℹ️ camera %s reports no PTZ: %s", cfg.id, exc)
        ptz = None

    return _CameraConn(
        cam=cam,
        devmgmt=devmgmt,
        ptz=ptz,
        profile_token=token,
        main_uri=main_uri,
        sub_uri=sub_uri,
        manufacturer=getattr(info, "Manufacturer", None),
        model=getattr(info, "Model", None),
        firmware=getattr(info, "FirmwareVersion", None),
        ptz_capable=ptz_capable,
    )


async def _get_conn(cfg: CameraConfig) -> _CameraConn:
    """Return the cached ONVIF connection for a camera, connecting on first use."""
    async with _lock_for(cfg.id):
        conn = _conns.get(cfg.id)
        if conn is not None:
            return conn
        conn = await _connect(cfg)
        _conns[cfg.id] = conn
        return conn


async def _drop_conn(camera_id: str) -> None:
    conn = _conns.pop(camera_id, None)
    if conn is not None:
        try:
            await conn.cam.close()
        except Exception:  # noqa: BLE001
            pass


async def close_all() -> None:
    """Close every cached ONVIF connection (CLI exit / webapp shutdown)."""
    for camera_id in list(_conns):
        await _drop_conn(camera_id)
    for task in list(_ptz_watchdogs.values()):
        task.cancel()
    _ptz_watchdogs.clear()


# --------------------------------------------------------------------------- #
# Public read                                                                 #
# --------------------------------------------------------------------------- #
async def _read_one(cfg: CameraConfig) -> CameraInfo:
    try:
        conn = await _get_conn(cfg)
        # Liveness ping: a cached connection's camera may have gone offline since
        # it was opened, so re-read device info rather than trusting the cache.
        await conn.devmgmt.GetDeviceInformation()
        return CameraInfo(
            id=cfg.id,
            host=cfg.host,
            reachable=True,
            manufacturer=conn.manufacturer,
            model=conn.model,
            firmware=conn.firmware,
            ptz_capable=conn.ptz_capable,
        )
    except Exception as exc:  # noqa: BLE001
        await _drop_conn(cfg.id)
        return CameraInfo(id=cfg.id, host=cfg.host, reachable=False, error=str(exc))


async def fetch_cameras(path: Optional[Path] = None) -> List[CameraInfo]:
    """Read every configured camera concurrently. Unreachable ones are flagged."""
    cameras = load_cameras(path)
    if not cameras:
        return []
    return list(await asyncio.gather(*(_read_one(cfg) for cfg in cameras)))


# --------------------------------------------------------------------------- #
# RTSP helpers + ffmpeg capture                                               #
# --------------------------------------------------------------------------- #
def _with_credentials(rtsp_uri: str, cfg: CameraConfig) -> str:
    """Inject URL-encoded creds into an RTSP URI (ONVIF returns them bare)."""
    parts = urlsplit(rtsp_uri)
    netloc = f"{quote(cfg.username)}:{quote(cfg.password)}@{parts.hostname or cfg.host}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


async def _stream_url_for(cfg: CameraConfig, *, main: bool) -> str:
    conn = await _get_conn(cfg)
    return _with_credentials(conn.main_uri if main else conn.sub_uri, cfg)


async def snapshot(camera_id: str, path: Optional[Path] = None) -> bytes:
    """Grab a single fresh JPEG off the substream via ffmpeg, returned as bytes."""
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
    return out


async def mjpeg_frames(
    camera_id: str, fps: int = 8, path: Optional[Path] = None
) -> AsyncIterator[bytes]:
    """Yield JPEG frames off the substream as an MJPEG transcode (live view).

    ffmpeg runs only while this generator is consumed (i.e. while a viewer is
    connected); it is torn down when the consumer disconnects.
    """
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
# PTZ                                                                          #
# --------------------------------------------------------------------------- #
async def _ptz_safety_stop(camera_id: str) -> None:
    try:
        await asyncio.sleep(_PTZ_SAFETY_STOP_S)
        logger.info("ℹ️ camera %s PTZ safety auto-stop fired", camera_id)
        await ptz_stop(camera_id)
    except asyncio.CancelledError:
        pass


async def ptz_start(
    camera_id: str,
    *,
    pan: float = 0.0,
    tilt: float = 0.0,
    zoom: float = 0.0,
    path: Optional[Path] = None,
) -> None:
    """Begin a continuous PTZ move; arm a safety auto-stop in case stop is lost."""
    cfg = get_camera_config(camera_id, path)
    conn = await _get_conn(cfg)
    if conn.ptz is None:
        raise CameraCommandError(f"camera {camera_id} has no PTZ")
    move = conn.ptz.create_type("ContinuousMove")
    move.ProfileToken = conn.profile_token
    move.Velocity = {"PanTilt": {"x": pan, "y": tilt}, "Zoom": {"x": zoom}}
    try:
        await conn.ptz.ContinuousMove(move)
    except Exception as exc:  # noqa: BLE001
        raise CameraCommandError(f"camera {camera_id} PTZ move failed: {exc}") from exc
    old = _ptz_watchdogs.pop(camera_id, None)
    if old is not None:
        old.cancel()
    _ptz_watchdogs[camera_id] = asyncio.create_task(_ptz_safety_stop(camera_id))


async def ptz_stop(camera_id: str, path: Optional[Path] = None) -> None:
    """Stop any in-flight PTZ move."""
    watchdog = _ptz_watchdogs.pop(camera_id, None)
    if watchdog is not None:
        watchdog.cancel()
    cfg = get_camera_config(camera_id, path)
    conn = await _get_conn(cfg)
    if conn.ptz is None:
        return
    stop = conn.ptz.create_type("Stop")
    stop.ProfileToken = conn.profile_token
    stop.PanTilt = True
    stop.Zoom = True
    try:
        await conn.ptz.Stop(stop)
    except Exception as exc:  # noqa: BLE001
        raise CameraCommandError(f"camera {camera_id} PTZ stop failed: {exc}") from exc


# --------------------------------------------------------------------------- #
# Recording (server-side ffmpeg clip)                                         #
# --------------------------------------------------------------------------- #
_recordings: Dict[str, asyncio.subprocess.Process] = {}


def is_recording(camera_id: str) -> bool:
    proc = _recordings.get(camera_id)
    return proc is not None and proc.returncode is None


async def start_record(camera_id: str, path: Optional[Path] = None) -> str:
    """Start recording the main stream to ``webapp/camera_captures/``. Returns the filename."""
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
