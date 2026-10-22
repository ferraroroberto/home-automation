# Cameras

## Cameras

The **Security** tab has an open **📹 Cameras** group for open
RTSP/ONVIF cameras (a **Reolink E1 Outdoor Pro** today) — no vendor cloud, hub,
or subscription (the feasibility spike findings
and go/no-go are in [`camera-spike.md`](camera-spike.md)). Each camera
is a row showing a **last-snapshot thumbnail** (icon + name, aligned under the
card title), its model, and reachability; tapping the thumbnail **zooms** the
last frame, and tapping the name opens a detail modal with a **fresh snapshot**
(grabbed at open time, which *becomes* the new persisted last frame) and a rename
field, plus an **Open live view** button. The full-screen live view streams
MJPEG, with a **PTZ d-pad** that toggles between **Step** (one click = one fixed
nudge — precise, the default) and **Hold** (press-and-hold continuous move),
**saved position presets** (Position 1, 2, … — recall/save/delete), **manual
pan/tilt/zoom coordinate** entry, **Zoom out / Zoom in** buttons, a **Screenshot** button (downloads a still),
and a **Record** toggle (server-side mp4) — every action carries a visible word, never a bare glyph; each row also has a labelled **Live** button. The precise-PTZ controls are
**capability-gated**: presets and absolute-coordinate entry appear only on
cameras whose ONVIF stack supports them; the universal step-nudge works on any
PTZ camera. Cameras are accessed the same vendor-neutral way the rest of the
fleet is: ONVIF for discovery/profiles/PTZ, RTSP + **ffmpeg** for the
snapshot/stream/recording. The eventual goal is alarm-triggered scene capture
with AI analysis.

- **Config:** declare cameras in gitignored `config/cameras.json` — copy
  `config/cameras.sample.json` and fill in each camera's `id`, `host`,
  `onvif_port` (Reolink default 8000), `rtsp_port` (554), `username`, and
  `password` (the on-device **device account**, NOT the cloud login). Custom
  labels persist to gitignored `config/camera_display_names.json`. Position
  presets on cameras **without** native ONVIF presets fall back to
  gitignored `config/camera_presets.json` (absolute coordinates); the most
  recent frame per camera is persisted under gitignored
  `webapp/camera_captures/last/`.
- **Address recovery model:** give a camera an optional **`mac`** in
  `config/cameras.json` and a stale DHCP IP **self-heals** like the plugs and
  access point do. When the configured host is unreachable, the app
  looks the MAC up in the access-point's attached-device table
  (`network_client.resolve_ip_by_mac`), reconnects at the rediscovered IP, and
  **persists the recovered address back to `config/cameras.json`**. Without a
  `mac` (or when the MAC isn't on the network), the camera is simply marked
  unreachable — recovery is best-effort and never fatal.
- **Prerequisite:** **enable RTSP + ONVIF on the camera first** — Reolink ships
  them off (app: Settings → Network → Advanced → Server Settings).
- **Endpoints:** `GET /api/cameras` (list; each carries `ptz_presets` /
  `ptz_absolute` / `ptz_relative` capability flags), `GET /api/cameras/{id}/snapshot`
  (fresh JPEG, also persisted as the last frame), `GET /api/cameras/{id}/last_snapshot`
  (the persisted last frame — never hits the camera), `GET /api/cameras/{id}/stream`
  (live MJPEG; reachable from the PWA via `?token=`), `POST /api/cameras/{id}/ptz`
  (`{action: start|stop|step, direction, zoom}`), `GET /api/cameras/{id}/ptz/status`
  (live pan/tilt/zoom + bounds), `POST /api/cameras/{id}/ptz/absolute`
  (`{pan, tilt, zoom?}`), `GET/POST /api/cameras/{id}/presets` (list / save
  current), `POST /api/cameras/{id}/presets/{token}/goto`,
  `DELETE /api/cameras/{id}/presets/{token}`, `POST /api/cameras/{id}/record`
  (`{action: start|stop}` → mp4 in gitignored `webapp/camera_captures/`),
  `PUT /api/cameras/{id}/display_name`.
- **Needs ffmpeg on PATH.** Smoke command:

  ```powershell
  & .\.venv\Scripts\python.exe -m src.list_cameras
  ```

Do not commit camera IPs, the device-account password, the UID/MAC, captured
frames, or location names; this repository is public.
