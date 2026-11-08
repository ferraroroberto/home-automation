"""Guarded writes for paths a caller names.

Runtime output (state, caches, logs, databases) lives in the gitignored
``config/``, ``webapp/`` and ``logs/`` folders at the repo root or beside the
checkout, never inside a folder that holds code. A helper that writes to a path
its caller supplies cannot say statically where that is, so every such write
goes through this module, which resolves the path and raises
:class:`RuntimeWriteRefused` when it falls under one of :data:`CODE_FOLDERS`.
The check runs on every call, so the guarantee holds at runtime instead of
being asserted in a comment.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import IO, Any, Union

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The ``code_folders`` of ``.fleet.toml`` ``[runtime_write_ratchet]``; a test keeps the two equal.
CODE_FOLDERS = ("src", "app", "scripts", "custom_components")

PathLike = Union[str, Path]


class RuntimeWriteRefused(ValueError):
    """The path resolves under a code folder, so it is not a runtime file."""


def ensure_runtime_path(path: PathLike) -> Path:
    """Return ``path`` resolved, or raise when it sits under a code folder."""
    resolved = Path(path).resolve()
    for folder in CODE_FOLDERS:
        if resolved.is_relative_to(REPO_ROOT / folder):
            logger.warning("⚠️ refused a runtime write under %s/: %s", folder, resolved)
            raise RuntimeWriteRefused(
                f"{resolved} is under the code folder {folder}/; runtime files belong in "
                "config/, webapp/, logs/ or outside the checkout"
            )
    return resolved


def make_dirs(path: PathLike) -> Path:
    """Create ``path`` and its parents after the guard; return the resolved path."""
    resolved = ensure_runtime_path(path)
    # runtime-write-exempt: guarded by ensure_runtime_path, which refuses any path under a code folder
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def open_runtime(path: PathLike, mode: str, **kwargs: Any) -> IO[Any]:
    """``open(path, mode, **kwargs)`` after the guard (the caller creates the parent)."""
    resolved = ensure_runtime_path(path)
    # runtime-write-exempt: guarded by ensure_runtime_path, which refuses any path under a code folder
    return open(resolved, mode, **kwargs)


def write_bytes(path: PathLike, data: bytes) -> None:
    """Write ``data`` to ``path`` after the guard."""
    with open_runtime(path, "wb") as fh:
        fh.write(data)


def write_text(path: PathLike, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` after the guard."""
    with open_runtime(path, "w", encoding=encoding) as fh:
        fh.write(text)
