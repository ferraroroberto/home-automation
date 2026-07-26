"""Google Calendar write client (issue #313).

The only file in this repo that imports Google libraries — keeps
``src/calendar_events.py``'s parsing/shaping pure and unit-testable with no
OAuth stack involved. Adapted from (not copied from) the OAuth pattern
whatsapp-radar's #217 shipped for the same problem: an installed-app
authorization-code flow, a narrow write-only scope, and an atomically
persisted token file independent of any other app's token — mirrored here
rather than reinvented, per the fleet's "same problem, don't solve it twice"
convention, but written fresh for this repo's single-module ``src/``
convention and its own atomic-write helper.

Create-only surface: no ``delete_event``/list/update — this issue has no
cancel/edit acceptance criterion (unlike wake alarms/reminders), so there's
nothing to add until a follow-up issue asks for it.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from src._atomic_json import write_json_atomic

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

# Narrowest write scope — insert/update/delete only, no calendar-list/ACL
# read. A separate scope/token from any read-only calendar grant elsewhere
# in the fleet, since Google scopes can't be upgraded in place on a token.
SCOPE = "https://www.googleapis.com/auth/calendar.events"


def credentials_path() -> Path:
    """Path to the downloaded OAuth Desktop-app client JSON.

    Reads ``GOOGLE_CALENDAR_CREDENTIALS_PATH`` lazily (mirroring
    ``src/melcloud_client.py``'s ``load_dotenv()``-then-``os.getenv`` shape)
    rather than as a module-level constant, since nothing loads ``.env``
    into the process before this module is first imported at server startup.
    """

    load_dotenv()
    raw = os.getenv("GOOGLE_CALENDAR_CREDENTIALS_PATH")
    return Path(raw) if raw else _CONFIG_DIR / "calendar_credentials.json"


def write_token_path() -> Path:
    """Path to the persisted write-scope token (gitignored)."""

    load_dotenv()
    raw = os.getenv("GOOGLE_CALENDAR_TOKEN_PATH")
    return Path(raw) if raw else _CONFIG_DIR / "calendar_write_token.json"


def timezone_name() -> str:
    """IANA timezone stamped on created events (household default)."""

    load_dotenv()
    return os.getenv("GOOGLE_CALENDAR_TIMEZONE") or "Europe/Madrid"


class CalendarWriteError(Exception):
    """Raised when the calendar client can't be built or a write fails.

    The router catches this and speaks a fallback instead of a bare 500 —
    the same "always say something" contract as every other voice endpoint.
    """


def build_google_calendar_write_client(token_path: Optional[Path] = None) -> Any:
    """Build an authorized Google Calendar API v3 client, refreshing the
    token if it's expired.

    Requires ``scripts/auth_calendar_write.py`` to have already been run
    once to create the token file — this function never opens a browser.
    Google's authorized-user token format already embeds the client
    id/secret needed to refresh, so no credentials-file path is needed here.
    """

    token_path = Path(token_path) if token_path is not None else write_token_path()
    if not token_path.exists():
        raise CalendarWriteError(
            f"No calendar write token at {token_path}. "
            "Run: python -m scripts.auth_calendar_write"
        )

    try:
        creds = Credentials.from_authorized_user_file(str(token_path), scopes=[SCOPE])
    except (OSError, ValueError) as exc:
        raise CalendarWriteError(f"Could not read calendar write token: {exc}") from exc

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:  # noqa: BLE001 — any google-auth refresh failure
            raise CalendarWriteError(f"Could not refresh calendar write token: {exc}") from exc
        write_json_atomic(token_path, json.loads(creds.to_json()))
        logger.info("💾 Refreshed calendar write token at %s", token_path)

    if not creds.valid:
        raise CalendarWriteError(
            "Calendar write token is invalid or revoked. "
            "Run: python -m scripts.auth_calendar_write"
        )

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def insert_event(
    event_body: Dict[str, Any],
    calendar_id: str = "primary",
    client: Optional[Any] = None,
) -> Dict[str, Any]:
    """Create ``event_body`` on ``calendar_id`` (default: the account's
    primary calendar) and return the created event resource."""

    client = client or build_google_calendar_write_client()
    try:
        return client.events().insert(calendarId=calendar_id, body=event_body).execute()
    except Exception as exc:  # noqa: BLE001 — surfaces any googleapiclient HttpError
        raise CalendarWriteError(f"Failed to create calendar event: {exc}") from exc
