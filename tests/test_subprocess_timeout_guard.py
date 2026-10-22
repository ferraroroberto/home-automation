"""Structural check for the #990 recurring-audit class: a documented "never
raises" / timeout contract that a ``subprocess.run(..., timeout=...)`` call
can silently break by letting ``subprocess.TimeoutExpired`` escape unguarded.

``src/hyperv_client.py``'s ``_run`` is the concrete instance this issue fixed:
``fetch_hyperv_state`` documented "Raises only HyperVConfigError" while a slow
``Get-VM`` could raise ``TimeoutExpired`` straight through it, and a graceful
``Stop-VM`` past 120s escaped as an unhandled 500 with ``HYPERV_SNAPSHOT``
never invalidated. This AST-scans every ``subprocess.run`` call carrying a
``timeout=`` kwarg and asserts the *enclosing function* also catches
``subprocess.TimeoutExpired`` (directly, via ``SubprocessError``, or a bare
``Exception``/``BaseException``) somewhere lexically around it — catching
only ``OSError`` does not count, since ``TimeoutExpired`` is not an
``OSError`` and that specific near-miss is what ``host_shutdown.py`` has
today (baselined below, pre-existing and out of scope for #990).

Pre-existing call sites outside this issue's fix are baselined by
``(file, function)`` — same shrink-only convention as
``scripts/static_ratchet.py``'s baseline: existing debt is tracked, not
hidden, and this test fails the moment a *new* unguarded call site appears
anywhere in the tree, or ``hyperv_client.py`` regresses.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import FrozenSet, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = ("src", "app", "scripts")

# Catching one of these around subprocess.run is sufficient to keep
# TimeoutExpired from escaping the function; OSError alone is NOT (it is not
# TimeoutExpired's base class) -- that gap is exactly this issue's bug class.
_SAFE_EXCEPT_NAMES = {"TimeoutExpired", "SubprocessError", "Exception", "BaseException"}

# Pre-existing violations outside #990's fixed scope (home-automation#990).
# Each is the same unguarded-timeout gap; left for a future, separately
# scoped cleanup rather than bundled into this issue's fix.
_BASELINE: FrozenSet[Tuple[str, str]] = frozenset(
    {
        ("src/host_shutdown.py", "initiate_shutdown"),
        ("src/host_shutdown.py", "initiate_hibernate"),
        ("src/host_shutdown.py", "cancel_shutdown"),
        ("src/network_host.py", "_run_quiet"),
        ("src/searxng_client.py", "_compose"),
        ("src/searxng_client.py", "_container_status"),
        ("src/ups_client.py", "_read_nut"),
        ("src/ups_client.py", "_read_nut_direct"),
        ("src/ups_client.py", "_read_windows_battery"),
        ("app/tray/tray.py", "_run_tailscale"),
    }
)


def _handler_is_safe(handler: ast.excepthandler) -> bool:
    if handler.type is None:
        return True  # bare `except:`
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    for node in types:
        name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
        if name in _SAFE_EXCEPT_NAMES:
            return True
    return False


class _Scanner(ast.NodeVisitor):
    """Finds every unguarded ``subprocess.run(timeout=...)`` call site."""

    def __init__(self) -> None:
        self.func_stack: list[str] = []
        self.try_stack: list[list[ast.excepthandler]] = []
        self.violations: list[Tuple[str, int]] = []  # (function, lineno)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.func_stack.append(node.name)
        self.generic_visit(node)
        self.func_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Try(self, node: ast.Try) -> None:
        self.try_stack.append(list(node.handlers))
        for stmt in node.body:
            self.visit(stmt)
        self.try_stack.pop()
        for stmt in (*node.orelse, *node.finalbody):
            self.visit(stmt)
        for handler in node.handlers:
            for stmt in handler.body:
                self.visit(stmt)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        is_subprocess_run = (
            isinstance(func, ast.Attribute)
            and func.attr == "run"
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
        )
        if is_subprocess_run and any(kw.arg == "timeout" for kw in node.keywords):
            guarded = any(
                any(_handler_is_safe(h) for h in handlers) for handlers in self.try_stack
            )
            if not guarded:
                fn = self.func_stack[-1] if self.func_stack else "<module>"
                self.violations.append((fn, node.lineno))
        self.generic_visit(node)


def _scan_repo() -> list[Tuple[str, str, int]]:
    """``[(relative_path, function_name, lineno), ...]`` for every unguarded site."""
    found: list[Tuple[str, str, int]] = []
    for root in SCAN_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            rel = path.relative_to(REPO_ROOT)
            if "tests" in rel.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(rel))
            scanner = _Scanner()
            scanner.visit(tree)
            for fn, lineno in scanner.violations:
                found.append((str(rel).replace("\\", "/"), fn, lineno))
    return found


def test_no_new_unguarded_subprocess_timeouts_beyond_baseline() -> None:
    found = _scan_repo()
    unexpected = [
        (path, fn, lineno) for path, fn, lineno in found if (path, fn) not in _BASELINE
    ]
    assert not unexpected, (
        "New subprocess.run(..., timeout=...) call(s) with no enclosing guard "
        "for subprocess.TimeoutExpired (home-automation#990's bug class — a "
        "'never raises'/timeout contract silently broken on one branch): "
        f"{unexpected}. Either catch TimeoutExpired/SubprocessError in the "
        "enclosing function, or add the site to _BASELINE with a reason if "
        "it is deliberately deferred, separately-scoped debt."
    )


def test_hyperv_run_specifically_guards_its_timeout() -> None:
    """Locks in this issue's fix: ``hyperv_client._run`` must never regress to
    letting ``TimeoutExpired`` escape, even if the file is ever re-baselined
    above for an unrelated reason."""
    found = _scan_repo()
    assert not [
        (path, fn, lineno)
        for path, fn, lineno in found
        if path == "src/hyperv_client.py"
    ]
