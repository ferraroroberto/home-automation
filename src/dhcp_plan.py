"""Ordered DHCP reservation planner — categorised IP ranges (issue #170).

UI-free, network-free core for the "tidy the LAN" planner. Given the current
attached-device inventory and a category-range config, it assigns **each known
device the lowest free IP inside its category's range**, so an IP tells you what
a device is (``2–10`` infra, ``11–20`` phones, …). The user applies the result
manually in the F6600P's *DHCP Binding* form — **this module never writes to the
router** (automated write-back is an explicit phase 2; the seam lives stubbed on
:class:`src.network_client.RouterClient`).

**Config source.** Ranges + classification rules live in ``config/dhcp_plan.json``
(gitignored; a committed ``config/dhcp_plan.sample.json`` documents the shape). A
missing or invalid file is not an error — :func:`load_dhcp_plan_config` returns an
empty config and :func:`build_plan` then reports every device as *unassigned* with
a single warning, the same "graceful default" pattern as :mod:`src.tariff`.

**Assignment is deterministic and stable.** Devices are processed in MAC order, and
a device whose current IP already falls inside its own category range keeps it
(minimises churn) — only misplaced or new devices move. Range overflow, in-range
collisions, unclassified devices, and randomised (un-reservable) MACs all surface
as explicit, ordered warnings rather than silent drops.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "dhcp_plan.json"

# Fallback subnet when the config omits one (last octet is all the planner sets).
_DEFAULT_SUBNET_PREFIX = "192.168.0"


# --------------------------------------------------------------------------- #
# Config                                                                      #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CategoryRange:
    """One ordered category with its inclusive last-octet window ``[start, end]``."""

    label: str
    start: int
    end: int

    @property
    def capacity(self) -> int:
        return max(0, self.end - self.start + 1)


@dataclass(frozen=True)
class DhcpPlanConfig:
    """Parsed ``dhcp_plan.json``: subnet, ordered ranges, rules, MAC overrides."""

    subnet_prefix: str
    ranges: Tuple[CategoryRange, ...]
    # (category-label, keywords) in priority order — first keyword hit wins.
    rules: Tuple[Tuple[str, Tuple[str, ...]], ...]
    overrides: Dict[str, str]  # normalised MAC -> category label

    @property
    def labels(self) -> Tuple[str, ...]:
        return tuple(r.label for r in self.ranges)


# --------------------------------------------------------------------------- #
# Plan result                                                                 #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Assignment:
    """One device's planned reservation: where it is now vs where it should go."""

    mac: str
    label: str  # human display for the device (display-name → vendor → host → MAC)
    category: Optional[str]
    current_ip: Optional[str]
    planned_ip: Optional[str]
    randomized: bool
    # Action this row implies against the router's static-binding table (phase 2,
    # issue #176): "reserved" already bound to planned_ip (no-op), "create" not yet
    # bound, "change" bound to a different IP, "none" nothing to write (unplaced /
    # randomised / unassigned). "none" whenever the real bindings aren't known.
    status: str = "none"


@dataclass(frozen=True)
class CategoryPlan:
    """A range plus the devices assigned into it, in planned-IP order."""

    label: str
    start: int
    end: int
    assignments: Tuple[Assignment, ...]


@dataclass(frozen=True)
class DhcpPlan:
    """The full plan: per-category assignments, the unassigned tail, warnings."""

    categories: Tuple[CategoryPlan, ...]
    unassigned: Tuple[Assignment, ...]
    warnings: Tuple[str, ...]


# --------------------------------------------------------------------------- #
# Device input                                                                #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DeviceInput:
    """Normalised device the planner classifies + places (UI/transport-agnostic)."""

    mac: str
    ip: Optional[str] = None
    name: Optional[str] = None  # router/AP hostname
    display_name: Optional[str] = None  # user override label
    vendor: Optional[str] = None  # OUI vendor
    randomized: bool = False  # locally-administered MAC → not reservable


def normalize_mac(mac: str) -> str:
    """Canonical key form: upper-case, colon-separated as reported, trimmed."""
    return (mac or "").strip().upper()


