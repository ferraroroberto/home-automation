"""Regression guard against re-duplicating the router hide-flag payload shape.

jscpd's block-level clone detector never saw the hide-flag duplication this
issue fixed: ``security.py``, ``tuya.py``, ``circuits.py`` and ``network.py``
each hand-rolled their own byte-identical ``class HiddenPayload(BaseModel):
hidden: bool`` instead of importing the one shared in
:mod:`app.webapp.routers._helpers`, and the small per-site variation in the
surrounding handler (log wording, response field name) kept the blocks under
jscpd's duplicate-token threshold. This test walks the AST of every router
module instead and fails the moment a *second* home for that exact shape
reappears outside ``_helpers.py`` — the one place it, and the
``make_hidden_endpoint`` factory that uses it, are meant to live.

Deliberately narrow to this one field signature rather than "any two routers
define the same shape": a general duplicate-shape scan also flags unrelated,
coincidental one-field payloads (e.g. a ``name: str`` rename-group payload
and a camera preset-name payload) that are not instances of this finding's
class and would just be noisy baseline debt.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator, List

ROUTERS_DIR = Path(__file__).resolve().parent.parent / "app" / "webapp" / "routers"

# The exact shape the hide-flag endpoints repeated: one boolean field named
# "hidden", nothing else.
_HIDDEN_FLAG_SIGNATURE = (("hidden", "Name(id='bool', ctx=Load())"),)


def _basemodel_classes_matching(path: Path, signature: tuple) -> Iterator[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        base_names = {b.id for b in node.bases if isinstance(b, ast.Name)}
        if "BaseModel" not in base_names:
            continue
        fields = tuple(
            (stmt.target.id, ast.dump(stmt.annotation))
            for stmt in node.body
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
        )
        if fields == signature:
            yield node.name


def test_no_duplicate_hidden_flag_payload_outside_helpers() -> None:
    """Only ``_helpers.HiddenPayload`` may define the ``{hidden: bool}`` shape.

    A router that hand-rolls its own ``class XHiddenPayload(BaseModel):
    hidden: bool`` instead of registering through ``make_hidden_endpoint`` is
    the exact regression this issue's hide-flag finding was.
    """
    offenders: List[str] = []
    for path in sorted(ROUTERS_DIR.glob("*.py")):
        if path.name == "_helpers.py":
            continue
        offenders.extend(
            f"{path.name}:{name}"
            for name in _basemodel_classes_matching(path, _HIDDEN_FLAG_SIGNATURE)
        )

    assert not offenders, (
        "hand-rolled {hidden: bool} payload model(s) found outside "
        "app/webapp/routers/_helpers.py — register the endpoint through "
        f"make_hidden_endpoint instead: {offenders}"
    )
