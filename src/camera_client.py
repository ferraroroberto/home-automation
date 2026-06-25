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
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "cameras.json"
CAPTURE_DIR = Path(__file__).resolve().parent.parent / "webapp" / "camera_captures"
# The most recent snapshot per camera, persisted so the list can show a
# last-known frame without re-hitting the camera (issue #190). Lives under the
# already-gitignored captures dir.
LAST_SNAPSHOT_DIR = CAPTURE_DIR / "last"

# A continuous PTZ move is auto-stopped after this long even if the client never
# sends an explicit stop (a dropped pointerup on mobile must not run the motor
# into its hard stop).
_PTZ_SAFETY_STOP_S = 4.0
# A fixed-step nudge is a short continuous move that self-stops after this long —
# one click moves exactly one increment, instead of the press-and-hold that
# overshoots (issue #190). The velocity magnitude is chosen by the caller.
_PTZ_STEP_DURATION_S = 0.25
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
    # Optional stable MAC. When set, a camera unreachable at its configured host
    # is rediscovered by MAC from the network table and the recovered IP is
    # persisted back to cameras.json — the same self-heal the plugs/AP use (#190).
    mac: Optional[str] = None


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
    # Which precise-PTZ paths this camera actually supports (issue #190). The UI
    # renders only the controls a camera can honour, so hardware that lacks
    # absolute-move / native presets degrades cleanly instead of faulting.
    ptz_presets: bool = False  # native ONVIF GetPresets / SetPreset / GotoPreset
    ptz_absolute: bool = False  # AbsoluteMove + GetStatus (manual coordinates)
    ptz_relative: bool = False  # RelativeMove
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
                mac=str(entry["mac"]) if entry.get("mac") else None,
            )
        )
    return cameras


def get_camera_config(camera_id: str, path: Optional[Path] = None) -> CameraConfig:
    """Look up one camera by id, or raise if it isn't configured."""
    for cam in load_cameras(path):
        if cam.id == camera_id:
            return cam
    raise CameraCommandError(f"camera {camera_id} is not configured")


def _persist_camera_host(
    camera_id: str, new_host: str, path: Optional[Path] = None
) -> None:
    """Atomically rewrite one camera's host in cameras.json, preserving all else."""
    target = Path(path) if path is not None else _CONFIG_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    items = raw.get("cameras", raw) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return
    changed = False
    for entry in items:
        if isinstance(entry, dict) and str(entry.get("id") or entry.get("host")) == camera_id:
            entry["host"] = new_host
            changed = True
    if not changed:
        return
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)
    logger.info("💾 camera %s host updated to %s in %s", camera_id, new_host, target.name)


async def _recover_host(cfg: CameraConfig) -> Optional[CameraConfig]:
    """Rediscover a camera by MAC when its configured host is unreachable.

    Returns a config pointing at the recovered IP (persisted to disk), or None
    when there's no MAC, no network table, or the address hasn't changed.
    """
    if not cfg.mac:
        return None
    try:
        from src.network_client import resolve_ip_by_mac

        new_ip = await resolve_ip_by_mac(cfg.mac)
    except Exception as exc:  # noqa: BLE001 — recovery is best-effort, never fatal
        logger.info("ℹ️ camera %s MAC rediscovery failed: %s", cfg.id, exc)
        return None
    if not new_ip or new_ip == cfg.host:
        return None
    logger.info("ℹ️ camera %s rediscovered at %s (was %s)", cfg.id, new_ip, cfg.host)
    _persist_camera_host(cfg.id, new_ip)
    return replace(cfg, host=new_ip)


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
    ptz_presets: bool = False
    ptz_absolute: bool = False
    ptz_relative: bool = False
    # Absolute-move coordinate bounds (min, max) per axis, read from the PTZ
    # node's supported spaces — drives input validation + UI hints (issue #190).
    pan_range: Optional[Tuple[float, float]] = None
    tilt_range: Optional[Tuple[float, float]] = None
    zoom_range: Optional[Tuple[float, float]] = None


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


