"""Unit tests for the pure control law in :mod:`src.hvac_automation`.

Focus on :func:`next_setpoint` — the asymmetric drive-harder-by-step /
jump-to-idle-on-reaching-target behaviour (issue #114) — plus the fleet
boost sequencer :func:`next_boost_admission` and its transition write
:func:`transition_setpoint` (issue #562). No event loop, no MELCloud client, no
clock: every decision here is a pure function of its arguments.
"""

from src.hvac_automation import (
    IDLE_OFFSET,
    MIN_SETTLE_INTERVAL_S,
    BoostCoordinatorConfig,
    boosted_target,
    load_boost_config,
    next_boost_admission,
    next_boost_state,
    next_setpoint,
    save_boost_config,
    transition_setpoint,
    validate_boost_config,
)

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


# ------------------------------------------ transition setpoint write (#562)
_TS_KW = dict(buffer=0.5, tmin=16.0, tmax=31.0)


def _ts(**over):
    return transition_setpoint(**{**_TS_KW, **over})


def test_transition_jumps_straight_to_the_target_not_one_step():
    """The reason the sequencer is sound: an admission has to produce a
    measurable change in draw *inside* one settle interval.

    Cool, room 26, boosted target 25, parked at 28 from an earlier idle. The
    gradual law would write 27.5 and take another ~hour to reach 25; the
    transition commands 25 now.
    """
    assert _ts(
        operation_mode="Cool", room_temperature=26.0, set_temperature=28.0, target=25.0
    ) == 25.0


def test_transition_on_the_satisfied_side_keeps_the_idle_jump():
    # Room already at/below the boosted target — commanding the target itself
    # would be more drive than the steering law ever applies, so this side keeps
    # next_setpoint's behaviour exactly.
    assert _ts(
        operation_mode="Cool", room_temperature=24.0, set_temperature=20.0, target=25.0
    ) == 25.0 + IDLE_OFFSET


def test_transition_heat_is_symmetric():
    assert _ts(
        operation_mode="Heat", room_temperature=18.0, set_temperature=19.0, target=23.0
    ) == 23.0


def test_transition_is_clamped_to_the_mode_range():
    # A big boost offset can push the target under tmin; the clamp still bounds
    # the write exactly as the gradual law's does.
    assert _ts(
        operation_mode="Cool", room_temperature=30.0, set_temperature=20.0, target=14.0
    ) == 16.0


def test_transition_holds_inside_the_deadband():
    assert _ts(
        operation_mode="Cool", room_temperature=25.3, set_temperature=20.0, target=25.0
    ) is None


def test_transition_holds_when_the_setpoint_is_already_there():
    assert _ts(
        operation_mode="Cool", room_temperature=26.0, set_temperature=25.0, target=25.0
    ) is None


def test_transition_unsteerable_mode_and_missing_readings_hold():
    assert _ts(
        operation_mode="Auto", room_temperature=26.0, set_temperature=28.0, target=25.0
    ) is None
    assert _ts(
        operation_mode="Cool", room_temperature=None, set_temperature=28.0, target=25.0
    ) is None
    assert _ts(
        operation_mode="Cool", room_temperature=26.0, set_temperature=None, target=25.0
    ) is None
    assert _ts(
        operation_mode="Cool", room_temperature=26.0, set_temperature=28.0, target=None
    ) is None


# ------------------------------------------------- fleet boost sequencer (#562)
_COORD = BoostCoordinatorConfig(
    settle_interval_s=300, admission_margin_w=0.0, hard_deficit_w=1000.0
)
# Two adjacent buckets on the source's 5-minute publish grid.
_BUCKET_A = "2026-07-30 12:00"
_BUCKET_B = "2026-07-30 12:05"


def _nba(**over):
    kw = dict(
        wants_boost={},
        admitted_order=(),
        pv_surplus_w=9000.0,
        now_monotonic=10_000.0,
        last_change_monotonic=None,
        last_change_as_of=None,
        energy_as_of=_BUCKET_B,
        surplus_on_w=1500.0,
        config=_COORD,
    )
    kw.update(over)
    return next_boost_admission(**kw)


def test_never_admits_more_than_one_unit_in_one_tick():
    """The acceptance case: surplus big enough for all three admits exactly one."""
    decision = _nba(wants_boost={"a": True, "b": True, "c": True})
    assert decision.admit == "a"  # deterministic order, not fetch order
    assert decision.reason == "admitted"
    assert decision.shed == ()


