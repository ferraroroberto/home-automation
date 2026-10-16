"""Single home for locating the ``tailscale`` CLI.

Two call sites need it: ``app/tray/tray.py`` (Copy Tailscale URL) and
``scripts/gen_tailscale_cert.py`` (HTTPS cert provisioning/renewal, including
the ``--check`` hook both boot paths run before uvicorn binds). The GUI
installer drops ``tailscale.exe`` under Program Files but doesn't always add
it to PATH, and both callers can run with a minimal environment — the tray at
login, the webapp boot path under a service-like launch — so PATH alone isn't
enough (#914: before this module the tray had the Program Files fallback and
the cert script didn't, so a renewal could silently fail where "Copy
Tailscale URL" still worked).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional


def tailscale_binary() -> Optional[str]:
    """Return the path to the tailscale CLI, or ``None`` if not found."""
    found = shutil.which("tailscale")
    if found:
        return found
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Tailscale" / "tailscale.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Tailscale" / "tailscale.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


__all__ = ["tailscale_binary"]
