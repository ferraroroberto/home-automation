"""The solar-forecast chart on a DST-change day (#956).

The server frames a day from local midnight to midnight (#932), so the actual
curve has 25 points on the autumn change (``hour`` 2 twice) and 23 on the
spring one. ``charts.js`` plotted every point by its ``hour`` on a fixed
24-slot axis, so the repeated hour collapsed onto one slot and the first
reading was lost. These tests feed the real ``setForecastData`` the curve the
real ``_actual_curve`` produces, through Node, so the JS is exercised for real.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from app.webapp.routers import energy as E
from tests.energy.test_energy_forecast_curve import (
    _FALL_BACK,
    _MADRID,
    _SPRING_FORWARD,
    _covered,
    _local_day,
)

_CHARTS_JS = Path(__file__).resolve().parents[2] / "app" / "webapp" / "static" / "charts.js"

_HARNESS = """
import { setForecastData } from {charts_url};
const { expected, actual } = JSON.parse(process.argv[2]);
const chart = { data: { labels: [], datasets: [{ data: [] }, { data: [] }] }, update() {} };
setForecastData(chart, expected, actual);
console.log(JSON.stringify({
  labels: chart.data.labels,
  expected: chart.data.datasets[0].data,
  actual: chart.data.datasets[1].data,
  radius: chart.data.datasets[1].pointRadius,
}));
"""


def _plot(tmp_path: Path, expected: List[Dict[str, Any]], actual: List[Dict[str, Any]]) -> Dict[str, Any]:
    node = shutil.which("node")
    assert node, "node is required on PATH to exercise charts.js"
    script = tmp_path / "harness.mjs"
    script.write_text(_HARNESS.replace("{charts_url}", json.dumps(_CHARTS_JS.as_uri())), encoding="utf-8")
    out = subprocess.run(
        [node, str(script), json.dumps({"expected": expected, "actual": actual})],
        capture_output=True, text=True, encoding="utf-8", check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return json.loads(out.stdout)


def _curve(day, measured: Dict[int, float]) -> List[Dict[str, Any]]:
    """The server's actual curve for a Madrid day with ``measured`` Wh per slot index."""
    slots = _local_day(day)
    for i, wh in measured.items():
        slots[i].update(_covered(wh))
    return E._actual_curve(slots, tz=_MADRID)


_EXPECTED = [{"hour": h, "wh": 10.0} for h in range(24)]


def test_fall_back_day_plots_both_02_hours_in_order(tmp_path: Path) -> None:
    # Slots 2 and 3 are the two 02:00 hours; give every slot its own value.
    measured = {i: 100.0 * (i + 1) for i in range(25)}
    plotted = _plot(tmp_path, _EXPECTED, _curve(_FALL_BACK, measured))

    assert plotted["labels"] == [f"{h:02d}" for h in (*range(0, 3), *range(2, 24))]
    assert plotted["actual"] == [100.0 * (i + 1) / 1000 for i in range(25)]
    assert len(plotted["expected"]) == 25


def test_fall_back_day_marks_the_estimated_slot_by_position(tmp_path: Path) -> None:
    """An in-progress / under-covered hour is flagged on its own slot, not its hour's."""
    actual = _curve(_FALL_BACK, {i: 500.0 for i in range(25)})
    actual[3]["estimated"] = True  # the second 02:00
    plotted = _plot(tmp_path, _EXPECTED, actual)

    assert plotted["radius"] == [4 if i == 3 else 0 for i in range(25)]


def test_spring_forward_day_keeps_the_24_slot_axis_with_a_gap_at_02(tmp_path: Path) -> None:
    measured = {i: 100.0 * (i + 1) for i in range(23)}
    plotted = _plot(tmp_path, _EXPECTED, _curve(_SPRING_FORWARD, measured))

    assert plotted["labels"] == [f"{h:02d}" for h in range(24)]
    assert plotted["actual"][2] is None
    # Slot 2 on the spring day is 03:00 local; it must land on label "03".
    assert plotted["actual"][3] == 300.0 / 1000
    assert plotted["actual"][23] == 2300.0 / 1000


def test_an_ordinary_day_is_unchanged(tmp_path: Path) -> None:
    actual = [{"hour": h, "wh": 1000.0 * h, "estimated": False} for h in range(24)]
    plotted = _plot(tmp_path, _EXPECTED, actual)

    assert plotted["labels"] == [f"{h:02d}" for h in range(24)]
    assert plotted["actual"] == [float(h) for h in range(24)]
