"""One-time interactive OAuth bootstrap for Google Calendar write access (issue #313).

Why this exists
----------------
Voice-created calendar events (``POST /api/calendar/voice``) write directly to
the Google Calendar API, scoped to ``calendar.events`` (insert/update/delete
only — no calendar-list/ACL read). Getting that access requires one
interactive browser consent; this script is that one step. Everything else
in the feature is buildable and testable without it.

Usage
-----
    1. Create (or reuse an existing) Google Cloud OAuth client of type
       "Desktop app" at https://console.cloud.google.com/apis/credentials
       and download its JSON.
    2. Set GOOGLE_CALENDAR_CREDENTIALS_PATH in .env to that file's path
       (defaults to config/calendar_credentials.json).
    3. python -m scripts.auth_calendar_write

Opens a system browser for a one-time Google consent, then persists the
resulting refresh token to GOOGLE_CALENDAR_TOKEN_PATH (defaults to
config/calendar_write_token.json, gitignored). Re-run any time to re-consent
(e.g. after revoking access at https://myaccount.google.com/permissions).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402  — sys.path tweak above

load_dotenv(PROJECT_ROOT / ".env")

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402

from src._atomic_json import write_json_atomic  # noqa: E402
from src.calendar_write import SCOPE, credentials_path, write_token_path  # noqa: E402


def main() -> int:
    creds_path = credentials_path()
    if not creds_path.exists():
        print(f"❌ No OAuth client credentials at {creds_path}")
        print("   Download a Desktop-app OAuth client JSON from")
        print("   https://console.cloud.google.com/apis/credentials and set")
        print("   GOOGLE_CALENDAR_CREDENTIALS_PATH in .env to its path.")
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), scopes=[SCOPE])
    # access_type="offline" + prompt="consent" forces a refresh token even on
    # a re-consent (Google otherwise omits it after the first-ever grant).
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    if not creds.refresh_token:
        print("❌ Google did not return a refresh token.")
        print("   Revoke this app's prior access at")
        print("   https://myaccount.google.com/permissions and re-run.")
        return 1

    token_path = write_token_path()
    write_json_atomic(token_path, json.loads(creds.to_json()))
    print(f"✅ Wrote calendar write token to {token_path}")
    print("   Restart the webapp (tray or webapp.bat) so it picks up the value.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
