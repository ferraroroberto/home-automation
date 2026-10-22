# RISCO alarm and Security tab

## RISCO alarm / Security tab

The **Security** tab integrates the RISCO Cloud alarm through `pyrisco` for
state/events and the native RISCO WebUI command path for arm/disarm actions.
The tab opens on a **glance card**
that answers "is the house protected?": the mode word with any exception chips
(AC power lost, trouble), the arm control as one segmented control (`Off` /
`Partial` / `Perimeter` / `Full`, the selected segment is the mode, never red),
the next schedule as one line (`Next: Arm full at 23:00`) and the **Kids home**
switch. Below it are open groups on the shared row (`row.js`): **Detectors**,
**Cameras**, **People**, **Recent events** and **Automations** (Schedules, Scene
capture and Override, each a sheet). The same state line and segmented control
are mirrored, actionable, on the **Home** tab.

**Per-detector data.** The RISCO Cloud API does not expose
per-detector battery — only a generic per-zone *trouble* boolean (surfaced via the
Trouble indicator below). The system-wide low-battery flag is deliberately not
surfaced because it is a noisy/sticky proxy; per-detector battery/connection
differentiation would need the panel's local interface, which isn't exposed.

**AC-power-lost alert.** When the panel loses mains power and runs on
backup battery, a red **`AC power lost`** badge (warning icon + text) appears on the `Alarm state`
line (Home + Security tiles) from the aggregate `ac_lost` flag, clearing when
mains power returns.

Each detector row carries chips for exceptions only (`Triggered` danger,
`Trouble` attention, `Bypassed` and `Trouble ignored` neutral; an active
detector shows none) and its active/bypassed switch (active = the accent blue,
bypassed = red). A detector that needs you (triggered, or trouble not ignored)
sorts first, the rest A–Z by label. The group shows the first five with **Show
all N** below, and a **Filter detectors** field once more than twelve are
listed. Tapping a row opens the detector sheet showing its type, status, and
trouble state, plus a **Display name** field with the original
RISCO **system name** shown beneath it for correspondence. Detectors arrive named
`1`, `2`, … so a custom label is saved via `PUT /api/security/zones/{id}/display_name`
to a gitignored `config/security_display_names.json` (zone id → label, parallel to
the unit `config/display_names.json` and plug `config/tuya_display_names.json`); a
missing file is not an error, and the override wins over the RISCO name everywhere.

**Hiding unused detectors.** The detail modal also has a **Hidden**
toggle that parks an unused detector out of the default list, saved via
`PUT /api/security/zones/{id}/hidden` to a gitignored `config/security_hidden.json`
(reusing the same atomic store as the display-name files). The Detectors group's
footer then shows **Show N hidden / Hide N hidden**, which reveals the hidden
detectors (dimmed) on demand so they can be un-hidden.

**Ignoring a detector's trouble.** A detector's `Trouble` flag rolls
up to an **`N trouble`** chip on the Home/Security state line counting detectors
that are troubled. On the Security tab the chip opens the detector (one) or
shows the troubled ones at the top of Detectors (several). For a known/accepted trouble (a detector that can't be
serviced yet), the detail modal has an **Ignore trouble** toggle: an ignored
detector reads muted **`Trouble — ignored`** in the list and is dropped from the
main-card count, so muting the known ones quiets the card while a new/un-ignored
trouble still shows there. Saved via `PUT /api/security/zones/{id}/trouble_ignored`
to a gitignored `config/security_trouble_ignore.json` (same atomic store; surfaced
as `trouble_ignored` per zone on `GET /api/security`). Differentiating the trouble
*cause* (battery vs comms) per detector isn't possible from the cloud.

**Resilient live read.** A momentary panel-unreachable blip — RISCO
returns a non-retryable result code such as `26` on the *live* state read even
though the login, site, and PIN steps all succeed — does not black out the
tab. `fetch_security_state()` falls back to the **cloud-cached** snapshot
(`fromControlPanel=False`), so the alarm state, the detector list, and the
trouble flags keep rendering and the action pills stay actionable.
The response is flagged `assumed_control_panel_state=true` to mark it as cached
rather than a fresh live read, an info-level breadcrumb logs each fallback, and
only a failure of *both* the live and cached reads surfaces as an error. This
mirrors the stale-cloud energy guard. The write paths
(arm/disarm, bypass) do not fall back — a live read after a command is correct
there.

