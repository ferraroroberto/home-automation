# Cameras — open RTSP/ONVIF: spike findings & decision (#89 → #161)

Decision record for replacing the YI Home / Kami cameras with **open, self-hosted** cameras. This is **path 2** from the #85 feasibility study (the firmware-flash path was rejected): cameras that speak **standalone RTSP + ONVIF on the device itself**, with no vendor cloud, hub, or subscription. The #89 spike validated one unit end-to-end from Python; #161 turned that into the product (`src/camera_client.py` + the Security-tab **Cameras** tile). The throwaway spike script has been removed — its logic lives in `src/camera_client.py`.

## Hardware

**Reolink E1 Outdoor Pro** — mains-powered WiFi-6, weatherproof, 355° pan / 50° tilt / 3× optical zoom, on-device RTSP + ONVIF, no subscription. Best-in-class *open* WiFi-mains camera for 2026 (see #89 for the full rationale and the battery-camera lock-in trap to avoid). Brand-agnostic in principle — ONVIF keeps any compliant camera swappable — but Reolink has the strongest local story.

## Go / no-go decision (2026-06-24): **GO**

The open, vendor-neutral path works end-to-end with no vendor cloud or hub — validated live on firmware `v3.1.0.5714`: ONVIF discovery + device info, RTSP main/substream URIs from ONVIF, ffmpeg snapshot + clip, and PTZ pan/tilt/zoom all passed. Reolink/ONVIF is confirmed; the integration (#161) is built on this.

Honest status of the operational items — deliberately **not** blocking the software integration (the owner verifies these on the hardware):

- Open-path validation (ONVIF/RTSP/PTZ/ffmpeg): **proven**.
- No-internet / IoT-VLAN, local microSD recording, outdoor-mount weatherproofing + day/night + WiFi-at-mount: **still to verify by the owner**.
- Final fleet model list (remaining **2 external + 4 internal**): **to confirm at purchase** — leading pick is all-Reolink (3× E1 Outdoor Pro + 4× E1 Pro/Zoom) for best native integration while staying ONVIF-swappable.

The eventual goal driving the multi-camera + video work is **alarm-triggered scene capture with AI analysis** (on an alarm, snapshot every camera and classify real-vs-false / what triggered it) — tracked separately as a later phase.

## Operational prerequisite (still applies to the product)

Reolink ships RTSP and ONVIF **disabled** on current firmware — until they are enabled, the camera only answers on its proprietary control port (9000) and the open path is unreachable. Enable both in the Reolink app: **Settings → Network → Advanced → Server Settings (Port Settings)** → toggle **ONVIF** and **RTSP** on. ONVIF auth then uses the on-device **device account** (not the cloud login).

## Reference — Reolink RTSP/ONVIF URLs

Pulled programmatically via ONVIF `GetStreamUri`. The E1 Outdoor Pro returns (older Reolink models use the `h264Preview_01_*` form instead):

- Main stream: `rtsp://<user>:<pass>@<camera-ip>:554/Preview_01_main`
- Substream: `rtsp://<user>:<pass>@<camera-ip>:554/Preview_01_sub`
- ONVIF endpoint: `http://<camera-ip>:8000/onvif/device_service`

Relates to #85 (firmware-flash path rejected → this is path 2) and #161 (the product integration).