def device_inputs_from_inventory(
    devices: Sequence[object], overrides: Dict[str, str]
) -> List["DeviceInput"]:
    """Bridge AP/router inventory rows → :class:`DeviceInput` (one place, no drift).

    Reads ``.mac`` / ``.ip`` / ``.name`` off each row (duck-typed
    :class:`src.network_client.NetDevice`), folds in the per-MAC display-name
    ``overrides``, and derives the OUI ``vendor`` + ``randomized`` flag from
    :mod:`src.network_oui`. Shared by the CLI and the webapp endpoint so both
    classify the exact same way.
    """
    from src.network_oui import is_randomized_mac, vendor_for_mac

    out: List[DeviceInput] = []
    for d in devices:
        mac = getattr(d, "mac", None) or ""
        out.append(
            DeviceInput(
                mac=mac,
                ip=getattr(d, "ip", None),
                name=getattr(d, "name", None),
                display_name=overrides.get(normalize_mac(mac)),
                vendor=vendor_for_mac(mac),
                randomized=is_randomized_mac(mac),
            )
        )
    return out


def device_label(d: DeviceInput) -> str:
    """Identity precedence: display-name → vendor → hostname → MAC.

    Mirrors ``_device_label`` in the network router so the planner names a device
    the same way the Network list does.
    """
    return d.display_name or d.vendor or d.name or d.mac


def binding_name(label: str, mac: str) -> str:
    """A router-safe binding ``Name`` (1–32 chars) derived from a device label.

    The F6600P's *DHCP Binding* form caps the name at 1–32 chars; this keeps
    alnum/space/.-_, collapses the rest, truncates to 32, and falls back to
    ``dev-<last-4-hex>`` so the length rule is always satisfied. Shared by the CLI
    ``--apply`` path and the webapp apply endpoint so both name rows identically.
    """
    import re

    cleaned = re.sub(r"[^A-Za-z0-9 ._-]+", " ", label or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)[:32].strip()
    if cleaned:
        return cleaned
    tail = re.sub(r"[^A-Za-z0-9]", "", mac or "")[-4:] or "0000"
    return f"dev-{tail}"


# --------------------------------------------------------------------------- #
# Loading                                                                     #
# --------------------------------------------------------------------------- #
def _empty_config() -> DhcpPlanConfig:
    """The unconfigured fallback: a default subnet and no ranges/rules."""
    return DhcpPlanConfig(
        subnet_prefix=_DEFAULT_SUBNET_PREFIX, ranges=(), rules=(), overrides={}
    )


def load_dhcp_plan_config(path: Optional[Path] = None) -> DhcpPlanConfig:
    """Load ``config/dhcp_plan.json``, or the empty fallback on any problem.

    A missing file, unreadable file, bad JSON, or a structurally invalid config
    all degrade to :func:`_empty_config` with a warning — the planner then reports
    every device as unassigned rather than raising.
    """
    target = Path(path) if path is not None else DEFAULT_PATH
    if not target.exists():
        return _empty_config()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("⚠️ Could not read %s (%s); no DHCP plan", target, exc)
        return _empty_config()
    if not isinstance(raw, dict):
        logger.warning("⚠️ %s is not a JSON object; no DHCP plan", target)
        return _empty_config()

    try:
        prefix = str(raw.get("subnet_prefix") or _DEFAULT_SUBNET_PREFIX).strip()

        ranges: List[CategoryRange] = []
        for spec in raw.get("ranges") or []:
            if not isinstance(spec, dict):
                continue
            label = str(spec.get("label", "")).strip()
            if not label:
                continue
            ranges.append(
                CategoryRange(
                    label=label, start=int(spec["start"]), end=int(spec["end"])
                )
            )

        rules: List[Tuple[str, Tuple[str, ...]]] = []
        for spec in raw.get("rules") or []:
            if not isinstance(spec, dict):
                continue
            category = str(spec.get("category", "")).strip()
            keywords = tuple(
                str(k).strip().lower() for k in (spec.get("match") or []) if str(k).strip()
            )
            if category and keywords:
                rules.append((category, keywords))

        overrides_raw = raw.get("overrides") or {}
        overrides = (
            {normalize_mac(k): str(v).strip() for k, v in overrides_raw.items() if str(v).strip()}
            if isinstance(overrides_raw, dict)
            else {}
        )

        return DhcpPlanConfig(
            subnet_prefix=prefix,
            ranges=tuple(ranges),
            rules=tuple(rules),
            overrides=overrides,
        )
    except (TypeError, ValueError, KeyError) as exc:
        logger.warning("⚠️ %s is malformed (%s); no DHCP plan", target, exc)
        return _empty_config()


