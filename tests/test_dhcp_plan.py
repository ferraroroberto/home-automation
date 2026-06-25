"""Pure-logic unit tests for the DHCP reservation planner (issue #170).

No network, no FastAPI — exercises :mod:`src.dhcp_plan` directly: config loading
+ graceful fallback, classification precedence, and the deterministic, stable
assignment (lowest-free IP, in-range stability, overflow / randomised / unassigned
/ overlap warnings).
"""

from __future__ import annotations

import json
from pathlib import Path

from src.dhcp_plan import (
    CategoryRange,
    DeviceInput,
    DhcpPlanConfig,
    build_plan,
    classify,
    device_inputs_from_inventory,
    load_dhcp_plan_config,
)


def _config() -> DhcpPlanConfig:
    """A small three-category config used across the assignment tests."""
    return DhcpPlanConfig(
        subnet_prefix="192.168.0",
        ranges=(
            CategoryRange("Infra", 2, 4),
            CategoryRange("Phones", 11, 20),
            CategoryRange("Cameras", 21, 22),  # tiny range to force overflow
        ),
        rules=(
            ("Infra", ("router", "nas")),
            ("Phones", ("iphone", "ipad")),
            ("Cameras", ("camera", "-cam", "reolink")),
        ),
        overrides={"AA:AA:AA:AA:AA:AA": "Infra"},
    )


# --------------------------------------------------------------- loading
def test_load_missing_file_returns_empty_config(tmp_path: Path) -> None:
    config = load_dhcp_plan_config(tmp_path / "nope.json")
    assert config.ranges == ()
    assert config.rules == ()
    assert config.overrides == {}
    assert config.subnet_prefix == "192.168.0"  # sensible default


def test_load_malformed_file_falls_back(tmp_path: Path) -> None:
    bad = tmp_path / "dhcp_plan.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert load_dhcp_plan_config(bad).ranges == ()


def test_load_valid_file_parses(tmp_path: Path) -> None:
    target = tmp_path / "dhcp_plan.json"
    target.write_text(
        json.dumps(
            {
                "subnet_prefix": "10.0.0",
                "ranges": [{"label": "Infra", "start": 2, "end": 10}],
                "rules": [{"category": "Infra", "match": ["Router", "NAS"]}],
                "overrides": {"aa:bb:cc:dd:ee:ff": "Infra"},
            }
        ),
        encoding="utf-8",
    )
    config = load_dhcp_plan_config(target)
    assert config.subnet_prefix == "10.0.0"
    assert config.ranges == (CategoryRange("Infra", 2, 10),)
    # Keywords are lower-cased; override MAC is normalised upper-case.
    assert config.rules == (("Infra", ("router", "nas")),)
    assert config.overrides == {"AA:BB:CC:DD:EE:FF": "Infra"}


# --------------------------------------------------------------- classify
def test_classify_override_beats_rules() -> None:
    config = _config()
    # MAC override wins even though the hostname would match Phones.
    d = DeviceInput(mac="AA:AA:AA:AA:AA:AA", name="my-iphone")
    assert classify(d, config) == "Infra"


def test_classify_keyword_rule_and_unassigned() -> None:
    config = _config()
    assert classify(DeviceInput(mac="1", name="Living-Room-Camera"), config) == "Cameras"
    assert classify(DeviceInput(mac="2", vendor="Apple", name="Kitchen iPad"), config) == "Phones"
    assert classify(DeviceInput(mac="3", name="mystery-box"), config) is None


# --------------------------------------------------------------- assignment
def test_assigns_lowest_free_ip_in_range_deterministically() -> None:
    config = _config()
    devices = [
        DeviceInput(mac="00:00:00:00:00:02", name="iphone-b"),
        DeviceInput(mac="00:00:00:00:00:01", name="iphone-a"),
    ]
    plan = build_plan(devices, config)
    phones = next(c for c in plan.categories if c.label == "Phones")
    # Sorted by MAC, lowest-free first: .11 then .12.
    assert [(a.mac, a.planned_ip) for a in phones.assignments] == [
        ("00:00:00:00:00:01", "192.168.0.11"),
        ("00:00:00:00:00:02", "192.168.0.12"),
    ]


