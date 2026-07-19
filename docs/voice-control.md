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
| Logic | `/v1/chat/completions`     | `qwen3.5-4b-nothink` | ~0.7–1.0 s (local; was `claude-haiku-4-5` at 3–8 s — #234) |
| TTS   | `/v1/audio/speech`         | `piper` (voice `amy`) | ~0.06 s warm for a short phrase (resident `piper.exe`, ONNX voice loaded once — `local-llm-hub#163`); 22.05 kHz |

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
   confirmation step for destructive actions. **Model picked (#234):** `qwen3.5-4b` with
   **thinking disabled** — 100 % schema-valid, 96 % accurate, ~300 ms, zero extra VRAM. Full
   benchmark in [`voice-model-benchmark.md`](voice-model-benchmark.md). **Now wired live:** the
   conversation agent runs the hub's no-think alias `qwen3.5-4b-nothink` (`local-llm-hub#159`
   + `#161`); end-to-end answers in ~0.7–1.0 s vs the old haiku brain's 3–8 s. The live agent
   still actuates via OpenAI function-calling (`execute_services`) rather than the dedicated
   Tier-2 component, which remains a separate future issue.
3. **Tier 3 — freeform / open tool-calling.** Only for open questions or novel composition;
   a larger/slower model is acceptable because these are rare.

Do **not** outsource all tool use to the model: for a bounded, safety-relevant command set,
deterministic classification + coded execution wins on latency, reliability, testability,
and safety. (Model evaluation for the Tier-2/3 role: #234.)

### Where actuation goes

Device control reaches the app through its existing **API-first** backend — `POST
…:8447/api/units/{id}` etc., behind `BearerTokenMiddleware` (loopback bypasses; remote uses a
bearer token). The app's logic lives in `src/` and the PWA is just another `/api/*` client, so
HA can call exactly what the PWA calls. The durable form is now the native HA integration under
[`custom_components/home_automation_app/`](../custom_components/home_automation_app/) (#235), documented in [`home-assistant-integration/`](home-assistant-integration/README.md): it exposes climate/switch/alarm/binary-sensor/energy-sensor entities while reusing the app API as the single source of truth.

## Native entity bridge (live, #235)

The Home Assistant integration exposes compatible app devices as first-class HA entities:

| Device surface | HA entity domain | Voice path |
|---|---|---|
| MELCloud HVAC | `climate` | Built-in HA climate intents: on/off, set temperature, mode where supported. |
| Tuya plugs | `switch` | Built-in HA switch intents. |
| RISCO alarm | `alarm_control_panel` | Built-in HA alarm services: away = full, home = perimeter, night = partial. |
| RISCO zones | `binary_sensor` | Read/status and automations. |
| SMA energy | `sensor` | Read/status and dashboards. |

This is the preferred path for new voice-capable devices because the command is entity-native and deterministic. Keep safety gates explicit: disarm still uses the existing code-gated custom sentence path until an equivalent HA-native code-gated flow is separately validated.

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
  alarm.yaml`, `configuration.snippet.yaml`, `secrets.snippet.yaml`. The same directory
  also carries the wake-alarm (#306), family-locator (#438), and grocery-list (#315)
  bridges — the grocery one targets the sibling grocery-shopping-automation app on
  `:8502` and includes the first **multi-turn** flow (`assist_satellite.ask_question`);
  see its README section.
- **Family locator (#438) + same-turn ETA (#470, #485, #487):** a read-only "where's mom/dad"
  query — `{who}` (a spoken name or household role) resolves server-side against role
  aliases / display names (`src.presence_roles`), then answers with the person's current
  place from cached Find My data matched against user-configured named places
  (`src.presence_places`) — home, a named place (e.g. "the gym"), or away. No new iCloud
  locate cost. Configured entirely from the webapp (Security tab → Presence card →
  per-person "Role" + "Places"), not from YAML. Resolution is variant-tolerant (accents,
  "Anna"↔"Ana", "mum"/"mamá"→mom — #446) and the locator also answers in Spanish on the
  "Hey Mycroft" pipeline ("¿dónde está papá?" → "Roberto está en casa"). When the person
  is **away**, the same reply also speaks a traffic-aware ETA (`GET /api/presence/eta` →
  `src.travel_time` over the Google Routes API, `routingPreference: TRAFFIC_AWARE`; needs
  `GOOGLE_MAPS_API_KEY` in the app's `.env`, degrades to a spoken fallback without it) —
  one turn, no question asked (#485 removed the earlier "do you want the ETA?"
  `assist_satellite.ask_question` follow-up because it didn't reliably get answered on the
  Voice PE hardware). Because it still needs a satellite handle to target the announce
  (`trigger.device_id`), the whole locator is the `presence_locator` conversation-trigger
  automation, not an `intent_script` — the `custom_sentences/*/locate.yaml` intents are
  emptied to hand it the match. Spanish turns used to come out in the English TTS voice
  because plain `message:` announces let HA pick the engine, and that pick doesn't follow
  the wake-word-triggered pipeline (#487) — fixed by pre-rendering the announcement as a
  `media-source://tts/<engine>` URI naming the exact TTS entity for `lang` (see
  "Pipelines" above) and passing it as `media_id` instead. See
  [`voice-pe-config/README.md`](voice-pe-config/README.md#family-locator-issue-438--wheres-momdad--same-turn-eta-470-485).
- **Adding more commands:** [`voice-commands-howto.md`](voice-commands-howto.md) — the
  reusable recipe (hassil sentence syntax, the `stop`/`action_response` gotcha,
  reload-vs-restart, code-gating, and testing a command without speaking). Read this
  before wiring the next device.

A known wart, captured in the how-to: the shipped intents speak off the HTTP `200`, so an
arm that the panel silently refuses (e.g. a zone left open) is still confirmed aloud —
the recommended fix is to read the *resulting* state back before speaking (#241).

### STT vocabulary bias — household names (#444)

"Ok Nabu, where's dad" was intermittently mis-transcribed by Whisper as "where's
that" — a phonetic near-miss for this household's non-native-English accent, verified
via text probe (`--text "where is that"` reproduces the exact failure; `--text "where
is dad"` resolves correctly). The fix biases whisper's recognition toward the
household's names/roles, but **not** via the HA integration's `prompt` option (see
above — verified as a no-op on this whisper.cpp build). Instead:

- `local-llm-hub#290` added a gitignored local overlay
  (`config/transcription_glossary.local.json`) to the hub's existing launch-time
  `boost_terms` vocabulary-boosting mechanism (issue #91), so private vocabulary never
  has to be committed to that public repo.
- The overlay is populated live-only on the hub host with this household's names/roles
  (`dad`, `mom`, `papá`, `mamá`, plus first names) — not committed here or there, per
  the same "repo is public, no real names in git" rule as `config/presence_roles.json`.
  Binds on the next `whisper` model-row (re)start (Models tab or admin API — boosting
  is a launch-time arg, not a live toggle).
- `voice-transcriber#131` was a **prerequisite**: port 8090 is mutex-shared between
  that project and `local-llm-hub`, and voice-transcriber's `whisper_server.yaml`
  defaulted to `mode: local`, which hard-fails if it ever needs to (re)start while the
  hub's process holds the port — flipped to `mode: external` (graceful reuse) so
  handing the port to the hub's boosted instance can't silently break dictation later.

**What's verified vs. not:** the boost vocabulary is confirmed live in the running
whisper-server's actual `--prompt` argument (checked via its process command line), and
round-trip TTS→STT probes confirm boosting has no observable effect on
already-unambiguous words ("dad", "mom" transcribe correctly with or without it — no
regression). What synthetic testing **cannot** prove is whether boosting actually
corrects this household's specific accent-driven confusion — clean TTS audio never
reproduces the ambiguity a real accent creates, and a `Nonna`-mishearing probe used as
a proxy showed no measurable correction from boosting alone. The real acceptance test
is physical: say "Ok Nabu, where's dad" a few times and check
`logs/presence_locate.jsonl` for `who: dad` resolving correctly. If it still misfires,
whisper's prompt-based biasing may simply be too weak for this specific confusion, and
the issue's own "last resort" (phonetic-confusion tolerance in
`src.presence_roles.resolve_person`) would need reconsidering.

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
  Its options flow exposes a per-request `prompt` field, but **do not set it** —
  verified empirically (#444) that this whisper.cpp server build silently ignores
  a per-request prompt entirely (its own request form doesn't even list one); the
  only mechanism that measurably biases recognition is the hub's launch-time
  `--carry-initial-prompt` + `boost_terms`. Household-name STT bias (mishearing
  "dad" as "that", #444) is wired there instead — see "STT vocabulary bias"
  below.
- **OpenAI TTS** (sfortis) — entry "OpenAI TTS - Hub Orpheus": endpoint
  `http://192.168.0.13:8000/v1/audio/speech`, model **`piper`**, voice **`amy`** (→
  `en_US-amy-medium`), `audio_format` mp3, chime OFF, key = hub token. Was Orpheus
  (`orpheus`/`tara`) until #280 — switched to the hub's **resident Piper** for ~25–30×
  faster speech (the hub-side change is `local-llm-hub#162`/`#163`); the voice moved from
  `ryan` to `amy` in #286 once the hub made Amy its default (`local-llm-hub#171`). The
  `model`/`voice` live in the integration's **profile subentry** in
  `/config/.storage/core.config_entries`
  (`entries[openai_tts].subentries[].data.model` / `.voice`), not in `configuration.yaml`;
  a pre-change backup is under `/config/backups/voice-tts-piper/` (and the ryan→amy switch
  under `/config/backups/voice-tts-amy/`). The TTS entity id is
  still `tts.openai_tts_orpheus` and the entry title still reads "Hub Orpheus" — both are
  now cosmetic labels only (renaming the entity would break the pipeline's `tts_engine`
  reference, so the surgical switch leaves them). To revert just the voice, set `voice`
  back to `ryan`; to **revert** to Orpheus, set `model` back to
  `orpheus` and `voice` to `tara` in that subentry and `ha core restart`. **Do not** use
  the sfortis "Update TTS Agent" UI dialog (see Troubleshooting — it resets the voice).
- **extended_openai_conversation** — entry "Hub Haiku": base URL
  `http://192.168.0.13:8000/v1`, model **`qwen3.5-4b-nothink`** (was `claude-haiku-4-5` —
  #234), key = hub token, max_tokens 150, temp 0.5, terse prompt (one short spoken
  sentence, no follow-up questions, no pleasantries). The model id is the hub's
  no-think alias (`local-llm-hub#161`) — the integration can't send
  `chat_template_kwargs`, so the hub injects `enable_thinking: false` for this id. The
  config lives in the entry's `conversation` subentry in `/config/.storage/core.config_entries`
  (the integration's options flow writes the same field); a pre-change backup is under
  `/config/backups/voice-brain-rewire/`. To revert, set `chat_model` back to
  `claude-haiku-4-5` and `ha core restart`.

### Pipelines

**"Focused local assistant"** (English, wake word "Okay Nabu"): conversation agent =
**Extended OpenAI Conversation**; STT = **Custom Whisper** (`stt_language: en`); TTS =
**hub Piper (amy)**; **"Prefer handling commands locally" = ON**.

**"Asistente (es)"** (Spanish, wake word "Hey Mycroft" — #315, #468): conversation agent =
**Home Assistant built-in** (deterministic only, no LLM fallback); STT = **Custom
Whisper** (`stt_language: es`); TTS = **Piper add-on** (`tts.piper`, voice
`es_ES-sharvard-medium`). Created directly in `/config/.storage/assist_pipeline.pipelines`
(backup under `/config/backups/voice-es-pipeline/`); the grocery bridge and its spoken
help menu live here.

### Wake words — two per puck, each bound to a pipeline

The Voice PE firmware detects wake words **on-device** (microWakeWord) and supports **two
simultaneous wake words per puck**, each routed to its own assistant pipeline. The binding
lives on the **device**, not in the pipeline config — both pipelines have `wake_word_entity:
null` in `/config/.storage/assist_pipeline.pipelines`. Each puck exposes a *Wake word* +
*Assistant* select pair plus a second *Wake word 2* + *Assistant 2* pair
(`select.home_assistant_voice_<id>_wake_word[_2]` / `…_assistant[_2]`). The *Assistant*
select is what names the pipeline that fires when its wake word is heard.

Both pucks are configured identically — a deliberate English/Spanish split (#315):

| Wake word | Routes to pipeline | What it does |
|---|---|---|
| **"Okay Nabu"** (slot 1) | **Focused local assistant** (en) | Hub stack — Custom Whisper STT (en hint) → Extended OpenAI (local-first, then the LLM) → hub Piper TTS (`amy`). English commands, freeform questions, the alarm + wake-alarm bridges. |
| **"Hey Mycroft"** (slot 2) | **Asistente (es)** | The Spanish assistant — Custom Whisper STT (es hint) → **built-in deterministic agent** (no LLM fallback) → **Piper add-on TTS** (`es_ES-sharvard-medium`). Carries the grocery-list bridge ("¿qué puedo hacer?" speaks the command menu), the family locator, and — since #466 — the **RISCO alarm** ("arma la alarma", "¿cómo está la alarma?", "desarma la alarma \<código\>") and **wake alarms** ("pon una alarma para las siete y media entre semana") in Spanish. Same deterministic doctrine: an unmatched phrase gets "no entiendo", never an LLM improvisation. |

Slot 2 was originally "Hey Jarvis"; swapped to "Hey Mycroft" in #468 because "Hey Jarvis"
is the only firmware built-in with no [microWakeWord v2 model](https://github.com/OHF-Voice/micro-wake-word/releases)
— it still runs the old v1 model, which v2 roughly doubles the accuracy of and specifically
improves for background noise and non-native accents. Sensitivity was already maxed with no
other lever, so the fix was the model, not a setting.

So say **"Okay Nabu"** in English, **"Hey Mycroft"** in Spanish. Language is a
pipeline-level property (STT hint, TTS voice, and sentence matching all follow it), so
the wake word *is* the language switch — see `voice-commands-howto.md` "Mixing English
and Spanish" for why mid-conversation switching can't work.

**Available wake words are limited to the firmware built-ins** — `no_wake_word`,
`Hey Jarvis`, `Hey Mycroft`, `Okay Nabu` (the on-device microWakeWord models baked into the
Voice PE firmware). A **custom** wake word (e.g. a Spanish phrase) is **not** selectable
here: it requires training a custom model and **flashing custom ESPHome firmware** to each
puck — investigated and deferred in #266. microWakeWord is the on-device route (the
maintainer rates a good custom model "very difficult"); openWakeWord trains easily on Colab
but needs an unofficial *streaming*-firmware fork (always-on audio to the server) the stock
Voice PE firmware doesn't support; and synthetic training data for Spanish has weak speaker
variety. Not worth the flash risk for now.

**Changing the mapping** — which built-in word sits in a slot, or which pipeline a slot
routes to — is a device-side select, so it needs no firmware flash and no browser. Over the
REST API (loopback or token; `$HA_URL`/`$HA_TOKEN` from `.env`):

```bash
# point "Hey Mycroft" (slot 2) at the Focused local assistant instead of Home Assistant
curl -s -X POST -H "Authorization: Bearer $HA_TOKEN" -H "Content-Type: application/json" \
  -d '{"entity_id":"select.home_assistant_voice_<id>_assistant_2","option":"Focused local assistant"}' \
  "$HA_URL/api/services/select/select_option"

# swap the wake word in a slot (built-ins only)
curl -s -X POST -H "Authorization: Bearer $HA_TOKEN" -H "Content-Type: application/json" \
  -d '{"entity_id":"select.home_assistant_voice_<id>_wake_word","option":"Hey Mycroft"}' \
  "$HA_URL/api/services/select/select_option"
```

The current values are restored on boot from `/config/.storage/core.restore_state`; read
them back with `GET $HA_URL/api/states/select.home_assistant_voice_<id>_wake_word`.

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
- **Slow LLM answers** → the brain now runs the local `qwen3.5-4b-nothink` (~0.7–1.0 s
  end-to-end, #234), not `claude-haiku-4-5`. If answers regress to 3–8 s, check the agent's
  `chat_model` hasn't reverted to a `claude-*` (cloud `claude -p`) id, and that the hub's
  qwen backend is up (`GET :8000/admin/api/models`). See
  [`voice-model-benchmark.md`](voice-model-benchmark.md).
- **Do not** reconfigure the sfortis "Update TTS Agent" dialog casually — it shows defaults,
  not saved values, and submitting it resets the configured voice (now `amy`) to `alloy`
  and the model off `piper`. Edit the profile subentry in `.storage` instead (see Setup).
- **Weak Wi-Fi** is the most common root cause of disconnects and audio issues. A healthy
  Voice PE pings <10 ms; sustained ~100 ms+ means the kitchen signal needs improving.

### Diagnostics quick-reference

- Hub raw log (all requests incl. TTS): `GET :8000/admin/api/hub/log/tail?lines=N`
- Hub telemetry (chat/STT only, with latency — does **not** record TTS):
  `GET :8000/admin/api/telemetry/recent?limit=N`
- Hub errors: `GET :8000/admin/api/hub/errors/recent`
- Probe TTS directly: `POST :8000/v1/audio/speech {model:piper,voice:amy,input,response_format}`
- Puck link quality: `ping 192.168.0.42` (expect <10 ms)
- Device state + Activity log: Settings → Devices & Services → ESPHome → the device
- Test the brain by text (no voice): Voice assistants → *Focused local assistant* → ⋮ →
  Start conversation
- Per-stage pipeline trace: Voice assistants → ⋮ → Debug

## Known constraints

- Two Pipers, both load-bearing — don't confuse them. The **hub Piper** (OpenAI-shape
  `/v1/audio/speech`, sfortis integration, voice `amy`) serves the English pipeline; the
  **wyoming Piper add-on** on the VM (`core_piper`, voice `es_ES-sharvard-medium` since
  #315) serves the Spanish pipeline's TTS. The add-on was the setup wizard's leftover
  baseline and was slated for removal until the Spanish pipeline repurposed it — it
  stays. (**speech-to-phrase** remains installed but unused.)
- Local hub models are text-only; OpenAI-shape tool-use on the local llama backends requires
  `--jinja`. The hub's `claude -p` path accepts a `tools` payload but its tool-call emission
  is unverified — prefer deterministic routing (Tier 1) for actions.

## Roadmap (tracked as issues, not here)

- Action bridge — voice → real device control (deterministic): **live** for the alarm
  (#88 Phase 4; see above + [`voice-commands-howto.md`](voice-commands-howto.md)); native
  HA integration: #235.
- Local-model evaluation for the brain/classifier role: **done + wired live** (#234) —
  Tier-2 pick `qwen3.5-4b` (thinking off), now serving the live brain via the hub's
  `qwen3.5-4b-nothink` alias (`local-llm-hub#159` + `#161`); ~0.7–1.0 s end-to-end. See
  [`voice-model-benchmark.md`](voice-model-benchmark.md).
- Hardware: external powered speaker (3.5 mm) + stronger kitchen 2.4 GHz Wi-Fi.