# --------------------------------------------------------------------------- #
# Classification                                                              #
# --------------------------------------------------------------------------- #
def classify(d: DeviceInput, config: DhcpPlanConfig) -> Optional[str]:
    """Category label for a device, or ``None`` when nothing classifies it.

    Precedence: a manual per-MAC ``override`` wins; otherwise the first keyword
    rule whose any substring appears in the device's "display-name hostname
    vendor" text. An override naming an unknown category is returned as-is —
    :func:`build_plan` then routes it to *unassigned* with a warning.
    """
    key = normalize_mac(d.mac)
    if key in config.overrides:
        return config.overrides[key]
    text = " ".join(filter(None, [d.display_name, d.name, d.vendor])).lower()
    for category, keywords in config.rules:
        if any(kw in text for kw in keywords):
            return category
    return None


# --------------------------------------------------------------------------- #
# Planning                                                                    #
# --------------------------------------------------------------------------- #
def _current_octet(ip: Optional[str], prefix: str) -> Optional[int]:
    """Last octet of ``ip`` if it sits on ``prefix``; else ``None``."""
    if not ip or not prefix:
        return None
    want = prefix if prefix.endswith(".") else prefix + "."
    if not ip.startswith(want):
        return None
    tail = ip[len(want):]
    try:
        octet = int(tail)
    except ValueError:
        return None
    return octet if 0 <= octet <= 255 else None


def _binding_status(reserved_ip: Optional[str], planned_ip: Optional[str]) -> str:
    """The write action a planned row implies given its current reservation.

    ``reserved_ip`` is the device's IP in the router's static-binding table (or
    ``None`` if it has no reservation / the table is unknown). See
    :class:`Assignment.status`.
    """
    if planned_ip is None:
        return "none"
    if reserved_ip is None:
        return "create"
    return "reserved" if reserved_ip == planned_ip else "change"


def _overlap_warnings(ranges: Sequence[CategoryRange]) -> List[str]:
    """Warn on overlapping category windows — two ranges sharing an octet would
    let devices in different categories be assigned the *same* IP (a collision).
    """
    out: List[str] = []
    for i, a in enumerate(ranges):
        for b in ranges[i + 1:]:
            if a.start <= b.end and b.start <= a.end:
                out.append(
                    f"Ranges '{a.label}' ({a.start}–{a.end}) and '{b.label}' "
                    f"({b.start}–{b.end}) overlap — categories may collide on an IP."
                )
    return out


def _assign_category(
    rng: CategoryRange,
    devices: Sequence[DeviceInput],
    prefix: str,
    warnings: List[str],
    bindings: Dict[str, str],
    bindings_known: bool,
) -> List[Assignment]:
    """Place one category's devices: keep stable in-range IPs, then fill the gaps.

    Pass 1 pins any device already sitting at a free octet inside the range
    (minimises churn). Pass 2 hands every remaining device the lowest free octet.
    Randomised MACs are never assigned (a rotating address can't be reserved).
    Overflow and in-range collisions are recorded as warnings.

    The stability anchor in pass 1 is the device's **existing router reservation**
    (``bindings``: normalised MAC → reserved IP) when known, else its current
    observed IP — so once #176 supplies the real binding table the planner keeps
    already-reserved devices exactly where the router has them.
    """
    ordered = sorted(devices, key=lambda d: normalize_mac(d.mac))
    available = set(range(rng.start, rng.end + 1))
    planned: Dict[str, Optional[int]] = {}

    # An octet already reserved on the router for a device that ISN'T being placed
    # here (typically an offline device absent from the live inventory) is occupied
    # — never plan a new device onto it, or applying that row collides with the
    # existing reservation (issue #176: an offline iPad's .11 was being re-suggested
    # for a different device). Pass 1 below still anchors a placed device on its own
    # reservation; this only removes IPs owned by *others*.
    placed = {normalize_mac(d.mac) for d in devices}
    for mac, ip in bindings.items():
        if mac in placed:
            continue
        taken = _current_octet(ip, prefix)
        if taken is not None and rng.start <= taken <= rng.end:
            available.discard(taken)

    reservable = [d for d in ordered if not d.randomized]
    for d in ordered:
        if d.randomized:
            planned[normalize_mac(d.mac)] = None
            warnings.append(
                f"{device_label(d)} ({d.mac}) has a randomised MAC — disable "
                f"its Private Wi-Fi Address before it can hold a reservation."
            )

    # Pass 1 — respect a device already correctly placed inside this range. Prefer
    # the existing reservation as the anchor; fall back to the observed lease IP.
    for d in reservable:
        anchor = bindings.get(normalize_mac(d.mac)) or d.ip
        octet = _current_octet(anchor, prefix)
        if octet is not None and octet in available:
            available.discard(octet)
            planned[normalize_mac(d.mac)] = octet

    # Pass 2 — lowest free octet for everything still unplanned.
    for d in reservable:
        key = normalize_mac(d.mac)
        if key in planned:
            continue
        if available:
            octet = min(available)
            available.discard(octet)
            planned[key] = octet
        else:
            planned[key] = None
            warnings.append(
                f"Range '{rng.label}' ({prefix}.{rng.start}–{rng.end}) is full — "
                f"{device_label(d)} ({d.mac}) could not be assigned."
            )

    result: List[Assignment] = []
    for d in ordered:
        octet = planned.get(normalize_mac(d.mac))
        planned_ip = f"{prefix}.{octet}" if octet is not None else None
        result.append(
            Assignment(
                mac=d.mac,
                label=device_label(d),
                category=rng.label,
                current_ip=d.ip,
                planned_ip=planned_ip,
                randomized=d.randomized,
                status=(
                    _binding_status(bindings.get(normalize_mac(d.mac)), planned_ip)
                    if bindings_known
                    else "none"
                ),
            )
        )
    # Show in planned-IP order (unplaced rows sink to the bottom).
    result.sort(key=lambda a: (a.planned_ip is None, a.planned_ip or "", normalize_mac(a.mac)))
    return result