def test_existing_in_range_ip_is_kept_stable() -> None:
    config = _config()
    devices = [
        DeviceInput(mac="00:00:00:00:00:01", name="iphone-a"),               # no IP
        DeviceInput(mac="00:00:00:00:00:02", ip="192.168.0.15", name="iphone-b"),  # in range
    ]
    plan = build_plan(devices, config)
    phones = {a.mac: a.planned_ip for c in plan.categories if c.label == "Phones" for a in c.assignments}
    assert phones["00:00:00:00:00:02"] == "192.168.0.15"   # kept where it is
    assert phones["00:00:00:00:00:01"] == "192.168.0.11"   # lowest free, not .15


def test_out_of_range_current_ip_moves_into_category() -> None:
    config = _config()
    # A camera currently sitting in the Phones range must move into Cameras.
    d = DeviceInput(mac="00:00:00:00:00:09", ip="192.168.0.15", name="front-camera")
    plan = build_plan([d], config)
    cams = next(c for c in plan.categories if c.label == "Cameras")
    assert cams.assignments[0].current_ip == "192.168.0.15"
    assert cams.assignments[0].planned_ip == "192.168.0.21"


def test_overflow_warns_and_leaves_device_unplaced() -> None:
    config = _config()  # Cameras range is .21–.22 → capacity 2
    devices = [
        DeviceInput(mac=f"00:00:00:00:00:1{i}", name=f"camera-{i}") for i in range(3)
    ]
    plan = build_plan(devices, config)
    cams = next(c for c in plan.categories if c.label == "Cameras")
    planned = [a.planned_ip for a in cams.assignments]
    assert planned.count(None) == 1                       # one couldn't be placed
    assert "192.168.0.21" in planned and "192.168.0.22" in planned
    assert any("is full" in w for w in plan.warnings)


def test_randomized_mac_is_not_assigned_and_warns() -> None:
    config = _config()
    d = DeviceInput(mac="DA:A1:19:00:00:01", name="someones-iphone", randomized=True)
    plan = build_plan([d], config)
    phones = next(c for c in plan.categories if c.label == "Phones")
    assert phones.assignments[0].planned_ip is None
    assert any("randomised MAC" in w for w in plan.warnings)


def test_unassigned_devices_grouped_and_warned() -> None:
    config = _config()
    d = DeviceInput(mac="00:00:00:00:00:07", name="mystery")
    plan = build_plan([d], config)
    assert [a.mac for a in plan.unassigned] == ["00:00:00:00:00:07"]
    assert all(a.planned_ip is None for a in plan.unassigned)
    assert any("did not match any category" in w for w in plan.warnings)


def test_empty_config_makes_everything_unassigned() -> None:
    d = DeviceInput(mac="00:00:00:00:00:01", name="iphone")
    plan = build_plan([d], load_dhcp_plan_config(Path("does-not-exist")))
    assert plan.categories == ()
    assert [a.mac for a in plan.unassigned] == ["00:00:00:00:00:01"]
    assert any("No DHCP plan configured" in w for w in plan.warnings)


def test_overlapping_ranges_warn() -> None:
    config = DhcpPlanConfig(
        subnet_prefix="192.168.0",
        ranges=(CategoryRange("A", 10, 20), CategoryRange("B", 15, 25)),
        rules=(),
        overrides={},
    )
    plan = build_plan([], config)
    assert any("overlap" in w for w in plan.warnings)


# ----------------------------------------------------- inventory bridge
class _Row:
    """Minimal duck-typed NetDevice stand-in for the bridge test."""

    def __init__(self, mac, ip=None, name=None):
        self.mac = mac
        self.ip = ip
        self.name = name


def test_device_inputs_from_inventory_folds_vendor_override_and_randomized() -> None:
    rows = [
        _Row("5C:CF:7F:11:22:33", "192.168.0.5", None),   # known OUI → Espressif
        _Row("DA:A1:19:00:00:01", "192.168.0.6", None),   # locally-administered
    ]
    overrides = {"5C:CF:7F:11:22:33": "Boiler sensor"}
    out = device_inputs_from_inventory(rows, overrides)
    assert out[0].vendor == "Espressif"
    assert out[0].display_name == "Boiler sensor"
    assert out[0].randomized is False
    assert out[1].randomized is True
    assert out[1].vendor is None  # randomised MAC is never vendored
