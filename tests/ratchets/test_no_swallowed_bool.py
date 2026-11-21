"""A broad ``except`` must not fold "could not establish the fact" into a bare bool.

``except Exception: return False`` turns an unreadable input into the same
answer as a healthy one (the cert expiry check once reported "not expiring"
for a corrupt cert). A probe that can fail to establish a fact raises a typed
error or returns a distinct unknown state. A handler that really is a
yes/no answer carries ``# bool-fold-exempt: <why>`` on its ``except`` line.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("app", "scripts", "src")
BROAD = {"Exception", "BaseException"}


def _is_bare_bool_return(handler: ast.ExceptHandler) -> bool:
    body = handler.body
    return (
        len(body) == 1
        and isinstance(body[0], ast.Return)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, bool)
    )


def _is_broad(handler: ast.ExceptHandler) -> bool:
    return handler.type is None or (isinstance(handler.type, ast.Name) and handler.type.id in BROAD)


def _offenders() -> list[str]:
    found: list[str] = []
    for top in SCANNED:
        for path in (ROOT / top).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            lines = source.splitlines()
            for node in ast.walk(ast.parse(source)):
                if not (isinstance(node, ast.ExceptHandler) and _is_broad(node) and _is_bare_bool_return(node)):
                    continue
                if "bool-fold-exempt:" in lines[node.lineno - 1]:
                    continue
                found.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
    return found


def test_no_broad_except_folds_into_a_bool() -> None:
    assert _offenders() == [], (
        "broad `except` returning a bare True/False hides an unknown state; raise a typed error, "
        "return a distinct unknown value, or mark the line `# bool-fold-exempt: <why>`"
    )
