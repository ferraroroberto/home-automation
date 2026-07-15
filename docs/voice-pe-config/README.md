# Voice PE config — deterministic alarm action bridge (#88 Phase 4)

Sanitized Home Assistant config that turns spoken phrases into **deterministic** RISCO alarm commands against this app's webapp (`/api/security/*`). No LLM is on the command path: Home Assistant's local sentence engine matches these phrases directly (Tier 1 of the routing in [`../voice-control.md`](../voice-control.md)), so a hallucinated model reply can never arm or disarm the alarm. These files are the durable, secret-free record of what is installed on the HA VM — the live copies live under the VM's `/config`.

> **Wiring more commands?** This directory is the worked example; the reusable recipe (sentence syntax, the `stop`/`action_response` gotcha, reload-vs-restart, code-gating, testing without a voice) is in [`../voice-commands-howto.md`](../voice-commands-howto.md).

## What it does

| You say (after "Okay Nabu, …") | Intent | App call |
|---|---|---|
| "alarm on" · "full alarm on" · "turn the alarm fully on" · "fully arm" · "activate the alarm" | full arm | `POST /api/security/arm` |
| "perimeter on" · "the perimeter on" · "put the perimeter on" · "perimeter mode" | perimeter | `POST /api/security/perimeter` |
| "partial on" · "partial alarm on" · "arm partial" · "partial mode" | partial | `POST /api/security/partial` |
| "what's the alarm status" · "what's the state of the alarm" · "is the alarm on" · "how is the alarm" | status (read) | `GET /api/security` → speaks `label` |
| "disarm \<code\>" · "turn off the alarm \<code\>" · "perimeter off \<code\>" | disarm (gated) | `POST /api/security/disarm` *only if the spoken code matches* |
| "disarm" · "alarm off" · "perimeter off" · "partial off" (no code) | prompt only | nothing — speaks how to disarm |

The full phrase lists are in `custom_sentences/en/alarm.yaml` — widen them freely; an unlisted phrasing falls through to the LLM instead of matching locally.

Arming, perimeter, partial and status are one-shot. **Disarm requires a spoken code** (`voice_disarm_pin`) in the same utterance — a wrong or missing code never calls disarm. That voice code is a gate layered on top of the RISCO panel PIN the app already holds server-side, so the real panel PIN is never spoken aloud.

Each alarm `rest_command` (`alarm_arm` / `alarm_partial` / `alarm_perimeter` / `alarm_disarm`) sends an `x-automation-source: voice-pe` header alongside its bearer token, so `logs/alarm.jsonl`'s `manual` entries tag voice-triggered commands with `actor: "voice-pe"` — distinct from the webapp PWA (`actor: "webapp"`) and the HA integration (`actor: "ha"`, issue #405). Useful for ruling voice in or out when an arm/disarm wasn't expected.

### Wake alarms (issue #306)

A **separate** feature from the RISCO alarm above — every phrase says "wake alarm" / "wake-up alarm" so the two grammars never collide. These drive the app's persisted wake-alarm list (Step 1, `/api/wake-alarms`); a fired alarm rings on the Home-tab card.

| You say (after "Okay Nabu, …") | Intent | App call |
|---|---|---|
| "set a wake alarm for 7 am" · "wake me up at half past six" · "set a wake-up alarm for 7 on weekdays" · "new wake alarm for 8 tomorrow" | set | `POST /api/wake-alarms/voice` → parses the spoken time, appends, speaks it back |
| "cancel my wake alarm" · "delete my wake alarms" · "turn off my wake-up alarm" | cancel | `POST /api/wake-alarms/voice/cancel` → cancels the **soonest** upcoming one (repeat for the next) |
| "what wake alarms do I have" · "list my wake alarms" · "when are my wake alarms" | list (read) | `GET /api/wake-alarms/voice` → speaks a summary |

**Supported spoken time/schedule** (parsed server-side in `src/wake_alarms.py:parse_spoken_alarm`, so the sentences stay thin):

- **Time:** `7` / `7 am` / `7 pm` / `7 30` / `seven thirty` / `half past six` / `quarter to seven` / `noon`. A bare number with no am/pm is taken as spoken (24-hour if ≥ 13, else AM).
- **Schedule:** `on weekdays` (Mon–Fri) · `on weekends` (Sat/Sun) · `every day` · a weekday name (`on monday`) → recurring; `tomorrow` / `today` → a one-shot that auto-disables after it fires. No schedule → every day.

The sentence lists are in `custom_sentences/en/wake_alarm.yaml`. Both the set and cancel intents reuse the existing `!secret app_api_authorization` — **no new secret**.

