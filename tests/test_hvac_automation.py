"""Unit tests for the pure control law in :mod:`src.hvac_automation`.

Focus on :func:`next_setpoint` — the asymmetric drive-harder-by-step /
jump-to-idle-on-reaching-target behaviour (issue #114). No event loop, no
MELCloud client; the decision is a pure function.
"""

from src.hvac_automation import IDLE_OFFSET, boosted_target, next_boost_state, next_setpoint

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


# --------------------------------------------------------- solar boost (#554)
_BOOST_KW = dict(surplus_on_w=1500.0, surplus_off_w=500.0, min_duration_s=1800.0)


def _nbs(**over):
    return next_boost_state(**{**_BOOST_KW, **over})


def test_boost_starts_when_surplus_crosses_on_threshold():
    assert _nbs(
        currently_boosting=False, boosting_since=None, pv_surplus_w=1600.0, now_monotonic=0.0,
    ) is True


def test_boost_does_not_start_below_on_threshold():
    assert _nbs(
        currently_boosting=False, boosting_since=None, pv_surplus_w=1400.0, now_monotonic=0.0,
    ) is False


def test_boost_holds_through_min_duration_even_if_surplus_drops():
    # Started at t=0, only 600s elapsed (< 1800s min) — holds even though
    # surplus is now below the OFF threshold.
    assert _nbs(
        currently_boosting=True, boosting_since=0.0, pv_surplus_w=100.0, now_monotonic=600.0,
    ) is True


def test_boost_stays_on_in_the_hysteresis_band_after_min_duration():
    # Past min duration, surplus is between OFF (500) and ON (1500) — the
    # hysteresis band holds an active boost rather than flapping.
    assert _nbs(
        currently_boosting=True, boosting_since=0.0, pv_surplus_w=800.0, now_monotonic=2000.0,
    ) is True


def test_boost_ends_below_off_threshold_after_min_duration():
    assert _nbs(
        currently_boosting=True, boosting_since=0.0, pv_surplus_w=400.0, now_monotonic=2000.0,
    ) is False


def test_boost_none_surplus_never_starts_or_stops():
    # Stale/unreachable FusionSolar read: hold whatever state it already had.
    assert _nbs(
        currently_boosting=False, boosting_since=None, pv_surplus_w=None, now_monotonic=0.0,
    ) is False
    assert _nbs(
        currently_boosting=True, boosting_since=0.0, pv_surplus_w=None, now_monotonic=5000.0,
    ) is True


def test_boosted_target_shifts_cool_colder():
    assert boosted_target(
        operation_mode="Cool", target=27.0, boost_offset_c=2.0, is_boosting=True,
    ) == 25.0


def test_boosted_target_shifts_heat_warmer():
    assert boosted_target(
        operation_mode="Heat", target=21.0, boost_offset_c=2.0, is_boosting=True,
    ) == 23.0


def test_boosted_target_no_shift_when_not_boosting():
    assert boosted_target(
        operation_mode="Cool", target=27.0, boost_offset_c=2.0, is_boosting=False,
    ) == 27.0


def test_boosted_target_none_passthrough():
    assert boosted_target(
        operation_mode="Cool", target=None, boost_offset_c=2.0, is_boosting=True,
    ) is None
