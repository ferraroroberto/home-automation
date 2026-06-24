r"""
Camera spike — validate an open WiFi RTSP/ONVIF camera end-to-end (issue #89)
=============================================================================
Throwaway proof-of-concept that exercises a single in-place camera (the Reolink
**E1 Outdoor Pro**) over the *generic, vendor-neutral* path the whole fleet is
betting on for "open & future-proof":

* **ONVIF** — WS-Discovery probe, device info, media profiles, and PTZ — proves
  the camera speaks the open standard with no vendor cloud/hub in the loop.
* **RTSP** — main + substream URIs pulled *from ONVIF* (not hard-coded), then a
  still snapshot and a short clip grabbed with **ffmpeg** straight off the RTSP
  stream — mirrors how the rest of the fleet (HVAC, SMA) is accessed from Python.

This is **not** the product. The issue scopes the real ``src/camera_client.py`` +
webapp tile as the *follow-up* once this spike says go; deliberately self-contained
(reads ``.env`` directly, no ``src`` imports) so it can be deleted wholesale.

Prerequisite: enable **ONVIF + RTSP** on the camera first (Reolink app:
Settings > Network > Advanced > Server Settings) and create the on-device
"device account" — the spike authenticates with that, never the cloud login.

Run from the project root with the venv interpreter::

    & .\.venv\Scripts\python.exe -m spike.camera_spike                 # Windows
    & .\.venv\Scripts\python.exe -m spike.camera_spike --no-ptz         # skip PTZ
    & .\.venv\Scripts\python.exe -m spike.camera_spike --clip-seconds 8
    ./.venv/bin/python -m spike.camera_spike                            # POSIX

Config comes from ``.env`` (gitignored — public repo)::

    CAMERA_HOST / CAMERA_USERNAME / CAMERA_PASSWORD
    CAMERA_ONVIF_PORT (default 8000) / CAMERA_RTSP_PORT (default 554)

Captures land in gitignored ``webapp/camera_captures/`` (an outdoor frame can
reveal the location). Credentials are never printed — the password is masked in
any RTSP URL shown on screen.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlsplit, urlunsplit

from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("camera_spike")
# Silence zeep/onvif transport chatter unless something goes wrong.
logging.getLogger("zeep").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

# Captures are gitignored — an outdoor frame can reveal the home/location.
CAPTURE_DIR = Path(__file__).resolve().parent.parent / "webapp" / "camera_captures"
# ONVIF "NetworkVideoTransmitter" device type — the WS-Discovery filter that
# matches cameras (vs. NVRs/displays) on the LAN.
_ONVIF_NVT_NS = "http://www.onvif.org/ver10/network/wsdl"


@dataclass
class StepResult:
    """One acceptance step: pass/fail plus a one-line human note."""

    name: str
    ok: bool = False
    note: str = ""


@dataclass
class CameraConfig:
    host: str
    username: str
    password: str
    onvif_port: int = 8000
    rtsp_port: int = 554


def load_config() -> CameraConfig:
    """Read CAMERA_* from ``.env`` / the environment. Raises if host is unset."""
    load_dotenv()
    host = os.getenv("CAMERA_HOST", "").strip()
    if not host or host.endswith(".x"):
        raise RuntimeError(
            "CAMERA_HOST is not set in .env — copy the CAMERA_* block from "
            ".env.example and fill in the camera's LAN IP + device account."
        )
    return CameraConfig(
        host=host,
        username=os.getenv("CAMERA_USERNAME", "admin").strip(),
        password=os.getenv("CAMERA_PASSWORD", "").strip(),
        onvif_port=int(os.getenv("CAMERA_ONVIF_PORT", "8000") or "8000"),
        rtsp_port=int(os.getenv("CAMERA_RTSP_PORT", "554") or "554"),
    )


def _inject_credentials(rtsp_uri: str, user: str, password: str) -> str:
    """Put URL-encoded creds into an RTSP URI returned by ONVIF (which omits them)."""
    parts = urlsplit(rtsp_uri)
    host = parts.hostname or ""
    netloc = f"{quote(user)}:{quote(password)}@{host}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _mask(rtsp_uri: str) -> str:
    """Mask the password in an RTSP URI so it's safe to print to the terminal."""
    parts = urlsplit(rtsp_uri)
    if "@" not in (parts.netloc or ""):
        return rtsp_uri
    user = parts.username or ""
    host = parts.hostname or ""
    netloc = f"{user}:***@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


# --------------------------------------------------------------------------- #
# 1. ONVIF WS-Discovery                                                       #
# --------------------------------------------------------------------------- #
def discover_onvif(timeout: float = 4.0) -> StepResult:
    """Probe the LAN for ONVIF NetworkVideoTransmitter (camera) services."""
    res = StepResult("ONVIF discovery")
    try:
        from wsdiscovery import QName
        from wsdiscovery.discovery import ThreadedWSDiscovery
    except Exception as exc:  # pragma: no cover - import guard
        res.note = f"wsdiscovery import failed: {exc}"
        return res

    wsd = ThreadedWSDiscovery()
    try:
        wsd.start()
        nvt = QName(_ONVIF_NVT_NS, "NetworkVideoTransmitter")
        services = wsd.searchServices(types=[nvt], timeout=int(timeout))
        xaddrs = [x for s in services for x in s.getXAddrs()]
        res.ok = bool(xaddrs)
        if xaddrs:
            res.note = f"{len(services)} camera(s): " + ", ".join(xaddrs)
        else:
            res.note = (
                "no ONVIF cameras answered the multicast probe "
                "(some networks block WS-Discovery; ONVIF-by-IP below still works)"
            )
    except Exception as exc:
        res.note = f"discovery error: {exc}"
    finally:
        try:
            wsd.stop()
        except Exception:
            pass
    return res


