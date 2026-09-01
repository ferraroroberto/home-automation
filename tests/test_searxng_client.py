"""Coverage for the SearXNG control surface (#716).

`start_searxng` and `restart_searxng` now share one `_compose` helper. These
pin the two things that refactor could silently change: the exact `docker
compose` argv each one builds, and the mapping from a non-zero exit to
`SearxngCommandError`.
"""

from __future__ import annotations

import subprocess

import pytest

from src import searxng_client as client
from src.searxng_client import SearxngCommandError, SearxngConfigError, SearxngState


@pytest.fixture
def stack(monkeypatch):
    """A configured stack whose docker calls are recorded, not run."""
    monkeypatch.setattr(client, "compose_path", lambda: "C:/stack/docker-compose.yml")
    monkeypatch.setattr(
        client,
        "fetch_searxng_state",
        lambda: SearxngState(available=True, container_status="running", reachable=True),
    )
    calls = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(client.subprocess, "run", _run)
    return calls


def test_start_builds_an_idempotent_up_command(stack) -> None:
    state = client.start_searxng()
    assert stack == [["docker", "compose", "-f", "C:/stack/docker-compose.yml", "up", "-d"]]
    assert state.available is True


def test_restart_builds_a_restart_command(stack) -> None:
    """`up -d` is a no-op on a running-but-wedged container; restart is not."""
    client.restart_searxng()
    assert stack == [["docker", "compose", "-f", "C:/stack/docker-compose.yml", "restart"]]


def test_a_failing_docker_command_raises_with_its_stderr(monkeypatch) -> None:
    monkeypatch.setattr(client, "compose_path", lambda: "C:/stack/docker-compose.yml")
    monkeypatch.setattr(
        client.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="daemon not running"),
    )
    with pytest.raises(SearxngCommandError, match="daemon not running"):
        client.start_searxng()


def test_an_unconfigured_stack_path_raises_config_error(monkeypatch) -> None:
    monkeypatch.delenv("SEARXNG_COMPOSE_PATH", raising=False)
    monkeypatch.setattr(client.os, "getenv", lambda name, default=None: None)
    with pytest.raises(SearxngConfigError):
        client.compose_path()