def test_a_second_admission_waits_out_the_settle_interval():
    common = dict(
        wants_boost={"a": True, "b": True},
        admitted_order=("a",),
        last_change_monotonic=10_000.0,
        last_change_as_of=_BUCKET_A,
        energy_as_of=_BUCKET_B,
    )
    early = _nba(now_monotonic=10_100.0, **common)  # only 100s of 300s elapsed
    assert early.admit is None
    assert early.reason == "held_settle"
    assert early.held == ("b",)  # still a candidate, retried next interval

    later = _nba(now_monotonic=10_400.0, **common)
    assert later.admit == "b"
    assert later.reason == "admitted"


def test_admission_is_blocked_while_the_solar_bucket_has_not_advanced():
    """Wall-clock alone cannot tell a fresh reading from a frozen feed — this
    source emits permanent multi-bucket holes, and re-reading the pre-admission
    surplus is exactly how the herd gets reconstructed."""
    common = dict(
        wants_boost={"a": True, "b": True},
        admitted_order=("a",),
        now_monotonic=11_000.0,  # well past the settle interval
        last_change_monotonic=10_000.0,
        last_change_as_of=_BUCKET_A,
    )
    stale = _nba(energy_as_of=_BUCKET_A, **common)
    assert stale.admit is None
    assert stale.reason == "held_settle"

    fresh = _nba(energy_as_of=_BUCKET_B, **common)
    assert fresh.admit == "b"


def test_admission_is_held_while_remaining_surplus_is_inside_the_margin():
    config = BoostCoordinatorConfig(
        settle_interval_s=300, admission_margin_w=500.0, hard_deficit_w=1000.0
    )
    common = dict(
        config=config,
        wants_boost={"a": True, "b": True},
        admitted_order=("a",),
        now_monotonic=11_000.0,
        last_change_monotonic=10_000.0,
        last_change_as_of=_BUCKET_A,
        energy_as_of=_BUCKET_B,
    )
    # Over the entry threshold (1500) but not by the margin — hold, not fail.
    held = _nba(pv_surplus_w=1800.0, **common)
    assert held.admit is None
    assert held.reason == "held_margin"
    assert held.held == ("b",)

    # Surplus rises: it resumes with no operator action.
    released = _nba(pv_surplus_w=2100.0, **common)
    assert released.admit == "b"
    assert released.reason == "admitted"


def test_sequential_shed_is_lifo_over_the_units_eligible_to_stop():
    """'c' was admitted last but is still inside its min-duration (its candidacy
    is still True), so it is skipped rather than jumped ahead of — min-duration
    wins over strict LIFO."""
    decision = _nba(
        wants_boost={"a": False, "b": False, "c": True},
        admitted_order=("a", "b", "c"),
        now_monotonic=11_000.0,
        last_change_monotonic=10_000.0,
        pv_surplus_w=100.0,
    )
    assert decision.shed == ("b",)  # last-admitted of the eligible ones
    assert decision.reason == "shed_sequential"
    assert decision.admit is None

    # Next interval, 'b' gone: 'a' is next down the order, 'c' still held.
    following = _nba(
        wants_boost={"a": False, "c": True},
        admitted_order=("a", "c"),
        now_monotonic=11_400.0,
        last_change_monotonic=11_000.0,
        pv_surplus_w=100.0,
    )
    assert following.shed == ("a",)


def test_sequential_shed_also_waits_out_the_settle_interval():
    decision = _nba(
        wants_boost={"a": False, "b": False},
        admitted_order=("a", "b"),
        now_monotonic=10_100.0,
        last_change_monotonic=10_000.0,
        pv_surplus_w=100.0,
    )
    assert decision.shed == ()
    assert decision.reason == "held_settle"


def test_hard_deficit_sheds_every_boosted_unit_at_once():
    """Sustained import overrides both min-duration and the settle interval: all
    three units still *want* boost and only 10s have passed, yet the house is
    importing 1.5 kW."""
    decision = _nba(
        wants_boost={"a": True, "b": True, "c": True},
        admitted_order=("a", "b", "c"),
        now_monotonic=10_010.0,
        last_change_monotonic=10_000.0,
        pv_surplus_w=-1500.0,
    )
    assert decision.reason == "shed_deficit"
    assert decision.shed == ("c", "b", "a")  # last-admitted first
    assert decision.admit is None


