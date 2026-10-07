"""A model that never became ready must not crash the benchmark's final table (#833)."""

from __future__ import annotations

from scripts.voice_bench.runner import markdown_table

_STATS = {
    "validity_pct": 100, "intent_acc_pct": 90, "slot_acc_pct": 80, "cot_leak_pct": 0,
    "warm_p50_ms": 10, "warm_p95_ms": 20, "ttft_p50_ms": 5, "cold_ms": 100, "errors": 0,
}


def test_markdown_table_renders_an_unready_model_row() -> None:
    table = markdown_table({"Ready": {"free": _STATS}, "Down": {"_error": "not ready"}})

    rows = table.splitlines()
    assert any(r.startswith("| Ready | free | 100 |") for r in rows)
    down = next(r for r in rows if r.startswith("| Down |"))
    assert "not ready" in down
    assert down.count("|") == rows[0].count("|")  # same column count as the header
