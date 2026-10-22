# Voice control — HA Voice PE + local LLM hub

Operations & integration guide for hands-free, fully-local voice control: a Home Assistant
Voice Preview Edition (Voice PE) puck driven by the `local-llm-hub`, with **no cloud**. This
is a reference manual (architecture, setup, operating, troubleshooting), not a changelog —
history lives in git.

> **Secrets are not here** (the repo is public). The Wi-Fi password, hub token value, and
> device MAC addresses live only in the gitignored `_local/voice-pe-spike-STARTER.md` on the
> build machine.

## What it is

Loop: **wake word → speech-to-text → conversation agent → action or spoken answer → text-to-
speech**, all on the local stack. Common commands (time, on/off, set, weather) are answered
**instantly by Home Assistant's built-in intents**; only open-ended questions go to the LLM.

Common commands route **deterministically** — spoken phrase → HA local sentence match →
`intent_script` → `rest_command` → the app's HTTP API — so no LLM is on the command path and a
hallucinated reply can never actuate. The first live bridge is **alarm control**: *"Okay Nabu,
perimeter on"* / *"disarm now"* / *"what's the alarm status"* hit
`POST /api/security/{arm,partial,perimeter,disarm}` and `GET /api/security`, with a
spoken-code gate on disarm (see "Action bridge" below). **Wake alarms** are the second
bridge, **reminders** the third and **calendar** the fourth — see
[`timers-reminders-calendar.md`](timers-reminders-calendar.md).

Related docs: [`voice-commands-howto.md`](voice-commands-howto.md) (the reusable recipe for
wiring more commands — start there for every new command),
[`voice-pe-config/`](voice-pe-config/) (the installed, secret-free HA config),
[`home-assistant-vm.md`](home-assistant-vm.md) (the HA VM: SSH, `ha` CLI, REST API, when the
browser is allowed), [`security-alarm.md`](security-alarm.md), [`presence.md`](presence.md),
[`search-engine.md`](search-engine.md).

## Architecture

### Reuse the hub — no new models

All three voice stages are OpenAI-shaped endpoints the `local-llm-hub` already serves on
`http://192.168.0.13:8000` (binds `0.0.0.0`):

| Stage | Hub endpoint | model | Expected latency |
|---|---|---|---|
| STT   | `/v1/audio/transcriptions` | `whisper`           | 0.5–0.9 s |
| Logic | `/v1/chat/completions`     | `qwen3.5-4b-nothink` | ~0.7–1.0 s (local; a cloud `claude-*` brain would take 3–8 s) |
| TTS   | `/v1/audio/speech`         | `piper` (voice `amy`) | ~0.06 s warm for a short phrase (resident `piper.exe`, ONNX voice loaded once); 22.05 kHz |

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
   confirmation step for destructive actions. **Model picked:** `qwen3.5-4b` with
   **thinking disabled** — 100 % schema-valid, 96 % accurate, ~300 ms, zero extra VRAM. Full
   benchmark in [`voice-model-benchmark.md`](voice-model-benchmark.md). The conversation
   agent runs the hub's no-think alias `qwen3.5-4b-nothink`; end-to-end answers take
   ~0.7–1.0 s. The live agent actuates via OpenAI function-calling (`execute_services`)
   rather than the dedicated Tier-2 component, which is not built yet.
3. **Tier 3 — freeform / open tool-calling.** Only for open questions or novel composition;
   a larger/slower model is acceptable because these are rare.

Do **not** outsource all tool use to the model: for a bounded, safety-relevant command set,
deterministic classification + coded execution wins on latency, reliability, testability,
and safety. (Model evaluation for the Tier-2/3 role: [`voice-model-benchmark.md`](voice-model-benchmark.md).)

### Where actuation goes

Device control reaches the app through its existing **API-first** backend — `POST
…:8447/api/units/{id}` etc., behind `BearerTokenMiddleware` (loopback bypasses; remote uses a
bearer token). The app's logic lives in `src/` and the PWA is just another `/api/*` client, so
HA can call exactly what the PWA calls. The durable form is the native HA integration under
[`custom_components/home_automation_app/`](../custom_components/home_automation_app/),
documented in [`home-assistant-integration/`](home-assistant-integration/README.md): it exposes climate/switch/alarm/binary-sensor/energy-sensor entities while reusing the app API as the single source of truth.

