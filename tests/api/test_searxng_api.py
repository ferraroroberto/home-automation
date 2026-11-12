"""API smoke for the SearXNG control routes: a timed-out docker call is a 502.

``subprocess.run`` inside the client is faked, so no docker command runs.
"""

from __future__ import annotations

import subprocess

from fastapi.testclient import TestClient

from src.voice import searxng_client as sx


def _expire(cmd, **kwargs):
    raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])


def test_start_answers_502_when_docker_compose_times_out(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(sx, "compose_path", lambda: "C:/stack/docker-compose.yml")
    monkeypatch.setattr(sx.subprocess, "run", _expire)
    resp = client.post("/api/searxng/start")
    assert resp.status_code == 502
    assert "timed out after 60 s" in resp.json()["detail"]


def test_status_answers_502_when_docker_inspect_times_out(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(sx.subprocess, "run", _expire)
    resp = client.get("/api/searxng")
    assert resp.status_code == 502
    assert "docker inspect timed out" in resp.json()["detail"]
