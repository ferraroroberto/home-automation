"""The tab view-state machine in ``view-state.js``, driven through Node.

Every tab's ``data-state`` comes from one declared set (``ViewState``) and one
derive function (``deriveViewState``). These tests run the real module with a
stubbed DOM, so the guard on ``set()`` is the shipped code, not a copy.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict

import pytest

from src._no_window import NO_WINDOW

_VIEW_STATE_JS = Path(__file__).resolve().parents[2] / "app" / "webapp" / "static" / "view-state.js"

# view-state.js imports modules that read `document` at load; a permissive stub
# lets the real file import without a browser.
_HARNESS = """
const stub = () => new Proxy(function () {}, {
  get: (t, k) => (k === Symbol.toPrimitive ? () => '' : stub()),
  apply: () => stub(),
});
globalThis.document = stub();
globalThis.window = globalThis;
globalThis.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
const warnings = [];
console.warn = (msg) => warnings.push(String(msg));
const m = await import({module_url});
const out = {};
const { derive, walk } = JSON.parse(process.argv[2]);
out.states = Object.values(m.ViewState);
out.derived = derive.map(([outcome, facts]) => {
  try { return m.deriveViewState(outcome, facts); } catch (exc) { return 'throws'; }
});
const view = m.createViewState(null);
out.walk = walk.map((next) => { view.set(next); return view.state; });
out.warnings = warnings;
out.isReading = Object.values(m.ViewState).map((s) => [s, m.isReading(s)]);
console.log(JSON.stringify(out));
"""


def _run(tmp_path: Path, derive: list, walk: list) -> Dict[str, Any]:
    node = shutil.which("node")
    assert node, "node is required on PATH to exercise view-state.js"
    script = tmp_path / "harness.mjs"
    script.write_text(_HARNESS.replace("{module_url}", json.dumps(_VIEW_STATE_JS.as_uri())), encoding="utf-8")
    out = subprocess.run(
        [node, str(script), json.dumps({"derive": derive, "walk": walk})],
        capture_output=True, text=True, timeout=60, check=False, encoding="utf-8", creationflags=NO_WINDOW,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_view_state_set_is_closed(tmp_path: Path) -> None:
    got = _run(tmp_path, [], [])
    assert got["states"] == ["idle", "loading", "ready", "empty", "error", "stale", "hidden"]


@pytest.mark.parametrize(
    ("outcome", "facts", "expected"),
    [
        ("fetched", {"ready": True}, "ready"),
        ("fetched", {"ready": False}, "empty"),
        ("restored", {"ready": True}, "stale"),
        ("restored", {"ready": False}, "empty"),
        ("failed", {"hasData": True}, "stale"),
        ("failed", {"hasData": False}, "error"),
        ("failed", None, "error"),
        ("bogus", {}, "throws"),
    ],
)
def test_derive_view_state_table(tmp_path: Path, outcome: str, facts: Any, expected: str) -> None:
    assert _run(tmp_path, [[outcome, facts]], [])["derived"] == [expected]


def test_view_state_refuses_illegal_transition(tmp_path: Path) -> None:
    """state-machine-proof: view-state

    Nothing returns to ``idle`` and an unknown name is never stored: each
    refused move leaves the state where it was and says so once.
    """
    got = _run(tmp_path, [], ["loading", "ready", "idle", "bogus", "stale"])
    assert got["walk"] == ["loading", "ready", "ready", "ready", "stale"]
    assert got["warnings"] == [
        "view-state: refused ready -> idle",
        "view-state: refused ready -> bogus",
    ]


def test_is_reading_is_idle_or_loading(tmp_path: Path) -> None:
    reading = {state for state, flag in _run(tmp_path, [], [])["isReading"] if flag}
    assert reading == {"idle", "loading"}
