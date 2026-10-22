# Wake alarms, reminders and calendar

## Wake alarms & timers

Alexa-style **wake alarms** and countdown **timers**, managed from the Home tab's **Next up** card: each alarm is a time-led row with its switch that opens a staged editor (time, label, days or just once, delete; Save is the only write), a running timer is a row counting down with × to cancel, and **Start timer** opens a sheet of presets and minutes. This is deliberately **separate from the RISCO "Alarm controls"** card — a wake alarm rings/notifies at a set time, it never arms or disarms the security system.

- **Wake alarms** — recurring (any set of weekdays) or one-shot (a specific date). Each has a label, a `HH:MM` time, and an Enabled toggle. When one comes due the tray-owned webapp marks it "ringing" (shown in the card + a best-effort Telegram notify via the existing notifier), then rearms a weekly alarm for its next matching day or auto-disables a one-shot. Evaluated server-side so they fire while the PWA is closed. Persisted to gitignored `config/wake_alarms.json` (committed `…sample.json` shows the shape).
- **Timers** — one-off countdowns started from the timer sheet (presets 5/10/15/30 min or a custom minute count). Deliberately **in-memory and unpersisted**, mirroring Home Assistant's own ephemeral voice-set timers — a webapp restart clears active timers.

API: `GET`/`PUT /api/wake-alarms` (list/replace), `POST /api/wake-alarms/{id}/test` (fire immediately) + `…/dismiss`; `GET`/`POST /api/wake-timers` and `DELETE /api/wake-timers/{id}`.

**Voice.** The household's HA Voice PE creates/cancels/lists wake alarms hands-free — *"Okay Nabu, set a wake alarm for 7 am on weekdays"* / *"cancel my wake alarm"* / *"what wake alarms do I have"* — via a thin voice API that parses the spoken time server-side: `POST /api/wake-alarms/voice` (`{phrase}` → parse, append, speak it back), `POST /api/wake-alarms/voice/cancel` (cancels the **soonest** upcoming one), `GET /api/wake-alarms/voice` (spoken summary). Parsing lives in `src/wake_alarms.py:parse_spoken_alarm` (tested): times like `7 am` / `7 30` / `seven thirty` / `half past six` / `noon`; schedules `on weekdays` · `on weekends` · `every day` · a weekday name · `tomorrow`/`today` (one-shot). The HA sentences/wiring are in [`voice-pe-config/`](voice-pe-config/) (`wake_alarm.yaml`), kept collision-free from the RISCO "alarm" grammar. **Countdown timers** need nothing here — HA's native *"set a timer for 5 minutes"* already works on the Voice PE.

Optional `.env` knobs:

| Key | Default | Meaning |
|-----|---------|---------|
| `WAKE_ALARMS_ENABLED` | `true` | Master switch. Set `false`/`0` to disable the evaluator while keeping the UI/API available. |
| `WAKE_ALARMS_POLL_INTERVAL_S` | `15` | How often the tray-owned webapp checks for due alarms and expired timers. |

