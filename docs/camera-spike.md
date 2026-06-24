# Cameras — open WiFi RTSP/ONVIF spike (issue #89)

Proof-of-concept for replacing the YI Home / Kami camera fleet with **open,
self-hosted** cameras, validated end-to-end from Python before committing to the
full 7-camera buy. This is **path 2** from the #85 feasibility study (the
firmware-flash path was rejected): a hardware swap to cameras that speak
**standalone RTSP + ONVIF on the device itself**, with no vendor cloud, hub, or
subscription in the loop.

The spike validates **one external unit** end-to-end. Building the actual fleet
integration (a `src/camera_client.py` + a webapp tile) is the **follow-up** once
this spike records a go.

## Hardware under test

**Reolink E1 Outdoor Pro** — mains-powered WiFi-6, weatherproof, 355° pan / 50°
tilt / 3× optical zoom, on-device RTSP + ONVIF, no subscription. Picked as the
best-in-class *open* WiFi-mains camera for 2026 (see the issue for the full
rationale and the battery-camera lock-in trap to avoid). Brand-agnostic in
principle — ONVIF keeps any compliant camera swappable — but Reolink has the
strongest local story.

## What the spike does

A deliberately throwaway script, `spike/camera_spike.py`, self-contained (reads
`.env` directly, no `src` imports) so it can be deleted wholesale once the real
client is built. It exercises the **generic vendor-neutral path**:

1. **ONVIF WS-Discovery** — multicast probe for `NetworkVideoTransmitter` services.
2. **ONVIF device info + media profiles** — connect by IP, pull manufacturer /
   model / firmware and the media profiles.
3. **RTSP stream URIs** — `GetStreamUri` for the main + substream, *from ONVIF*
   (not hard-coded), proving the standard discovery path.
4. **ffmpeg capture** — a still snapshot and a short clip grabbed straight off
   the RTSP stream, mirroring how the rest of the fleet is read from Python.
5. **PTZ** — pan/tilt (and zoom) via ONVIF `ContinuousMove`/`Stop`.

Run it from the project root with the venv interpreter:

```powershell
& .\.venv\Scripts\python.exe -m spike.camera_spike                 # Windows
& .\.venv\Scripts\python.exe -m spike.camera_spike --no-ptz         # skip PTZ
& .\.venv\Scripts\python.exe -m spike.camera_spike --clip-seconds 8
```

```bash
./.venv/bin/python -m spike.camera_spike                            # POSIX
```

Config comes from `.env` (gitignored — this repo is public). Copy the `CAMERA_*`
block from `.env.example`:

| Key | Meaning |
|-----|---------|
| `CAMERA_HOST` | Camera LAN IP. |
| `CAMERA_USERNAME` / `CAMERA_PASSWORD` | The on-device **device account** created in the Reolink app (NOT the cloud login). |
| `CAMERA_ONVIF_PORT` | ONVIF port. Reolink default `8000`. |
| `CAMERA_RTSP_PORT` | RTSP port. Reolink default `554`. |

Captures land in gitignored `webapp/camera_captures/` — an outdoor frame can
reveal the home/location, so it never enters git. Credentials are never printed;
the password is masked in any RTSP URL shown on screen.

> **Dependencies:** `onvif-zeep-async` + `wsdiscovery` (in `requirements.txt`),
> plus **ffmpeg on PATH** for the snapshot/clip grab.

## Prerequisite: enable RTSP + ONVIF on the camera

Reolink ships RTSP and ONVIF **disabled** on current firmware — they are opt-in
toggles. Until they are enabled, the camera only answers on its proprietary
control port and the open path is unreachable. Enable both in the Reolink app:

> **Settings → Network → Advanced → Server Settings (Port Settings)** → toggle
> **ONVIF** and **RTSP** on.

Then re-run the spike — ports 554 (RTSP) and 8000 (ONVIF) should open and ONVIF
auth uses the device-account credentials in `.env`.

## Findings (2026-06-24)

Initially a port probe showed only the proprietary Reolink control port **9000
open**, with RTSP (554) and ONVIF (8000) closed — the open protocols ship
disabled. After enabling **RTSP + ONVIF** in the app (Server Settings), a rerun
of the spike **passed every software-checkable item end-to-end**, on firmware
`v3.1.0.5714`:

- **ONVIF WS-Discovery** found the camera at `http://<camera-ip>:8000/onvif/device_service`.
- **ONVIF device info** read back `REOLINK E1 Outdoor Pro`.
- **RTSP** main + substream URIs came back from ONVIF (`/Preview_01_main`,
  `/Preview_01_sub`).
- **ffmpeg** wrote a snapshot (`snapshot.jpg`) and a 5 s clip (`clip.mp4`) off
  the RTSP substream.
- **PTZ** pan + zoom move/stop were accepted (the camera physically moved).

The open, vendor-neutral path works with no cloud/hub in the loop. The remaining
acceptance items are operational (no-internet/VLAN, microSD, outdoor mount) and
the go/no-go record below.

## Acceptance checklist

Software-checkable (run by the spike) — **all passing 2026-06-24**:

- [x] ONVIF discovery succeeds (WS-Discovery found the camera).
- [x] RTSP main + substream URIs obtained from ONVIF.
- [x] Python snapshot saved to disk via ffmpeg over RTSP.
- [x] Short stream grab (clip) works.
- [x] PTZ control (pan/tilt + zoom) exercised via ONVIF.

Manual (hardware/operational — owner verifies, not the spike):

- [ ] Camera reachable on the LAN / IoT VLAN with **no internet access** (proves
      no cloud dependency).
- [ ] Local microSD recording confirmed with the cloud account unused/removed.
- [ ] Outdoor mount tested in summer conditions: waterproofing, day/night image,
      WiFi signal at the mounting point.

## Go / no-go decision (2026-06-24)

**GO.** The open, vendor-neutral path works end-to-end with no vendor cloud or
hub: ONVIF discovery + control, RTSP main/substream, ffmpeg capture, and PTZ all
passed against the live E1 Outdoor Pro. The Reolink ecosystem is confirmed as the
spike target, and the follow-up integration starts from this proven code.

Honest status of the remaining items (decided **not** to block the software
integration on them — they are operational checks the owner verifies on the
hardware):

- Open-path validation (ONVIF/RTSP/PTZ/ffmpeg): **proven**.
- No-internet / IoT-VLAN, local microSD recording, outdoor-mount weatherproofing
  + day/night + WiFi-at-mount: **still to verify by the owner** (not yet done).
- Final fleet model list (remaining **2 external + 4 internal**): **to confirm at
  purchase** — leading recommendation is all-Reolink (3× E1 Outdoor Pro + 4× E1
  Pro/Zoom) for best native integration while staying ONVIF-swappable.

Follow-up integration tracked separately (wire the camera into the Security tab:
PTZ, snapshot, full-screen control view, video). The eventual goal driving the
multi-camera + video work is **alarm-triggered scene capture with AI analysis**
(on an alarm, snapshot every camera and classify real-vs-false / what triggered
it) — a later phase.

## Reference — Reolink RTSP/ONVIF URLs

Pulled programmatically by the spike via ONVIF `GetStreamUri`. The E1 Outdoor Pro
returned (older Reolink models use the `h264Preview_01_*` form instead):

- Main stream: `rtsp://<user>:<pass>@<camera-ip>:554/Preview_01_main`
- Substream: `rtsp://<user>:<pass>@<camera-ip>:554/Preview_01_sub`
- ONVIF endpoint: `http://<camera-ip>:8000/onvif/device_service`

Relates to #85 (firmware-flash path rejected → this is path 2).
