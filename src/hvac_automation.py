"""Per-unit HVAC automation: dynamic setpoint rules + daily schedules.

Two UI-free concerns, persisted like :mod:`src.display_names` (atomic write
to a gitignored ``config/*.json``, missing file → empty, committed
``.sample.json``):

* **Temperature rule** — a *dynamic setpoint controller*, NOT an on/off
  thermostat. The unit's own setpoint is an unreliable black box (set it to
  27 °C in cool mode and the room overshoots to 25–26 °C), so we never trust
  it and never auto power-cycle the compressor (rapid cycling damages it).
  Instead the rule holds a desired **room** temperature and the engine steers
  the unit's *setpoint* each adjustment interval to drive the room toward it.
  The loop is asymmetric: while the room is still past the target it nudges one
  step at a time (a slow integral drive), but the moment the room reaches the
  target it jumps the setpoint to one degree on the satisfied side so the unit
  idles immediately rather than overshooting deep and recovering one step at a
  time. Per-mode targets (``cool_target`` / ``heat_target``); the active one is
  chosen by the unit's current operation mode. Dormant when the unit is off
  or in Auto/Fan (no meaningful setpoint to steer).

* **Schedule entries** — one or more daily local ``HH:MM`` entries per unit.
  Each entry can be a simple power-off event or a power-on/full-profile event
  (mode, setpoint, fan, vanes). Orthogonal to the rule: schedules decide
  *whether/how* the unit runs; the rule thereafter only steers the setpoint
  while the unit is on.

* **Boost coordinator** — the fleet-level knobs (issue #562) deciding how the
  per-unit solar boost below is *sequenced* across units: at most one admission
  or shed per settle interval, so five units never enter boost in the same tick
  and swamp the surplus they are all reacting to.

The pure control decision (:func:`next_setpoint`) lives here so it is unit
-testable without an event loop or the MELCloud client; the asyncio engine
that calls ``fetch_devices`` / ``set_device_state`` is
:mod:`app.webapp.automation`.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src._atomic_json import write_json_atomic
from src._schedule_store import read_json, save_json

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
RULES_PATH = _CONFIG_DIR / "hvac_rules.json"
SCHEDULES_PATH = _CONFIG_DIR / "hvac_schedules.json"
BOOST_CONFIG_PATH = _CONFIG_DIR / "hvac_boost.json"

# Modes the controller can steer, and the direction lowering the setpoint
# pushes the room. Cool/Dry: lower setpoint → more cooling → room falls.
# Heat: higher setpoint → more heat → room rises. Auto/Fan are not steerable.
COOL_MODES = frozenset({"Cool", "Dry"})
HEAT_MODES = frozenset({"Heat"})


@dataclass
class TempRule:
    """Dynamic-setpoint rule for one unit (per-mode desired room temps)."""

    enabled: bool = False
    cool_target: Optional[float] = None
    heat_target: Optional[float] = None
    # Solar-surplus boost (#554) — opt-in modifier on top of an active rule.
    # Inert unless `enabled` is also true: boost shifts the rule's own target,
    # so there must be a target to shift.
    boost_enabled: bool = False
    boost_offset_c: float = 2.0


@dataclass
class ScheduleEntry:
    """One daily schedule entry for a unit."""

    id: str = "default"
    enabled: bool = True
    time: str = "08:00"  # local HH:MM, recurs daily
    power: bool = True
    operation_mode: Optional[str] = None
    # set_temperature is the setpoint applied to the unit at the scheduled time
    # (the "base" the unit runs at now); target_temperature is the goal the
    # eventual solar load-balancing automation steers toward, which may differ
    # from the applied base (e.g. apply 27 now, target 28) — see #206.
    set_temperature: Optional[float] = None
    target_temperature: Optional[float] = None
    fan_speed: Optional[str] = None
    vane_vertical_direction: Optional[str] = None
    vane_horizontal_direction: Optional[str] = None


# --------------------------------------------------------------- control law
def target_for_mode(rule: TempRule, operation_mode: Optional[str]) -> Optional[float]:
    """Desired room target for the unit's current mode, or ``None`` if dormant.

    Returns ``None`` when the rule is disabled, the mode is not steerable
    (Auto/Fan), or no target is set for the active direction.
    """
    if not rule.enabled or operation_mode is None:
        return None
    if operation_mode in COOL_MODES:
        return rule.cool_target
    if operation_mode in HEAT_MODES:
        return rule.heat_target
    return None


#: How far past the target the idle setpoint sits (°C). One degree on the
#: satisfied side is enough for the unit's own thermostat to stop actively
#: driving while the unit stays on.
IDLE_OFFSET = 1.0


def next_setpoint(
    *,
    operation_mode: Optional[str],
    room_temperature: Optional[float],
    set_temperature: Optional[float],
    target: Optional[float],
    buffer: float,
    step: float,
    tmin: float,
    tmax: float,
) -> Optional[float]:
    """One adjustment of the unit's setpoint toward the desired room ``target``.

    Asymmetric by design:

    * **Drive-harder side** (room past the target by more than ``buffer``) moves
      the setpoint one ``step`` per call, so the slow-responding room has time to
      react before the next adjustment and a genuinely hot/cold room ramps in
      steadily.
    * **Satisfied side** (room has reached the target) jumps the setpoint
      *immediately* to one ``IDLE_OFFSET`` degree on the satisfied side of the
      target (Cool: ``target + 1``; Heat: ``target - 1``). The unit's own
      thermostat then idles — it stays on but stops actively driving — instead of
      clawing back one step at a time, which would park the setpoint at an
      extreme and overshoot the room deep past the target during the slow
      recovery.

    Returns the new setpoint, or ``None`` to hold (room inside the deadband
    between the target and ``target ± buffer``, an un-steerable mode, missing
    readings, or the result already equals the current clamped setpoint).
    """
    if room_temperature is None or set_temperature is None or target is None:
        return None

    if operation_mode in COOL_MODES:
        if room_temperature > target + buffer:
            new = set_temperature - step  # too warm → cool harder (gradual)
        elif room_temperature <= target:
            new = target + IDLE_OFFSET  # reached target → idle immediately
        else:
            return None  # (target, target+buffer] deadband → hold
    elif operation_mode in HEAT_MODES:
        if room_temperature < target - buffer:
            new = set_temperature + step  # too cold → heat harder (gradual)
        elif room_temperature >= target:
            new = target - IDLE_OFFSET  # reached target → idle immediately
        else:
            return None  # [target-buffer, target) deadband → hold
    else:
        return None

    new = max(tmin, min(tmax, new))
    if new == set_temperature:
        return None
    return new


def transition_setpoint(
    *,
    operation_mode: Optional[str],
    room_temperature: Optional[float],
    set_temperature: Optional[float],
    target: Optional[float],
    buffer: float,
    tmin: float,
    tmax: float,
) -> Optional[float]:
    """The setpoint to command *immediately* on a boost admission/shed (#562).

    :func:`next_setpoint`'s law with one branch changed: the drive-harder side
    jumps straight to ``target`` instead of moving one ``step``. Everything else
    — the satisfied-side idle jump, the deadband hold, the ``tmin``/``tmax``
    clamp, and returning ``None`` when the result is already the current
    setpoint — is identical, deliberately.

    **Why a second function rather than a flag on the gradual law.** The one
    -step-per-``adjust_interval_s`` drive is right for steady state (the room
    responds slowly), but it expresses a 2 °C boost as ~0.5 °C per 15 minutes —
    up to an hour to materialise, with the first increment landing as much as a
    full interval after admission. A sequencer that admits a unit and then
    measures the surplus five minutes later would see essentially no added draw,
    conclude there is still room, and admit the whole fleet before any of it
    arrived. The immediate write is what makes staggered admission measurable.

    Scoped to the **transition only**: while a unit sits boosted it goes back to
    :func:`next_setpoint`'s gradual drive. The satisfied side keeps the idle
    jump because writing ``setpoint = target`` for a room that has *already*
    reached the target would be more drive than the steering law ever applies.
    """
    if room_temperature is None or set_temperature is None or target is None:
        return None

    if operation_mode in COOL_MODES:
        if room_temperature > target + buffer:
            new = target  # drive to the (boosted) target now, not one step
        elif room_temperature <= target:
            new = target + IDLE_OFFSET  # reached target → idle immediately
        else:
            return None  # (target, target+buffer] deadband → already there
    elif operation_mode in HEAT_MODES:
        if room_temperature < target - buffer:
            new = target
        elif room_temperature >= target:
            new = target - IDLE_OFFSET
        else:
            return None
    else:
        return None

    new = max(tmin, min(tmax, new))
    if new == set_temperature:
        return None
    return new


# ----------------------------------------------------------------- solar boost
def next_boost_state(
    *,
    currently_boosting: bool,
    boosting_since: Optional[float],
    pv_surplus_w: Optional[float],
    now_monotonic: float,
    surplus_on_w: float,
    surplus_off_w: float,
    min_duration_s: float,
) -> bool:
    """Hysteresis + min-duration decision: does this unit *want* to boost now?

    Since #562 this is a **candidacy** signal, not the final answer: it says
    whether this unit, on its own, wants boost given the fleet-wide surplus.
    :func:`next_boost_admission` then decides whether it gets it *this tick* —
    at most one unit enters or leaves boost per settle interval.

    ``surplus_on_w`` (higher) starts a boost from idle; ``surplus_off_w``
    (lower) is the only level that can end one, and only after
    ``min_duration_s`` has elapsed since ``boosting_since`` — this is the
    debounce against thrashing as surplus fluctuates near the threshold.
    ``pv_surplus_w is None`` (stale/unreachable FusionSolar read) is "no
    signal": it never starts or stops a boost, only holds the current state.
    """
    if pv_surplus_w is None:
        return currently_boosting
    if not currently_boosting:
        return pv_surplus_w >= surplus_on_w
    elapsed = now_monotonic - (boosting_since if boosting_since is not None else now_monotonic)
    if elapsed < min_duration_s:
        return True
    return pv_surplus_w > surplus_off_w


def boosted_target(
    *,
    operation_mode: Optional[str],
    target: Optional[float],
    boost_offset_c: float,
    is_boosting: bool,
) -> Optional[float]:
    """Shift ``target`` toward comfort by ``boost_offset_c`` while boosting.

    Cool: lower the target (pre-cool harder). Heat: raise it (pre-heat
    harder). The result still passes through :func:`next_setpoint`'s own
    ``tmin``/``tmax`` clamp — no separate bound is applied here.
    """
    if target is None or not is_boosting:
        return target
    if operation_mode in COOL_MODES:
        return target - boost_offset_c
    if operation_mode in HEAT_MODES:
        return target + boost_offset_c
    return target


# ----------------------------------------------------------- boost coordinator
#: Hard floor on the settle interval (seconds). FusionSolar publishes on a
#: 5-minute grid and one cloud response is reused for a further cache TTL, so a
#: shorter settle makes the sequencer re-read the *same bucket* — the
#: pre-admission surplus — conclude there is still room, and admit again. That
#: reconstructs the very herd this coordinator exists to prevent. The interval
#: must also cover the inverter compressor's own ramp-up, not just the meter's
#: publish cadence — so the floor **stands unchanged now that local Modbus
#: serves the flow about a second old** (issue #618). The publish cadence stopped
#: being the binding reason; the compressor ramp never was cloud-specific, and
#: the cloud is still the fallback. Not configurable below this; the loader
#: clamps up and the writer refuses.
MIN_SETTLE_INTERVAL_S = 300
#: Sanity ceiling — a typo'd interval must not park the coordinator for a day.
MAX_SETTLE_INTERVAL_S = 3600

#: Admission/shed ordering policies. v1 ships one deterministic order, keyed on
#: unit id: with LIFO shedding, admission order *is* shed order, so it must not
#: depend on MELCloud's device-fetch order, which is not guaranteed stable. A
#: fairness rotation (so the same room is not always admitted first) is a later
#: value of this same knob, not a second knob.
ORDERING_POLICIES = ("stable",)
DEFAULT_ORDERING_POLICY = "stable"


@dataclass
class BoostCoordinatorConfig:
    """Fleet-level knobs for the solar-boost sequencer (issue #562).

    Global on purpose — these describe the *fleet*, not a room. The per-unit
    opt-in (``boost_enabled`` / ``boost_offset_c`` on :class:`TempRule`) stays
    per unit: rooms are deliberately excluded from boost one by one, and that
    must never become a global policy.
    """

    #: Minimum seconds between two changes to the boosted set.
    settle_interval_s: int = MIN_SETTLE_INTERVAL_S
    #: Extra headroom over the entry threshold required to admit the *next*
    #: unit. Defaults to 0: the surplus reading is measured, not modelled, so it
    #: already contains what the units admitted so far actually draw — "still at
    #: least the entry threshold spare" is a real test on its own.
    admission_margin_w: float = 0.0
    #: Sustained **import** (a positive magnitude of watts) at which every
    #: boosted unit is shed at once. Stored positive so the JSON, the API and
    #: the UI all speak the same number and the sign convention lives in exactly
    #: one comparison.
    hard_deficit_w: float = 1000.0
    ordering_policy: str = DEFAULT_ORDERING_POLICY


@dataclass(frozen=True)
class BoostDecision:
    """One tick's coordinator decision — at most one admission *or* one shed."""

    admit: Optional[str] = None
    shed: Tuple[str, ...] = ()
    #: ``admitted`` · ``held_margin`` · ``held_settle`` · ``shed_sequential`` ·
    #: ``shed_deficit`` · ``no_signal`` · ``idle``.
    reason: str = "idle"
    #: Candidates blocked this tick — still candidates, retried next interval.
    held: Tuple[str, ...] = ()


def _order_candidates(candidates: Sequence[str], policy: str) -> List[str]:
    """Candidates in admission order for ``policy`` (v1: deterministic by id)."""
    return sorted(candidates)


def next_boost_admission(
    *,
    wants_boost: Dict[str, bool],
    admitted_order: Sequence[str],
    pv_surplus_w: Optional[float],
    now_monotonic: float,
    last_change_monotonic: Optional[float],
    last_change_as_of: Optional[str],
    energy_as_of: Optional[str],
    surplus_on_w: float,
    config: BoostCoordinatorConfig,
) -> BoostDecision:
    """Sequence boost across the fleet: admit one, shed one, or hold.

    ``wants_boost`` is the per-unit candidacy from :func:`next_boost_state`;
    ``admitted_order`` is the currently-boosted set **in admission order**.
    Pure: no clock, no network, no I/O — the whole state machine is the
    arguments.

    Priority, highest first:

    1. **No signal.** ``pv_surplus_w is None`` (stale/unreachable FusionSolar
       read) is never zero — it neither admits nor sheds, it freezes.
    2. **Hard deficit.** Sustained import past ``config.hard_deficit_w`` sheds
       **every** boosted unit at once, ignoring both the settle interval and the
       per-unit ``min_duration_s`` (without that override a herd-induced deficit
       would persist for the full 30-minute debounce — exactly the amplitude
       problem this function exists to fix). It sheds *all* rather than one per
       tick because there is deliberately no per-unit power model to size a
       partial shed with, and the meter runs several minutes behind: shedding
       one per tick would re-decide repeatedly against the *same stale bucket*
       while the house keeps importing — fake precision. Recovery is
       self-correcting, since the units become candidates again immediately and
       re-admit one at a time under the guards below.
    3. **Sequential shed**, one per settle interval, last-admitted-first over
       the units *eligible to stop*. A unit still inside its min-duration is
       **skipped, never jumped ahead of** — min-duration wins over strict LIFO.
       (With a 300 s settle and a 1800 s min-duration the two coincide in
       practice: staggered admissions clear min-duration in the same order.)
    4. **Admission**, one per settle interval, blocked while the newest measured
       bucket is the same one the last change was made against (``held_settle``
       — wall-clock alone cannot tell a fresh reading from a frozen feed, and
       this source emits permanent multi-bucket holes) or while the remaining
       measured surplus is under ``surplus_on_w + admission_margin_w``
       (``held_margin``). A block is a **hold, not a failure**: the unit stays a
       candidate and is retried next interval.
    """
    if pv_surplus_w is None:
        return BoostDecision(reason="no_signal")

    boosted = list(admitted_order)
    if boosted and pv_surplus_w <= -config.hard_deficit_w:
        # Reverse admission order so the payload reads last-admitted-first, the
        # same ordering the sequential shed uses.
        return BoostDecision(shed=tuple(reversed(boosted)), reason="shed_deficit")

    elapsed = (
        float("inf")
        if last_change_monotonic is None
        else now_monotonic - last_change_monotonic
    )
    settled = elapsed >= config.settle_interval_s

    # Boosted units whose own hysteresis now says stop, last-admitted first.
    # Default True for anything not evaluated this tick: an unknown unit holds
    # its boost rather than being shed on missing information.
    stoppers = [uid for uid in reversed(boosted) if not wants_boost.get(uid, True)]
    if stoppers:
        if not settled:
            return BoostDecision(reason="held_settle", held=(stoppers[0],))
        return BoostDecision(shed=(stoppers[0],), reason="shed_sequential")

    boosted_set = set(boosted)
    candidates = [
        uid for uid, wants in wants_boost.items() if wants and uid not in boosted_set
    ]
    if not candidates:
        return BoostDecision(reason="idle")

    ordered = tuple(_order_candidates(candidates, config.ordering_policy))
    if not settled:
        return BoostDecision(reason="held_settle", held=ordered)
    if last_change_as_of is not None and energy_as_of == last_change_as_of:
        # Same 5-minute bucket the last change was made against: this reading
        # cannot yet contain that change's draw.
        return BoostDecision(reason="held_settle", held=ordered)
    if pv_surplus_w < surplus_on_w + config.admission_margin_w:
        return BoostDecision(reason="held_margin", held=ordered)
    return BoostDecision(admit=ordered[0], reason="admitted")


# ------------------------------------------- boost-coordinator persistence
# Read and write are deliberately different contracts, the same split
# :mod:`src.pv_system_config` documents: the loader is *lenient* so a
# hand-edited file degrades (clamp + log) instead of stopping the engine
# mid-flight, while the writer is *strict* and raises naming the field, because
# silently clamping a value the user just typed into the Energy tab is a bug,
# not resilience. Don't route the writer through the reader's parsing.
_OWNED_BOOST_KEYS = frozenset(
    {"settle_interval_s", "admission_margin_w", "hard_deficit_w", "ordering_policy"}
)


def _coerce_float(value: Any, default: float, field_name: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        logger.warning("⚠️ Invalid %s=%r; using %s", field_name, value, default)
        return default
    if out < 0:
        logger.warning("⚠️ %s must be >= 0 (got %s); using %s", field_name, out, default)
        return default
    return out


def load_boost_config(path: Optional[Path] = None) -> BoostCoordinatorConfig:
    """Read the coordinator knobs from disk; missing/malformed → defaults.

    Re-read every tick (not frozen into ``AutomationConfig`` at start-up) so an
    edit from the Energy tab is live without a tray restart — the same contract
    :func:`load_rules` / :func:`load_schedules` already have.

    The :data:`MIN_SETTLE_INTERVAL_S` floor is enforced here too, not only in
    the writer: the file stays hand-editable, and a hand-written 60 would
    otherwise silently reconstruct the herd.
    """
    target = Path(path) if path is not None else BOOST_CONFIG_PATH
    raw = read_json(target, {})
    if not isinstance(raw, dict):
        logger.warning("⚠️ %s is not a JSON object; using boost defaults", target)
        raw = {}

    defaults = BoostCoordinatorConfig()

    settle: Any = raw.get("settle_interval_s", defaults.settle_interval_s)
    try:
        settle = int(float(settle))
    except (TypeError, ValueError):
        logger.warning("⚠️ Invalid settle_interval_s=%r; using %s", settle, defaults.settle_interval_s)
        settle = defaults.settle_interval_s
    clamped = max(MIN_SETTLE_INTERVAL_S, min(MAX_SETTLE_INTERVAL_S, settle))
    if clamped != settle:
        logger.warning(
            "⚠️ settle_interval_s=%s out of range; clamped to %ds (FusionSolar "
            "publishes on a 5-minute grid)", settle, clamped,
        )

    policy = str(raw.get("ordering_policy", defaults.ordering_policy))
    if policy not in ORDERING_POLICIES:
        logger.warning("⚠️ Unknown ordering_policy=%r; using %s", policy, defaults.ordering_policy)
        policy = defaults.ordering_policy

    return BoostCoordinatorConfig(
        settle_interval_s=clamped,
        admission_margin_w=_coerce_float(
            raw.get("admission_margin_w", defaults.admission_margin_w),
            defaults.admission_margin_w,
            "admission_margin_w",
        ),
        hard_deficit_w=_coerce_float(
            raw.get("hard_deficit_w", defaults.hard_deficit_w),
            defaults.hard_deficit_w,
            "hard_deficit_w",
        ),
        ordering_policy=policy,
    )


def validate_boost_config(config: BoostCoordinatorConfig) -> None:
    """Strictly validate a config bound for disk — raises on the first problem.

    Every message names the offending field so the API can hand it back as a
    400 the editor can show against the right input.
    """
    try:
        settle = int(config.settle_interval_s)
    except (TypeError, ValueError):
        raise ValueError("settle_interval_s must be a whole number of seconds")
    if settle < MIN_SETTLE_INTERVAL_S:
        raise ValueError(
            f"settle_interval_s must be at least {MIN_SETTLE_INTERVAL_S} seconds — "
            "the solar meter publishes on a 5-minute grid, so a shorter settle "
            "re-reads the same reading and admits again before the last "
            "admission has shown up"
        )
    if settle > MAX_SETTLE_INTERVAL_S:
        raise ValueError(
            f"settle_interval_s must be at most {MAX_SETTLE_INTERVAL_S} seconds"
        )

    for field_name, value in (
        ("admission_margin_w", config.admission_margin_w),
        ("hard_deficit_w", config.hard_deficit_w),
    ):
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{field_name} must be a number")
        if not math.isfinite(number):
            raise ValueError(f"{field_name} must be a number")
        if number < 0:
            raise ValueError(f"{field_name} must be zero or greater")

    if config.ordering_policy not in ORDERING_POLICIES:
        raise ValueError(
            "ordering_policy must be one of: " + ", ".join(ORDERING_POLICIES)
        )


def save_boost_config(
    config: BoostCoordinatorConfig, path: Optional[Path] = None
) -> None:
    """Validate and atomically persist the coordinator knobs.

    Preserves any top-level key this module doesn't own, so a hand-written
    ``_doc`` note explaining why a home picked its settle interval survives an
    edit from the app (same rule as :mod:`src.pv_system_config`).
    """
    validate_boost_config(config)
    target = Path(path) if path is not None else BOOST_CONFIG_PATH

    payload: Dict[str, Any] = {}
    existing = read_json(target, None)
    if isinstance(existing, dict):
        payload = {k: v for k, v in existing.items() if k not in _OWNED_BOOST_KEYS}

    payload["settle_interval_s"] = int(config.settle_interval_s)
    payload["admission_margin_w"] = float(config.admission_margin_w)
    payload["hard_deficit_w"] = float(config.hard_deficit_w)
    payload["ordering_policy"] = config.ordering_policy

    write_json_atomic(target, payload)
    logger.info(
        "💾 Saved boost coordinator (settle %ds, margin %.0fW, fast-shed %.0fW) to %s",
        payload["settle_interval_s"], payload["admission_margin_w"],
        payload["hard_deficit_w"], target,
    )


# --------------------------------------------------------------- persistence
def _load(path: Path) -> Dict[str, dict]:
    raw = read_json(path, {})
    if not isinstance(raw, dict):
        logger.warning("⚠️ %s is not a JSON object; returning empty", path)
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def _clean_entry(raw: dict, fallback_id: str) -> ScheduleEntry:
    """Coerce untrusted JSON/API data into a ScheduleEntry."""
    allowed = {
        "id",
        "enabled",
        "time",
        "power",
        "operation_mode",
        "set_temperature",
        "target_temperature",
        "fan_speed",
        "vane_vertical_direction",
        "vane_horizontal_direction",
    }
    data = {k: raw[k] for k in allowed if k in raw}
    data["id"] = str(data.get("id") or fallback_id)
    # Keep ids compact/safe for DOM keys and fire-state keys. If a client sends
    # something odd, preserve uniqueness but remove whitespace/control chars.
    data["id"] = "-".join(data["id"].split()) or fallback_id
    return ScheduleEntry(**data)


def load_rules(path: Optional[Path] = None) -> Dict[str, TempRule]:
    """Return {unit_id: TempRule} from disk, or {} if absent."""
    target = Path(path) if path is not None else RULES_PATH
    return {uid: TempRule(**raw) for uid, raw in _load(target).items()}


def save_rules(rules: Dict[str, TempRule], path: Optional[Path] = None) -> None:
    """Atomically persist the whole rule map."""
    target = Path(path) if path is not None else RULES_PATH
    save_json(target, {uid: asdict(r) for uid, r in rules.items()})


def set_rule(unit_id: str, rule: TempRule, path: Optional[Path] = None) -> None:
    """Set (or, when fully default+disabled, drop) one unit's rule."""
    rules = load_rules(path)
    if (
        rule.enabled
        or rule.cool_target is not None
        or rule.heat_target is not None
        or rule.boost_enabled
    ):
        rules[unit_id] = rule
    else:
        rules.pop(unit_id, None)
    save_rules(rules, path)


def load_schedules(path: Optional[Path] = None) -> Dict[str, List[ScheduleEntry]]:
    """Return {unit_id: [ScheduleEntry, ...]} from disk, or {} if absent.

    Backward-compatible with the issue-83 shape where each unit mapped directly
    to one schedule object. Legacy entries load with id ``default``.
    """
    target = Path(path) if path is not None else SCHEDULES_PATH
    raw = read_json(target, {})
    if not isinstance(raw, dict):
        logger.warning("⚠️ %s is not a JSON object; returning empty", target)
        return {}

    out: Dict[str, List[ScheduleEntry]] = {}
    for uid, value in raw.items():
        entries: List[ScheduleEntry] = []
        if isinstance(value, list):
            for idx, item in enumerate(value, start=1):
                if isinstance(item, dict):
                    entries.append(_clean_entry(item, f"schedule-{idx}"))
        elif isinstance(value, dict):
            # Legacy single-schedule object.
            entries.append(_clean_entry(value, "default"))
        if entries:
            out[str(uid)] = entries
    return out


def save_schedules(
    schedules: Dict[str, List[ScheduleEntry]],
    path: Optional[Path] = None,
) -> None:
    """Atomically persist the whole schedule map."""
    target = Path(path) if path is not None else SCHEDULES_PATH
    payload = {
        uid: [asdict(s) for s in entries]
        for uid, entries in schedules.items()
        if entries
    }
    save_json(target, payload)


def set_schedules(
    unit_id: str,
    entries: List[ScheduleEntry],
    path: Optional[Path] = None,
) -> None:
    """Replace one unit's schedule-entry list (empty list removes it)."""
    schedules = load_schedules(path)
    clean = [entry for entry in entries if entry.id]
    if clean:
        schedules[unit_id] = clean
    else:
        schedules.pop(unit_id, None)
    save_schedules(schedules, path)
