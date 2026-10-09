"""Unit tests for the Spanish voice web search core (issue #865, part of #842).

Both network edges (``_fetch_results`` → SearXNG, ``_summarise`` → hub) are
monkeypatched, so nothing here touches the network. Each failure mode is
asserted to produce its own reply, ``source`` and log line.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from src import web_search as ws


def _hit(i: int, content: str = "") -> dict:
    return {"title": f"Título {i}", "content": content or f"contenido {i}", "url": f"http://x/{i}"}


def _patch(monkeypatch, *, fetch=None, summarise=None):
    calls = {"fetch": [], "summarise": []}

    async def _fetch(query):
        calls["fetch"].append(query)
        if isinstance(fetch, Exception):
            raise fetch
        return fetch

    async def _sum(query, results):
        calls["summarise"].append((query, results))
        if isinstance(summarise, Exception):
            raise summarise
        return summarise

    monkeypatch.setattr(ws, "_fetch_results", _fetch)
    monkeypatch.setattr(ws, "_summarise", _sum)
    return calls


def test_trim_keeps_top_8_and_200_char_snippets() -> None:
    raw = [_hit(i, content="x" * 500) for i in range(12)]
    out = ws.trim_results(raw)
    assert len(out) == 8
    assert all(len(h["snippet"]) == 200 for h in out)
    assert out[0]["title"] == "Título 0"


def test_trim_skips_textless_and_malformed_entries() -> None:
    raw = [{"title": "", "content": ""}, "nope", {"title": "ok", "content": "  a   b "}]
    assert ws.trim_results(raw) == [{"title": "ok", "snippet": "a b"}]


def test_user_message_carries_question_and_numbered_hits() -> None:
    msg = ws.build_user_message("¿quién ganó?", [{"title": "T", "snippet": "S"}])
    assert "Pregunta: ¿quién ganó?" in msg
    assert "1. T — S" in msg


def test_success_returns_model_speech(monkeypatch, caplog) -> None:
    calls = _patch(monkeypatch, fetch=[_hit(1), _hit(2)], summarise="Ganó España.")
    with caplog.at_level(logging.INFO, logger="web_search"):
        ans = asyncio.run(ws.answer_query("quién ganó el mundial"))
    assert (ans.ok, ans.speech, ans.source) == (True, "Ganó España.", ws.SOURCE_OK)
    assert calls["fetch"] == ["quién ganó el mundial"]
    assert len(calls["summarise"][0][1]) == 2
    info = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(info) == 1 and "source=ok" in info[0].getMessage()
    assert "results=2" in info[0].getMessage()
    # The breadcrumb carries the length, never the query text.
    assert "mundial" not in info[0].getMessage()


def test_searxng_down_gets_fixed_reply_and_skips_hub(monkeypatch, caplog) -> None:
    calls = _patch(monkeypatch, fetch=ws.SearchBackendError("ConnectError: refused"))
    with caplog.at_level(logging.INFO, logger="web_search"):
        ans = asyncio.run(ws.answer_query("qué tiempo hace en Madrid"))
    assert (ans.ok, ans.speech, ans.source) == (
        False, ws.SPEECH_SEARXNG_DOWN, ws.SOURCE_SEARXNG_DOWN,
    )
    assert "ahora mismo" in ans.speech
    assert calls["summarise"] == []
    assert any("SearXNG unavailable" in r.getMessage() for r in caplog.records)


def test_empty_results_get_their_own_reply_and_skip_hub(monkeypatch, caplog) -> None:
    calls = _patch(monkeypatch, fetch=[{"title": "", "content": ""}])
    with caplog.at_level(logging.INFO, logger="web_search"):
        ans = asyncio.run(ws.answer_query("asdfgh"))
    assert (ans.ok, ans.speech, ans.source) == (
        False, ws.SPEECH_NO_RESULTS, ws.SOURCE_NO_RESULTS,
    )
    assert calls["summarise"] == []
    assert any("source=no_results" in r.getMessage() for r in caplog.records)


def test_hub_error_gets_its_own_reply(monkeypatch, caplog) -> None:
    _patch(monkeypatch, fetch=[_hit(1)], summarise=RuntimeError("hub 500"))
    with caplog.at_level(logging.INFO, logger="web_search"):
        ans = asyncio.run(ws.answer_query("cuándo es el eclipse"))
    assert (ans.ok, ans.speech, ans.source) == (
        False, ws.SPEECH_HUB_ERROR, ws.SOURCE_HUB_ERROR,
    )
    assert any("hub summary failed: RuntimeError" in r.getMessage() for r in caplog.records)


def test_hub_empty_text_is_a_hub_error(monkeypatch) -> None:
    _patch(monkeypatch, fetch=[_hit(1)], summarise="")
    ans = asyncio.run(ws.answer_query("algo"))
    assert (ans.ok, ans.source) == (False, ws.SOURCE_HUB_ERROR)


def test_failure_replies_are_all_distinct() -> None:
    speeches = {
        ws.SPEECH_EMPTY_QUERY, ws.SPEECH_SEARXNG_DOWN,
        ws.SPEECH_NO_RESULTS, ws.SPEECH_HUB_ERROR,
    }
    assert len(speeches) == 4


def test_blank_query_touches_neither_backend(monkeypatch) -> None:
    calls = _patch(monkeypatch, fetch=[_hit(1)], summarise="x")
    ans = asyncio.run(ws.answer_query("   "))
    assert (ans.ok, ans.source) == (False, ws.SOURCE_EMPTY_QUERY)
    assert calls == {"fetch": [], "summarise": []}


def _mock_client(monkeypatch, handler) -> None:
    real = httpx.AsyncClient
    monkeypatch.setattr(
        ws.httpx, "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )


def test_fetch_results_queries_spanish_json(monkeypatch) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        seen["path"] = request.url.path
        return httpx.Response(200, json={"results": [_hit(1)]})

    _mock_client(monkeypatch, handler)
    assert asyncio.run(ws._fetch_results("hola")) == [_hit(1)]
    assert seen["path"] == "/search"
    assert seen["params"] == {"q": "hola", "format": "json", "language": "es"}


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json={"no_results_key": []}),
    ],
    ids=["http-500", "not-json", "no-results-list"],
)
def test_fetch_results_maps_bad_responses_to_backend_error(monkeypatch, response) -> None:
    _mock_client(monkeypatch, lambda request: response)
    with pytest.raises(ws.SearchBackendError):
        asyncio.run(ws._fetch_results("hola"))


def test_fetch_results_maps_connection_failure_to_backend_error(monkeypatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    _mock_client(monkeypatch, refuse)
    with pytest.raises(ws.SearchBackendError):
        asyncio.run(ws._fetch_results("hola"))
