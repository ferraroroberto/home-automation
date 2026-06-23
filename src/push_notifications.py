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

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
PUSH_CONFIG_PATH = _CONFIG_DIR / "push_config.json"
SUBSCRIPTIONS_PATH = _CONFIG_DIR / "push_subscriptions.json"


def _read_json(path: Path) -> Any:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("⚠️ Could not read %s (%s); returning empty", path, exc)
        return {}


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load_push_config(path: Optional[Path] = None) -> Dict[str, str]:
    """Return Web Push VAPID config from env/config, or empty strings."""

    raw = _read_json(Path(path) if path is not None else PUSH_CONFIG_PATH)
    if not isinstance(raw, dict):
        raw = {}
    return {
        "public_key": (os.getenv("WEB_PUSH_PUBLIC_KEY") or raw.get("public_key") or "").strip(),
        "private_key": (os.getenv("WEB_PUSH_PRIVATE_KEY") or raw.get("private_key") or "").strip(),
        "subject": (os.getenv("WEB_PUSH_SUBJECT") or raw.get("subject") or "mailto:admin@example.com").strip(),
    }


def load_subscriptions(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Return stored browser push subscriptions."""

    raw = _read_json(Path(path) if path is not None else SUBSCRIPTIONS_PATH)
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
    _write_json(target, {"subscriptions": subs})
    return len(subs)


def send_push(title: str, body: str, *, url: str = "/") -> int:
    """Send a Web Push notification to all subscriptions; never raises."""

    cfg = load_push_config()
    if not cfg["public_key"] or not cfg["private_key"]:
        logger.info("ℹ️ Web Push not configured; skipping transition notification")
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
