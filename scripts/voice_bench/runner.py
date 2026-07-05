"""Benchmark orchestrator (#234).

Drives the hub to benchmark each candidate model on the Tier-2 voice-intent
task, in two decoding modes (free vs grammar-constrained), measuring
structured-output validity, intent/slot accuracy, latency (warm p50/p95 +
cold first-call) and chain-of-thought leakage. Swaps models one at a time to
respect the GPU VRAM budget and restores the original rotation on exit.

CLI:
  python -m scripts.voice_bench                     # full swap-based run, both modes
  python -m scripts.voice_bench --models qwen,haiku # subset
  python -m scripts.voice_bench --modes free        # one mode
  python -m scripts.voice_bench --no-swap           # bench only currently-loaded models
  python -m scripts.voice_bench --probe-only        # just the response_format passthrough probe
  python -m scripts.voice_bench --out results.json  # also dump raw metrics
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import yaml
from jsonschema import Draft202012Validator

from .hub import ChatResult, HubClient
from .schema import INTENT_JSON_SCHEMA, RESPONSE_FORMAT, SYSTEM_PROMPT

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows capture → cp1252 guard
    sys.stderr.reconfigure(encoding="utf-8")

_VALIDATOR = Draft202012Validator(INTENT_JSON_SCHEMA)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_INNER = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)
_DATASET = Path(__file__).with_name("dataset.yaml")
_NO_THINK = {"enable_thinking": False}  # Qwen3.5 / Gemma4 soft thinking switch


@dataclass
class Candidate:
    key: str
    hub_id: str       # registry id used for admin start/stop
    display: str      # id addressed on /v1/chat/completions (also the --alias)
    port: Optional[int]
    cloud: bool       # claude path: free mode only, never swapped, no grammar

    def backend_base(self, row: Optional[Dict[str, Any]]) -> str:
        if row and row.get("url"):
            return str(row["url"])
        return f"http://127.0.0.1:{self.port}/v1"


CANDIDATES: Dict[str, Candidate] = {
    "qwen": Candidate("qwen", "qwen35_4b", "qwen3.5-4b", 8088, False),
    "gemma-e4b": Candidate("gemma-e4b", "gemma4_e4b", "gemma4-e4b-it", 8086, False),
    "gemma-26b": Candidate("gemma-26b", "gemma4_26b", "gemma4-26b-a4b-it", 8087, False),
    "haiku": Candidate("haiku", "claude_haiku", "claude-haiku-4-5", None, True),
}
DEFAULT_ORDER = ["qwen", "gemma-e4b", "gemma-26b", "haiku"]

# The only modes run_mode() recognizes; a typo'd value falls through its
# if/if/else to the "grammar" branch (the else), silently mislabeling that
# run's results under the typo'd name instead of erroring. Validated in main().
MODES = {"free", "nothink", "grammar"}


# --- parsing & scoring ----------------------------------------------------

def extract_json(content: str) -> Optional[Dict[str, Any]]:
    """Best-effort: strip <think> + code fences, take the first {...} object."""
    text = _THINK_RE.sub("", content or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start:i + 1])
                    return obj if isinstance(obj, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


def has_cot_leak(content: str) -> bool:
    """True if reasoning leaked: a *non-empty* <think> block, or prose before
    the JSON. An empty ``<think></think>`` wrapper (what enable_thinking:false
    still emits on Qwen3.5) is not a leak."""
    if any(inner.strip() for inner in _THINK_INNER.findall(content or "")):
        return True
    stripped = _THINK_RE.sub("", content or "").strip()
    stripped = re.sub(r"^```(?:json)?", "", stripped).strip()
    brace = stripped.find("{")
    return brace > 0  # any non-whitespace text precedes the object


def slot_matches(expected: Any, got: Any) -> bool:
    if got is None:
        return False
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        try:
            return float(got) == float(expected)
        except (TypeError, ValueError):
            return False
    # Free-text/enum slots: case-insensitive substring (handles "the living room").
    return str(expected).lower() in str(got).lower()


def score_row(obj: Optional[Dict[str, Any]], expected_intent: str,
              expected_slots: Dict[str, Any]) -> Dict[str, bool]:
    valid = obj is not None and _VALIDATOR.is_valid(obj)
    intent_ok = bool(obj) and obj.get("intent") == expected_intent
    got_slots = (obj or {}).get("slots") or {}
    slots_ok = all(slot_matches(v, got_slots.get(k)) for k, v in expected_slots.items())
    return {"valid": valid, "intent_ok": intent_ok, "slots_ok": bool(intent_ok and slots_ok)}


# --- aggregation ----------------------------------------------------------

@dataclass
class ModeAgg:
    n: int = 0
    valid: int = 0
    intent_ok: int = 0
    slots_ok: int = 0
    cot_leak: int = 0
    warm_total: List[float] = field(default_factory=list)
    warm_ttft: List[float] = field(default_factory=list)
    cold_total: Optional[float] = None
    errors: int = 0

    def pct(self, num: int) -> float:
        return 100.0 * num / self.n if self.n else 0.0

    @staticmethod
    def _p(vals: List[float], q: float) -> Optional[float]:
        if not vals:
            return None
        s = sorted(vals)
        idx = min(len(s) - 1, int(round(q * (len(s) - 1))))
        return s[idx]

    def summary(self) -> Dict[str, Any]:
        return {
            "n": self.n,
            "validity_pct": round(self.pct(self.valid), 1),
            "intent_acc_pct": round(self.pct(self.intent_ok), 1),
            "slot_acc_pct": round(self.pct(self.slots_ok), 1),
            "cot_leak_pct": round(self.pct(self.cot_leak), 1),
            "warm_p50_ms": round(self._p(self.warm_total, 0.5)) if self.warm_total else None,
            "warm_p95_ms": round(self._p(self.warm_total, 0.95)) if self.warm_total else None,
            "ttft_p50_ms": round(self._p(self.warm_ttft, 0.5)) if self.warm_ttft else None,
            "cold_ms": round(self.cold_total) if self.cold_total is not None else None,
            "errors": self.errors,
        }


# --- orchestration --------------------------------------------------------

def running_local(hub: HubClient) -> List[str]:
    """Keys of local candidates currently reachable + owned by the hub."""
    out = []
    for key, c in CANDIDATES.items():
        if c.cloud:
            continue
        row = hub.model_row(c.hub_id)
        if row and row.get("reachable") and row.get("ownership") == "ours":
            out.append(key)
    return out


def ensure_only(hub: HubClient, cand: Candidate, log) -> bool:
    """Stop every other local candidate, then start `cand` and wait ready."""
    for key, other in CANDIDATES.items():
        if other.cloud or other.hub_id == cand.hub_id:
            continue
        row = hub.model_row(other.hub_id)
        if row and (row.get("reachable") or row.get("ownership") == "ours"):
            log(f"  stop {other.display} (free VRAM)…")
            hub.stop(other.hub_id)
    log(f"  start {cand.display}…")
    hub.start(cand.hub_id)
    ok = hub.wait_ready(cand.hub_id)
    log(f"  {'ready' if ok else 'NOT READY (timeout)'}: {cand.display}")
    return ok


def run_mode(hub: HubClient, cand: Candidate, mode: str, dataset: List[Dict[str, Any]],
             backend_base: str, max_tokens: int, log) -> ModeAgg:
    agg = ModeAgg()
    msgs = lambda u: [{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": u}]

    def call(utt: str) -> ChatResult:
        # free: hub default path (thinking as the model normally runs).
        if mode == "free":
            if cand.cloud:
                return hub.chat_free_blocking(cand.display, msgs(utt), max_tokens=max_tokens)
            return hub.chat_free_stream(cand.display, msgs(utt), max_tokens=max_tokens)
        # nothink: backend direct, thinking disabled (fast clean JSON).
        if mode == "nothink":
            return hub.chat_backend_stream(backend_base, cand.display, msgs(utt),
                                           max_tokens=max_tokens, chat_template_kwargs=_NO_THINK)
        # grammar: backend direct, thinking off + json_schema-constrained decoding.
        return hub.chat_backend_stream(backend_base, cand.display, msgs(utt),
                                       max_tokens=max_tokens, chat_template_kwargs=_NO_THINK,
                                       response_format=RESPONSE_FORMAT)

    # Cold first-call (its latency is the cold number; output discarded).
    cold = call(dataset[0]["text"])
    agg.cold_total = cold.total_ms

    for row in dataset:
        res = call(row["text"])
        agg.n += 1
        if not res.ok:
            agg.errors += 1
            continue
        agg.warm_total.append(res.total_ms)
        if res.ttft_ms is not None:
            agg.warm_ttft.append(res.ttft_ms)
        if has_cot_leak(res.content):
            agg.cot_leak += 1
        obj = extract_json(res.content)
        s = score_row(obj, row["expected_intent"], row.get("expected_slots") or {})
        agg.valid += s["valid"]
        agg.intent_ok += s["intent_ok"]
        agg.slots_ok += s["slots_ok"]
    log(f"    [{mode}] {agg.summary()}")
    return agg


def probe_passthrough(hub: HubClient, log) -> Dict[str, Any]:
    """Confirm whether the hub's public /v1/chat/completions forwards
    response_format to llama-server. Needs a local model already loaded."""
    loaded = running_local(hub)
    if not loaded:
        log("probe: no local model loaded — start one first (e.g. qwen).")
        return {"ran": False}
    cand = CANDIDATES[loaded[0]]
    row = hub.model_row(cand.hub_id)
    payload = {
        "model": cand.display, "max_tokens": 64, "temperature": 0.0,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": "turn the living room AC on"}],
        "response_format": RESPONSE_FORMAT,
    }
    out: Dict[str, Any] = {"ran": True, "model": cand.display}
    with httpx.Client(timeout=60.0) as c:
        rh = c.post(f"{hub.base}/v1/chat/completions", json=payload)
        out["hub_status"] = rh.status_code
        try:
            hub_content = rh.json()["choices"][0]["message"].get("content") or ""
        except Exception:  # noqa: BLE001
            hub_content = rh.text[:200]
        rb = c.post(f"{cand.backend_base(row).rstrip('/')}/chat/completions", json=payload)
        out["backend_status"] = rb.status_code
        try:
            be_content = rb.json()["choices"][0]["message"].get("content") or ""
        except Exception:  # noqa: BLE001
            be_content = rb.text[:200]
    out["hub_constrained"] = extract_json(hub_content) is not None and "{" == (hub_content.strip()[:1] or "")
    out["backend_constrained"] = extract_json(be_content) is not None
    log(f"probe: hub HTTP {out['hub_status']} constrained={out['hub_constrained']} | "
        f"backend HTTP {out['backend_status']} constrained={out['backend_constrained']}")
    log(f"  hub content:     {hub_content[:160]!r}")
    log(f"  backend content: {be_content[:160]!r}")
    return out


def restore(hub: HubClient, initial: List[str], log) -> None:
    log("restoring rotation…")
    for key, c in CANDIDATES.items():
        if c.cloud:
            continue
        want = key in initial
        row = hub.model_row(c.hub_id)
        is_up = bool(row and row.get("reachable"))
        if want and not is_up:
            log(f"  restart {c.display}")
            hub.start(c.hub_id)
        elif not want and (row and row.get("ownership") == "ours"):
            log(f"  stop {c.display}")
            hub.stop(c.hub_id)
    for key in initial:
        hub.wait_ready(CANDIDATES[key].hub_id, timeout_s=120.0)


def markdown_table(results: Dict[str, Dict[str, Any]]) -> str:
    hdr = ("| Model | Mode | Valid % | Intent % | Slot % | CoT-leak % | "
           "Warm p50 | Warm p95 | TTFT p50 | Cold | Err |")
    sep = "|" + "---|" * 11
    lines = [hdr, sep]
    for model, modes in results.items():
        for mode, s in modes.items():
            lines.append(
                f"| {model} | {mode} | {s['validity_pct']} | {s['intent_acc_pct']} | "
                f"{s['slot_acc_pct']} | {s['cot_leak_pct']} | "
                f"{s['warm_p50_ms']} | {s['warm_p95_ms']} | {s['ttft_p50_ms']} | "
                f"{s['cold_ms']} | {s['errors']} |")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Voice Tier-2 model benchmark (#234)")
    ap.add_argument("--models", default=",".join(DEFAULT_ORDER),
                    help="comma list of candidate keys: " + ",".join(CANDIDATES))
    ap.add_argument("--modes", default="free,nothink,grammar",
                    help="any of: free (hub, default thinking), nothink (backend, "
                         "enable_thinking:false), grammar (backend, no-think + json_schema)")
    ap.add_argument("--no-swap", action="store_true", help="don't start/stop; bench loaded models")
    ap.add_argument("--probe-only", action="store_true", help="only run the passthrough probe")
    ap.add_argument("--max-tokens", type=int, default=128)
    ap.add_argument("--out", default="", help="write raw JSON metrics to this path")
    args = ap.parse_args(argv)

    def log(m: str) -> None:
        print(m, flush=True)

    hub = HubClient()
    dataset = yaml.safe_load(_DATASET.read_text(encoding="utf-8"))
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    bad_modes = [m for m in modes if m not in MODES]
    if bad_modes:
        log(f"unknown modes: {bad_modes} (valid: {sorted(MODES)})")
        return 2
    keys = [k.strip() for k in args.models.split(",") if k.strip()]
    bad = [k for k in keys if k not in CANDIDATES]
    if bad:
        log(f"unknown model keys: {bad}")
        return 2

    if args.probe_only:
        probe_passthrough(hub, log)
        hub.close()
        return 0

    initial = running_local(hub)
    log(f"initial local rotation: {initial or '(none)'}")
    results: Dict[str, Dict[str, Any]] = {}
    try:
        for key in keys:
            cand = CANDIDATES[key]
            log(f"\n=== {cand.display} ({key}) ===")
            if not cand.cloud and not args.no_swap:
                if not ensure_only(hub, cand, log):
                    results[cand.display] = {"_error": "not ready"}
                    continue
            row = hub.model_row(cand.hub_id)
            backend_base = cand.backend_base(row)
            results[cand.display] = {}
            for mode in modes:
                if mode in ("grammar", "nothink") and cand.cloud:
                    continue  # backend-only modes; the cloud path has no llama-server
                agg = run_mode(hub, cand, mode, dataset, backend_base, args.max_tokens, log)
                results[cand.display][mode] = agg.summary()
    finally:
        if not args.no_swap:
            restore(hub, initial, log)
        hub.close()

    log("\n" + markdown_table(results))
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2), encoding="utf-8")
        log(f"\nwrote {args.out}")
    return 0
