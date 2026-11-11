"""The declared lifecycle states: container phase, VM phase, vehicle state.

Each concept has one enum and one derive function in its owner module, and the
readers (watchdog, power toggle, sampler, JS views) ask the derived value
instead of comparing a status word. Nothing here talks to Docker, Hyper-V or
Tesla: the derive functions are pure, and the one action path under test is
monkeypatched so it fails loudly if it is ever reached.
"""

from __future__ import annotations

import pytest

from app.webapp import searxng_watchdog as wd
from app.webapp import tesla_sampler as sampler
from src.devices import hyperv_client as H
from src.devices import tesla_client as T
from src.voice import searxng_client as S


# ------------------------------------------------------------ SearXNG phase
@pytest.mark.parametrize(
    ("status", "reachable", "phase"),
    [
        ("running", True, S.SearxngPhase.SERVING),
        ("running", False, S.SearxngPhase.UNREACHABLE),
        ("exited", False, S.SearxngPhase.DOWN),
        ("created", False, S.SearxngPhase.DOWN),
        ("paused", False, S.SearxngPhase.DOWN),
        ("dead", False, S.SearxngPhase.DOWN),
        ("unknown", False, S.SearxngPhase.DOWN),
        ("not_found", False, S.SearxngPhase.NOT_FOUND),
        (None, False, S.SearxngPhase.NOT_FOUND),
        # A stale probe answer never makes a stopped container serve.
        ("exited", True, S.SearxngPhase.DOWN),
        ("not_found", True, S.SearxngPhase.NOT_FOUND),
    ],
)
def test_searxng_phase_table(status: str, reachable: bool, phase: S.SearxngPhase) -> None:
    assert S.derive_searxng_phase(status, reachable) is phase


def test_searxng_state_carries_its_phase_on_the_wire() -> None:
    state = S.SearxngState(available=False, container_status="running", reachable=False)
    assert state.to_dict()["phase"] == "unreachable"


def test_watchdog_never_restarts_a_stopped_container_or_a_booting_one() -> None:
    """state-machine-proof: searxng-phase

    A restart is only legal from UNREACHABLE and only past the grace window;
    DOWN and NOT_FOUND get ``up -d`` and a booting container gets nothing.
    """
    fresh = wd._WatchdogState()
    for status in ("exited", "created", "paused", "dead", "not_found", "unknown"):
        state = S.SearxngState(available=False, container_status=status, reachable=False)
        assert wd._plan(state, fresh) == "start"
    wedged = S.SearxngState(available=False, container_status="running", reachable=False)
    assert wd._plan(wedged, wd._WatchdogState(unreachable_ticks=wd.UNREACHABLE_GRACE_TICKS)) is None
    assert wd._plan(wedged, wd._WatchdogState(unreachable_ticks=wd.UNREACHABLE_GRACE_TICKS + 1)) == "restart"


# ---------------------------------------------------------------- VM phase
@pytest.mark.parametrize(
    ("word", "phase"),
    [
        ("running", H.VmPhase.RUNNING),
        ("off", H.VmPhase.OFF),
        ("not_found", H.VmPhase.NOT_FOUND),
        ("unknown", H.VmPhase.UNKNOWN),
        ("saved", H.VmPhase.TRANSITIONING),
        ("paused", H.VmPhase.TRANSITIONING),
        ("starting", H.VmPhase.TRANSITIONING),
        ("stopping", H.VmPhase.TRANSITIONING),
    ],
)
def test_vm_phase_table(word: str, phase: H.VmPhase) -> None:
    assert H.derive_vm_phase(word) is phase
    assert H.HyperVState(available=True, state=word).to_dict()["phase"] == phase.value


def test_vm_power_toggle_refuses_a_move_into_the_state_it_is_already_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """state-machine-proof: vm-phase

    Start on a RUNNING VM and stop on an OFF one are refused before any cmdlet
    runs; the cmdlet runner here fails the test if it is reached.
    """
    monkeypatch.setattr(H, "vm_name", lambda: "vm")

    def no_cmdlet(*args: object, **kwargs: object) -> None:
        raise AssertionError("a refused transition must not run a cmdlet")

    monkeypatch.setattr(H, "_run", no_cmdlet)
    monkeypatch.setattr(H, "fetch_hyperv_state", lambda: H.HyperVState(available=True, state="running"))
    with pytest.raises(H.HyperVStateError):
        H.start_vm()
    monkeypatch.setattr(H, "fetch_hyperv_state", lambda: H.HyperVState(available=True, state="off"))
    with pytest.raises(H.HyperVStateError):
        H.stop_vm()
    monkeypatch.setattr(H, "fetch_hyperv_state", lambda: H.HyperVState(available=False, state="not_found"))
    with pytest.raises(H.HyperVNotFoundError):
        H.start_vm()


# ------------------------------------------------------------ vehicle state
@pytest.mark.parametrize(
    ("word", "awake"),
    [
        ("online", True),
        ("asleep", False),
        ("offline", False),
        ("unconfigured", False),
        ("error", False),
        ("unknown", False),
        # A word Tesla grows later is not "awake" until it is declared.
        ("waking", False),
    ],
)
def test_only_an_online_reading_is_awake(word: str, awake: bool) -> None:
    assert T.TeslaReading(state=word).awake is awake


def test_a_non_awake_reading_never_advances_the_idle_clock() -> None:
    """state-machine-proof: vehicle-state

    The quiet-window clock only runs on an awake reading with no activity; an
    asleep, offline or errored reading resets it instead of counting toward
    pausing data reads.
    """
    config = sampler.TeslaSamplerConfig()
    cadence = sampler.CadenceState(idle_since=100.0)
    for word in (T.VehicleState.ASLEEP, T.VehicleState.OFFLINE, T.VehicleState.ERROR, T.VehicleState.UNCONFIGURED):
        cadence.idle_since = 100.0
        sampler.note_reading(cadence, config, T.TeslaReading(state=word.value), now=500.0)
        assert cadence.idle_since is None
        assert cadence.quiet_until == 0.0
