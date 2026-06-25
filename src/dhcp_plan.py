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
) -> List[Assignment]:
    """Place one category's devices: keep stable in-range IPs, then fill the gaps.

    Pass 1 pins any device already sitting at a free octet inside the range
    (minimises churn). Pass 2 hands every remaining device the lowest free octet.
    Randomised MACs are never assigned (a rotating address can't be reserved).
    Overflow and in-range collisions are recorded as warnings.
    """
    ordered = sorted(devices, key=lambda d: normalize_mac(d.mac))
    available = set(range(rng.start, rng.end + 1))
    planned: Dict[str, Optional[int]] = {}

    reservable = [d for d in ordered if not d.randomized]
    for d in ordered:
        if d.randomized:
            planned[normalize_mac(d.mac)] = None
            warnings.append(
                f"{device_label(d)} ({d.mac}) has a randomised MAC — disable "
                f"its Private Wi-Fi Address before it can hold a reservation."
            )

    # Pass 1 — respect a device already correctly placed inside this range.
    for d in reservable:
        octet = _current_octet(d.ip, prefix)
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
        result.append(
            Assignment(
                mac=d.mac,
                label=device_label(d),
                category=rng.label,
                current_ip=d.ip,
                planned_ip=f"{prefix}.{octet}" if octet is not None else None,
                randomized=d.randomized,
            )
        )
    # Show in planned-IP order (unplaced rows sink to the bottom).
    result.sort(key=lambda a: (a.planned_ip is None, a.planned_ip or "", normalize_mac(a.mac)))
    return result


def build_plan(devices: Sequence[DeviceInput], config: DhcpPlanConfig) -> DhcpPlan:
    """Compute the full reservation plan from the inventory + category config.

    Each device is classified, then placed into its category's range by
    :func:`_assign_category`. Devices that don't classify (or whose override names
    an unknown category) become *unassigned*. Warnings are emitted in a stable
    order: per-category (overflow / randomised / collisions), then a single
    unassigned summary, then the empty-config notice.
    """
    warnings: List[str] = []
    valid = set(config.labels)

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
        assignments = _assign_category(rng, buckets[rng.label], config.subnet_prefix, warnings)
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
