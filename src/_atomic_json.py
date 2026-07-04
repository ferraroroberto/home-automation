"""Shared atomic write helper (issue #327).

Single canonical implementation of the write-tmp + ``os.replace`` pattern
that used to be copy-pasted across 18 ``src/`` modules — plain dict/list
JSON, dataclass JSON, and a couple of camera modules persisting raw JPEG
bytes all shared the exact same on-disk swap mechanics. Centralizes only
the write mechanics: callers keep their own read/clean/log logic, since
payload shapes (dict/list/dataclass) and log messages differ by call
site.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomically write raw bytes to ``path`` (write-tmp + ``os.replace``).

    Creates parent directories as needed. The tmp file is ``path`` with
    ``.tmp`` appended to its suffix (e.g. ``foo.json`` -> ``foo.json.tmp``,
    ``foo.jpg`` -> ``foo.jpg.tmp``), matching every duplicated call site
    this replaces.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def write_json_atomic(
    path: Path,
    data: Any,
    *,
    indent: Optional[int] = 2,
    ensure_ascii: bool = False,
) -> None:
    """Atomically serialize ``data`` as JSON and write it to ``path``.

    Defaults (``indent=2``, ``ensure_ascii=False``) match the majority of
    call sites; pass overrides to reproduce a call site's exact original
    serialization (e.g. ``webapp_config.py`` used bare
    ``json.dumps(payload, indent=2)`` — implicit ``ensure_ascii=True``;
    ``alarm_scene_cursor.py`` used bare ``json.dumps(...)`` — implicit
    ``indent=None, ensure_ascii=True``).
    """
    text = json.dumps(data, indent=indent, ensure_ascii=ensure_ascii)
    atomic_write_bytes(path, text.encode("utf-8"))