# --------------------------------------------------------------------------- #
# 2. ONVIF connect → device info → stream URIs → PTZ                          #
# --------------------------------------------------------------------------- #
@dataclass
class OnvifResult:
    info: StepResult = field(default_factory=lambda: StepResult("ONVIF device info"))
    streams: StepResult = field(default_factory=lambda: StepResult("RTSP stream URIs"))
    ptz: StepResult = field(default_factory=lambda: StepResult("PTZ control (ONVIF)"))
    # Profile token → RTSP URI (credentials NOT yet injected), main first.
    stream_uris: list[tuple[str, str]] = field(default_factory=list)


async def _stream_uri(media, token: str) -> str:
    """GetStreamUri for one media profile as an RTSP-over-RTP unicast URL."""
    req = media.create_type("GetStreamUri")
    req.ProfileToken = token
    req.StreamSetup = {
        "Stream": "RTP-Unicast",
        "Transport": {"Protocol": "RTSP"},
    }
    resp = await media.GetStreamUri(req)
    return resp.Uri


async def _exercise_ptz(cam, token: str, result: StepResult) -> None:
    """Nudge pan/tilt (+zoom) via ONVIF ContinuousMove, then Stop. Best-effort."""
    try:
        ptz = await cam.create_ptz_service()
        # A camera with no PTZ node simply has no configurations — report that,
        # don't treat it as a failure of the open path.
        configs = await ptz.GetConfigurations()
        if not configs:
            result.note = "no PTZ configuration (fixed camera?)"
            return
        move = ptz.create_type("ContinuousMove")
        move.ProfileToken = token
        move.Velocity = {
            "PanTilt": {"x": 0.4, "y": 0.0},
            "Zoom": {"x": 0.0},
        }
        await ptz.ContinuousMove(move)
        await asyncio.sleep(1.2)
        stop = ptz.create_type("Stop")
        stop.ProfileToken = token
        stop.PanTilt = True
        stop.Zoom = True
        await ptz.Stop(stop)
        # Zoom in briefly (E1 Outdoor Pro has 3x optical); harmless if unsupported.
        zoom = ptz.create_type("ContinuousMove")
        zoom.ProfileToken = token
        zoom.Velocity = {"PanTilt": {"x": 0.0, "y": 0.0}, "Zoom": {"x": 0.4}}
        await ptz.ContinuousMove(zoom)
        await asyncio.sleep(1.0)
        await ptz.Stop(stop)
        result.ok = True
        result.note = "pan + zoom move/stop accepted"
    except Exception as exc:
        result.note = f"PTZ error: {exc}"


async def probe_onvif(cfg: CameraConfig, do_ptz: bool) -> OnvifResult:
    """Connect over ONVIF and pull device info, stream URIs, and exercise PTZ."""
    out = OnvifResult()
    try:
        from onvif import ONVIFCamera
    except Exception as exc:  # pragma: no cover - import guard
        out.info.note = f"onvif import failed: {exc}"
        return out

    cam = ONVIFCamera(
        cfg.host,
        cfg.onvif_port,
        cfg.username,
        cfg.password,
        # adjust_time guards against camera clock skew breaking WS-Security auth
        # (common on Reolink) — the HA integration does the same.
        adjust_time=True,
    )
    try:
        await cam.update_xaddrs()
        devmgmt = await cam.create_devicemgmt_service()
        info = await devmgmt.GetDeviceInformation()
        out.info.ok = True
        out.info.note = (
            f"{info.Manufacturer} {info.Model} fw={info.FirmwareVersion}"
        )

        media = await cam.create_media_service()
        profiles = await media.GetProfiles()
        for prof in profiles:
            try:
                uri = await _stream_uri(media, prof.token)
                out.stream_uris.append((prof.token, uri))
            except Exception as exc:
                logger.warning("stream URI for profile %s failed: %s", prof.token, exc)
        out.streams.ok = bool(out.stream_uris)
        out.streams.note = (
            f"{len(out.stream_uris)} profile(s): "
            + " | ".join(_mask(u) for _, u in out.stream_uris)
            if out.stream_uris
            else "GetStreamUri returned nothing"
        )

        if do_ptz and profiles:
            await _exercise_ptz(cam, profiles[0].token, out.ptz)
        elif not do_ptz:
            out.ptz.note = "skipped (--no-ptz)"
    except Exception as exc:
        msg = str(exc)
        hint = ""
        if "401" in msg or "auth" in msg.lower() or "NotAuthorized" in msg:
            hint = " — check CAMERA_USERNAME/PASSWORD (the device account)"
        elif "refused" in msg.lower() or "timed out" in msg.lower() or "timeout" in msg.lower():
            hint = " — is ONVIF enabled on the camera and is the port right?"
        if not out.info.note:
            out.info.note = f"ONVIF connect failed: {msg}{hint}"
    finally:
        try:
            await cam.close()
        except Exception:
            pass
    return out


