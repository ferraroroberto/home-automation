# Voice Tier-2 model benchmark (#234)

Which local model should turn a spoken command into a structured intent, and how
should it be decoded? This is the empirical answer for the **Tier-2 classifier**
role in the voice routing described in [`voice-control.md`](voice-control.md) —
optimised for *constrained structured-output reliability and latency*, not
agentic cleverness.

**Run date:** 2026-06-30 · **Box:** RTX 5060 Ti 16 GB, Ryzen 7 7800X3D · **Harness:**
[`scripts/voice_bench/`](../scripts/voice_bench) (`python -m scripts.voice_bench`).

## TL;DR

- **Pick: `qwen3.5-4b` with `enable_thinking: false`, plain (un-grammared) decoding.**
  100 % schema-valid, 96 % intent+slot accuracy, **p50 302 ms / p95 517 ms**,
  TTFT 123 ms. It is already the resident `agentic_light` model, so this costs
  **zero extra VRAM**. That is ~20× faster than the old `claude-haiku-4-5` brain
  (p50 5.9 s) and clears the < 1.5 s target with huge margin.
- **This is now live.** The HA voice brain runs the hub's `qwen3.5-4b-nothink` alias;
  end-to-end answers in ~0.7–1.0 s. See *Wiring status* below.
- **Thinking is the latency killer.** With a voice-sized 128-token budget,
  thinking-on models produce *no usable output* — they spend the whole budget
  reasoning. Disabling thinking is the single biggest win.
- **Grammar-constrained decoding works, but is model-dependent.** `response_format:
  json_schema` forces 100 %-valid JSON on the Gemma models, but **400s on
  `qwen3.5-4b`** because Qwen3.5 always emits an inline `<think>…</think>` wrapper
  that collides with llama.cpp's strict JSON grammar. So "grammar ⇒ 100 % valid"
  only holds where the model keeps reasoning out of the content stream.
- **If strict 100 % validity is ever required**, the fallback is `gemma4-e4b-it`
  **+ grammar** (100 % valid, 100 % intent, 92 % slot, p50 482 ms). At ~4.7 GB it
  can co-reside with qwen + whisper.
- Tier-3 freeform stays on a larger model (`agentic_heavy` / `claude_haiku`).

## Method

- **Dataset:** [`dataset.yaml`](../scripts/voice_bench/dataset.yaml) — 26 utterances in
  three buckets: device commands (HVAC / plugs / alarm / volume), status queries,
  and freeform questions. Each command/status row carries the expected `intent` +
  `slots`; free-text slots (area, device) match case-insensitively as substrings,
  enum slots match exactly.
- **Schema:** [`schema.py`](../scripts/voice_bench/schema.py) — a closed-set intent
  enum + typed slots. This is the contract Python would validate against an
  allow-list before any actuation (a hallucinated intent can never arm the alarm).
- **Modes** (per model):
  - `free` — through the hub's public `/v1/chat/completions`, model's default
    config (= thinking on). This is "what you get today if HA points at the hub".
  - `nothink` — straight to the llama-server backend port, `enable_thinking: false`.
  - `grammar` — backend port, no-think **+** `response_format: json_schema`.
- **Metrics:** schema-validity %, intent accuracy %, slot accuracy %, CoT-leak %,
  warm latency p50/p95, TTFT p50, and the cold first-call latency. Latency is wall
  clock through the same path a client would use; streaming captures TTFT.
- **VRAM discipline:** only ~2.8 GB was free at the start, so the harness swaps
  one model at a time (stop the previous, start the next, poll ready) and restores
  the original rotation (`agentic_light` + whisper) in a `finally` block.

## Results

| Model | Mode | Valid % | Intent % | Slot % | Leak % | Warm p50 | Warm p95 | TTFT p50 | Cold | Err |
|---|---|---|---|---|---|---|---|---|---|---|
| qwen3.5-4b | free | 0.0 | 0.0 | 0.0 | 0.0 | 1650 | 1697 | — | 1787 | 0 |
| **qwen3.5-4b** | **nothink** | **100.0** | **96.2** | **96.2** | **0.0** | **302** | **517** | **123** | **364** | **0** |
| qwen3.5-4b | grammar | 0.0 | 0.0 | 0.0 | 0.0 | — | — | — | — | 26 |
| gemma4-e4b-it | free | 26.9 | 84.6 | 26.9 | 0.0 | 532 | 1620 | 333 | 1521 | 0 |
| gemma4-e4b-it | nothink | 73.1 | 100.0 | 73.1 | 0.0 | 242 | 357 | 56 | 376 | 0 |
| gemma4-e4b-it | grammar | 100.0 | 100.0 | 92.3 | 0.0 | 482 | 605 | 129 | 561 | 0 |
| gemma4-26b-a4b-it | free | 0.0 | 0.0 | 0.0 | 0.0 | 7203 | 7442 | 651 | 8784 | 0 |
| gemma4-26b-a4b-it | nothink | 100.0 | 100.0 | 100.0 | 0.0 | 1647 | 2644 | 216 | 3362 | 0 |
| gemma4-26b-a4b-it | grammar | 100.0 | 100.0 | 100.0 | 0.0 | 3050 | 3792 | 457 | 3050 | 0 |
| claude-haiku-4-5 (baseline) | free | 84.6 | 100.0 | 80.8 | 0.0 | 5920 | 9426 | — | 11917 | 0 |

Latencies in ms. `grammar` is backend-only, so the cloud row has no grammar mode.

## Findings

