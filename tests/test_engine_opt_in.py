"""Automation engines start only in the launcher-opted-in live instance (#876).

A throwaway webapp (a sibling worktree for a design review, a manual
``uvicorn`` on a spare port) boots with the live ``.env`` and ``config/`` but
its own ``logs/`` and its own ``config/.automation-owner.lock`` — so it won the
#690 ownership lock and ran every engine: the alarm schedule re-fired the
05:00 disarm and paged the household three times in one afternoon. These tests
drive the real ``lifespan`` with every engine starter replaced by a recorder,
so nothing here can reach a device, a notifier or the network.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import app.webapp.server as server
from src import automation_owner

_STARTERS = (
    "start_sampler",
    "start_telemetry_sampler",
    "start_automation",
    "start_presence_refresher",
    "start_presence_automation",
    "start_security_schedules",
    "start_blind_schedules",
    "start_wake_alarms",
    "start_power_monitor",
    "start_ha_trace_collector",
    "start_searxng_watchdog",
    "start_nightly_speedtest",
)


class _FakeOwnership:
    """Always wins the #690 lock — exactly what a worktree instance got."""

    def __init__(self, port: int) -> None:
        self.held = True
        self.owner_info = None

    def release(self) -> None:
        pass


@pytest.fixture
def boot(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Run the real lifespan once; return the engine starters it called."""
    started: list[str] = []
    for name in _STARTERS:
        monkeypatch.setattr(server, name, lambda name=name: started.append(name))

    async def _noop() -> None:
        return None

    monkeypatch.setattr(server.telemetry, "init_db", lambda: None)
    monkeypatch.setattr(server, "validate_push_config", lambda: None)
    monkeypatch.setattr(server, "warn_unconfigured_quick_actions", lambda: None)
    monkeypatch.setattr(server, "AutomationOwnership", _FakeOwnership)
    monkeypatch.setattr(server.read_snapshot, "registered", lambda: [])
    monkeypatch.setattr(server.energy.ENERGY_SNAPSHOT, "warm", _noop)
    monkeypatch.setattr(server.weather.WEATHER_SNAPSHOT, "warm", _noop)
    # A .env without the switch, wherever the opt-in check looks for one.
    env_file = tmp_path / ".env"
    env_file.write_text("SOME_SETTING=1\n", encoding="utf-8")
    monkeypatch.setattr(automation_owner, "DOTENV_PATH", env_file, raising=False)

    def _run() -> tuple[list[str], SimpleNamespace]:
        app = SimpleNamespace(state=SimpleNamespace(webapp_config=SimpleNamespace(port=9999)))

        async def _go() -> None:
            async with server.lifespan(app):
                pass

        asyncio.run(_go())
        return started, app

    return SimpleNamespace(run=_run, env_file=env_file)


def test_a_boot_without_the_launcher_opt_in_starts_no_engine(boot, monkeypatch) -> None:
    monkeypatch.delenv("HOME_AUTOMATION_ENGINES", raising=False)
    started, app = boot.run()
    assert started == []
    assert app.state.automation_owned is False


def test_the_tray_opt_in_starts_every_engine(boot, monkeypatch) -> None:
    monkeypatch.setenv("HOME_AUTOMATION_ENGINES", "1")
    started, app = boot.run()
    assert started == list(_STARTERS)
    assert app.state.automation_owned is True


def test_an_opt_in_written_into_dotenv_is_refused(boot, monkeypatch) -> None:
    """``.env`` is copied into every worktree, so it must never be the switch."""
    boot.env_file.write_text("HOME_AUTOMATION_ENGINES=1\n", encoding="utf-8")
    monkeypatch.setenv("HOME_AUTOMATION_ENGINES", "1")  # as load_dotenv would set it
    started, _ = boot.run()
    assert started == []


def test_the_tray_manager_opts_its_child_in() -> None:
    import inspect

    from app.webapp import manager

    assert 'env["HOME_AUTOMATION_ENGINES"] = "1"' in inspect.getsource(manager.WebappManager.start)
