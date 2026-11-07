"""The password route locks out a guessing client and alerts the owner.

Runs on a fake clock (no sleeping) and a fake notifier (no Telegram), so it
proves the wiring in ``app/webapp/routers/auth.py`` without waiting or sending.
"""

from __future__ import annotations

from typing import Any, Callable, Iterator, List

import pytest
from fastapi.testclient import TestClient

from app.webapp.routers.auth import make_login_alert
from src.login_lockout import LoginLockout

_PASSWORD = "correct-horse"
_WRONG = "wrong-guess-xyz"
_TOKEN = "test-bearer-xyz"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _FakeNotifier:
    def __init__(self) -> None:
        self.sent: List[str] = []

    def send_text(self, text: str) -> None:
        self.sent.append(text)


class _InlineThread:
    """Runs the target at ``start()`` so the test needs no waiting; counts spawns."""

    started = 0

    def __init__(self, target: Callable[[], Any], daemon: bool = False) -> None:
        self._target = target

    def start(self) -> None:
        type(self).started += 1
        self._target()


@pytest.fixture()
def clock() -> _Clock:
    return _Clock()


@pytest.fixture()
def notifier() -> _FakeNotifier:
    return _FakeNotifier()


@pytest.fixture()
def lockout(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, notifier: _FakeNotifier
) -> Iterator[LoginLockout]:
    from app.webapp.server import app as _app

    _InlineThread.started = 0
    monkeypatch.setattr("app.webapp.routers.auth.threading.Thread", _InlineThread)

    cfg = _app.state.webapp_config
    monkeypatch.setattr(cfg, "auth_token", _TOKEN)
    monkeypatch.setattr(cfg, "auth_password", _PASSWORD)
    lk = LoginLockout(clock=clock, on_alert=make_login_alert(lambda: notifier))
    # Saved and restored by hand: pytest 9.2's monkeypatch reads the old value from
    # `target.__dict__`, and Starlette's `State` keeps its values in `_state`, so it
    # would record the live lockout as absent and delete it on teardown.
    original = _app.state.login_lockout
    _app.state.login_lockout = lk
    try:
        yield lk
    finally:
        _app.state.login_lockout = original


def _client(host: str) -> TestClient:
    from app.webapp.server import app as _app

    return TestClient(_app, client=(host, 12345))


def _login(client: TestClient, password: str):
    return client.post("/api/login", json={"password": password})


def test_seventh_attempt_is_429_then_correct_after_the_wait(
    lockout: LoginLockout, clock: _Clock
) -> None:
    remote = _client("192.0.2.1")
    # Five wrong passwords are free; the sixth is still a 401 but starts the wait.
    for _ in range(6):
        assert _login(remote, _WRONG).status_code == 401

    locked = _login(remote, _WRONG)
    assert locked.status_code == 429
    wait = int(locked.headers["Retry-After"])
    assert wait >= 1

    # A locked client is refused even with the right password, until the wait is over.
    assert _login(remote, _PASSWORD).status_code == 429

    clock.now += wait + 1
    ok = _login(remote, _PASSWORD)
    assert ok.status_code == 200
    assert ok.json() == {"token": _TOKEN}


def test_three_wrong_passwords_send_one_alert(
    lockout: LoginLockout, notifier: _FakeNotifier
) -> None:
    remote = _client("192.0.2.7")
    for _ in range(2):
        _login(remote, _WRONG)
    assert notifier.sent == []

    _login(remote, _WRONG)
    assert len(notifier.sent) == 1
    for _ in range(2):  # the rest of the streak raises no second alert
        _login(remote, _WRONG)

    assert len(notifier.sent) == 1
    assert _InlineThread.started == 1  # the send never runs on the login request
    text = notifier.sent[0]
    assert "home-automation" in text
    assert "192.0.2.7" in text
    assert "3" in text
    assert _WRONG not in text and _PASSWORD not in text and _TOKEN not in text


def test_the_pc_itself_is_never_locked_out_or_alerted(
    lockout: LoginLockout, notifier: _FakeNotifier
) -> None:
    pc = _client("127.0.0.1")
    for _ in range(10):
        assert _login(pc, _WRONG).status_code == 401
    assert _login(pc, _PASSWORD).status_code == 200
    assert notifier.sent == []
