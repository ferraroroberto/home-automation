# Voice control — HA Voice PE + local LLM hub

Operations & integration guide for hands-free, fully-local voice control: a Home Assistant
Voice Preview Edition (Voice PE) puck driven by the `local-llm-hub`, with **no cloud**. This
is a reference manual (architecture, setup, operating, troubleshooting), not a changelog —
status and history live in issue #88, its PR, and git.

> **Secrets are not here** (the repo is public). The Wi-Fi password, hub token value, and
> device MAC addresses live only in the gitignored `_local/voice-pe-spike-STARTER.md` on the
> build machine.

## What it is

Loop: **wake word → speech-to-text → conversation agent → action or spoken answer → text-to-
speech**, all on the local stack. Common commands (time, on/off, set, weather) are answered
**instantly by Home Assistant's built-in intents**; only open-ended questions go to the LLM.

## Architecture

### Reuse the hub — no new models

All three voice stages are OpenAI-shaped endpoints the `local-llm-hub` already serves on
`http://192.168.0.13:8000` (binds `0.0.0.0`):

| Stage | Hub endpoint | model | Expected latency |
|---|---|---|---|
| STT   | `/v1/audio/transcriptions` | `whisper`           | 0.5–0.9 s |
| Logic | `/v1/chat/completions`     | `claude-haiku-4-5`  | 3–8 s (LLM path only) |
| TTS   | `/v1/audio/speech`         | `orpheus` (voice `tara`) | ~1.7–2.2 s full synthesis (real-time factor ≈ 1); 24 kHz |

The hub `auth_token` is a non-secret dummy (Tailscale is the real gate); it is used directly
in each HA integration's API-key field.

### Command routing — tiered hybrid

Guiding principle: **the model does language understanding; Python does actuation.** A
hallucinated tool call must never actuate (critical for alarm arm/disarm).

1. **Tier 1 — deterministic intents, no LLM.** HA's built-in intent engine over exposed
   entities + built-in intents (on/off, set, time, weather). <100 ms, offline. Enabled via
   the pipeline's **"Prefer handling commands locally"**. Most commands never touch an LLM.
2. **Tier 2 — LLM-as-classifier with constrained output.** For phrasings Tier 1 can't match:
   a small fast model emits a **schema-validated enum of intents + slots** (structured output,
   not free tool-calls); Python validates against an allow-list and executes, with a
   confirmation step for destructive actions.
3. **Tier 3 — freeform / open tool-calling.** Only for open questions or novel composition;
   a larger/slower model is acceptable because these are rare.

