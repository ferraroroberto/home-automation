# Wiring a new deterministic voice command — how-to

The quick-reference playbook for adding hands-free voice commands that actuate this
app **deterministically** (Tier 1: Home Assistant's local sentence matcher → the app's
HTTP API), with **no LLM on the command path**. This is the operating recipe distilled
from building the alarm bridge (#88 Phase 4); the architecture and the alarm specifics
live in [`voice-control.md`](voice-control.md), the secret-free installed config in
[`voice-pe-config/`](voice-pe-config/).

> **Why deterministic.** A hallucinated tool call must never actuate a real device
> (alarm, lock, …). The model does *language understanding*; **Home Assistant + this
> app do actuation**. Every command below is matched by HA's rigid sentence engine and
> executed by a fixed `rest_command` — the LLM is only ever a fallback for phrasings
> the sentences miss, and it cannot reach these endpoints.

## The chain (mental model)

```
"Okay Nabu, perimeter on"
   → wake word (on-device)        strips "Okay Nabu"
   → STT  (hub Whisper)           "perimeter on"
   → conversation agent           "Prefer handling commands locally" = ON
       → custom_sentences/en/*.yaml   matches the sentence to an INTENT  ← you write this
       → intent_script: <Intent>      runs an action + speaks a reply    ← you write this
           → rest_command.<name>      POST/GET to the app API            ← you write this
               → app  /api/...        the actual actuation
   → TTS  (hub Orpheus)           speaks the intent_script reply
```

You add three things per command: **a sentence set**, **an intent_script**, **a
rest_command**. Everything else (wake word, STT, agent, TTS) is already wired.

## The 5-minute recipe

All paths are on the **HA VM** (`192.168.0.4:8123`). The app is at
`https://192.168.0.13:8447` on the LAN.

**Deploy by code (preferred).** Edit the repo-owned files under
[`voice-pe-config/`](voice-pe-config/) and push them with
`scripts/ha_config_sync.py deploy` over SSH — it backs up, writes, runs
`ha core check`, and reloads/restarts for you (see
[`voice-pe-config/README.md`](voice-pe-config/README.md) for the one-time SSH
bootstrap). The **File editor** add-on (Settings → Add-ons → File editor) is the
fallback when SSH isn't available; the steps below describe the files either way.
The SSH channel is the **Terminal & SSH add-on** shell (mounts `/config`), *not*
HAOS host SSH on `:22222` (that's a break-glass developer channel, not used here).

1. **Pick the app endpoint.** Find (or add) the `/api/...` route that does the thing.
   Reads are `GET`, actuations are `POST`. Loopback bypasses auth; LAN callers (HA) must
   send the bearer token.

2. **Add a `rest_command`** in `/config/configuration.yaml` (merge under the existing
   `rest_command:` key — a YAML key may appear only once):

   ```yaml
   rest_command:
     my_thing_on:
       url: "https://192.168.0.13:8447/api/things/on"
       method: POST
       headers:
         authorization: !secret app_api_authorization
       verify_ssl: false        # cert is for the .ts.net name, not the LAN IP
       timeout: 30
   ```

3. **Add an `intent_script`** (merge under the existing `intent_script:` key). The
   `- stop` line is **mandatory** if you want to speak anything about the result — see
   [the response gotcha](#the-response-gotcha-stop--action_response):

   ```yaml
   intent_script:
     MyThingOn:
       action:
         - service: rest_command.my_thing_on
           response_variable: r
         - stop: ""
           response_variable: r
       speech:
         text: >-
           {% if action_response is defined and action_response.status == 200 %}Done.{% else %}Sorry, that did not work.{% endif %}
   ```

4. **Add the sentences** in `/config/custom_sentences/en/<topic>.yaml` (create the
   folders if missing — the File editor's "new folder" needs a clean, non-dirty editor):

   ```yaml
   language: "en"
   intents:
     MyThingOn:
       data:
         - sentences:
             - "turn [the] thing on"
             - "thing on"
             - "(activate|start) [the] thing"
   ```

5. **Reload, then test.** Sentences hot-reload; intent_script/rest_command need a full
   restart — see [Reload vs restart](#reload-vs-restart). `ha_config_sync.py deploy`
   picks the right one for you (narrow `conversation.reload` for a sentences-only
   change; it prints that a `configuration.yaml` change needs `--restart`). Then probe
   by text before you speak — `ha_config_sync.py probe`, or see
   [Testing](#testing-without-talking).

## hassil sentence syntax (cheat-sheet)

The `sentences:` use [hassil](https://github.com/home-assistant/hassil) template syntax,
matched **case- and punctuation-insensitively** after normalisation:

| Syntax | Means | Example |
|---|---|---|
| `[word]` | optional | `turn [the] alarm on` → "turn alarm on" / "turn the alarm on" |
| `(a\|b\|c)` | alternatives (one required) | `(arm\|set) the alarm` |
| `[(a\|b)]` | optional alternatives | `perimeter [(on\|mode)]` |
| `{list}` | a named list (see `lists:`) | `disarm {code}` |
| `lists: { code: { wildcard: true } }` | free-text capture (1+ words) | grabs whatever was said |

Tips learned the hard way:
- **Cast a wide net.** "alarm on", "full alarm on", "turn the alarm fully on", "fully
  arm", "activate the alarm" are all the *same* intent to a human — list them all. A
  phrase you didn't write falls through to the LLM ("I don't have the tools…").
- A **wildcard list matches one or more words**, so `... {code}` won't match a bare
  utterance with no trailing word — that's exactly how a code-gated command tells
  "disarm now" (has a code) from "disarm" (no code → prompt).
- Cross-intent collisions resolve to the **most specific** match: `is the alarm on`
  (status) wins over `alarm on` (arm) because it's longer/more literal.

## The response gotcha (`stop` + `action_response`)

**This cost three restarts to diagnose.** An `intent_script` speech template can read the
action's result as `action_response` — but **only if the action explicitly returns it**
with a trailing `- stop: "" / response_variable: <name>`. Setting `response_variable` on
the service call alone is **not** enough: the speech template then sees `action_response`
as *undefined* and every command speaks the failure branch even though the REST call
returned `200`. This is the canonical pattern straight from the HA `intent_script` docs:

```yaml
action:
  - service: rest_command.my_thing_status
    response_variable: r
  - stop: ""               # ← without this, action_response is never populated
    response_variable: r
speech:
  text: "{{ action_response.content.label }}"   # .status = HTTP code, .content = parsed body
```

`action_response.status` is the HTTP status; `action_response.content` is the parsed JSON
body (so a `GET /api/security` lets you speak `action_response.content.label`).

### Confirm the *result*, not just HTTP 200 (recommended)

A `200` means *"the app accepted the command,"* **not** *"the device did what you asked."*
The alarm bridge hit this live: an arm command returned `200` and happily said *"Arming
the perimeter,"* but the panel **stayed disarmed** because a zone (a kitchen opening
contact) was open and RISCO silently refused to finish arming. For anything safety-
relevant, read the *resulting state* back out of `action_response.content` and say the
truth:

```yaml
speech:
  text: >-
    {% if action_response is defined and action_response.status == 200 %}{% set st = action_response.content %}{% if st.label != 'Disarmed' %}The alarm is now {{ st.label }}.{% else %}{% set open = st.zones | selectattr('triggered') | rejectattr('bypassed') | map(attribute='display_name') | list %}The alarm did not arm{% if open %}; {{ open | join(', ') }} {{ 'is' if open|length == 1 else 'are' }} open{% endif %}.{% endif %}{% else %}Sorry, the alarm did not respond.{% endif %}
```

> The shipped alarm intents in `configuration.snippet.yaml` follow this pattern; keep
> future safety-relevant commands on the same result-state confirmation model.

## Code-gating a destructive command

For commands that must not fire on a misheard word (disarm, unlock), require a **spoken
code in the same utterance** and validate it against a secret *before* the actuation. A
failing `condition` halts the script, so the `rest_command` never runs and
`action_response` stays unset — which is how the speech tells a wrong code from success:

```yaml
AlarmDisarmWithCode:
  action:
    - variables:
        expected: !secret voice_disarm_pin
    - condition: template
      value_template: "{{ (code | string | lower | trim) == (expected | string | lower | trim) }}"
    - service: rest_command.alarm_disarm
      response_variable: r
    - stop: ""
      response_variable: r
  speech:
    text: >-
      {% if action_response is defined and action_response.status == 200 %}Disarming.{% else %}That code is not correct, or it did not respond.{% endif %}
```

Pair it with a **no-code prompt** intent (literal "disarm" with no `{code}`) that just
tells the user how — never actuates. The voice code is a *gate layered on top of* the
panel PIN the app already holds server-side; the real PIN is never spoken.

## Reload vs restart

| You changed | To apply |
|---|---|
| `custom_sentences/**` (sentences only) | **No restart.** Call the `conversation.reload` service (Developer Tools → Actions → `conversation.reload`, or `POST /api/services/conversation/reload`). |
| `intent_script` or `rest_command` (in `configuration.yaml`) | **Full restart** — Settings → System → power icon → **Restart Home Assistant**. |
| `secrets.yaml` | Full restart. |

**"Quick reload" (YAML) is not enough** for new custom sentences or intent_script — they
load at startup. Use the full **Restart Home Assistant**, and verify after (below); a
Quick reload silently leaving the old config is a classic false-negative.

## Testing without talking

Probe the conversation engine by **text** — same matcher the voice path uses with
"Prefer local" ON — via `POST /api/conversation/process` on the HA frontend (or curl with
a token). Reads (status) are side-effect-free; **actuating intents really fire**, so:

- Test **read** intents freely.
- Test **actuation matching** safely when the device can't act (e.g. the alarm won't arm
  while a zone is open → the command is a no-op you can confirm matched), or accept the
  actuation and undo it.
- A reply of type `action_done` with your confirmation text = matched locally. A generic
  *"I don't have the tools…"* or *"not aware of any device…"* = it fell through to the
  LLM, i.e. **your sentences didn't match** — widen them.

Minimal browser probe (run in the HA frontend tab's console):

```js
const c = await window.hassConnection;
const tok = c.auth.accessToken;
const r = await fetch(c.auth.data.hassUrl + '/api/conversation/process', {
  method: 'POST',
  headers: { Authorization: 'Bearer ' + tok, 'Content-Type': 'application/json' },
  body: JSON.stringify({ text: "what's the alarm status", language: 'en' })
});
console.log((await r.json()).response?.speech?.plain?.speech);
```

## Diagnostics quick-reference

- **Did it reach the LLM (= local miss)?** Hub telemetry shows the path: a
  `…/audio/transcriptions` (Whisper) immediately followed by a `…/chat/completions`
  (the agent) means the command **fell through** to the LLM. A transcription with **no**
  following chat call means it matched locally.
  `GET http://192.168.0.13:8000/admin/api/telemetry/recent?limit=25`
- **Is STT mis-hearing the words?** Round-trip a phrase through the hub: synthesise it
  (`POST :8000/v1/audio/speech`) and transcribe it back (`POST :8000/v1/audio/
  transcriptions`). Whisper renders clear speech faithfully — a "not recognised" command
  is far more often a *phrasing* gap than a transcription error.
- **What is the app actually returning?** Hit it over **loopback** from the host PC — no
  token needed: `curl.exe -sk https://127.0.0.1:8447/api/security`. This isolates the app
  from HA entirely (was the bug in the sentence, the wiring, or the device?).
- HA's old `GET /api/error_log` REST endpoint is **gone** in current HA; pipeline-run
  detail is in Settings → Voice assistants → ⋮ → **Debug** (per-stage STT/intent/TTS).

## Gotchas checklist (all hit while building the alarm bridge)

- [ ] **"Prefer handling commands locally" must be ON** in the pipeline, or every
      sentence is sent to the LLM and nothing is deterministic.
- [ ] **`- stop: "" / response_variable`** in every intent that speaks about its result,
      or the reply always reads the failure branch.
- [ ] **Full restart** (not Quick reload) for intent_script / rest_command changes.
- [ ] **`verify_ssl: false`** on every rest_command — the cert is for the `.ts.net` name,
      not the LAN IP.
- [ ] **`!secret app_api_authorization`** = the literal `Bearer ` + the webapp
      `auth_token`; never inline a token in `configuration.yaml`.
- [ ] **Confirm the resulting state**, not the HTTP 200 — a device can accept and not act.
- [ ] **Widen the sentences** — an unrecognised phrase silently goes to the LLM.
- [ ] **File editor footgun:** the configurator's "current file" path can lag the editor
      buffer; re-pin it before Ctrl+S so you don't save one file's content over another.