def build_plan(
    devices: Sequence[DeviceInput],
    config: DhcpPlanConfig,
    bindings: Optional[Dict[str, str]] = None,
) -> DhcpPlan:
    """Compute the full reservation plan from the inventory + category config.

    Each device is classified, then placed into its category's range by
    :func:`_assign_category`. Devices that don't classify (or whose override names
    an unknown category) become *unassigned*. Warnings are emitted in a stable
    order: per-category (overflow / randomised / collisions), then a single
    unassigned summary, then the empty-config notice.

    ``bindings`` is the router's static-binding table (normalised MAC → reserved
    IP, from :meth:`src.network_client.RouterClient.read_dhcp_bindings`). When it is
    a dict it both anchors stability and drives each :class:`Assignment.status`
    (reserved / create / change) — an **empty** dict means the table is known-empty,
    so every placed row reads as ``create``. ``None`` means the table is *unknown*
    (no router read / a read failure): every row is ``status="none"`` and the
    planner falls back to the observed lease IP (the phase-1 behaviour), so a failed
    read never misleadingly offers to "create" reservations that may already exist.
    """
    warnings: List[str] = []
    valid = set(config.labels)
    bindings_known = bindings is not None
    binds = {normalize_mac(k): v for k, v in (bindings or {}).items() if v}

    if not config.ranges:
        warnings.append(
            "No DHCP plan configured — copy config/dhcp_plan.sample.json to "
            "config/dhcp_plan.json and define category ranges."
        )
    warnings.extend(_overlap_warnings(config.ranges))

    buckets: Dict[str, List[DeviceInput]] = {label: [] for label in config.labels}
    unassigned_devices: List[DeviceInput] = []
    for d in devices:
        category = classify(d, config)
        if category in valid:
            buckets[category].append(d)
        else:
            unassigned_devices.append(d)

    categories: List[CategoryPlan] = []
    for rng in config.ranges:
        assignments = _assign_category(
            rng, buckets[rng.label], config.subnet_prefix, warnings, binds, bindings_known
        )
        categories.append(
            CategoryPlan(
                label=rng.label, start=rng.start, end=rng.end, assignments=tuple(assignments)
            )
        )

    unassigned = tuple(
        Assignment(
            mac=d.mac,
            label=device_label(d),
            category=classify(d, config),  # may be an unknown override label
            current_ip=d.ip,
            planned_ip=None,
            randomized=d.randomized,
        )
        for d in sorted(unassigned_devices, key=lambda d: normalize_mac(d.mac))
    )
    if unassigned:
        warnings.append(
            f"{len(unassigned)} device(s) did not match any category — add a rule "
            f"or a per-MAC override in config/dhcp_plan.json."
        )

    return DhcpPlan(categories=tuple(categories), unassigned=unassigned, warnings=tuple(warnings))