def _axis_range(space_list: object) -> Optional[Tuple[float, float]]:
    """Pull (min, max) out of the first entry of a supported-PTZ-space list."""
    try:
        rng = space_list[0].XRange  # type: ignore[index]
        return (float(rng.Min), float(rng.Max))
    except Exception:  # noqa: BLE001 — any malformed/absent space → no range
        return None


async def _probe_ptz(ptz: object, token: str) -> Dict[str, object]:
    """Detect which precise-PTZ operations a camera supports.

    Continuous move is assumed (the d-pad already relies on it). Absolute /
    relative support and the coordinate bounds come from the PTZ node's
    ``SupportedPTZSpaces``; native preset support is probed by actually calling
    ``GetPresets`` so a node that advertises presets but rejects the call is
    treated as unsupported.
    """
    caps: Dict[str, object] = {
        "presets": False, "absolute": False, "relative": False,
        "pan_range": None, "tilt_range": None, "zoom_range": None,
    }
    try:
        nodes = await ptz.GetNodes()
    except Exception as exc:  # noqa: BLE001
        logger.info("ℹ️ PTZ GetNodes unavailable: %s", exc)
        nodes = []
    if nodes:
        spaces = getattr(nodes[0], "SupportedPTZSpaces", None)
        if spaces is not None:
            abs_pt = getattr(spaces, "AbsolutePanTiltPositionSpace", None)
            if abs_pt:
                caps["absolute"] = True
                caps["pan_range"] = _axis_range(abs_pt)
                tilt = abs_pt[0]
                try:
                    caps["tilt_range"] = (float(tilt.YRange.Min), float(tilt.YRange.Max))
                except Exception:  # noqa: BLE001
                    caps["tilt_range"] = None
            abs_zoom = getattr(spaces, "AbsoluteZoomPositionSpace", None)
            if abs_zoom:
                caps["zoom_range"] = _axis_range(abs_zoom)
            if getattr(spaces, "RelativePanTiltTranslationSpace", None):
                caps["relative"] = True
    try:
        await ptz.GetPresets({"ProfileToken": token})
        caps["presets"] = True
    except Exception as exc:  # noqa: BLE001
        logger.info("ℹ️ PTZ GetPresets unsupported: %s", exc)
    return caps


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
    caps: Dict[str, object] = {}
    try:
        ptz = await cam.create_ptz_service()
        ptz_capable = bool(await ptz.GetConfigurations())
        if ptz_capable:
            caps = await _probe_ptz(ptz, token)
    except Exception as exc:  # noqa: BLE001
        logger.info("ℹ️ camera %s reports no PTZ: %s", cfg.id, exc)
        ptz = None
        ptz_capable = False

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
        ptz_presets=bool(caps.get("presets")),
        ptz_absolute=bool(caps.get("absolute")),
        ptz_relative=bool(caps.get("relative")),
        pan_range=caps.get("pan_range"),  # type: ignore[arg-type]
        tilt_range=caps.get("tilt_range"),  # type: ignore[arg-type]
        zoom_range=caps.get("zoom_range"),  # type: ignore[arg-type]
    )