> **Not** in scope here: mirroring **voice-set** HA-native timers into the webapp — HA exposes no stable poll API for those, so the app-native timer above is a separate, unbridged pool. Triggering an HVAC unit from an alarm is covered by the per-unit [HVAC schedules](hvac.md#hvac-automation), not duplicated here.

## Reminders

A free-text **reminders** checklist, bidirectional between the app and Home Assistant Voice — a reminder created by voice shows up in the app, and one created in the app is speakable/listable/completable by voice. Managed from the Home tab's **Next up** card, as rows led by their due time, with the done switch and the staged editor one tap away — deliberately distinct from that feature: a wake alarm rings at a set time, a reminder is a checklist item you complete, optionally due on a date/time.

- **Card:** dense-collection list — each reminder is a flat row (text, an optional due-date caption, and a Done toggle) with a staged **Add/Edit reminder** dialog (text + an optional "Due date" toggle revealing date/time fields). Same shared `denseListEditor` shell as the Security tab's Schedules card.
- Persisted to gitignored `config/reminders.json` (committed `…sample.json` shows the shape). No recurring reminders and no active "due now" firing/notification — this is a passive list/complete surface, not a second alarm.

API: `GET`/`PUT /api/reminders` (list/replace).

**Voice.** *"Okay Nabu, remind me to take out the trash at 6 pm tomorrow"* / *"mark my reminder done"* / *"what reminders do I have"* — via a thin voice API that parses the spoken phrase server-side: `POST /api/reminders/voice` (`{phrase}` → split text from an optional due cue, append, speak it back), `POST /api/reminders/voice/complete` (marks the **earliest-due** pending reminder done, falling back to the **oldest-created** among undated ones), `GET /api/reminders/voice` (spoken summary of what's still pending). Parsing lives in `src/reminders.py:parse_spoken_reminder` (tested): due cues like `at 6pm` / `at 14:30`, `tomorrow`, `today`, `on friday` — a phrase with none of those is just an undated checklist item, which covers most "remind me to X" phrases. The HA sentences/wiring are in [`voice-pe-config/`](voice-pe-config/) (`reminder.yaml`), kept collision-free from `wake_alarm.yaml` and the RISCO `alarm.yaml` grammars. **English only for now** — no Spanish sentences ship for this feature (unlike wake alarms).

> **Not** in scope here: LLM-based parsing (stays regex/best-effort, Tier-1 local matching, same convention as wake alarms), recurring reminders, and a native Home Assistant `todo.reminders` entity (the voice surface is custom-sentence + `rest_command` only, mirroring wake alarms — a native entity is a natural follow-up if it's ever wanted).

## Calendar

Voice-only, **create-only** Google Calendar integration — "Okay Nabu, add dentist appointment to my calendar tomorrow at 3pm" creates a real event in your Google Calendar. Deliberately **not** app-local storage and **not** Home Assistant's native calendar integration: this app owns a Google OAuth token directly (mirroring whatsapp-radar's pattern) and writes straight to the Google Calendar API, independent of any HA calendar entity — there's no local list, so there's nothing to expose as an HA `calendar.py` platform either.

**Voice.** *"add dentist appointment to my calendar tomorrow at 3pm"* — a thin voice API parses the spoken event summary + optional due-date/time server-side and creates it: `POST /api/calendar/voice` (`{phrase}` → parse, shape, create, speak the confirmation). Parsing lives in `src/calendar_events.py:parse_spoken_calendar_event` (tested): a date + time → a timed 60-minute event; a date with no time → all-day; a time with no date defaults to today/tomorrow; no date at all fails gracefully ("I need at least a date..."). The HA sentences/wiring are in [`voice-pe-config/`](voice-pe-config/) (`calendar.yaml`).

**One-time Google Cloud setup:**
1. In [Google Cloud Console](https://console.cloud.google.com/apis/credentials), create (or reuse whatsapp-radar's existing) OAuth client of type **Desktop app** — OAuth *clients* aren't tied to one codebase, so reusing one is fine. Download its JSON.
2. Set `GOOGLE_CALENDAR_CREDENTIALS_PATH` in `.env` to that downloaded file's path.
3. Run `& .\.venv\Scripts\python.exe -m scripts.auth_calendar_write` once — opens a browser for consent, writes the **independent** write token to `config/calendar_write_token.json` (gitignored). The token is per-app even when the OAuth client is shared, so this app's calendar access can be revoked/rotated without touching whatsapp-radar's.

Optional `.env` knobs:

| Key | Default | Meaning |
|-----|---------|---------|
| `GOOGLE_CALENDAR_CREDENTIALS_PATH` | `config/calendar_credentials.json` | Path to the downloaded OAuth Desktop-app client JSON. |
| `GOOGLE_CALENDAR_TOKEN_PATH` | `config/calendar_write_token.json` | Where the write token persists (gitignored). |
| `GOOGLE_CALENDAR_TIMEZONE` | `Europe/Madrid` | IANA timezone stamped on created events. |

> **Not** in scope here: listing/editing/cancelling an existing calendar event (create-only — a natural follow-up), a native Home Assistant `calendar.py` platform (nothing local to expose — Google Calendar is the only store), and any PWA UI surface (voice + Google Calendar's own apps only).
