"""Entry point: ``python -m app.tray`` launches the tray.

``tray.bat`` invokes this with ``pythonw.exe -m app.tray`` from the repo
root, so ``app`` and ``src`` resolve without any extra path wiring.
"""

from __future__ import annotations

import logging
import sys

from app.tray.tray import run_tray


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


if __name__ == "__main__":
    _configure_logging()
    sys.exit(run_tray())
