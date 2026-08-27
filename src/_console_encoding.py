"""Single home for the Windows native-console-tool decoding pin.

Windows console tools (``ping``, ``netsh``, ``powershell.exe`` + ``ConvertTo-Json``,
…) write the **OEM** code page, not UTF-8, and this app's webapp process runs
under ``PYTHONUTF8=1`` / ``PYTHONIOENCODING=utf-8`` (``app/webapp/manager.py``)
— so a ``subprocess.run(..., text=True)`` capture that inherits the ambient
locale decodes with the wrong codec. That does not raise: it hands back empty
or replacement-filled text, which every caller here reads as "the query
failed" rather than as a decoding bug. The fleet rule ("Windows Python: UTF-8
stdout under capture", ``~/.claude/CLAUDE.md``) therefore requires every such
call site to pin its own decoding rather than inherit ``text=True``'s ambient
locale — never ``text=True`` alone; always ``encoding=console_encoding(),
errors="replace"``.

POSIX has no ``oem`` codec and its tools speak UTF-8, so the branch is read
per call; ``errors="replace"`` at each call site keeps one odd byte costing a
character rather than the whole read.

Usage::

    from src._console_encoding import console_encoding

    subprocess.run(cmd, text=True, encoding=console_encoding(), errors="replace", ...)

A function, not a module-level constant, deliberately -- tests monkeypatch
``sys.platform`` per-case (``tests/test_network_client.py``) to exercise both
the Windows and POSIX branches, which only works if the platform is read at
call time.

Extracted from ``src/network_host.py``'s ``_console_encoding()`` (issue #701)
after ``src/hyperv_client.py`` and ``src/ups_client.py`` were found shelling
out to ``powershell.exe`` with the same shape but no decoding pin — same "one
home so it can't drift" role as ``src/_no_window.py`` and ``src/_atomic_json.py``.
"""

from __future__ import annotations

import sys


def console_encoding() -> str:
    """Decoding to pin when capturing a native OS console tool's stdout."""
    return "oem" if sys.platform.startswith("win") else "utf-8"


__all__ = ["console_encoding"]
