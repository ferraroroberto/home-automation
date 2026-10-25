"""Throwaway webapp for the design-review walk's synthetic run (fleet-config#1383).

``/design-review home-automation --synthetic`` (and ``measure --e2e``) runs this
(``[design.review.synthetic]`` in ``.fleet.toml``) with the repo's venv python,
from the repo root. It boots a disposable webapp on a free loopback port with
the e2e autoboot's environment (``tests/e2e/conftest.py::_autoboot_env``): every
automation engine off and every runtime database in a temp directory. It prints
``URL=<base>`` once ``/healthz`` answers, then waits until stdin closes and
tears the webapp down.

Unlike the e2e suite, the walk stubs no route, so the instance itself must not
reach the household's accounts or LAN:

* every key in ``.env`` is blanked in the child's environment and
  ``dotenv.load_dotenv`` is turned into a no-op (``athom_client`` calls it with
  ``override=True`` on each read, which would otherwise restore them);
* ``src.energy.athom_client.discover_meters`` reports "found nothing", so
  ``/api/circuits`` never sweeps mDNS or reads a meter.

What it cannot isolate is ``config/*.json`` (device lists such as
``cameras.json`` are read from the checkout, with no override): run it from a
checkout whose ``config/`` holds only the ``*.sample.json`` twins.

Never points at the live tray on :8447. The engine-off environment is imported,
not copied, so a new engine switch added to the autoboot is off here too.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

logger = logging.getLogger("design_review_synthetic")
STARTUP_TIMEOUT_S = 60.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _serve(port: int, ssl_files: Optional[Tuple[str, str]]) -> int:
    """Child process: neutralise the outbound integrations, then run uvicorn."""
    import dotenv
    import uvicorn

    dotenv.load_dotenv = lambda *args, **kwargs: False

    from app.webapp.event_loop import LOOP_FACTORY
    from src.energy import athom_client

    async def _no_discovery(force: bool = False) -> Tuple[List, Optional[str]]:
        return [], None

    athom_client.discover_meters = _no_discovery
    kwargs: Dict[str, object] = {}
    if ssl_files:
        kwargs["ssl_certfile"], kwargs["ssl_keyfile"] = ssl_files
    uvicorn.run(
        "app.webapp.server:app", host="127.0.0.1", port=port,
        log_level="warning", loop=LOOP_FACTORY, **kwargs,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--cert", help=argparse.SUPPRESS)
    parser.add_argument("--key", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        return _serve(args.serve, (args.cert, args.key) if args.cert and args.key else None)

    from dotenv import dotenv_values

    from tests.e2e.conftest import _CERT, _KEY, _autoboot_env, _healthz_ok

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    port = _free_port()
    https = _CERT.exists() and _KEY.exists()
    base = f"{'https' if https else 'http'}://127.0.0.1:{port}"
    data_dir = Path(tempfile.mkdtemp(prefix="ha_synthetic_data_"))
    cmd = [sys.executable, str(Path(__file__).resolve()), "--serve", str(port)]
    if https:
        cmd += ["--cert", str(_CERT), "--key", str(_KEY)]
    env = _autoboot_env(data_dir)
    env.update({key: "" for key in dotenv_values(ROOT / ".env")})
    log_fp = (data_dir / "webapp.log").open("w", encoding="utf-8", errors="replace")
    kwargs: dict = dict(cwd=str(ROOT), stdout=log_fp, stderr=subprocess.STDOUT, env=env)
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(cmd, **kwargs)
    try:
        deadline = time.time() + STARTUP_TIMEOUT_S
        while not _healthz_ok(base):
            if proc.poll() is not None:
                logger.error("❌ synthetic webapp exited early; log: %s", data_dir / "webapp.log")
                return 1
            if time.time() > deadline:
                logger.error("❌ synthetic webapp not reachable after %ss", STARTUP_TIMEOUT_S)
                return 1
            time.sleep(0.2)
        logger.info("✅ synthetic webapp up on %s (data %s)", base, data_dir)
        print(f"URL={base}", flush=True)
        # The walk closes our stdin when it is done; reading to EOF blocks until then.
        sys.stdin.read()
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_fp.close()
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
