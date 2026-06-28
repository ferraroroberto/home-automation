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

## Files

- `custom_sentences/en/alarm.yaml` → `/config/custom_sentences/en/alarm.yaml`
- `configuration.snippet.yaml` → append both blocks to `/config/configuration.yaml`
- `secrets.snippet.yaml` → add both keys to `/config/secrets.yaml` **with real values** (never committed)

## Install (HA VM)

1. **secrets.yaml** — add `app_api_authorization` (`Bearer ` + the webapp `auth_token` from the host's `config/webapp_config.json`) and `voice_disarm_pin` (a short spoken word you choose).
2. **configuration.yaml** — append the `rest_command:` and `intent_script:` blocks. If you already have a `rest_command:` or `intent_script:` key, merge the entries under the existing one (a YAML key can appear only once).
3. **custom_sentences/en/alarm.yaml** — create the file (folders included).
4. **Developer Tools → YAML → Check configuration**, then **Restart Home Assistant** (the `intent_script` / `rest_command` blocks load only at startup — a "Quick reload" is not enough). After the first install, editing **only** `alarm.yaml` no longer needs a restart — call the `conversation.reload` service instead.

## Verify

- Read-only first: "Okay Nabu, what's the alarm status?" → it speaks the current state.
- Then a full cycle: "perimeter on" → check the app's Security tab → "disarm \<code\>".

## Requirements

- The Assist pipeline's **"Prefer handling commands locally" = ON** (already set — see `../voice-control.md`). With it off, these sentences would be sent to the LLM instead of matched locally.
- The webapp reachable from the HA VM at the configured LAN URL (`/healthz` answers 200; LAN calls to `/api/*` need the bearer token).
