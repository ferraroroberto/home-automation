"""Tesla CLI: the one-off owner setup steps and a read-only probe.

    python -m src.list_tesla auth-url          # print the login URL to open once
    python -m src.list_tesla auth-code <code>  # store the tokens from the redirect
    python -m src.list_tesla register          # register TESLA_DOMAIN with Tesla (once)
    python -m src.list_tesla probe             # read the car once (never wakes it)
    python -m src.list_tesla stored            # what the local store holds (no Tesla call)

``probe`` prints the connectivity state and, when the car is awake, the sample
the sampler would store: metric units, no VIN, no coordinates. Setup order and
what each step needs: ``docs/tesla.md``. Tokens and secrets are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import sys

import aiohttp

from src.devices import tesla_history
from src.devices.tesla_auth import (
    TeslaAuthError,
    authorize_url,
    exchange_code,
    load_app_config,
    partner_token,
)
from src.devices.tesla_client import REQUESTS, read_vehicle

logger = logging.getLogger("list_tesla")


async def _register() -> None:
    """Register the app's public-key domain for this region (Tesla requires it once)."""
    config = load_app_config()
    domain = (os.getenv("TESLA_DOMAIN") or "").strip()
    if not domain:
        raise SystemExit("TESLA_DOMAIN must be set in .env")
    token = await partner_token(config)
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            f"{config.fleet_base_url}/api/1/partner_accounts",
            headers={"Authorization": f"Bearer {token}"},
            json={"domain": domain},
        ) as resp:
            if resp.status != 200:
                raise SystemExit(f"partner registration returned HTTP {resp.status}")
    logger.info("✅ Domain registered with Tesla for this region")


async def _probe() -> None:
    reading = await read_vehicle()
    logger.info("state: %s", reading.state)
    if reading.error:
        logger.info("error: %s", reading.error)
    if reading.sample is not None:
        logger.info("%s", json.dumps(reading.sample.to_dict(), indent=2))
    logger.info("requests made: %d", REQUESTS.count)


def _stored() -> None:
    """The latest stored sample, recent sessions and monthly totals, from the store only."""
    tesla_history.init_db()
    logger.info("latest: %s", json.dumps(tesla_history.latest(), indent=2))
    logger.info("recent: %s", json.dumps(tesla_history.recent_sessions(5), indent=2))
    logger.info("monthly: %s", json.dumps(tesla_history.history("month"), indent=2))


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="python -m src.list_tesla")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("auth-url")
    code = sub.add_parser("auth-code")
    code.add_argument("code")
    sub.add_parser("register")
    sub.add_parser("probe")
    sub.add_parser("stored")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "auth-url":
            logger.info("%s", authorize_url(load_app_config(), secrets.token_urlsafe(16)))
        elif args.cmd == "auth-code":
            asyncio.run(exchange_code(load_app_config(), args.code))
        elif args.cmd == "register":
            asyncio.run(_register())
        elif args.cmd == "stored":
            _stored()
        else:
            asyncio.run(_probe())
    except TeslaAuthError as exc:
        raise SystemExit(f"❌ {exc}") from None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main(sys.argv[1:])
