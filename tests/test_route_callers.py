"""Every HTTP route has a caller outside its own tests.

FastAPI registers routes by decorator, so a dead write endpoint is invisible to
dead-code detectors (vulture sees a used function) and stays alive on the strength
of its own tests alone -- false confidence for code that writes to a device. This
scans every ``@router.<verb>("/path")`` / ``@app.<verb>("/path")`` and requires its
path to appear in at least one real client: the PWA (``static/``), the Home Assistant
integration, the CLIs and scripts, the tray, background modules, or the repo-owned
voice config. ``tests/`` and the router modules themselves do not count.

Matching is textual and per path, not per verb: a ``GET`` and ``POST`` on one path
are both satisfied by either caller. A parameterised route (``/x/{id}``) is matched
by its static prefix including the trailing slash. A route whose only callers live
outside this repo goes in ``EXTERNAL_CALLERS`` with the reason; the allowlist is
itself checked, so an entry that gains an in-repo caller or loses its route fails.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_ROUTE_RE = re.compile(
    r"""@(?:router|app)\.(get|post|put|delete|patch)\(\s*["']([^"']+)["']"""
)

# Route declarations: never counted as their own callers.
_ROUTER_FILES = {
    *(REPO_ROOT / "app" / "webapp" / "routers").glob("*.py"),
    REPO_ROOT / "app" / "webapp" / "server.py",
}

# (directory, glob patterns) that hold real clients of the HTTP API.
_CALLER_SOURCES = (
    ("app/webapp/static", ("*.js", "*.html")),
    ("app/webapp", ("*.py",)),
    ("app/tray", ("*.py",)),
    ("custom_components", ("*.py",)),
    ("scripts", ("*.py", "*.ps1")),
    ("src", ("*.py",)),
    ("docs/voice-pe-config", ("*.yaml",)),
)

# "VERB /path" -> who calls it from outside this repo. Add an entry only for a
# client that cannot be grepped here; a route nobody calls is deleted instead.
EXTERNAL_CALLERS = {
    "POST /api/presence/webhooks/{person_id}/{state}": (
        "phone home/away automations configured on the handset (docs/presence.md)"
    ),
}


def _routes() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for path in sorted(_ROUTER_FILES):
        text = path.read_text(encoding="utf-8")
        found += [(m.group(1).upper(), m.group(2)) for m in _ROUTE_RE.finditer(text)]
    return found


def _caller_texts() -> list[str]:
    texts: list[str] = []
    for directory, patterns in _CALLER_SOURCES:
        for pattern in patterns:
            for path in sorted((REPO_ROOT / directory).rglob(pattern)):
                if path not in _ROUTER_FILES:
                    texts.append(path.read_text(encoding="utf-8", errors="ignore"))
    return texts


def _needle(route_path: str) -> re.Pattern[str]:
    """A static path must end at the path boundary; a parameterised one by its prefix."""
    if "{" in route_path:
        return re.compile(re.escape(route_path.split("{", 1)[0]))
    return re.compile(re.escape(route_path) + r"(?![\w/-])")


def _uncalled() -> list[str]:
    texts = _caller_texts()
    return [
        f"{verb} {path}"
        for verb, path in _routes()
        if not any(_needle(path).search(t) for t in texts)
    ]


def test_route_scan_finds_the_routes() -> None:
    """Guards the scan itself: a broken regex must not make the check vacuous."""
    assert len(_routes()) >= 100
    assert ("GET", "/api/units") in _routes()


def test_every_route_has_a_caller() -> None:
    unexplained = [r for r in _uncalled() if r not in EXTERNAL_CALLERS]
    assert not unexplained, (
        "routes with no caller in the PWA, HA integration, CLIs, scripts or voice "
        "config -- delete them (and their tests), or list the outside client in "
        "EXTERNAL_CALLERS:\n  " + "\n  ".join(unexplained)
    )


@pytest.mark.parametrize("route", sorted(EXTERNAL_CALLERS))
def test_external_caller_entries_are_still_needed(route: str) -> None:
    """An allowlist entry must name a real route that still has no in-repo caller."""
    declared = {f"{verb} {path}" for verb, path in _routes()}
    assert route in declared, f"{route} is no longer a route; drop it from EXTERNAL_CALLERS"
    assert route in _uncalled(), (
        f"{route} now has an in-repo caller; drop it from EXTERNAL_CALLERS"
    )