Do **not** outsource all tool use to the model: for a bounded, safety-relevant command set,
deterministic classification + coded execution wins on latency, reliability, testability,
and safety. (Model evaluation for the Tier-2/3 role: #234.)

### Where actuation goes

Device control reaches the app through its existing **API-first** backend — `POST
…:8447/api/units/{id}` etc., behind `BearerTokenMiddleware` (loopback bypasses; remote uses a
bearer token). The app's logic lives in `src/` and the PWA is just another `/api/*` client, so
HA can call exactly what the PWA calls. The durable form of this is a native HA integration
(climate/switch/alarm/sensor) so HA provides UI + voice + automation for the app's devices
(#235).

## Action bridge — deterministic alarm commands (live, #88 Phase 4)

The first real actuation is **voice control of the RISCO alarm**, wired as Tier-1
deterministic commands — spoken phrase → HA local sentence match → `intent_script` →
`rest_command` → the app's `POST /api/security/{arm,partial,perimeter,disarm}` (and
`GET /api/security` for status). No LLM touches the command path, so a hallucinated reply
can never arm or disarm. Disarm requires a **spoken code** in the same utterance, gated
against a secret before the command fires.

Spoken (after "Okay Nabu, …"): *"alarm on"* / *"full alarm on"* / *"turn the alarm fully
on"* (arm) · *"perimeter on"* · *"partial on"* · *"disarm now"* · *"what's the alarm
status"*. The full phrase lists and the exact HA config are the secret-free record in
[`voice-pe-config/`](voice-pe-config/).

- **Installed config:** [`voice-pe-config/`](voice-pe-config/) — `custom_sentences/en/
  alarm.yaml`, `configuration.snippet.yaml`, `secrets.snippet.yaml`.
- **Adding more commands:** [`voice-commands-howto.md`](voice-commands-howto.md) — the
  reusable recipe (hassil sentence syntax, the `stop`/`action_response` gotcha,
  reload-vs-restart, code-gating, and testing a command without speaking). Read this
  before wiring the next device.

A known wart, captured in the how-to: the shipped intents speak off the HTTP `200`, so an
arm that the panel silently refuses (e.g. a zone left open) is still confirmed aloud —
the recommended fix is to read the *resulting* state back before speaking (#241).

## Setup reference

### Substrate

Home Assistant OS in a **Hyper-V Gen2 VM** on an **External/bridged vSwitch** tied to the
wired NIC, so HA gets a real LAN IP and the Wi-Fi puck reaches it with no NAT. A Windows
Firewall inbound rule allows the VM to reach the hub on TCP 8000. The HA **Terminal & SSH**
add-on is the shell, and is the **code-driven config path**: `scripts/ha_config_sync.py`
deploys the repo-owned voice-PE config into `/config` over LAN SSH and validates it
(#243; see [`voice-commands-howto.md`](voice-commands-howto.md)). HAOS **host** SSH on
`:22222` is a separate break-glass developer channel — not used for config deploys.

### Network

| Host | Address | Notes |
|---|---|---|
| local-llm-hub (PC) | `192.168.0.13` | hub `:8000` (admin UI at `:8000/admin/`); app API `:8447` |
| HA VM | `192.168.0.4` | wired/bridged, ~0 ms; UI `http://192.168.0.4:8123`; static-MAC + DHCP-reserved (issue #240) |
| Voice PE puck | `192.168.0.42` | 2.4 GHz Wi-Fi; **reserved IP** (a re-associate can move it here from a prior lease — verify by ping) |

### HA integrations (HACS)

- **OpenAI Whisper Cloud** (STT) — entry "Custom Whisper": provider *Custom*, URL
  `http://192.168.0.13:8000/v1/audio/transcriptions`, model `whisper`, key = hub token.
- **OpenAI TTS** (sfortis) — agent "Orpheus (orpheus-tara)": endpoint
  `http://192.168.0.13:8000/v1/audio/speech`, model `orpheus`, voice `tara`, chime OFF,
  key = hub token.
- **extended_openai_conversation** — entry "Hub Haiku": base URL
  `http://192.168.0.13:8000/v1`, model `claude-haiku-4-5`, key = hub token, max_tokens 64,
  temp 0.5, terse prompt (one short spoken sentence, no follow-up questions, no pleasantries).

### Pipeline ("Focused local assistant")

Conversation agent = **Extended OpenAI Conversation**; STT = **Custom Whisper**; TTS =
**Orpheus (tara)**; **"Prefer handling commands locally" = ON**; wake word "Okay Nabu".

## Operating

- **Volume is the master knob.** On the built-in speaker, high volume causes audio garble
  *and* a self-answering echo loop (the speaker feeds the mic). Keep it at/below ~85%, or use
  an external speaker (below) to run louder safely.
- **Two-speed behaviour is expected:** commands/time/weather are near-instant (local intents);
  open-ended questions take 3–8 s (the LLM path).
- **Hardware for a good experience:** the built-in speaker is small by design — use the Voice
  PE's **3.5 mm line-out → a powered external speaker**, placed *away* from the puck (louder
  and far less echo). The dual-mic array is adequate; the real input-side limiter is Wi-Fi.

## Troubleshooting

- **Garbled or dropped audio** → usually volume too high (echo) or a slow synth underrunning
  the puck buffer on weak Wi-Fi. Lower the volume; prefer an external speaker; improve Wi-Fi.
  The hub TTS itself can be ruled in/out with a direct probe (below).
- **It answers itself in a loop** → the puck's speaker output re-triggers its own mic.
  Lower the volume; use an external speaker placed apart. Confirm via the hub raw log (count
  STT→chat→TTS cycles) and the device Activity log (a clean run ends in Idle).
- **Puck shows a flashing-blue light / entities Unavailable** → it is on Wi-Fi but not
  connected to HA, typically after a Wi-Fi re-associate moved it to a different IP and HA's
  cached connection went stale. Verify the current IP (`ping 192.168.0.42` vs `.103`), then
  **Settings → Devices & Services → ESPHome → ⋮ → Reload** to re-resolve. A puck power-cycle
  (unplug USB-C) is a safe last resort — it is *not* an HA/server restart.
- **Slow freeform answers** → inherent to the hub's `claude -p` path, not the network.
  Faster freeform requires a faster model (#234).
- **Do not** reconfigure the sfortis "Update TTS Agent" dialog casually — it shows defaults,
  not saved values, and submitting it resets the voice from `tara` to `alloy`.
- **Weak Wi-Fi** is the most common root cause of disconnects and audio issues. A healthy
  Voice PE pings <10 ms; sustained ~100 ms+ means the kitchen signal needs improving.

### Diagnostics quick-reference

- Hub raw log (all requests incl. TTS): `GET :8000/admin/api/hub/log/tail?lines=N`
- Hub telemetry (chat/STT only, with latency — does **not** record TTS):
  `GET :8000/admin/api/telemetry/recent?limit=N`
- Hub errors: `GET :8000/admin/api/hub/errors/recent`
- Probe TTS directly: `POST :8000/v1/audio/speech {model:orpheus,voice:tara,input,response_format}`
- Puck link quality: `ping 192.168.0.42` (expect <10 ms)
- Device state + Activity log: Settings → Devices & Services → ESPHome → the device
- Test the brain by text (no voice): Voice assistants → *Focused local assistant* → ⋮ →
  Start conversation
- Per-stage pipeline trace: Voice assistants → ⋮ → Debug

## Known constraints

- The setup wizard installs a temporary **Piper** TTS + **speech-to-phrase** STT baseline
  (a `wyoming` integration); these are superseded by the hub pipeline and are slated for
  removal.
- Local hub models are text-only; OpenAI-shape tool-use on the local llama backends requires
  `--jinja`. The hub's `claude -p` path accepts a `tools` payload but its tool-call emission
  is unverified — prefer deterministic routing (Tier 1) for actions.

## Roadmap (tracked as issues, not here)

- Action bridge — voice → real device control (deterministic): **live** for the alarm
  (#88 Phase 4; see above + [`voice-commands-howto.md`](voice-commands-howto.md)); native
  HA integration: #235.
- Local-model evaluation for the brain/classifier role: #234.
- Hardware: external powered speaker (3.5 mm) + stronger kitchen 2.4 GHz Wi-Fi.
