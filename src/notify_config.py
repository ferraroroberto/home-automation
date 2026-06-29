"""Telegram notifier credentials for this app.

Wires the app's own config layer to the vendored, domain-free
``src.notify`` primitive (see ``src/notify/README.md``). Credentials come from
the gitignored ``config/notify_config.json`` and/or the environment
(``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID``, which take precedence). A
missing file is not an error — the same "graceful default" pattern as
``webapp_config.py``; with no credentials the notifier resolves to ``none`` and
every send becomes a silent no-op.

The real ``config/notify_config.json`` is gitignored (the bot token and chat id
are secrets in a public repo); ``config/notify_config.sample.json`` is the
committed template.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from src.notify import Notifier, TelegramConfig, build_notifier

logger = logging.getLogger("notify_config")

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "notify_config.json"


def load_notify_config(path: Optional[Path] = None) -> TelegramConfig:
    """Resolve Telegram credentials from env (preferred) then the config file.

    Returns a :class:`TelegramConfig` whose fields may be empty strings when
    nothing is configured — callers treat empty creds as "notifier disabled".
    """

    load_dotenv(override=True)
    target = Path(path) if path is not None else DEFAULT_PATH

    file_token = ""
    file_chat = ""
    if target.exists():
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                file_token = str(raw.get("bot_token") or "")
                file_chat = str(raw.get("chat_id") or "")
            else:
                logger.warning("⚠️ %s is not a JSON object; ignoring", target)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("⚠️ Could not read %s (%s); using env/defaults", target, exc)

    bot_token = (os.getenv("TELEGRAM_BOT_TOKEN") or file_token).strip()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or file_chat).strip()
    return TelegramConfig(bot_token=bot_token, chat_id=chat_id)


def is_notify_configured(path: Optional[Path] = None) -> bool:
    """True when both a bot token and a chat id are available."""

    cfg = load_notify_config(path)
    return bool(cfg.bot_token and cfg.chat_id)


def build_alarm_notifier(path: Optional[Path] = None) -> Optional[Notifier]:
    """Return a configured Telegram notifier, or ``None`` when unconfigured.

    ``None`` mirrors the vendored factory's ``"none"`` channel: callers record
    the message as skipped and carry on, never raising.
    """

    cfg = load_notify_config(path)
    name = "telegram" if (cfg.bot_token and cfg.chat_id) else "none"
    return build_notifier(name, cfg)