1. **Disabling thinking is non-negotiable for voice.** `free` mode at a 128-token
   budget is useless for thinking models: `qwen3.5-4b` and `gemma4-26b` both score
   0 % validity because the reasoning never finishes inside the budget (and the hub
   strips the unclosed `<think>` block, leaving empty content — the same trap the
   hub's `model-comparison.md` warns about). `gemma4-26b` `free` also shows the raw
   cost of thinking: **7.2 s** p50. `enable_thinking: false` collapses that to a
   clean single JSON object.
2. **Grammar is the reliability lever — where it applies.** It lifts `gemma4-e4b`
   from 73 % → 100 % validity. But it **cannot be used on `qwen3.5-4b`**: the
   strict JSON grammar rejects Qwen3.5's mandatory `<think>` token
   (`Failed to initialize samplers: Unexpected empty grammar stack after accepting
   piece: <think>`). qwen's saving grace is that it doesn't *need* grammar — its raw
   no-think validity is already 100 % on this task.
3. **The hub drops structured-output params.** The public `/v1/chat/completions`
   only forwards `tools` / `tool_choice`; `response_format` and `chat_template_kwargs`
   are silently dropped (`src/server.py:ChatCompletionRequest`). So both the
   no-think and grammar paths must address the llama-server backend port directly —
   which a Home Assistant client pointed at the hub cannot do. **This is what gates
   wiring the winner** (see below) and is filed as a hub pointer issue.
4. **The one qwen miss** (96.2 %): *"how much solar am I producing right now"* →
   classified `freeform` instead of `query_status`/`energy`. A conversational
   phrasing of an energy status query; trivially handled by a Tier-1 sentence
   template or one more prompt example.
5. **Bigger isn't worth it here.** `gemma4-26b` is the only model at 100/100/100,
   but at 1.6 s and 13 GB it can't co-reside with qwen, and the voice loop's real
   latency floor is STT (~0.5 s) + TTS (~1.7 s) — a 300 ms vs 1.6 s classifier is
   lost in that noise on accuracy-equivalent commands, but matters as headroom.

## Recommendation by tier

| Tier | Role | Pick | Why |
|---|---|---|---|
| **2 — classifier** | structured intent+slots | **`qwen3.5-4b` (`agentic_light`), `enable_thinking: false`** | 100 % valid, 96 % accurate, 302 ms, **0 extra VRAM** (already hot). |
| 2 — strict fallback | if 100 % validity is mandated | `gemma4-e4b-it` + grammar | 100 % valid / 100 % intent / 92 % slot, 482 ms, ~4.7 GB (co-resident OK). |
| **3 — freeform** | open questions / composition | `agentic_heavy` or `claude_haiku` | rare path; latency tolerance is higher. |

### On "faster open-source alternatives"

The genuinely faster path is not a different model — it is **turning thinking off**
and trusting the closed-set task (300 ms, already on the box). Grammar-constrained
decoding is the right *reliability* tool but is blocked on qwen by the inline-think
collision. Going smaller than 4 B (1.7 B / 0.6 B + grammar) could shave tens of ms,
but the voice loop is STT/TTS-bound, so the marginal value is low. Any newer entrant
should be vetted through the hub's quarterly **frontier** process
(`local-llm-hub/docs/frontier/`) rather than added ad hoc — and note this analysis
is anchored to the models the hub serves as of the run date.

## Wiring status (Part C — DONE, live)

The chosen config (`enable_thinking: false`) must reach the backend, but the HA
`extended_openai_conversation` integration (v2.0.2) can't send `chat_template_kwargs`,
and the hub used to drop it anyway. Both gaps were closed and the brain is now wired:

1. **`local-llm-hub#159`** (PR #160) — the hub now forwards `response_format` +
   `chat_template_kwargs` into the upstream payload on `/v1/chat/completions`.
2. **`local-llm-hub#161`** (PR #162) — a dedicated **`qwen3.5-4b-nothink`** model id:
   a virtual alias of the running qwen backend (`:8088`, no extra process/VRAM) that
   injects `enable_thinking: false`. Plain `agentic_light` stays thinking-capable for
   OpenClaw.
3. **This repo** — the live HA conversation agent ("Hub Haiku") was repointed
   `chat_model: claude-haiku-4-5 → qwen3.5-4b-nothink` and `max_tokens: 64 → 150`
   (the no-think tool call lands at ~55 tokens; 150 gives headroom). The edit was a
   backed-up `.storage/core.config_entries` change (stop → edit → start), reversible
   by restoring the backup under `/config/backups/voice-brain-rewire/`.

**Live verification** (HA `/api/conversation/process`, `conversation.extended_openai_conversation`):
*"what is two plus two"* → "Two plus two equals four." in **985 ms**; *"what's the
capital of France"* → "Paris." in **715 ms** — vs the old haiku brain's 3–8 s.
Function-calling (`execute_services`) was validated at the hub/model level (correct
`tool_calls` at the 64-token budget); not exercised against live devices to avoid
actuation. To revert: set `chat_model` back to `claude-haiku-4-5` (restore the backup)
and `ha core restart`.

## Re-running

```powershell
# full swap-based run, all candidates + baseline, both decode modes
& .\.venv\Scripts\python.exe -m scripts.voice_bench --out results.json

# subset / single mode / bench only what's loaded
& .\.venv\Scripts\python.exe -m scripts.voice_bench --models qwen,haiku --modes free,nothink
& .\.venv\Scripts\python.exe -m scripts.voice_bench --no-swap

# just confirm the hub still drops response_format
& .\.venv\Scripts\python.exe -m scripts.voice_bench --probe-only
```

The harness is a plain HTTP client of the hub (OpenAI-shape endpoint + admin
start/stop routes) — it does not serve models or wrap `claude -p`. It restores the
model rotation on exit even if interrupted.