def test_a_deficit_short_of_the_threshold_falls_through_to_the_sequential_shed():
    decision = _nba(
        wants_boost={"a": False, "b": False},
        admitted_order=("a", "b"),
        now_monotonic=11_000.0,
        last_change_monotonic=10_000.0,
        pv_surplus_w=-400.0,  # importing, but under the 1000W fast-shed bar
    )
    assert decision.reason == "shed_sequential"
    assert decision.shed == ("b",)


def test_after_a_deficit_shed_units_re_admit_one_at_a_time():
    """The recovery path: nothing boosted, the sun is back, three candidates —
    the ramp is staggered again rather than re-herding."""
    too_soon = _nba(
        wants_boost={"a": True, "b": True, "c": True},
        admitted_order=(),
        now_monotonic=10_050.0,
        last_change_monotonic=10_000.0,  # the deficit shed
        last_change_as_of=_BUCKET_A,
        energy_as_of=_BUCKET_B,
    )
    assert too_soon.admit is None
    assert too_soon.reason == "held_settle"

    settled = _nba(
        wants_boost={"a": True, "b": True, "c": True},
        admitted_order=(),
        now_monotonic=10_350.0,
        last_change_monotonic=10_000.0,
        last_change_as_of=_BUCKET_A,
        energy_as_of=_BUCKET_B,
    )
    assert settled.admit == "a"
    assert settled.shed == ()


def test_no_signal_neither_admits_nor_sheds():
    """``pv_surplus_w is None`` is 'no signal', never zero: freeze the fleet."""
    decision = _nba(
        wants_boost={"a": False, "b": True},
        admitted_order=("a",),
        pv_surplus_w=None,
        now_monotonic=99_000.0,
        last_change_monotonic=10_000.0,
    )
    assert decision.reason == "no_signal"
    assert decision.admit is None
    assert decision.shed == ()


def test_no_candidates_is_idle():
    decision = _nba(wants_boost={"a": True}, admitted_order=("a",))
    assert decision.reason == "idle"
    assert decision.admit is None
    assert decision.shed == ()


def test_an_unevaluated_boosted_unit_holds_its_boost():
    # Missing candidacy must never be read as "wants to stop" — a unit is not
    # shed on missing information.
    decision = _nba(
        wants_boost={},
        admitted_order=("a",),
        now_monotonic=11_000.0,
        last_change_monotonic=10_000.0,
        pv_surplus_w=100.0,
    )
    assert decision.shed == ()
    assert decision.reason == "idle"


# ------------------------------------------- coordinator config store (#562)
def test_boost_config_defaults_when_the_file_is_absent(tmp_path):
    config = load_boost_config(tmp_path / "nope.json")
    assert config.settle_interval_s == MIN_SETTLE_INTERVAL_S
    assert config.admission_margin_w == 0.0
    assert config.ordering_policy == "stable"


def test_a_hand_edited_settle_interval_below_the_floor_is_clamped_up(tmp_path):
    """The file stays hand-editable, so the floor cannot live only in the writer:
    a hand-written 60 would silently reconstruct the herd."""
    path = tmp_path / "hvac_boost.json"
    path.write_text('{"settle_interval_s": 60}', encoding="utf-8")
    assert load_boost_config(path).settle_interval_s == MIN_SETTLE_INTERVAL_S


def test_malformed_values_fall_back_to_defaults_rather_than_failing(tmp_path):
    path = tmp_path / "hvac_boost.json"
    path.write_text(
        '{"admission_margin_w": "lots", "hard_deficit_w": -5, '
        '"ordering_policy": "whatever"}',
        encoding="utf-8",
    )
    config = load_boost_config(path)
    assert config.admission_margin_w == 0.0
    assert config.hard_deficit_w == 1000.0
    assert config.ordering_policy == "stable"


def test_validate_rejects_a_settle_interval_under_the_floor():
    config = BoostCoordinatorConfig(settle_interval_s=120)
    try:
        validate_boost_config(config)
    except ValueError as exc:
        assert "settle_interval_s" in str(exc)
    else:  # pragma: no cover — the writer must not clamp silently
        raise AssertionError("expected a ValueError naming settle_interval_s")


def test_save_preserves_keys_the_store_does_not_own(tmp_path):
    path = tmp_path / "hvac_boost.json"
    path.write_text('{"_doc": "why 10 minutes here"}', encoding="utf-8")
    save_boost_config(BoostCoordinatorConfig(settle_interval_s=600), path)

    import json

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["_doc"] == "why 10 minutes here"
    assert raw["settle_interval_s"] == 600
    assert load_boost_config(path).settle_interval_s == 600
