"""The restart sheet's server half is mounted behind the app's own auth.

`src/self_restart.py` is vendored verbatim and tested in project-scaffolding;
this covers the app-side wiring: the route is behind the bearer gate (remote
callers need the token, loopback needs none), a cross-origin POST is refused,
and a real POST starts the declared tray recipe through a runner that this test
replaces, so no test ever restarts anything.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.webapp.middleware import BearerTokenMiddleware, require_authenticated
from app.webapp.server import app
from src.self_restart import TRAY_RESTART, RestartTarget, restart_router

TOKEN = "t" * 32
REMOTE = ("100.64.0.9", 4321)
ORIGIN = {"Origin": "http://testserver"}


def _guarded_app(tmp_path: Path, spawned: List[Path], token: str = TOKEN) -> FastAPI:
    """The restart router behind the real middleware and guard, with a stub runner."""
    guarded = FastAPI()
    guarded.add_middleware(BearerTokenMiddleware, get_token=lambda: token)
    guarded.include_router(restart_router(
        targets=[RestartTarget("app", "Restart app", TRAY_RESTART)],
        status_dir=tmp_path / "restart",
        guard=require_authenticated,
        root=tmp_path,
        spawn=lambda spec, root: spawned.append(spec),
    ))
    return guarded


def test_real_app_declares_the_tray_recipe_as_its_only_target(client: TestClient) -> None:
    res = client.get("/api/restart")
    assert res.status_code == 200
    assert [t["id"] for t in res.json()["targets"]] == ["app"]


def test_remote_caller_without_the_token_is_refused(client: TestClient) -> None:
    # The real app's token is whatever config/ holds, so only the refusal is asserted.
    remote = TestClient(app, client=REMOTE)
    saved = app.state.webapp_config.auth_token
    app.state.webapp_config.auth_token = TOKEN
    try:
        assert remote.get("/api/restart").status_code == 401
        assert remote.post("/api/restart", json={"target": "app"}, headers=ORIGIN).status_code == 401
    finally:
        app.state.webapp_config.auth_token = saved


def test_remote_caller_with_the_token_starts_a_restart(tmp_path: Path) -> None:
    spawned: List[Path] = []
    remote = TestClient(_guarded_app(tmp_path, spawned), client=REMOTE)
    bearer = {"Authorization": f"Bearer {TOKEN}"}

    assert remote.get("/api/restart", headers=bearer).status_code == 200
    res = remote.post("/api/restart", json={"target": "app"}, headers={**bearer, **ORIGIN})

    assert res.status_code == 202
    assert len(spawned) == 1
    assert remote.get("/api/restart", headers=bearer).json()["last"]["state"] == "running"


def test_wrong_token_is_refused_and_starts_nothing(tmp_path: Path) -> None:
    spawned: List[Path] = []
    remote = TestClient(_guarded_app(tmp_path, spawned), client=REMOTE)
    res = remote.post(
        "/api/restart", json={"target": "app"},
        headers={"Authorization": "Bearer wrong", **ORIGIN},
    )
    assert res.status_code == 401
    assert spawned == []


def test_cross_origin_post_is_refused_even_with_the_token(tmp_path: Path) -> None:
    spawned: List[Path] = []
    remote = TestClient(_guarded_app(tmp_path, spawned), client=REMOTE)
    res = remote.post(
        "/api/restart", json={"target": "app"},
        headers={"Authorization": f"Bearer {TOKEN}", "Origin": "https://elsewhere.example"},
    )
    assert res.status_code == 403
    assert spawned == []


def test_loopback_needs_no_token(tmp_path: Path) -> None:
    spawned: List[Path] = []
    local = TestClient(_guarded_app(tmp_path, spawned), client=("127.0.0.1", 1234))
    assert local.post("/api/restart", json={"target": "app"}, headers=ORIGIN).status_code == 202
    assert len(spawned) == 1


def test_a_route_behind_the_guard_refuses_when_the_middleware_is_missing(tmp_path: Path) -> None:
    unguarded = FastAPI()
    unguarded.include_router(restart_router(
        targets=[RestartTarget("app", "Restart app", TRAY_RESTART)],
        status_dir=tmp_path / "restart", guard=require_authenticated, root=tmp_path,
        spawn=lambda spec, root: pytest.fail("a restart started without auth"),
    ))
    res = TestClient(unguarded).post("/api/restart", json={"target": "app"}, headers=ORIGIN)
    assert res.status_code == 401