The pills use translucent colour tints in three identities — **green** Disarm,
**yellow** Partial/Perimeter, **red** Full — and only the actions you can
actually take carry colour: when disarmed the three arm options are active and
Disarm fades; when armed only Disarm is active. The current state is conveyed by
the `Alarm state` line, not by a highlighted pill. Tapping Disarm also clears a
trouble/alarm-memory condition. Every action shows an optimistic neutral
frosted toast on tap, then re-renders from the panel's live state.

**Weekly alarm schedules.** **Security tab → Automations → Schedules** (a
sheet) holds multiple local weekly schedule entries. Each entry can be enabled,
assigned to any weekday combination, given a time, and set to `Disarm`,
`Partial`, `Perimeter`, or `Full`. Saved schedules appear as compact summary
rows; tap a row or **Add schedule** to edit in a dialog, where **Save** commits
the change and closing discards it. The tray-owned webapp evaluates these entries
server-side, fires each entry at most once per calendar day, and logs failed
alarm commands without stopping the scheduler so a transient RISCO error can be
retried on the next poll. The entries live in gitignored
`config/security_schedules.json`; copy `config/security_schedules.sample.json`
for the persisted shape if editing by hand.

**Automatic-alarm notifications (Telegram).** **Settings ›
Notifications › Alarm** pushes a short Telegram message when the home
**automatically** arms or disarms —
and, by default, whenever an automatic attempt **fails** (e.g. the panel is
offline during a power cut), plus the two
adverse panel events (intrusion + panel mains-power loss). Seven independent
toggles, persisted to gitignored `config/alarm_notify_prefs.json`
(`config/alarm_notify_prefs.sample.json` committed), saved via
`GET`/`PUT /api/security/notify-prefs`:

