"""The gate's runtime-write-ratchet stage maps the vendored script's exit codes.

`scripts/runtime_write_ratchet.py` exits 3 when a new write target cannot be resolved,
git is unusable or a tracked Python file does not parse: a write went unchecked. These
tests pin that 3 fails as NOT CONFIRMED -- never a pass, never a skip -- that the
pre-ship gate calls the wrapper that guarantees it, and that both vendored files are
declared. The script's own detection is tested in project-scaffolding, where it is
authored.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import runtime_write_ratchet_gate as gate
from tests import _ratchet_gate_helpers as helpers


def test_only_exit_zero_passes_and_exit_three_is_not_confirmed() -> None:
    helpers.assert_only_exit_zero_passes_and_three_is_not_confirmed(gate)


def test_run_exits_nonzero_for_every_script_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    helpers.assert_run_maps_every_script_exit(gate, "runtime write ratchet", tmp_path, capsys)


def test_the_pre_ship_gate_calls_the_wrapper_not_the_script() -> None:
    helpers.assert_gate_stage_calls_the_wrapper(
        "runtime_write_ratchet", "runtime write ratchet", "conventions ratchet"
    )


def test_the_ratchet_and_its_resolver_are_declared_and_vendored() -> None:
    fleet = helpers.assert_ratchet_declared_and_vendored("runtime_write_ratchet")
    resolver = fleet["vendored"]["runtime_write_resolve"]
    assert resolver["dest"] == "scripts/runtime_write_resolve.py"
    assert (helpers.REPO_ROOT / resolver["dest"]).is_file()