async def _get_conn(cfg: CameraConfig) -> _CameraConn:
    """Return the cached ONVIF connection for a camera, connecting on first use.

    If the configured host is unreachable, try a one-shot MAC rediscovery (#190)
    and reconnect at the recovered address before giving up.
    """
    async with _lock_for(cfg.id):
        conn = _conns.get(cfg.id)
        if conn is not None:
            return conn
        try:
            conn = await _connect(cfg)
        except Exception:  # noqa: BLE001
            recovered = await _recover_host(cfg)
            if recovered is None:
                raise
            conn = await _connect(recovered)  # a still-failing reconnect propagates
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
            ptz_presets=conn.ptz_presets,
            ptz_absolute=conn.ptz_absolute,
            ptz_relative=conn.ptz_relative,
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
    LAST_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    target = last_snapshot_path(camera_id)
    tmp = target.with_suffix(".jpg.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except OSError as exc:  # noqa: BLE001 — persistence is best-effort, never fatal
        logger.warning("⚠️ camera %s could not persist last snapshot: %s", camera_id, exc)


async def snapshot(camera_id: str, path: Optional[Path] = None) -> bytes:
    """Grab a single fresh JPEG off the substream via ffmpeg, returned as bytes.

    The grabbed frame is also persisted as the camera's last-known snapshot so
    the list can show it without re-hitting the camera (issue #190).
    """
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


async def ptz_step(
    camera_id: str,
    *,
    pan: float = 0.0,
    tilt: float = 0.0,
    zoom: float = 0.0,
    path: Optional[Path] = None,
) -> None:
    """Nudge the camera by exactly one fixed step, then stop.

    A short low-speed ``ContinuousMove`` that self-stops — one call = one
    increment, no overshoot. Works on any camera that drives the d-pad, so it
    is the universal precise-control path (issue #190).
    """
    cfg = get_camera_config(camera_id, path)
    conn = await _get_conn(cfg)
    if conn.ptz is None:
        raise CameraCommandError(f"camera {camera_id} has no PTZ")
    move = conn.ptz.create_type("ContinuousMove")
    move.ProfileToken = conn.profile_token
    move.Velocity = {"PanTilt": {"x": pan, "y": tilt}, "Zoom": {"x": zoom}}
    # Cancel any in-flight press-and-hold watchdog so it can't stop us early.
    old = _ptz_watchdogs.pop(camera_id, None)
    if old is not None:
        old.cancel()
    try:
        await conn.ptz.ContinuousMove(move)
        await asyncio.sleep(_PTZ_STEP_DURATION_S)
    except Exception as exc:  # noqa: BLE001
        raise CameraCommandError(f"camera {camera_id} PTZ step failed: {exc}") from exc
    finally:
        await ptz_stop(camera_id, path)


async def get_ptz_status(camera_id: str, path: Optional[Path] = None) -> Dict[str, object]:
    """Return the camera's live pan/tilt/zoom plus the absolute-move bounds.

    Values are None when the camera doesn't report a position (no GetStatus /
    no absolute support); the bounds drive the manual-coordinate UI hints.
    """
    cfg = get_camera_config(camera_id, path)
    conn = await _get_conn(cfg)
    out: Dict[str, object] = {
        "pan": None, "tilt": None, "zoom": None,
        "absolute": conn.ptz_absolute,
        "pan_range": list(conn.pan_range) if conn.pan_range else None,
        "tilt_range": list(conn.tilt_range) if conn.tilt_range else None,
        "zoom_range": list(conn.zoom_range) if conn.zoom_range else None,
    }
    if conn.ptz is None:
        return out
    try:
        status = await conn.ptz.GetStatus({"ProfileToken": conn.profile_token})
        pos = getattr(status, "Position", None)
        pan_tilt = getattr(pos, "PanTilt", None)
        zoom = getattr(pos, "Zoom", None)
        if pan_tilt is not None:
            out["pan"] = float(pan_tilt.x)
            out["tilt"] = float(pan_tilt.y)
        if zoom is not None:
            out["zoom"] = float(zoom.x)
    except Exception as exc:  # noqa: BLE001
        logger.info("ℹ️ camera %s GetStatus unavailable: %s", camera_id, exc)
    return out


async def ptz_absolute(
    camera_id: str,
    *,
    pan: float,
    tilt: float,
    zoom: Optional[float] = None,
    path: Optional[Path] = None,
) -> None:
    """Move to an absolute pan/tilt(/zoom) position via ONVIF ``AbsoluteMove``."""
    cfg = get_camera_config(camera_id, path)
    conn = await _get_conn(cfg)
    if conn.ptz is None or not conn.ptz_absolute:
        raise CameraCommandError(f"camera {camera_id} has no absolute-move support")
    move = conn.ptz.create_type("AbsoluteMove")
    move.ProfileToken = conn.profile_token
    position: Dict[str, object] = {"PanTilt": {"x": pan, "y": tilt}}
    if zoom is not None:
        position["Zoom"] = {"x": zoom}
    move.Position = position
    try:
        await conn.ptz.AbsoluteMove(move)
    except Exception as exc:  # noqa: BLE001
        raise CameraCommandError(f"camera {camera_id} absolute move failed: {exc}") from exc


# --------------------------------------------------------------------------- #
# PTZ presets — native ONVIF, with a local coordinate-store fallback           #
# --------------------------------------------------------------------------- #
async def list_presets(camera_id: str, path: Optional[Path] = None) -> List[Dict[str, str]]:
    """List saved positions as ``[{token, name}]`` (native presets or local store)."""
    cfg = get_camera_config(camera_id, path)
    conn = await _get_conn(cfg)
    if conn.ptz is not None and conn.ptz_presets:
        try:
            presets = await conn.ptz.GetPresets({"ProfileToken": conn.profile_token})
        except Exception as exc:  # noqa: BLE001
            raise CameraCommandError(f"camera {camera_id} list presets failed: {exc}") from exc
        out: List[Dict[str, str]] = []
        for p in presets or []:
            token = getattr(p, "token", None) or getattr(p, "_token", None)
            if token is None:
                continue
            out.append({"token": str(token), "name": str(getattr(p, "Name", "") or token)})
        return out
    from src.camera_presets import list_local_presets

    return [{"token": p["token"], "name": p["name"]} for p in list_local_presets(camera_id)]


async def set_preset(
    camera_id: str, name: str, path: Optional[Path] = None
) -> Dict[str, str]:
    """Save the current position under ``name``; returns the new ``{token, name}``."""
    cfg = get_camera_config(camera_id, path)
    conn = await _get_conn(cfg)
    if conn.ptz is None:
        raise CameraCommandError(f"camera {camera_id} has no PTZ")
    if conn.ptz_presets:
        req = conn.ptz.create_type("SetPreset")
        req.ProfileToken = conn.profile_token
        req.PresetName = name
        try:
            resp = await conn.ptz.SetPreset(req)
        except Exception as exc:  # noqa: BLE001
            raise CameraCommandError(f"camera {camera_id} save preset failed: {exc}") from exc
        token = getattr(resp, "PresetToken", None) or resp
        return {"token": str(token), "name": name}
    # Local fallback: remember the current absolute coordinates for recall.
    if not conn.ptz_absolute:
        raise CameraCommandError(f"camera {camera_id} cannot save presets")
    from src.camera_presets import add_local_preset

    status = await get_ptz_status(camera_id, path)
    return add_local_preset(
        camera_id, name, status.get("pan"), status.get("tilt"), status.get("zoom")
    )


async def goto_preset(camera_id: str, token: str, path: Optional[Path] = None) -> None:
    """Recall a saved position by token (native preset or local coordinates)."""
    cfg = get_camera_config(camera_id, path)
    conn = await _get_conn(cfg)
    if conn.ptz is None:
        raise CameraCommandError(f"camera {camera_id} has no PTZ")
    if conn.ptz_presets:
        req = conn.ptz.create_type("GotoPreset")
        req.ProfileToken = conn.profile_token
        req.PresetToken = token
        try:
            await conn.ptz.GotoPreset(req)
        except Exception as exc:  # noqa: BLE001
            raise CameraCommandError(f"camera {camera_id} goto preset failed: {exc}") from exc
        return
    from src.camera_presets import get_local_preset

    preset = get_local_preset(camera_id, token)
    if preset is None:
        raise CameraCommandError(f"camera {camera_id} has no preset {token}")
    await ptz_absolute(
        camera_id, pan=preset["pan"], tilt=preset["tilt"], zoom=preset.get("zoom"), path=path
    )


async def remove_preset(camera_id: str, token: str, path: Optional[Path] = None) -> None:
    """Delete a saved position by token (native preset or local store)."""
    cfg = get_camera_config(camera_id, path)
    conn = await _get_conn(cfg)
    if conn.ptz is not None and conn.ptz_presets:
        req = conn.ptz.create_type("RemovePreset")
        req.ProfileToken = conn.profile_token
        req.PresetToken = token
        try:
            await conn.ptz.RemovePreset(req)
        except Exception as exc:  # noqa: BLE001
            raise CameraCommandError(f"camera {camera_id} remove preset failed: {exc}") from exc
        return
    from src.camera_presets import remove_local_preset

    remove_local_preset(camera_id, token)


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
