r"""
List iCloud Find My presence entities (CLI)
===========================================
Proof-of-concept: confirm whether iCloud Find My returns live enough location
data to drive a future HVAC home/away automation.

Run from the project root with the venv interpreter::

    & .\.venv\Scripts\python.exe -m src.list_presence          # Windows
    ./.venv/bin/python -m src.list_presence                    # POSIX

Reads ICLOUD_EMAIL / ICLOUD_PASSWORD from ``.env`` and caches the trusted
session under ``webapp/icloud_session`` by default.

A second account (``ICLOUD_EMAIL_2`` / ``ICLOUD_PASSWORD_2``) is listed too when
configured (issue #478). 2FA is per Apple ID, so target one account at a time.

Renewing Apple's ~30-day browser trust (issue #659) — the attended CLI fallback
to the app's Presence card → **Renew trust** button::

    & .\.venv\Scripts\python.exe -m src.list_presence --account 1 --renew-trust

One run: Apple pushes a 6-digit code to that account's trusted devices, you
type it at the prompt, the session is re-trusted. ``--2fa-code <code>`` remains
for the narrower case where the session token itself is invalid at build time
and Apple asks for 2FA during the fetch — the two-run "get the push, rerun with
the code" dance cannot renew trust (the second run's SRP opens a new Apple auth
session the earlier code is not valid for), so prefer ``--renew-trust``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from typing import Optional

from src.presence_client import (
    MAX_CODE_ATTEMPTS,
    PresenceAuthError,
    PresenceConfig,
    PresenceConfigError,
    PresenceEntity,
    begin_trust_renewal,
    complete_trust_renewal,
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
            "Required alongside --2fa-code / --renew-trust, since 2FA is per "
            "Apple ID. Omit to list every configured account."
        ),
    )
    parser.add_argument(
        "--renew-trust",
        action="store_true",
        help=(
            "Attended: renew this account's iCloud browser trust in one run — "
            "Apple pushes a 6-digit code to its trusted devices, you type it "
            "here. Requires --account N; nothing is listed."
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


def _renew_trust(config: PresenceConfig) -> None:
    """Attended one-run browser-trust renewal for one account (issue #659).

    The only place in the presence stack that reads from stdin — the library
    never prompts. Refuses to run without a terminal, since the code has to be
    typed here.
    """

    if not sys.stdin.isatty():
        raise SystemExit(
            "❌ --renew-trust is attended: run it from a terminal (a 6-digit code "
            "must be typed), or use the app's Presence card → Renew trust."
        )
    who = config.display_name
    print(f"Requesting a 2FA code for {who} (account {config.label})…")
    begun = begin_trust_renewal(config)
    if begun.status == "already_trusted":
        print(f"✅ {begun.detail}")
        return
    if begun.status != "code_sent":
        raise SystemExit(f"❌ Trust renewal could not start: {begun.detail}")
    print(f"📲 {begun.detail}")

    for _attempt in range(MAX_CODE_ATTEMPTS):
        code = input("Enter the 6-digit code shown on your trusted device: ").strip()
        done = complete_trust_renewal(config, code)
        if done.status == "trusted":
            print(f"✅ {done.detail}")
            return
        if done.status != "invalid_code":
            raise SystemExit(f"❌ Trust renewal failed ({done.status}): {done.detail}")
        print(f"⚠️ {done.detail}")
    raise SystemExit("❌ Trust renewal failed: too many rejected codes — run again for a new push.")


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
        if args.verification_code or args.renew_trust:
            raise SystemExit(
                "❌ Specify --account N with --2fa-code / --renew-trust; "
                "2FA is per Apple ID."
            )
        selected = list(enumerate(configs, start=1))

    if args.renew_trust:
        _renew_trust(selected[0][1])
        return

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