### Family locator (issue #438) — "where's mom/dad"

Read-only query — no actuation, so no code-gating needed. `{who}` is a free-text wildcard capturing the spoken name or household role; the app resolves it via role aliases (set from the Security tab's Presence card), display-name overrides, or raw names, then answers with the resolved place — a configured named place (e.g. "the gym"), "home", or "away" (cached Find My data only; no new iCloud locate cost).

| You say (after "Okay Nabu, …") | Intent | App call |
|---|---|---|
| "where's dad" · "where is mom" · "where's Roberto" · "locate Ana" · "find dad" | locate (read) | `GET /api/presence/locate?who=<text>` → speaks the resolved place, or that it doesn't know who/where |

The sentence list is in `custom_sentences/en/locate.yaml`. Named places and household-role aliases are configured from the Security tab's Presence card ("Places" and each person's detail-modal "Role" field) — nothing in `custom_sentences/` needs editing to add a new person or place. Reuses the existing `!secret app_api_authorization` — **no new secret**.

### Grocery list (issue #315) — Spanish, on its own pipeline

Voice control of the **grocery-shopping-automation** sibling app on `:8502` (its #86 built the endpoint, its #89 made the replies Spanish). The app's Excel-backed inventory is the store — the shopping list is derived (`comprar = cantidad − tenemos`). All intelligence is server-side: HA relays the free Spanish fragment to `POST /api/voice/command` with a deterministic intent, the app's hub-LLM parse matches items/quantities against the inventory, Python applies, and HA speaks back the returned Spanish `speech` string. Same doctrine as everything above: the LLM does language understanding only — it can never pick the operation or redirect a mutation (an invented row index is demoted to a new-item server-side).

**The whole feature lives on the dedicated Spanish pipeline** ("Asistente (es)", wake word **"Hey Jarvis"**): Spanish-hinted Whisper STT, Spanish Piper voice, and the built-in deterministic agent — unmatched speech gets "no entiendo", never an LLM improvisation. The first iteration bolted Spanish turns onto the English pipeline and failed the family test (en-hinted STT mangled the Spanish; the English voice mangled the replies) — full post-mortem in #315's decision log.

**Commands** (`custom_sentences/es/grocery.yaml`), all after **"Hey Jarvis, …"**:

| You say | Intent | What happens |
|---|---|---|
| "añade leche y dos huevos [a la lista]" · "apunta {…}" · "pon {…} en la lista" | `GroceryAdd` | one-shot add: bumps matched items' targets; unknown items become new rows |
| "pon el objetivo de leche a cuatro" · "objetivo de {…}" | `GroceryTarget` | sets the item's target (`cantidad`) |
| "anota que tenemos dos aceites" · "tenemos {…}" | `GroceryStock` | sets the item's have-count (`tenemos`) |
| "no quedan huevos" · "se acabó el pan" | `GroceryOut` | stock to **0** (the intent re-prefixes the zero cue the sentence consumed) |
| "qué hay que comprar" · "lee la lista [de la compra]" | `GroceryQuery` | reads the to-buy summary (no LLM call) |
| "¿qué puedo hacer?" · "opciones" · "ayuda" | `GroceryHelp` | speaks the command menu — the voice twin of #437's in-app cheat sheet |

**Multi-turn add** is the conversation-triggered automation `automation grocery_voice:` in the managed block, using `assist_satellite.ask_question` (HA 2025.7+): "Hey Jarvis, **quiero añadir cosas a la lista**" → the same puck asks *"¿Qué quieres añadir?"* → your free Spanish answer is captured whole by a wildcard → the app applies and the puck announces the confirmation. The one-shot `GroceryAdd` is the robust primary path; the multi-turn flow is the convenience layer on top. An item that matches nothing is **created** with empty super/buscador — grocery#87 (product search) will fill those; until then the grocery app's Items tab is the manual path.

Secret: `grocery_api_authorization` (the `Bearer` header for `:8502`). The grocery app currently ships with bearer auth **disabled**, so the live value is a placeholder (`Bearer grocery-auth-disabled`) that its middleware ignores — if that app's auth is ever enabled (`scripts/gen_token.py` there), update this secret to the real token and nothing else changes.

Why a separate pipeline: language is a pipeline-level property in HA (STT hint, TTS voice, sentence matching), so the wake word is the language switch — see [`../voice-commands-howto.md`](../voice-commands-howto.md) "Mixing English and Spanish" for the mechanics and the failed-first-iteration reasoning.

### Timers — already work, nothing to deploy

Home Assistant's built-in Assist intents (`HassStartTimer` / `HassCancelTimer` / …, since "Voice Chapter 7") give you ad-hoc countdown timers **with zero config in this repo**:

> "Okay Nabu, **set a timer for 5 minutes**" · "cancel the timer" · "how much time is left"

These are ephemeral, scoped per satellite, announced by TTS on completion — **not** the persisted wake alarms above, and not mirrored into the webapp (HA exposes no stable poll API for them; that bridge is a documented future gap). Don't rebuild them here.

## Files

- `custom_sentences/en/alarm.yaml` → `/config/custom_sentences/en/alarm.yaml` (RISCO security alarm)
- `custom_sentences/en/wake_alarm.yaml` → `/config/custom_sentences/en/wake_alarm.yaml` (wake alarms, #306)
- `custom_sentences/en/locate.yaml` → `/config/custom_sentences/en/locate.yaml` (family locator, #438)
- `custom_sentences/es/grocery.yaml` → `/config/custom_sentences/es/grocery.yaml` (grocery list in Spanish, #315)
- `configuration.snippet.yaml` → replace the marker section in `/config/configuration.yaml` (one managed block covers **every** feature's `rest_command` / `intent_script` / `automation` entries)
- `secrets.snippet.yaml` → add all keys to `/config/secrets.yaml` **with real values** (never committed)

`ha_config_sync.py deploy` pushes **every** `*.yaml` under `custom_sentences/<lang>/` (globbed across language dirs, not a hardcoded list — a new feature's or language's sentences file deploys with no script change) plus the managed config block, in one run.

## Deploy by code (preferred) — `scripts/ha_config_sync.py`

Once the one-time bootstrap below is done, all subsequent config work is terminal-driven from this repo: edit the snippets here, deploy them over SSH, validate with `ha core check`, reload/restart, and text-probe — no browser. This is the preferred path; the **File editor** flow further down is the fallback.

```
# settings come from .env (HA_SSH_HOST/PORT/USER/KEY, HA_URL, HA_TOKEN — see .env.example)
& .\.venv\Scripts\python.exe -m scripts.ha_config_sync preflight        # readiness, distinct failure per mode
& .\.venv\Scripts\python.exe -m scripts.ha_config_sync deploy --dry-run # unified diff, writes nothing
& .\.venv\Scripts\python.exe -m scripts.ha_config_sync deploy           # backup + write + ha core check (+ conversation.reload for sentence-only)
& .\.venv\Scripts\python.exe -m scripts.ha_config_sync deploy --restart # same, plus the full HA restart a configuration.yaml change needs
& .\.venv\Scripts\python.exe -m scripts.ha_config_sync rollback         # restore the most recent backup + recheck
& .\.venv\Scripts\python.exe -m scripts.ha_config_sync probe            # read-only "what is the alarm status" conversation probe
```

The deploy is idempotent: it replaces only the marked managed block in `/config/configuration.yaml` (everything else is preserved), writes the whole `custom_sentences/en/alarm.yaml`, takes a timestamped backup under `/config/backups/home-automation/` before every write, and runs `ha core check` before any restart. A sentences-only change is applied with the narrow `conversation.reload`; a `configuration.yaml` change prints that a full restart is required and only performs it with `--restart`. Real HA secrets stay live-only on the VM — the script checks that the `app_api_authorization` / `voice_disarm_pin` **key names** exist in `/config/secrets.yaml` but never reads, prints, copies, or commits their values.

### One-time bootstrap (HA VM) — enable SSH to `/config`

The deploy path needs the Home Assistant **Terminal & SSH add-on** reachable over the LAN. This is the *normal* automation channel (it mounts `/config` and the `ha` CLI); HAOS **host** SSH on `:22222` is a separate break-glass developer channel and is **not** used here. Do this once:

1. **Make a dedicated key on this PC** (no passphrase — the script doesn't prompt for one):
   ```powershell
   ssh-keygen -t ed25519 -f $env:USERPROFILE\.ssh\ha_ed25519 -C "ha-config-sync" -N '""'
   Get-Content $env:USERPROFILE\.ssh\ha_ed25519.pub      # the line you paste into HA
   ```
2. **Install the official "Terminal & SSH" add-on** (Settings → Add-ons → Add-on Store; slug `core_ssh`, runs as `root`). On its **Configuration** tab, add the public-key line under `authorized_keys`.
3. **Expose a LAN host port.** The add-on's UI **Network** card is sometimes not shown — set the port from the add-on's **web Terminal** instead, via the Supervisor API (pre-authenticated inside the add-on as `$SUPERVISOR_TOKEN`):
   ```bash
   curl -sX POST -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" -H "Content-Type: application/json" \
     -d '{"network": {"22/tcp": 2222}}' http://supervisor/addons/self/options   # -> {"result":"ok"}
   curl -sX POST -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" http://supervisor/addons/self/restart
   ```
   (`authorized_keys` can be set the same way if its UI field is awkward: `-d '{"options": {"authorized_keys": ["ssh-ed25519 AAAA... ha-config-sync"]}}'`. The add-on regenerates `/root/.ssh/authorized_keys` from this option on every start, so editing that file by hand won't stick.)
4. **Confirm key auth from this PC**, pointing explicitly at the key (the script uses `HA_SSH_KEY` directly, so the `-i` is only for this manual test):
   ```powershell
   ssh -i $env:USERPROFILE\.ssh\ha_ed25519 -o IdentitiesOnly=yes -p 2222 root@192.168.0.4 "ls /config/configuration.yaml"
   ```
   A bare `ssh` without `-i` gives `Permission denied (publickey)` because it never offers this key — that is not a server problem.
5. **Create the long-lived access token** (HA profile avatar → **Security → Long-lived access tokens → Create Token**) for the conversation probe.
6. **Fill `.env`** (`HA_SSH_HOST`, `HA_SSH_PORT`, `HA_SSH_USER`, `HA_SSH_KEY`, `HA_URL`, `HA_TOKEN` — see `.env.example`; use forward slashes and the **private** key path in `HA_SSH_KEY`), then run `… -m scripts.ha_config_sync preflight`. It should report `/config` present, `ha core check` passing, the required secret key names found, and a valid token. (`HA_TOKEN` is only needed for `probe`; `deploy`/`rollback` work over SSH alone.)

Leave HAOS host SSH on `:22222` disabled unless you have a specific host-debug need; routine config deploys never require it.

> **HA VM IP is static-MAC + DHCP-reservation pinned to `192.168.0.4`** (issue #240 — set a static MAC on the VM's Hyper-V adapter, then reserve `.4` to it on the router). The host/url in `.env` are the only place the IP is wired for deploys, so a future move to a different reserved IP is a one-line `.env` change plus a re-`preflight`. See the repo `README.md` "Home Assistant Hyper-V VM" section for the full reservation runbook.

## Install via the File editor add-on (fallback)

Use this only when SSH/script deploy is unavailable (add-on down, key not yet provisioned).

1. **secrets.yaml** — add `app_api_authorization` (`Bearer ` + the webapp `auth_token` from the host's `config/webapp_config.json`) and `voice_disarm_pin` (a short spoken word you choose).
2. **configuration.yaml** — first install: paste the snippet below the standard `default_config` / `automation` / `script` / `scene` lines. Later updates: replace the existing section from `# >>> home-automation:voice-pe-alarm` through the matching `# <<< home-automation:voice-pe-alarm` end marker. Do not duplicate `rest_command:` or `intent_script:` keys.
3. **custom_sentences/en/alarm.yaml** — create the file (folders included).
4. **Developer Tools → YAML → Check configuration**, then **Restart Home Assistant** (the `intent_script` / `rest_command` blocks load only at startup — a "Quick reload" is not enough). After the first install, editing **only** `alarm.yaml` no longer needs a restart — call the `conversation.reload` service instead.

## Verify

- Read-only first: "Okay Nabu, what's the alarm status?" → it speaks the current state.
- Then a full cycle: "perimeter on" → check the app's Security tab → "disarm \<code\>".
- **Wake alarms:** "set a wake alarm for 7 am on weekdays" → it speaks it back → confirm it appears on the Home-tab card (or `GET /api/wake-alarms`) → "cancel my wake alarm". Text-probe without speaking: `… -m scripts.ha_config_sync probe --text "set a wake alarm for 7 am" --actuate` (a reply of type `action_done` = matched locally).
- **Family locator:** set a role (Security tab → Presence → a person's detail modal → "Role") and at least one named place (Presence → "Places"), then "Okay Nabu, where's \<role\>" → it speaks the resolved place. Text-probe: `… -m scripts.ha_config_sync probe --text "where's dad"`.
- **Timers (native, no deploy):** "set a timer for 2 minutes" → wait for the TTS chime; "cancel the timer".

## Requirements

- The Assist pipeline's **"Prefer handling commands locally" = ON** (already set — see `../voice-control.md`). With it off, these sentences would be sent to the LLM instead of matched locally.
- The webapp reachable from the HA VM at the configured LAN URL (`/healthz` answers 200; LAN calls to `/api/*` need the bearer token).
