"""Shared SQLite connection helper.

Single canonical implementation of the WAL-mode connect context manager that
``src/energy/energy_history.py``, ``src/network/network_history.py`` and
``src/telemetry.py`` share — same pragmas
(``journal_mode=WAL``, ``busy_timeout=5000``), same ``mkdir`` + ``Row``
factory. Centralizes only the connection
mechanics: callers keep their own ``DEFAULT_DB_PATH``, schema, and queries.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from src.runtime_files import make_dirs

# How long a writer waits on a locked database before raising. Stated guess:
# long enough to ride out a concurrent sampler write, short enough that a
# wedged lock surfaces as an error instead of stalling a request thread.
_BUSY_TIMEOUT_MS = 5000


@contextmanager
def connect(default_path: Path, path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """Open a WAL-mode SQLite connection (concurrent writer + reader safe).

    ``default_path`` is the caller's own ``DEFAULT_DB_PATH``, used when
    ``path`` is not given (e.g. tests pointing at a tmp-path store).
    """
    target = Path(path) if path is not None else default_path
    make_dirs(target.parent)
    # runtime-write-exempt: connect must stay in this helper (must_use); make_dirs above refused a code-folder path
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        yield conn
    finally:
        conn.close()
