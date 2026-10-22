"""Import-time bootstrap for operator scripts that are run directly.

Importing it makes the console print emoji safely and puts the repo root on
``sys.path`` so ``src.*`` resolves, which a script launched from ``scripts/``
does not get by default.
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
