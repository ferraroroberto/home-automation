r"""
List iCloud Find My presence entities (CLI)
===========================================
Proof-of-concept: confirm whether iCloud Find My returns live enough location
data to drive a future HVAC home/away automation.

Run from the project root with the venv interpreter::

    & .\.venv\Scripts\python.exe -m src.list_presence          # Windows
    ./.venv/bin/python -m src.list_presence                    # POSIX

If Apple asks for 2FA, rerun with ``--2fa-code <code>`` from a trusted Apple
device. Reads ICLOUD_EMAIL / ICLOUD_PASSWORD from ``.env`` and caches the
trusted session under ``webapp/icloud_session`` by default.

A second account (``ICLOUD_EMAIL_2`` / ``ICLOUD_PASSWORD_2``) is listed too when
configured (issue #478). 2FA is per Apple ID, so target one account at a time to
trust/re-auth it: ``--account 2 --2fa-code <code>``.
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime
from typing import Optional

from src.presence_client import (
    PresenceAuthError,
    PresenceConfigError,
    PresenceEntity,
    fetch_presence,
    load_presence_configs,
)


def _fmt_coord(lat: Optional[float], lon: Optional[float]) -> str:
    if lat is None or lon is None:
        return "n/a"
    return f"{lat:.6f}, {lon:.6f}"


def _fmt_distance(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    if value >= 1000:
        return f"{value / 1000:.1f} km"
    return f"{value:.0f} m"


def _fmt_time(value: Optional[datetime]) -> str:
    if value is None:
        return "n/a"
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _fmt_battery(entity: PresenceEntity) -> str:
    if entity.battery_level_pct is None:
        return "n/a"
    suffix = f" ({entity.battery_status})" if entity.battery_status else ""
    return f"{entity.battery_level_pct}%{suffix}"


def _fmt_home(entity: PresenceEntity) -> str:
    if entity.at_home is None:
        return "unknown"
    return "home" if entity.at_home else "away"


def _short_id(value: str) -> str:
    if not value:
        return "n/a"
    return value[:10] + "…" if len(value) > 12 else value


def _print_entity(entity: PresenceEntity) -> None:
    print(f"  Name:                {entity.name}")
    print(f"  ID:                  {_short_id(entity.entity_id)}")
    print(f"  Model:               {entity.model or 'n/a'}")
    print(f"  Class:               {entity.device_class or 'n/a'}")
    print(f"  Presence:            {_fmt_home(entity)}")
    print(f"  Location:            {_fmt_coord(entity.latitude, entity.longitude)}")
    print(f"  Accuracy:            {_fmt_distance(entity.horizontal_accuracy_m)}")
    print(f"  Distance from home:  {_fmt_distance(entity.distance_from_home_m)}")
    print(f"  Last seen:           {_fmt_time(entity.last_seen)}")
    print(f"  Battery:             {_fmt_battery(entity)}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="List iCloud Find My locations.")
    parser.add_argument(
        "--2fa-code",
        dest="verification_code",
        help="Apple 2FA code from a trusted device, only needed when prompted.",
    )
    parser.add_argument(
        "--no-trust",
        action="store_true",
        help="Validate this 2FA code without requesting a trusted session.",
    )
    parser.add_argument(
        "--account",
        type=int,
        help=(
            "Query/trust only this configured iCloud account (1-based). "
            "Required alongside --2fa-code, since 2FA is per Apple ID. "
            "Omit to list every configured account."
        ),
    )
    return parser.parse_args()


def _print_entities(entities: list[PresenceEntity]) -> None:
    if not entities:
        print("No Find My entities found on this iCloud account.")
        return

    located = sum(1 for entity in entities if entity.has_location)
    print(f"\nFound {len(entities)} Find My entit(y/ies), {located} with location:\n")
    for index, entity in enumerate(entities, start=1):
        print(f"Entity {index}:")
        _print_entity(entity)
        print()


def main() -> None:
    """Fetch every visible Find My entity per configured account and print it."""

    args = _parse_args()
    configs = load_presence_configs()

    if args.account is not None:
        if not 1 <= args.account <= len(configs):
            raise SystemExit(
                f"❌ --account {args.account} out of range; "
                f"{len(configs)} account(s) configured."
            )
        selected = [(args.account, configs[args.account - 1])]
    else:
        if args.verification_code:
            raise SystemExit(
                "❌ Specify --account N with --2fa-code; 2FA is per Apple ID."
            )
        selected = list(enumerate(configs, start=1))

    for number, config in selected:
        # The verification code only applies to an explicitly-targeted account.
        code = args.verification_code if args.account is not None else None
        if len(configs) > 1:
            print(f"=== Account {number} ===")
        try:
            entities = fetch_presence(
                config=config,
                verification_code=code,
                trust_session=not args.no_trust,
            )
        except PresenceAuthError as exc:
            # Degrade this account only, so a healthy account still prints (#478).
            print(f"⚠️ {exc}")
            continue
        _print_entities(entities)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        main()
    except (PresenceConfigError, PresenceAuthError) as exc:
        raise SystemExit(f"❌ {exc}")
