"""Shared SQLite connection helper (issue #452).

Single canonical implementation of the WAL-mode connect context manager that
used to be copy-pasted byte-for-byte across ``src/energy_history.py``,
``src/network_history.py``, and ``src/telemetry.py`` — same pragmas
(``journal_mode=WAL``, ``busy_timeout=5000``), same ``mkdir`` + ``Row``
factory, differing only in a docstring. Centralizes only the connection
mechanics: callers keep their own ``DEFAULT_DB_PATH``, schema, and queries.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional


@contextmanager
def connect(default_path: Path, path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """Open a WAL-mode SQLite connection (concurrent writer + reader safe).

    ``default_path`` is the caller's own ``DEFAULT_DB_PATH``, used when
    ``path`` is not given (e.g. tests pointing at a tmp-path store).
    """
    target = Path(path) if path is not None else default_path
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        yield conn
    finally:
        conn.close()
