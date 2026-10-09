"""API smoke for the Spanish voice web search bridge (issue #865).

``POST /api/voice/search`` never touches SearXNG or the hub in tests — the two
network seams in ``src.web_search`` are monkeypatched. Every path is a 200 with
``{ok, speech, source}`` so HA always has a sentence to speak.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src import web_search as ws

_HIT = [{"title": "T", "content": "C"}]


def _patch(monkeypatch, *, fetch, summarise="") -> None:
    async def _fetch(query):
        if isinstance(fetch, Exception):
            raise fetch
        return fetch

    async def _sum(query, results):
        if isinstance(summarise, Exception):
            raise summarise
        return summarise

    monkeypatch.setattr(ws, "_fetch_results", _fetch)
    monkeypatch.setattr(ws, "_summarise", _sum)


def test_success_speaks_the_summary(client: TestClient, monkeypatch) -> None:
    _patch(monkeypatch, fetch=_HIT, summarise="Hace sol.")
    resp = client.post("/api/voice/search", json={"query": "qué tiempo hace en Sevilla"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "speech": "Hace sol.", "source": "ok"}


@pytest.mark.parametrize(
    "fetch, summarise, source, speech",
    [
        (ws.SearchBackendError("down"), "", ws.SOURCE_SEARXNG_DOWN, ws.SPEECH_SEARXNG_DOWN),
        ([], "", ws.SOURCE_NO_RESULTS, ws.SPEECH_NO_RESULTS),
        (_HIT, RuntimeError("500"), ws.SOURCE_HUB_ERROR, ws.SPEECH_HUB_ERROR),
    ],
    ids=["searxng-down", "empty-result", "hub-error"],
)
def test_each_failure_is_a_200_with_its_own_reply(
    client: TestClient, monkeypatch, fetch, summarise, source, speech
) -> None:
    _patch(monkeypatch, fetch=fetch, summarise=summarise)
    resp = client.post("/api/voice/search", json={"query": "algo"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "speech": speech, "source": source}


def test_blank_or_missing_query_is_a_200_and_never_searches(
    client: TestClient, monkeypatch
) -> None:
    _patch(monkeypatch, fetch=AssertionError("must not be called"))
    for body in ({"query": "  "}, {}):
        resp = client.post("/api/voice/search", json=body)
        assert resp.status_code == 200
        assert resp.json()["source"] == ws.SOURCE_EMPTY_QUERY
