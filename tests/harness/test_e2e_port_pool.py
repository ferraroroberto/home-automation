"""A failure from a drained loopback port pool is named as one, not left as a page failure.

Windows fails a ``connect()`` that lands on a TIME_WAIT pair with
``net::ERR_ADDRESS_IN_USE``, so a gate that shares the host with other runs can
fail a page load that has nothing wrong with it. ``tests/e2e/_port_pool.py``
leads such a report with ``PORT POOL EXHAUSTED`` and lists the tests in a section
of that name. Pinned here by running failing cases in a child pytest, because the
reporting is a hook over real reports; no browser is started.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from src._no_window import NO_WINDOW
from tests.e2e._port_pool import PORT_POOL_HEADLINE, is_port_pool_failure

_REPO_ROOT = Path(__file__).resolve().parents[2]

_CONFTEST = """\
from tests.e2e._port_pool import pytest_runtest_makereport  # noqa: F401
from tests.e2e._port_pool import write_summary


def pytest_terminal_summary(terminalreporter):
    write_summary(terminalreporter)
"""

_CASES = """\
def test_page_load_ran_out_of_ports():
    raise AssertionError("Page.goto: net::ERR_ADDRESS_IN_USE at https://127.0.0.1:1/")


def test_driver_could_not_connect():
    raise AssertionError("APIRequestContext.get: connect ENOBUFS 127.0.0.1:1")


def test_real_page_defect():
    raise AssertionError("expected 3 rows, saw 2")
"""


@pytest.mark.parametrize("text", ["net::ERR_ADDRESS_IN_USE", "connect ENOBUFS 127.0.0.1:8447"])
def test_the_two_port_pool_symptoms_are_recognised(text: str) -> None:
    assert is_port_pool_failure(f"Page.goto: {text} at https://x/")


def test_an_ordinary_failure_is_not_a_port_pool_failure() -> None:
    assert not is_port_pool_failure("expected 3 rows, saw 2")


def _run_child(tmp_path: Path) -> str:
    (tmp_path / "conftest.py").write_text(_CONFTEST, encoding="utf-8")
    (tmp_path / "test_cases.py").write_text(_CASES, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(_REPO_ROOT), "PYTHONUTF8": "1"}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
        creationflags=NO_WINDOW,
    )
    return result.stdout


def _reports(out: str) -> dict[str, str]:
    """Each failure report in the child's output, by test name."""
    found = re.findall(r"^_+ (test_\w+) _+\n(.*?)(?=^_+ test_|^=+ )", out, flags=re.S | re.M)
    return dict(found)


def _section(out: str) -> str:
    match = re.search(r"^=+ PORT POOL EXHAUSTED =+\n(.*?)(?=^=+ )", out, flags=re.S | re.M)
    return match.group(1) if match else ""


def test_a_port_pool_failure_leads_with_its_cause_and_is_listed_in_its_own_section(tmp_path: Path) -> None:
    out = _run_child(tmp_path)
    reports = _reports(out)

    for name in ("test_page_load_ran_out_of_ports", "test_driver_could_not_connect"):
        assert reports[name].startswith(PORT_POOL_HEADLINE), name
        assert f"::{name}" in _section(out), name


def test_an_ordinary_failure_keeps_its_own_report(tmp_path: Path) -> None:
    out = _run_child(tmp_path)
    defect = _reports(out)["test_real_page_defect"]

    assert "PORT POOL EXHAUSTED" not in defect
    assert "expected 3 rows, saw 2" in defect
    assert "test_real_page_defect" not in _section(out)