| Toggle | Fires when |
|--------|-----------|
| Automatic arm (schedule) | a weekly schedule armed the alarm |
| Automatic disarm (schedule) | a weekly schedule disarmed the alarm |
| Arm on everyone-away (presence) | presence automation armed (everyone left) |
| Disarm on arrival (presence) | presence automation disarmed (someone arrived) |
| Error on any automatic arm/disarm | an automatic attempt failed, carrying the panel's error text (`FAILED`). Presence auto-arm being unable to fire at all because a tracked person's presence is stuck `home` (names who, and since when) is a **separate, always-off-Telegram** condition (expected, frequent noise): it never pages regardless of this toggle. It is still shown in Settings › Home & people › Presence automation immediately and always logged to the local activity log — first once the block has persisted for `arm_block_notify_after_s` (default 900 s, `config/presence_automation.json`, below which it's just two people walking in a few seconds apart), then again roughly every 5 minutes for as long as it remains stuck. |
| Alarm triggered (intrusion) | the panel goes into ongoing/memory alarm (🚨) |
| Panel mains power lost/restored | the panel's `ac_lost` flag flips either way (⚠️/✅) |

**Defaults:** the four arm/disarm *success* toggles are **off** (opt-in); the three adverse ones — `error`, `intrusion`, `ac_lost` — are **on**. The intrusion + panel-AC alerts are edge-triggered off the live RISCO state read by the presence-automation loop, so they require that loop to be running (`PRESENCE_AUTOMATION_ENGINE_ENABLED`, default on); it is the only interval reader of RISCO state, so reusing it avoids a second poller hitting the cloud's third-party rate limit. The `ongoing_alarm`/`memory_alarm` panel flags behind the intrusion alert come from an undocumented, screen-scraped RISCO WebUI endpoint; a poll where that scrape comes back unreadable is treated as *unknown*, not *cleared* — otherwise the next successful poll re-observing a still-latched, days-old `memory_alarm` manufactures a false "new" intrusion alert with no attributable zone. Each intrusion log line also carries the raw flag values as a `diagnostic` field (log-only, not in the Telegram copy) so an occurrence is diagnosable without re-investigating. **Manual** arm/disarm from the app's own buttons is never
notified (you're already looking at the app). A persistently-failing automatic
action (an offline panel retried every poll) alerts **once per day**, not every
poll. Telegram delivery is best-effort: a delivery failure is logged and never
breaks the automation loop. Configure credentials by copying
`config/notify_config.sample.json` to gitignored `config/notify_config.json` and
filling in `bot_token` + `chat_id` (or set `TELEGRAM_BOT_TOKEN` /
`TELEGRAM_CHAT_ID` in `.env`, which take precedence); with no credentials the
notifier is a silent no-op. The notifier itself is the universal
`src/notify/` component vendored verbatim from `project-scaffolding`.

**Local activity log.** Independently of Telegram, **every** alarm command the
app issues — schedule, presence, *and* manual — is appended with its result
(`ok` / `error` / `blocked` + the accompanying text) to gitignored `logs/alarm.jsonl`, a local
alternative to the RISCO cloud event log so you can see what was attempted, how
often, and whether it worked. It is written through the reusable
`src.activity_log.append_activity(consumer, event)` facility (one append-only
JSONL writer, `logs/<consumer>.jsonl`), which the presence trigger log
(`logs/presence_triggers.jsonl`) also routes through. `source: manual`
entries also carry an `actor` field — `webapp` (the PWA's own Security tab,
the default when no header is sent), `ha` (the Home Assistant integration), or
`voice-pe` (the ESPHome voice bridge) — so an unexpected manual arm/disarm can
be traced to its caller without guessing. RISCO's own cloud event
log cannot make this distinction: every command, automated or manual, goes out
under the same dedicated WebUI account.

Config in `.env`:

| Key | Meaning |
|-----|---------|
| `RISCO_USERNAME` | RISCO Cloud login email. Use a dedicated sub-account if possible so the dashboard does not compete with the phone app session. |
| `RISCO_PASSWORD` | RISCO Cloud password. |
| `RISCO_PIN` | Panel PIN used by RISCO Cloud/WebUI commands. |
| `RISCO_PERIMETER_GROUP` | Optional group letter used only to label partially-set states more precisely. |
| `RISCO_PARTIAL_GROUP` | Optional group letter used only to label partially-set states more precisely. |
| `SECURITY_SCHEDULES_ENABLED` | Optional, default `true`; set `0` to disable the weekly alarm-schedule evaluator while keeping the UI/API available. |
| `SPEEDTEST_SCHEDULE_ENABLED` | Optional, default `true`; set `0` to never start the nightly speed-test loop (the e2e suite does). A process-level kill switch — the user-facing opt-in is the Network tile switch, which is off by default. |
| `SECURITY_SCHEDULES_POLL_INTERVAL_S` | Optional, default `60`; how often the tray-owned webapp checks due alarm schedules. |
| `BLIND_SCHEDULES_ENABLED` | Optional, default `true`; set `0` to disable the daily blind-schedule evaluator while keeping the UI/API available (the e2e autoboot does). |
| `BLIND_SCHEDULES_POLL_INTERVAL_S` | Optional, default `60`; how often the tray-owned webapp checks due blind schedules. |
| `TELEGRAM_BOT_TOKEN` | Optional; Telegram Bot API token for automatic-alarm notifications. Overrides `config/notify_config.json`. Unset → notifier is a silent no-op. |
| `TELEGRAM_CHAT_ID` | Optional; Telegram chat id the alerts are delivered to (the family-radar chat). Overrides `config/notify_config.json`. |
| `PC_FLEET_HUB_BASE` | Optional, default `http://127.0.0.1:8000`; base URL of the local-llm-hub whose machine API the PC-fleet shutdown/wake proxies use. |

Read-only smoke command:

```powershell
& .\.venv\Scripts\python.exe -m src.list_security
```

RISCO periodically blocks third-party clients. If login starts failing after
credentials were known-good, check for a newer `pyrisco` release before changing
the app code.

## Alarm scene capture + AI verdict

When the RISCO alarm trips, the detector that fired is matched to its configured
camera **pairings**; each paired camera is driven to its PTZ preset and
snapshotted, and the frames (plus each camera's most recent *calm* baseline, for
contrast) are sent to a vision LLM that returns a **real vs false alarm** verdict
with a short explanation (person / pet / vehicle / nothing moved). The verdict is
delivered over **Web Push and Telegram** — the Telegram message **attaches the
captured frame** — and every trigger + the full model reply is appended to the
gitignored `logs/alarm_scene.jsonl` audit log.

Only detectors you **pair** are photographed — a random detector firing never
captures the house. A tripped detector with no pairing is logged and skipped. The
feature rides the single RISCO read already done by the presence-automation loop
(no second poller), so it requires that loop running
(`PRESENCE_AUTOMATION_ENGINE_ENABLED`, default on). While no alarm is active, each
camera's calm baseline is refreshed on a low-frequency timer.

**Onset detection reads RISCO's event log, not the live alarm flags.**
RISCO's `ongoing_alarm`/`memory_alarm` system flags latch `True` until a
full disarm+dismiss cycle, so a *second* real alarm on the same detector within
one still-armed session never produces a fresh transition of those flags — and
the live per-zone `triggered` boolean can already be stale by the time a poll
observes it. Instead, while the system reports an active intrusion, the
automation periodically (`ALARM_SCENE_EVENT_SCAN_S`) diffs RISCO's event log
against a cursor — the timestamp of the last-processed alarm event, persisted
to gitignored `config/alarm_scene_cursor.json` rather than kept in memory — so
each individual alarm gets its own capture regardless of how many fired in the
same still-armed session, and a webapp restart mid-session resumes from the
persisted cursor instead of silently dropping an in-flight alarm.

The LLM call is routed through the **local hub** (`http://127.0.0.1:8000`,
Anthropic-shape `/v1/messages`) per the fleet rule — never an inline `claude -p`
wrapper. A hub or parse failure degrades to an "AI analysis unavailable — verify
manually" verdict and never breaks the alarm path.

**Configure pairings** in **Security tab → Automations → Scene capture**. Saved
pairings appear as compact detector-to-camera rows; tap a row or **Add pairing**
to edit the detector, camera, and optional PTZ preset in a dialog. Closing the
dialog without **Save** discards the change. Pairings can also be edited by hand in gitignored
`config/alarm_scene_pairings.json` (copy `config/alarm_scene_pairings.sample.json`;
each entry is `{zone_id, camera_id, preset_token?, preset_name?, enabled}`). API:
`GET/PUT /api/security/scene-pairings`.

Config in `.env`:

| Variable | Meaning |
| --- | --- |
| `ALARM_SCENE_ENABLED` | Optional, default `true`; set `0` to disable scene capture while keeping the pairing UI/API available. |
| `ALARM_SCENE_MODEL` | Optional, default `claude-haiku-4-5`; the vision model id the hub routes to. |
| `ALARM_SCENE_HUB_URL` | Optional, default `http://127.0.0.1:8000`; the local hub base URL. |
| `ALARM_SCENE_PRESET_SETTLE_MS` | Optional, default `4000`; how long to let a camera settle on its PTZ preset before the snapshot (a too-short wait captures a blurred mid-pan frame). |
| `ALARM_SCENE_BASELINE_REFRESH_S` | Optional, default `1800`; how often the calm per-camera baseline is refreshed while no alarm is active. |
| `ALARM_SCENE_EVENT_SCAN_S` | Optional, default `20` (floor `10`); how often the RISCO event log is diffed for new alarms while an intrusion is active. |

Captures + baselines live under the already-gitignored `webapp/camera_captures/`
(`alarm/` and `baselines/`); never commit frames or `config/alarm_scene_pairings.json`.

## Alarm override — auto-bypass after repeated false alarms

RISCO's own panel already auto-omits a repeatedly-triggered detector, but only
after an uncontrolled, undocumented number of repeats. **Security tab →
Automations → Override** (a sheet) lets you set
a much tighter, per-detector threshold: after 1-3 real alarms from the same
detector within one armed session (a windy garden gate, a roaming cat), the app
proactively bypasses just that detector — the rest of the system stays fully
armed — and restores it automatically the next time the panel is armed.

Reuses the same event-log-diff architecture as alarm scene capture
rather than a second poller: it rides `presence_automation.py`'s single RISCO
read, diffing `fetch_events()` against a cursor + per-zone trigger counts
persisted to gitignored `config/security_override_session.json`. Each
auto-bypass/restore is logged to the unified activity log (`GET
/api/activity?domain=security`, `event_type` `auto_bypass` / `auto_unbypass`).

**Configure** in **Security tab → Automations → Override**. Saved rules appear as
compact detector + threshold rows; tap a row or **Add override** to edit the
detector and "bypass after N triggers" threshold in a dialog. Closing without
**Save** discards the change. Rules can also be edited by hand in gitignored
`config/security_override.json` (copy `config/security_override.sample.json`;
each entry is `{zone_id, max_retries, enabled}`, `max_retries` clamped to
1-3). API: `GET/PUT /api/security/overrides`.

Config in `.env`:

| Variable | Meaning |
| --- | --- |
| `SECURITY_OVERRIDE_ENABLED` | Optional, default `true`; set `0` to disable the automation while keeping the override UI/API available. |
| `SECURITY_OVERRIDE_EVENT_SCAN_S` | Optional, default `20` (floor `10`); how often the RISCO event log is diffed for new alarms/arm events. |