## Native entity bridge

The Home Assistant integration exposes compatible app devices as first-class HA entities:

| Device surface | HA entity domain | Voice path |
|---|---|---|
| MELCloud HVAC | `climate` | Built-in HA climate intents: on/off, set temperature, mode where supported. |
| Tuya plugs | `switch` | Built-in HA switch intents. |
| RISCO alarm | `alarm_control_panel` | Built-in HA alarm services: away = full, home = perimeter, night = partial. |
| RISCO zones | `binary_sensor` | Read/status and automations. |
| FusionSolar energy | `sensor` | Read/status and dashboards. |

This is the preferred path for new voice-capable devices because the command is entity-native and deterministic. Keep safety gates explicit: disarm uses the existing code-gated custom sentence path until an equivalent HA-native code-gated flow is separately validated.

## Action bridge — deterministic alarm commands

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
  also carries the wake-alarm, family-locator, and grocery-list bridges — the grocery one
  targets the sibling grocery-shopping-automation app on `:8502` and includes the first
  **multi-turn** flow (`assist_satellite.ask_question`); see its README section.
- **Family locator + same-turn ETA:** a read-only "where's mom/dad"
  query — `{who}` (a spoken name or household role) resolves server-side against role
  aliases / display names (`src.presence_roles`), then answers with the person's current
  place from cached Find My data matched against user-configured named places
  (`src.presence_places`) — home, a named place (e.g. "the gym"), or away. No new iCloud
  locate cost. Configured entirely from the webapp (a person's "Role" in Security › People, named
  places in Settings › Home & people › Places), not from YAML. Resolution is variant-tolerant (accents,
  "Anna"↔"Ana", "mum"/"mamá"→mom) and the locator also answers in Spanish on the
  "Hey Mycroft" pipeline ("¿dónde está papá?" → "Roberto está en casa"). When the person
  is **away**, the same reply also speaks a traffic-aware ETA (`GET /api/presence/eta` →
  `src.travel_time` over the Google Routes API, `routingPreference: TRAFFIC_AWARE`; needs
  `GOOGLE_MAPS_API_KEY` in the app's `.env`, degrades to a spoken fallback without it) —
  one turn, no question asked: an `assist_satellite.ask_question` "do you want the ETA?"
  follow-up does not reliably get answered on the Voice PE hardware. Because the locator
  still needs a satellite handle to target the announce (`trigger.device_id`), the whole
  locator is the `presence_locator` conversation-trigger automation, not an
  `intent_script` — the `custom_sentences/*/locate.yaml` intents are emptied to hand it the
  match. Announcements are pre-rendered as a `media-source://tts/<engine>` URI naming the
  exact TTS entity for `lang` (see "Pipelines" below) and passed as `media_id`, because
  plain `message:` announces let HA pick the engine, and that pick doesn't follow the
  wake-word-triggered pipeline — Spanish turns would otherwise come out in the English TTS
  voice. See
  [`voice-pe-config/README.md`](voice-pe-config/README.md#family-locator-issue-438--wheres-momdad--same-turn-eta-470-485).
- **Adding more commands:** [`voice-commands-howto.md`](voice-commands-howto.md) — the
  reusable recipe (hassil sentence syntax, the `stop`/`action_response` gotcha,
  reload-vs-restart, code-gating, and testing a command without speaking). Read this
  before wiring the next device.

A known wart, captured in the how-to: the shipped intents speak off the HTTP `200`, so an
arm that the panel silently refuses (e.g. a zone left open) is still confirmed aloud —
the recommended fix is to read the *resulting* state back before speaking.

### STT vocabulary bias — household names

"Ok Nabu, where's dad" can be mis-transcribed by Whisper as "where's that" — a phonetic
near-miss for this household's non-native-English accent, verified via text probe
(`--text "where is that"` reproduces the exact failure; `--text "where is dad"` resolves
correctly). Recognition is biased toward the household's names/roles, but **not** via the HA
integration's `prompt` option (see "HA integrations" below — verified as a no-op on this
whisper.cpp build). Instead:

- The hub has a gitignored local overlay (`config/transcription_glossary.local.json`) on
  its launch-time `boost_terms` vocabulary-boosting mechanism, so private vocabulary never
  has to be committed to that public repo.
- The overlay is populated live-only on the hub host with this household's names/roles
  (`dad`, `mom`, `papá`, `mamá`, plus first names) — not committed here or there, per
  the same "repo is public, no real names in git" rule as `config/presence_roles.json`.
  Binds on the next `whisper` model-row (re)start (Models tab or admin API — boosting
  is a launch-time arg, not a live toggle).
- Port 8090 is mutex-shared between `voice-transcriber` and `local-llm-hub`, and
  voice-transcriber's `whisper_server.yaml` sets `mode: external` (graceful reuse) rather
  than `mode: local`, which hard-fails if it ever needs to (re)start while the hub's
  process holds the port — so handing the port to the hub's boosted instance can't
  silently break dictation later.

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
the last resort — phonetic-confusion tolerance in
`src.presence_roles.resolve_person` — would need reconsidering.

## Web search (Tier 3)

Without search, the freeform brain (`qwen3.5-4b-nothink`, Tier 3) has no web access: its
prompt says "if you do not know, say so", so any question needing current info (news,
sports scores, prices, facts outside training data) would just get "I don't know". Web
search is read-only information retrieval feeding the same terse one-sentence-spoken-reply
prompt — it never gains actuation capability, so it doesn't touch the Tier 1/2 safety
doctrine at all.

**Choice: self-hosted [SearXNG](https://docs.searxng.org/)**, not a hosted paid API —
no key, no per-query cost, keeps the local-first posture of the rest of this pipeline.
Runs as a Docker container on the hub PC (`192.168.0.13:8085`), in the sister
`local-llm-hub` repo's `docker/searxng/` stack (`start_searxng.bat`/`stop_searxng.bat`,
mirroring its `docker/langfuse/`) — not in this repo, since it's standalone host
infra rather than application code. JSON output is off by default in SearXNG
(abuse-prevention); `docker/searxng/config/settings.yml` enables `search.formats: [html,
json]`. Reachable from the HA VM with no extra firewall rule — Docker Desktop's WSL2
networking exposes published ports to the LAN without going through Windows Firewall the
way a native process would.

**Wiring:** a `rest`-type function (`web_search`) on the "Hub Haiku"
`extended_openai_conversation` subentry, alongside `execute_services` — the same
`.storage/core.config_entries` edit pattern as the model and TTS changes, backed up under
`/config/backups/voice-web-search/` before writing. The model
calls it with a `query` argument; `resource_template` hits
`http://192.168.0.13:8085/search?q={{ query | urlencode }}&format=json` (the executor
passes the function's arguments as the template's own variable namespace — `{{ query
}}`, not `{{ arguments.query }}`; test the template against HA's
`/api/template` endpoint before wiring it live); `value_template` trims the JSON response
to the **top 8** results' title + a **200-char** snippet so the tool-result context stays
small without starving the model of the current answer. The system prompt tells the model
to call `web_search` for current-info questions, to say so (never guess) if it finds
nothing useful, to trust the most specific dated result, and never to combine facts from
two different results. `temperature` is `0.2` for more faithful, less "creative"
summarization of search results.

### Why top-8 / 200-char, temperature 0.2

A narrow top-3/150-char window produces confidently wrong answers
for "who won X" style questions — e.g. "who won the last world cup" answered "Argentina
... 2022" after the 2026 World Cup (won by Spain) had already concluded. The tool-calling
path itself works: HA debug logs confirm the model correctly calls
`web_search`, SearXNG returns real, correct results, and the model faithfully summarizes
what it is given — the problem is that the top-ranked SearXNG hit for this query shape
is often a stale-cached reference/list page (Google's cached snippet for "List of FIFA
World Cup finals - Wikipedia" still read "...from 1930 to 2022"), while the correct
current answer sits at result #6+, outside a 3-snippet window. A secondary failure mode
at a wider window with `temperature: 0.5`: the small `qwen3.5-4b-nothink`
model occasionally conflates facts across two different snippets in the same reply (one
trial mixed an unrelated 1982-era snippet with the correct 2026 facts into "Italy...
defeating Argentina"). Top-8/200-char, the "don't combine facts" prompt
rule, and `temperature: 0.2` took the question from 0/2 correct to 10/10
correct in a fresh batch, verified live on the puck. This is inherently probabilistic
(free key-less search snippet freshness + a 4B model's synthesis fidelity), not something
config tuning alone can guarantee to 100% — a rare wrong answer on a very recently
current-events question is still possible.

**Spanish.** The Spanish pipeline ("Hey Mycroft") runs HA's built-in
conversation agent — deterministic-only, no LLM fallback, by design (see "Command
routing" above), so it does not get an LLM agent. Instead the pipeline stays
deterministic and the *app* does the searching: `docs/voice-pe-config/custom_sentences/es/websearch.yaml`
matches a bounded set of search-shaped phrasings (`busca ...`, `qué es ...`, `quién
ganó ...`, `cuándo es ...`, `cómo quedó ...`, `qué tiempo hace en ...`, `a cuánto está
...`, `dime ...`, etc. — no catch-all wildcard, which would shadow other intents) and fires
the `WebSearch` intent_script, whose `rest_command.voice_search` POSTs the spoken
question to the app's `POST /api/voice/search` (`{query}` → `{ok, speech, source}`, always
200). The endpoint searches SearXNG with `language=es`, takes the top 8 hits × 200-char
snippets, and makes one summarising pass through the local hub; HA speaks `speech`
verbatim. It never answers from the model's training cutoff: SearXNG down, no results and
a failing hub each return their own fixed Spanish sentence (SearXNG down: "No puedo
buscar ahora mismo."), and if the app itself is unreachable the intent_script's own
fallback says the same.

**Verified end-to-end** via `POST /api/conversation/process` (no browser, no live speaker
test needed — matches the probe-based verification used elsewhere in this repo):

| Question (English) | Answer | Latency |
|---|---|---|
| "who won the last super bowl" | "The Seattle Seahawks won the last Super Bowl against the New England Patriots." | 1788 ms |
| "what is the current price of one bitcoin in dollars" | "One Bitcoin is currently trading at approximately $64,000." | 1953 ms |
| "what is the weather forecast for tokyo tomorrow" | "Tokyo is expected to be rainy tomorrow with a high of 77 degrees and a low of 75 degrees." | 2036 ms |
| "what is 2 plus 2" (no search needed — regression check) | "2 plus 2 equals 4." | 323 ms |
| "what is the capital of france" (no search needed) | "The capital of France is Paris." | 311 ms |

With the current settings (top-8/200-char window, `temperature: 0.2`), 10 fresh trials of
the question below plus a live puck check:

| Question (English) | Answer | Result |
|---|---|---|
| "who won the last world cup" | "Spain won the 2026 FIFA World Cup, defeating Argentina 1-0 in extra time." | 10/10 correct |
| "what is the latest model from OpenAI" (live, on the puck) | correct, per live confirmation | 1/1 correct |

Search-backed answers land around **1.8–2.0 s** (vs. the ~0.7–1.0 s baseline) —
the added cost is one LAN round trip to SearXNG plus a second model pass to summarize the
result. Non-search questions are unaffected. Spanish search is verified by
`ha_config_sync.py probe`.

**Webapp status card:** the Home Assistant disclosure's **Search engine** sub-card
(`GET /api/searxng`, `POST /api/searxng/start`) shows the container's live status and a
Start button when it's down — `src/searxng_client.py` mirrors `src/hyperv_client.py`'s
shape (shell out, flatten, "partial data stays 200"), probing `/healthz` to distinguish
"container up" from "actually answering queries". More in
[`search-engine.md`](search-engine.md).

### Backend supervision

If the SearXNG container is down, `web_search` fails and the 4B brain answers from its
training cutoff instead — confidently and with no caveat. One observed outage: the puck
answered "what's the latest large language model from OpenAI" with **"GPT-4.0"** — twice —
after the container had exited cleanly (exit 0) and stayed **down for eight days**. Nothing
was wrong with the tool-calling path or the prompt; the sibling Langfuse stack in the same
daemon stayed `Up`, which ruled out a Docker-wide fault.

Two things make such an outage possible. Docker's `restart: unless-stopped` — correct
for most containers — deliberately honours a graceful stop *forever*, so one clean
`docker stop` is permanent. And the Search-engine card, while it renders the down state
correctly, is buried inside the Home Assistant disclosure, so nothing surfaces it anywhere
a human would trip over it.

The durable fix is that the webapp **supervises the container it already knows how to
read and start**: `app/webapp/searxng_watchdog.py`, a 120-second lifespan loop reusing the
exact `start_searxng()` path the card's Start button exposes — no new privilege,
just no longer requiring someone to be looking. A stopped container is brought back on the
next poll. A container that is *running but not answering `/healthz`* — the shape no
`restart:` policy catches — is `docker compose restart`ed, but only after a ~6-minute grace
window, since a healthy container looks exactly like that while it boots. Attempts back off
exponentially (2-minute base, 30-minute cap) so a genuinely broken stack does not thrash
`docker`, logging fires only on state changes, and a `docker compose` command that exits 0
without restoring `/healthz` is recorded as a **failure, not a recovery** — "the command
worked" and "search works" are different facts.

The sister `local-llm-hub` repo carries the complementary half (its SearXNG compose uses
`restart: always`, so the policy stops treating a manual stop as permanent). The two are
independent: only the watchdog covers the wedged-but-running case.

**Known gap — the deeper half.** When `web_search` is unavailable the model answers from
its cutoff rather than saying it cannot search, so the failure mode is a *wrong* answer
rather than a missing one. Hardening that is a live `.storage/core.config_entries` prompt
edit (not repo-managed code); self-healing the backend removes
the common trigger, not the underlying behaviour.

**To revert:** remove the `web_search` entry from the `functions` YAML in the Hub Haiku
subentry (restore from `/config/backups/voice-web-search/` for the original rollout, or
`/config/backups/voice-web-search-topn/` for just the top-8/temperature tuning) and
drop the added prompt rule(s), then `ha core restart`. The Spanish custom_sentences/
intent_script can stay — they degrade to a no-op "no entiendo" only if the English side is
also reverted and someone still asks in Spanish, which is harmless either way.

## Setup reference

### Substrate

Home Assistant OS in a **Hyper-V Gen2 VM** on an **External/bridged vSwitch** tied to the
wired NIC, so HA gets a real LAN IP and the Wi-Fi puck reaches it with no NAT. A Windows
Firewall inbound rule allows the VM to reach the hub on TCP 8000. The HA **Terminal & SSH**
add-on is the shell, and is the **code-driven config path**: `scripts/ha_config_sync.py`
deploys the repo-owned voice-PE config into `/config` over LAN SSH and validates it
(see "Home Assistant config deploy over SSH" below and
[`voice-commands-howto.md`](voice-commands-howto.md)). HAOS **host** SSH on
`:22222` is a separate break-glass developer channel — not used for config deploys.

### Network

| Host | Address | Notes |
|---|---|---|
| local-llm-hub (PC) | `192.168.0.13` | hub `:8000` (admin UI at `:8000/admin/`); app API `:8447` |
| HA VM | `192.168.0.4` | wired/bridged, ~0 ms; UI `http://192.168.0.4:8123`; static-MAC + DHCP-reserved |
| Voice PE puck | `192.168.0.42` | 2.4 GHz Wi-Fi; **reserved IP** (a re-associate can move it here from a prior lease — verify by ping) |

### HA integrations (HACS)

- **OpenAI Whisper Cloud** (STT) — entry "Custom Whisper": provider *Custom*, URL
  `http://192.168.0.13:8000/v1/audio/transcriptions`, model `whisper`, key = hub token.
  Its options flow exposes a per-request `prompt` field, but **do not set it** —
  verified empirically that this whisper.cpp server build silently ignores
  a per-request prompt entirely (its own request form doesn't even list one); the
  only mechanism that measurably biases recognition is the hub's launch-time
  `--carry-initial-prompt` + `boost_terms`. Household-name STT bias (mishearing
  "dad" as "that") is wired there instead — see "STT vocabulary bias" above.
- **OpenAI TTS** (sfortis) — entry "OpenAI TTS - Hub Orpheus": endpoint
  `http://192.168.0.13:8000/v1/audio/speech`, model **`piper`**, voice **`amy`** (→
  `en_US-amy-medium`), `audio_format` mp3, chime OFF, key = hub token. The model is the
  hub's **resident Piper**, ~25–30× faster than Orpheus (the hub keeps `piper.exe`
  resident with the ONNX voice loaded once). The
  `model`/`voice` live in the integration's **profile subentry** in
  `/config/.storage/core.config_entries`
  (`entries[openai_tts].subentries[].data.model` / `.voice`), not in `configuration.yaml`;
  a pre-change backup is under `/config/backups/voice-tts-piper/` (and the ryan→amy voice
  switch under `/config/backups/voice-tts-amy/`). The TTS entity id is
  `tts.openai_tts_orpheus` and the entry title reads "Hub Orpheus" — both are
  cosmetic labels only (renaming the entity would break the pipeline's `tts_engine`
  reference, so they are left as is). To revert just the voice, set `voice`
  back to `ryan`; to **revert** to Orpheus, set `model` back to
  `orpheus` and `voice` to `tara` in that subentry and `ha core restart`. **Do not** use
  the sfortis "Update TTS Agent" UI dialog (see Troubleshooting — it resets the voice).
- **extended_openai_conversation** — entry "Hub Haiku": base URL
  `http://192.168.0.13:8000/v1`, model **`qwen3.5-4b-nothink`**,
  key = hub token, max_tokens 150, temp 0.2 (faithful summarization of search results), terse
  prompt (one short spoken sentence, no follow-up questions, no pleasantries). The model id
  is the hub's no-think alias — the integration can't send
  `chat_template_kwargs`, so the hub injects `enable_thinking: false` for this id. The
  config lives in the entry's `conversation` subentry in `/config/.storage/core.config_entries`
  (the integration's options flow writes the same field); a pre-change backup is under
  `/config/backups/voice-brain-rewire/`. To revert, set `chat_model` back to
  `claude-haiku-4-5` and `ha core restart`.

### Pipelines

**"Focused local assistant"** (English, wake word "Okay Nabu"): conversation agent =
**Extended OpenAI Conversation**; STT = **Custom Whisper** (`stt_language: en`); TTS =
**hub Piper (amy)**; **"Prefer handling commands locally" = ON**.

**"Asistente (es)"** (Spanish, wake word "Hey Mycroft"): conversation agent =
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

Both pucks are configured identically — a deliberate English/Spanish split:

| Wake word | Routes to pipeline | What it does |
|---|---|---|
| **"Okay Nabu"** (slot 1) | **Focused local assistant** (en) | Hub stack — Custom Whisper STT (en hint) → Extended OpenAI (local-first, then the LLM) → hub Piper TTS (`amy`). English commands, freeform questions, the alarm + wake-alarm + reminders bridges. |
| **"Hey Mycroft"** (slot 2) | **Asistente (es)** | The Spanish assistant — Custom Whisper STT (es hint) → **built-in deterministic agent** (no LLM fallback) → **Piper add-on TTS** (`es_ES-sharvard-medium`). Carries the grocery-list bridge ("¿qué puedo hacer?" speaks the command menu), the family locator, the **RISCO alarm** ("arma la alarma", "¿cómo está la alarma?", "desarma la alarma \<código\>") and **wake alarms** ("pon una alarma para las siete y media entre semana") in Spanish. Same deterministic doctrine: an unmatched phrase gets "no entiendo", never an LLM improvisation. |

Slot 2 is "Hey Mycroft" rather than "Hey Jarvis" because "Hey Jarvis"
is the only firmware built-in with no [microWakeWord v2 model](https://github.com/OHF-Voice/micro-wake-word/releases)
— it still runs the old v1 model, which v2 roughly doubles the accuracy of and specifically
improves for background noise and non-native accents. Sensitivity is already maxed with no
other lever, so the model, not a setting, is what matters.

So say **"Okay Nabu"** in English, **"Hey Mycroft"** in Spanish. Language is a
pipeline-level property (STT hint, TTS voice, and sentence matching all follow it), so
the wake word *is* the language switch — see [`voice-commands-howto.md`](voice-commands-howto.md) "Mixing English
and Spanish" for why mid-conversation switching can't work.

**Available wake words are limited to the firmware built-ins** — `no_wake_word`,
`Hey Jarvis`, `Hey Mycroft`, `Okay Nabu` (the on-device microWakeWord models baked into the
Voice PE firmware). A **custom** wake word (e.g. a Spanish phrase) is **not** selectable
here: it requires training a custom model and **flashing custom ESPHome firmware** to each
puck — investigated and deferred. microWakeWord is the on-device route (the
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

## Devices: Voice PE rooms + push-to-talk

The **Home Assistant** group on the **Devices** tab holds three rows. The VM row says the state in words (`Online · up …` / `Off` / unavailable; IP·MAC and stale-snapshot detail sit in its hover tooltip) with the VM **power switch** (start, or confirm-gated stop) as its trailing item. **Voice satellites** (every `assist_satellite` Home Assistant currently owns, with push-to-talk) and **Recent interactions** (hidden until there is one) each open a sheet. **What can I do?** (the capabilities guide, a Settings card) is the "In this app" part of **What can I say?** (below).

Push-to-talk copies App Launcher's proven local path: the browser records with `MediaRecorder`, sends ordered one-second chunks to this app, and this app proxies Voice Transcriber's stable `POST /api/sessions` → `/chunk` → SSE `/events` → `/finish` contract. Rolling Whisper partials replace the visible text while you speak; Stop settles the canonical transcript and calls HA's `assist_satellite.announce` for that room. Sessions carry `source: home-automation`, so audio/transcript recovery and attribution stay in Voice Transcriber History. Voice Transcriber reuses the already-running `:8090` Whisper process (currently local-LLM-hub-owned); this app loads no model and retains no audio.

The webapp also ingests HA's bounded Assist debug traces while it is running. Every 15 seconds it lists the three pipelines, fetches only unseen completed runs, normalizes wake/pipeline, STT text, local-vs-fallback intent, action/target, spoken response, satellite, outcome, and timestamp, and stores only that compact event in `webapp/telemetry.sqlite3`. The dedupe set is capped at 256 IDs; no raw trace blob or audio is held. Detailed satellite reads happen once on entering the Devices tab, then every 15 s only while a Home Assistant sheet is open. Logging is intentionally best-effort: interactions during webapp downtime are not guaranteed.

Runtime API: `GET /api/ha`; `POST /api/ha/satellites/{entity_id}/announce`; and the App Launcher-shaped transcription proxy under `/api/ha/transcribe*`. It reuses `HA_URL` / `HA_TOKEN`; `VOICE_TRANSCRIBER_URL` defaults to `https://127.0.0.1:8443`; `HA_TRACE_ENABLED=0` disables only background trace ingestion.

**Measured warm-path latency.** A 4.24 s local speech sample, sent through the live HTTPS webapp in four browser-shaped chunks, produced its first rolling partial at **2.81 s**, returned the canonical transcript **1.33 s after Stop**, and had HA accept the room-specific `assist_satellite.announce` call **6.75 s later**. The final interval is HA's synchronous announce/TTS service time (the endpoint validates one entity state and does not reload registries); it measures HTTP acceptance, not when the puck finishes speaking. This supports the bounded push-to-talk interaction but is a **no-go for a Tier-3 always-open conversation mode**: continuous capture would add wake/VAD, interruption and full-duplex lifecycle work, idle microphone/privacy exposure, and sustained resource use without removing HA's dominant response delay. No Tier-3 conversation mode is planned.

## Settings: "What can I say?" cheat sheet

The reference companion to the push-to-talk mic above: the **"What can I say?"** sheet in Settings › Voice lists every wired voice command, so remembering one doesn't mean opening GitHub on a phone. Six groups — **Alarm**, **Wake alarms**, **Reminders**, **Family locator**, **Grocery list**, and the **built-ins** HA answers for free (timers, time/weather, exposed-entity on/off) — each command showing a worked example, its other phrasings, and what it speaks back. Phrasings are tagged with their wake word, since that is what picks the language: "Okay Nabu" is English, "Hey Mycroft" is Spanish (the family locator answers on both; grocery is Spanish-only) — "Hey Mycroft" rather than "Hey Jarvis" because Jarvis is the only built-in wake word still on the older, less accurate v1 [microWakeWord](https://github.com/OHF-Voice/micro-wake-word/releases) model. Read-only — commands are wired in Home Assistant, not here. Its last part, **In this app**, covers what *this webapp* does with Home Assistant rather than what you can *say*.

The content is curated in [`src/voice_commands.py`](../src/voice_commands.py) and served by `GET /api/voice-commands`; the card fetches it once on first open and never polls (it only changes when the app is redeployed). It is deliberately **not** derived from `docs/voice-pe-config/custom_sentences/*.yaml`: those are hassil templates rather than readable examples, and they don't cover the multi-turn grocery flow or HA's built-ins. The cost of curating — a second place to update — is paid down by step 6 of [`voice-commands-howto.md`](voice-commands-howto.md)'s recipe, which makes updating the cheat sheet part of wiring any new command. The card never publishes a code or secret: the disarm command shows the same `<your code>` placeholder [`voice-pe-config/README.md`](voice-pe-config/README.md) already does, and `tests/test_voice_commands.py` fails the build if a real one is ever pasted in.

## Home Assistant config deploy over SSH

`scripts/ha_config_sync.py` deploys the repo-owned voice-PE config into the HA VM's `/config` over SSH, so HA config work is code-driven instead of done in the browser File editor: edit → deploy → `ha core check` → reload/restart → text-probe. It pushes the managed block in `configuration.yaml` and every `custom_sentences/en/*.yaml` file (`alarm.yaml` + `wake_alarm.yaml` + `reminder.yaml`), takes a timestamped backup under `/config/backups/home-automation/` before every write, applies a sentences-only change with the narrow `conversation.reload`, and guards the full restart a `configuration.yaml` change needs behind `--restart`. Real HA secrets stay live-only on the VM — the script verifies the required secret **key names** exist in `/config/secrets.yaml` but never reads, prints, copies, or commits their values. SSH is the first choice for every HA interaction; the browser is a fallback — see [`home-assistant-vm.md`](home-assistant-vm.md).

```powershell
& .\.venv\Scripts\python.exe -m scripts.ha_config_sync preflight        # readiness; distinct failure per mode
& .\.venv\Scripts\python.exe -m scripts.ha_config_sync deploy --dry-run # unified diff, writes nothing
& .\.venv\Scripts\python.exe -m scripts.ha_config_sync deploy           # backup + write + ha core check
& .\.venv\Scripts\python.exe -m scripts.ha_config_sync deploy --restart # + the full HA restart a config change needs
& .\.venv\Scripts\python.exe -m scripts.ha_config_sync rollback         # restore most recent backup + recheck
& .\.venv\Scripts\python.exe -m scripts.ha_config_sync probe            # read-only "what is the alarm status" probe
```

**One-time bootstrap** (the SSH channel is the **Terminal & SSH add-on**, which mounts `/config`; HAOS host SSH on `:22222` is break-glass and not used): in the add-on's Configuration, paste this PC's public key into `authorized_keys` and expose a LAN-only host port (e.g. `2222`); create a long-lived access token; put the host/port/key/url/token into `.env` (`HA_SSH_*`, `HA_URL`, `HA_TOKEN` — see `.env.example`); then run `preflight`. Full bootstrap steps: [`voice-pe-config/README.md`](voice-pe-config/README.md). The script itself is [`../scripts/ha_config_sync.py`](../scripts/ha_config_sync.py).

## Operating

- **Volume is the master knob.** On the built-in speaker, high volume causes audio garble
  *and* a self-answering echo loop (the speaker feeds the mic). Keep it at/below ~85%, or use
  an external speaker (below) to run louder safely.
- **Two-speed behaviour is expected:** commands/time/weather are near-instant (local intents);
  open-ended questions go through the LLM (~0.7–1.0 s on the local brain, ~1.8–2.0 s when
  they trigger a web search).
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
- **Slow LLM answers** → the brain runs the local `qwen3.5-4b-nothink` (~0.7–1.0 s
  end-to-end), not a cloud `claude-*` model. If answers regress to 3–8 s, check the agent's
  `chat_model` hasn't reverted to a `claude-*` (cloud `claude -p`) id, and that the hub's
  qwen backend is up (`GET :8000/admin/api/models`). See
  [`voice-model-benchmark.md`](voice-model-benchmark.md).
- **Do not** reconfigure the sfortis "Update TTS Agent" dialog casually — it shows defaults,
  not saved values, and submitting it resets the configured voice (`amy`) to `alloy`
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
  **wyoming Piper add-on** on the VM (`core_piper`, voice `es_ES-sharvard-medium`)
  serves the Spanish pipeline's TTS, so it must stay installed. (**speech-to-phrase**
  remains installed but unused.)
- Local hub models are text-only; OpenAI-shape tool-use on the local llama backends requires
  `--jinja`. The hub's `claude -p` path accepts a `tools` payload but its tool-call emission
  is unverified — prefer deterministic routing (Tier 1) for actions.
