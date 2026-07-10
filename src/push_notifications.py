"""Best-effort mobile notifications for presence transitions.

Primary provider: browser Web Push subscriptions stored locally. If no VAPID
keys or subscriptions are configured, sending is a silent no-op.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from src._schedule_store import read_json, save_json

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
PUSH_CONFIG_PATH = _CONFIG_DIR / "push_config.json"
SUBSCRIPTIONS_PATH = _CONFIG_DIR / "push_subscriptions.json"

# Cache of the last-validated private key value, so a bad key logs its clear
# "pushes disabled" warning once (not once per subscription per send) while
# still re-validating automatically if the config value ever changes without
# a process restart.
_validated_private_key: Optional[str] = None
_validated_private_key_ok: bool = False


def load_push_config(path: Optional[Path] = None) -> Dict[str, str]:
    """Return Web Push VAPID config from env/config, or empty strings."""

    raw = read_json(Path(path) if path is not None else PUSH_CONFIG_PATH, {})
    if not isinstance(raw, dict):
        raw = {}
    return {
        "public_key": (os.getenv("WEB_PUSH_PUBLIC_KEY") or raw.get("public_key") or "").strip(),
        "private_key": (os.getenv("WEB_PUSH_PRIVATE_KEY") or raw.get("private_key") or "").strip(),
        "subject": (os.getenv("WEB_PUSH_SUBJECT") or raw.get("subject") or "mailto:admin@example.com").strip(),
    }


def load_subscriptions(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Return stored browser push subscriptions."""

    raw = read_json(Path(path) if path is not None else SUBSCRIPTIONS_PATH, {})
    subs = raw.get("subscriptions") if isinstance(raw, dict) else []
    if not isinstance(subs, list):
        return []
    return [s for s in subs if isinstance(s, dict)]


def save_subscription(subscription: Dict[str, Any], path: Optional[Path] = None) -> int:
    """Upsert one browser PushSubscription by endpoint and return total count."""

    endpoint = str(subscription.get("endpoint") or "")
    if not endpoint:
        raise ValueError("subscription endpoint is required")
    target = Path(path) if path is not None else SUBSCRIPTIONS_PATH
    subs = load_subscriptions(target)
    subs = [s for s in subs if s.get("endpoint") != endpoint]
    subs.append(subscription)
    save_json(target, {"subscriptions": subs})
    return len(subs)


def validate_push_config(cfg: Optional[Dict[str, str]] = None) -> bool:
    """Check the configured VAPID private key can actually be loaded.

    Loads the key once (via ``py_vapid``, the same parser ``pywebpush`` uses
    internally) and caches the result keyed by the private-key value, so a
    bad key logs its "pushes disabled" warning exactly once instead of once
    per subscription on every send. Returns ``True`` when push isn't
    configured at all (silent no-op — not an error) or the key loads
    cleanly; ``False`` only when a private key is present but unreadable.
    Safe to call at startup and/or lazily on first send.
    """

    global _validated_private_key, _validated_private_key_ok

    if cfg is None:
        cfg = load_push_config()
    private_key = cfg["private_key"]
    if not cfg["public_key"] or not private_key:
        return True
    if private_key == _validated_private_key:
        return _validated_private_key_ok

    try:
        from py_vapid import Vapid
    except ImportError:
        logger.warning("⚠️ py_vapid is not installed; cannot validate Web Push key")
        _validated_private_key = private_key
        _validated_private_key_ok = False
        return False

    try:
        Vapid.from_string(private_key=private_key)
        _validated_private_key_ok = True
    except Exception as exc:  # noqa: BLE001 — any parse failure disables push
        logger.warning("⚠️ Web Push private key unreadable — pushes disabled (%s)", exc)
        _validated_private_key_ok = False
    _validated_private_key = private_key
    return _validated_private_key_ok


def send_push(title: str, body: str, *, url: str = "/") -> int:
    """Send a Web Push notification to all subscriptions; never raises."""

    cfg = load_push_config()
    if not cfg["public_key"] or not cfg["private_key"]:
        logger.info("ℹ️ Web Push not configured; skipping transition notification")
        return 0
    if not validate_push_config(cfg):
        return 0
    subs = load_subscriptions()
    if not subs:
        logger.info("ℹ️ No Web Push subscriptions; skipping transition notification")
        return 0
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        logger.warning("⚠️ pywebpush is not installed; cannot send Web Push")
        return 0

    payload = json.dumps({"title": title, "body": body, "url": url}, ensure_ascii=False)
    sent = 0
    for sub in subs:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=cfg["private_key"],
                vapid_claims={"sub": cfg["subject"]},
            )
            sent += 1
        except WebPushException as exc:
            logger.warning("⚠️ Web Push send failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️ Web Push send failed: %s", exc)
    return sent
