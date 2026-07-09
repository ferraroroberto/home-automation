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
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Coroutine, Dict, Optional

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
# Proactor bridge (issue #399)                                                #
# --------------------------------------------------------------------------- #
# The webapp's own uvicorn loop is the *selector* loop (issue #397 — Windows'
# proactor loop kills its listening socket on any aborted client connection).
# But every ffmpeg call below is ``asyncio.create_subprocess_exec``, and
# Windows only implements asyncio subprocess transports on the *proactor*
# loop — called directly on the selector loop it raises a bare
# ``NotImplementedError``. So every ffmpeg subprocess call is dispatched to a
# lazily-started background thread that runs its own persistent proactor loop,
# and awaited from the caller's (selector) loop via ``_run_on_proactor``.
_proactor_loop: Optional[asyncio.AbstractEventLoop] = None
_proactor_lock = threading.Lock()


def _ensure_proactor_loop() -> asyncio.AbstractEventLoop:
    """Start (once) the background thread + its persistent proactor loop."""
    global _proactor_loop
    with _proactor_lock:
        if _proactor_loop is not None:
            return _proactor_loop
        ready = threading.Event()
        holder: Dict[str, asyncio.AbstractEventLoop] = {}

        def _run() -> None:
            loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            holder["loop"] = loop
            ready.set()
            loop.run_forever()

        threading.Thread(target=_run, name="camera-ffmpeg-proactor", daemon=True).start()
        ready.wait()
        _proactor_loop = holder["loop"]
        return _proactor_loop


def _run_on_proactor(coro_factory: Callable[[], Coroutine[Any, Any, Any]]) -> asyncio.Future:
    """Run ``coro_factory()`` on the persistent proactor loop; await from any loop."""
    loop = _ensure_proactor_loop()
    return asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro_factory(), loop))


def _safe_kill(proc: asyncio.subprocess.Process) -> None:
    try:
        proc.kill()
    except ProcessLookupError:
        pass


# Sentinel put on the frame queue by the proactor-side pump to signal EOF —
# distinct from any real frame (bytes), so ``is`` comparison is unambiguous.
_STREAM_DONE = object()


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

    async def _grab() -> tuple[Optional[int], bytes, bytes]:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
            "-i", url, "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), _FFMPEG_SNAPSHOT_TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        return proc.returncode, out, err

    try:
        returncode, out, err = await _run_on_proactor(_grab)
    except asyncio.TimeoutError as exc:
        raise CameraCommandError(f"camera {camera_id} snapshot timed out") from exc
    if returncode != 0 or not out:
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

    # ffmpeg + the read loop both run on the proactor thread (issue #399); a
    # bounded, drop-oldest queue bridges frames back to this (selector-loop)
    # generator without blocking the proactor thread on a full queue.
    frame_queue: queue.Queue[object] = queue.Queue(maxsize=4)
    proc_holder: Dict[str, asyncio.subprocess.Process] = {}

    async def _pump() -> None:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
            "-i", url, "-r", str(fps), "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        proc_holder["proc"] = proc
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
                        try:
                            frame_queue.put_nowait(frame)
                        except queue.Full:
                            try:
                                frame_queue.get_nowait()  # drop the oldest, keep the feed live
                            except queue.Empty:
                                pass
                            frame_queue.put_nowait(frame)
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
            frame_queue.put(_STREAM_DONE)

    loop = _ensure_proactor_loop()
    pump_future = asyncio.run_coroutine_threadsafe(_pump(), loop)
    try:
        while True:
            frame = await asyncio.get_running_loop().run_in_executor(None, frame_queue.get)
            if frame is _STREAM_DONE:
                break
            yield frame
    finally:
        # Consumer disconnected (generator closed) — kill ffmpeg on its own
        # loop, then wait for the pump to actually finish so its exceptions
        # (if any) surface here instead of as an unretrieved-future warning.
        proc = proc_holder.get("proc")
        if proc is not None:
            loop.call_soon_threadsafe(_safe_kill, proc)

        def _drain() -> None:
            try:
                pump_future.result(timeout=5.0)
            except Exception:  # noqa: BLE001 — best-effort cleanup, never fatal
                pass

        await asyncio.get_running_loop().run_in_executor(None, _drain)


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

    async def _start() -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
            "-i", url, "-c", "copy", str(out_path),
            stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )

    # The Process object below is bound to the proactor loop that created it
    # (issue #399); every later operation on it (stop_record) must keep
    # running on that same loop via _run_on_proactor, never awaited directly.
    proc = await _run_on_proactor(_start)
    _recordings[camera_id] = proc
    logger.info("⏺️ camera %s recording to %s", camera_id, out_path.name)
    return out_path.name


async def stop_record(camera_id: str) -> None:
    """Finalize an in-progress recording (graceful ffmpeg quit)."""
    from src.camera_client import CameraCommandError

    proc = _recordings.pop(camera_id, None)
    if proc is None or proc.returncode is not None:
        raise CameraCommandError(f"camera {camera_id} is not recording")

    async def _stop() -> None:
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

    await _run_on_proactor(_stop)
    logger.info("⏹️ camera %s recording stopped", camera_id)