# --------------------------------------------------------------------------- #
# 3. ffmpeg snapshot + short clip off the RTSP stream                         #
# --------------------------------------------------------------------------- #
def _run_ffmpeg(args: list[str], timeout: float) -> tuple[bool, str]:
    """Run ffmpeg, returning (ok, last-stderr-line)."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, "ffmpeg not found on PATH"
    except subprocess.TimeoutExpired:
        return False, "ffmpeg timed out"
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        return False, tail[-1] if tail else f"ffmpeg exit {proc.returncode}"
    return True, ""


def grab_snapshot(rtsp_with_creds: str) -> StepResult:
    """Pull a single still frame off the RTSP stream into a .jpg."""
    res = StepResult("ffmpeg snapshot")
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CAPTURE_DIR / "snapshot.jpg"
    ok, err = _run_ffmpeg(
        [
            "-rtsp_transport", "tcp",
            "-i", rtsp_with_creds,
            "-frames:v", "1",
            "-q:v", "2",
            str(out_path),
        ],
        timeout=30,
    )
    if ok and out_path.exists() and out_path.stat().st_size > 0:
        res.ok = True
        res.note = f"{out_path.name} ({out_path.stat().st_size // 1024} KB)"
    else:
        res.note = err or "no frame written"
    return res


def grab_clip(rtsp_with_creds: str, seconds: int) -> StepResult:
    """Record a short clip off the RTSP stream into a .mp4 (stream-copy)."""
    res = StepResult(f"ffmpeg {seconds}s clip")
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CAPTURE_DIR / "clip.mp4"
    ok, err = _run_ffmpeg(
        [
            "-rtsp_transport", "tcp",
            "-i", rtsp_with_creds,
            "-t", str(seconds),
            "-c", "copy",
            str(out_path),
        ],
        timeout=seconds + 25,
    )
    if ok and out_path.exists() and out_path.stat().st_size > 0:
        res.ok = True
        res.note = f"{out_path.name} ({out_path.stat().st_size // 1024} KB)"
    else:
        res.note = err or "no clip written"
    return res


# --------------------------------------------------------------------------- #
# Report                                                                      #
# --------------------------------------------------------------------------- #
def _print_report(steps: list[StepResult]) -> bool:
    print("\n=== Camera spike results (issue #89) ===")
    all_ok = True
    for s in steps:
        mark = "PASS" if s.ok else "FAIL"
        if not s.ok:
            all_ok = False
        print(f"  [{mark}] {s.name}: {s.note}")
    print(
        "\nManual acceptance items still pending (not software-checkable):\n"
        "  [ ] no-internet / IoT-VLAN proof (no cloud dependency)\n"
        "  [ ] local microSD recording with the cloud account unused\n"
        "  [ ] outdoor mount: waterproofing, day/night image, WiFi at the mount\n"
        "  [ ] go/no-go decision recorded in docs/camera-spike.md"
    )
    return all_ok


async def _amain(args: argparse.Namespace) -> int:
    cfg = load_config()
    print(f"Camera: {cfg.host}  (ONVIF :{cfg.onvif_port}, RTSP :{cfg.rtsp_port})")

    steps: list[StepResult] = []

    if not args.no_discovery:
        steps.append(discover_onvif())

    onvif = await probe_onvif(cfg, do_ptz=not args.no_ptz)
    steps.append(onvif.info)
    steps.append(onvif.streams)

    # Grab off the substream if present (lighter), else the main/only stream.
    if onvif.stream_uris and not args.no_capture:
        target = onvif.stream_uris[-1][1] if len(onvif.stream_uris) > 1 else onvif.stream_uris[0][1]
        with_creds = _inject_credentials(target, cfg.username, cfg.password)
        print(f"Capturing from: {_mask(with_creds)}")
        steps.append(grab_snapshot(with_creds))
        steps.append(grab_clip(with_creds, args.clip_seconds))
    elif not args.no_capture:
        skip = StepResult("ffmpeg capture", note="skipped — no RTSP URI from ONVIF")
        steps.append(skip)

    if not args.no_ptz:
        steps.append(onvif.ptz)

    all_ok = _print_report(steps)
    return 0 if all_ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Camera RTSP/ONVIF spike (issue #89)")
    parser.add_argument("--no-discovery", action="store_true", help="skip WS-Discovery probe")
    parser.add_argument("--no-ptz", action="store_true", help="skip PTZ control")
    parser.add_argument("--no-capture", action="store_true", help="skip ffmpeg snapshot/clip")
    parser.add_argument("--clip-seconds", type=int, default=5, help="clip length (default 5)")
    args = parser.parse_args()
    try:
        return asyncio.run(_amain(args))
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
