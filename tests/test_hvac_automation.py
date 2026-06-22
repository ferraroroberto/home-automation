"""Unit tests for the pure control law in :mod:`src.hvac_automation`.

Focus on :func:`next_setpoint` — the asymmetric drive-harder-by-step /
jump-to-idle-on-reaching-target behaviour (issue #114). No event loop, no
MELCloud client; the decision is a pure function.
"""

from src.hvac_automation import IDLE_OFFSET, next_setpoint

# Common knobs matching the engine defaults.
_KW = dict(buffer=0.5, step=0.5, tmin=16.0, tmax=31.0)


def _np(**over):
    return next_setpoint(**{**_KW, **over})


# --------------------------------------------------------- satisfied-side jump
def test_cool_reached_target_jumps_to_idle_not_one_step():
    """The acceptance case: room below target jumps straight to target+1.

    Valentina-style: Cool, room 25, target 27, parked at 18 from an earlier
    drive-down. Must jump to 28 (= target + IDLE_OFFSET), not inch up by step.
    """
    new = _np(
        operation_mode="Cool",
        room_temperature=25.0,
        set_temperature=18.0,
        target=27.0,
    )
    assert new == 27.0 + IDLE_OFFSET == 28.0


def test_cool_exactly_at_target_idles():
    new = _np(operation_mode="Cool", room_temperature=27.0, set_temperature=18.0, target=27.0)
    assert new == 28.0


def test_heat_reached_target_jumps_to_idle_below():
    """Heat is symmetric: room at/above target jumps to target-1."""
    new = _np(operation_mode="Heat", room_temperature=23.0, set_temperature=30.0, target=21.0)
    assert new == 21.0 - IDLE_OFFSET == 20.0


def test_idle_jump_clamped_to_range():
    # Cool target 31 → idle 32 → clamp to tmax 31.
    new = _np(operation_mode="Cool", room_temperature=20.0, set_temperature=18.0, target=31.0)
    assert new == 31.0


def test_idle_hold_when_already_at_idle():
    # Already parked at target+1 on the satisfied side → no write.
    assert _np(operation_mode="Cool", room_temperature=25.0, set_temperature=28.0, target=27.0) is None


# ------------------------------------------------------ drive-harder is gradual
def test_cool_too_warm_steps_down_one_step():
    new = _np(operation_mode="Cool", room_temperature=29.0, set_temperature=24.0, target=27.0)
    assert new == 24.0 - 0.5  # one step, not a jump


def test_heat_too_cold_steps_up_one_step():
    new = _np(operation_mode="Heat", room_temperature=18.0, set_temperature=22.0, target=21.0)
    assert new == 22.0 + 0.5


# ---------------------------------------------------------------- deadband/hold
def test_cool_deadband_holds():
    # target < room <= target+buffer → hold.
    assert _np(operation_mode="Cool", room_temperature=27.3, set_temperature=24.0, target=27.0) is None


def test_heat_deadband_holds():
    # target-buffer <= room < target → hold.
    assert _np(operation_mode="Heat", room_temperature=20.7, set_temperature=22.0, target=21.0) is None


# ---------------------------------------------------------- guards / un-steerable
def test_unsteerable_mode_returns_none():
    assert _np(operation_mode="Auto", room_temperature=25.0, set_temperature=24.0, target=27.0) is None


def test_missing_readings_return_none():
    assert _np(operation_mode="Cool", room_temperature=None, set_temperature=24.0, target=27.0) is None
    assert _np(operation_mode="Cool", room_temperature=25.0, set_temperature=None, target=27.0) is None
    assert _np(operation_mode="Cool", room_temperature=25.0, set_temperature=24.0, target=None) is None
