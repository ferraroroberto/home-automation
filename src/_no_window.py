"""Single home for the Windows "don't flash a console window" spawn flag.

Every ``subprocess`` spawn in this repo runs under a parent that may have no
console of its own — the pythonw tray, the uvicorn worker it starts, a Task
Scheduler job — so each unflagged spawn pops a console window at whoever is
sitting at the machine. The fleet rule (``~/.claude/CLAUDE.md``, "Subprocess
spawns must suppress the console window") therefore requires
``creationflags=subprocess.CREATE_NO_WINDOW`` on Windows at *every* call site,
and factoring the ternary into one helper once a repo passes three of them.

This module is that helper. Import it and pass the constant straight through::

    from src._no_window import NO_WINDOW

    subprocess.run(cmd, creationflags=NO_WINDOW, ...)

``NO_WINDOW`` is ``0`` off Windows, which ``subprocess`` accepts everywhere
(only a *non-zero* ``creationflags`` is rejected on POSIX), so call sites need
no platform branch at all. Before issue #572 the same flag was re-derived in
three incompatible spellings across eight modules — a module-level ternary, an
inline ``kwargs[...] = ...`` under a ``sys.platform`` guard, and a
``getattr(subprocess, ...)`` fallback — which is how ``scripts/gen_tailscale_cert.py``
came to be missed entirely. Same "one home so it can't drift" role as
``src/_mac.py`` and ``src/_atomic_json.py``.

Note: a long-lived child that later needs ``CTRL_BREAK_EVENT`` combines this
with its own process-group flag (``subprocess.CREATE_NEW_PROCESS_GROUP |
NO_WINDOW``, see ``app/webapp/manager.py``) — that flag stays at its single
call site because it is Windows-only *and* specific to signalling.
"""

from __future__ import annotations

import subprocess
import sys

# Windows: suppress the console window the child would otherwise get.
# Everywhere else: 0, the only value POSIX ``subprocess`` accepts.
NO_WINDOW: int = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

__all__ = ["NO_WINDOW"]
