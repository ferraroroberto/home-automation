"""``src._numbers.optional_float`` and the guard that keeps it the only copy.

The same "float or ``None``" parser used to be re-typed in four clients under
three names (``_num``, ``_as_float``, ``_to_float``), and jscpd never saw it:
each copy is a handful of tokens, under its duplicate threshold. This walks the
AST of every ``src`` module instead and fails when a new function has that body
outside :mod:`src._numbers`. ``src.energy.athom_client`` is the one allowed
variant, because it also maps ESPHome's ``"nan"`` to ``None``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List

import pytest

from src._numbers import optional_float

SRC = Path(__file__).resolve().parent.parent / "src"

# The module that owns the helper, and the one variant with extra semantics.
_ALLOWED = {"_numbers.py", "athom_client.py"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        (0, 0.0),
        (3, 3.0),
        ("1.5", 1.5),
        ("  230.4 ", 230.4),
        ("", None),
        ("n/a", None),
        ([], None),
        ({}, None),
    ],
)
def test_optional_float(raw: object, expected: object) -> None:
    assert optional_float(raw) == expected


def _is_float_call(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "float"


def _is_float_or_none(func: ast.FunctionDef) -> bool:
    """True for a one-argument function that only does ``try: float(x) except: return None``.

    The try body is a single ``return float(...)``, or ``n = float(...)`` with
    the function ending in ``return n``; a handler returns ``None``. A function
    that goes on to compute something from the float (a datetime, a message)
    is a different helper and does not match.
    """
    if len(func.args.args) != 1:
        return False
    body = [s for s in func.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
    for node in body:
        if not isinstance(node, ast.Try) or len(node.body) != 1:
            continue
        stmt = node.body[0]
        if not _is_float_call(stmt.value if isinstance(stmt, (ast.Return, ast.Assign)) else None):
            continue
        ends_on_value = (
            body[-1] is node
            if isinstance(stmt, ast.Return)
            else isinstance(body[-1], ast.Return) and isinstance(body[-1].value, ast.Name)
        )
        returns_none = any(
            isinstance(h_stmt, ast.Return)
            and (h_stmt.value is None or (isinstance(h_stmt.value, ast.Constant) and h_stmt.value.value is None))
            for handler in node.handlers
            for h_stmt in handler.body
        )
        if ends_on_value and returns_none:
            return True
    return False


def test_no_second_float_or_none_parser_in_src() -> None:
    offenders: List[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name in _ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and _is_float_or_none(node):
                offenders.append(f"{path.relative_to(SRC.parent).as_posix()}:{node.lineno} {node.name}")
    assert not offenders, (
        "a hand-written 'float or None' parser; import src._numbers.optional_float instead: "
        + ", ".join(offenders)
    )
