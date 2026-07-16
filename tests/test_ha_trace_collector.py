from __future__ import annotations

import asyncio

from app.webapp import ha_trace_collector as collector


def test_seen_runs_is_strictly_bounded(monkeypatch) -> None:
    monkeypatch.setattr(collector, "SEEN_RUN_LIMIT", 3)
    seen = collector._SeenRuns()
    for run_id in ("a", "b", "c", "d"):
        seen.add(run_id)
    assert seen.ids == {"b", "c", "d"}


def test_collector_records_completed_unseen_runs_only(monkeypatch) -> None:
    class Client:
        async def pipeline_runs(self, seen_run_ids):
            assert seen_run_ids == {"old"}
            return [
                {"run_id": "in-flight", "events": [{"type": "stt-end"}]},
                {
                    "run_id": "new",
                    "timestamp": "2026-07-15T19:08:45+00:00",
                    "events": [{"type": "run-end", "data": None}],
                },
            ]

    calls = []
    monkeypatch.setattr(collector.telemetry, "record_event", lambda *args, **kwargs: calls.append((args, kwargs)))
    seen = collector._SeenRuns()
    seen.add("old")

    count = asyncio.run(collector._record_new_runs(Client(), seen))

    assert count == 1
    assert "new" in seen.ids
    assert "in-flight" not in seen.ids
    assert calls[0][0] == ("ha_voice", "interaction")
    assert calls[0][1]["payload"]["run_id"] == "new"
