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

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Callable, Optional

from dotenv import load_dotenv

from src.notify import Notifier, NotifierError, TelegramConfig, build_notifier

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

    # Hard safety net: never build a REAL notifier while running under pytest.
    # record_alarm_action / record_power_event default their notifier_factory to
    # this function, and a default argument binds the function object at def time,
    # so monkeypatching the module attribute can't neutralise the default path. A
    # test that forgets to inject a fake notifier would otherwise fire a real
    # Telegram alert to the family chat — this has bitten us twice already.
    # Guarding the single choke point makes every present and
    # future test send-proof, regardless of which seam it does or doesn't mock.
    if "pytest" in sys.modules:
        return None

    cfg = load_notify_config(path)
    name = "telegram" if (cfg.bot_token and cfg.chat_id) else "none"
    return build_notifier(name, cfg)


async def notify_best_effort(
    text: str,
    *,
    label: str,
    notifier_factory: Callable[[], Optional[Notifier]] = build_alarm_notifier,
) -> None:
    """Best-effort Telegram send, next to the factory it calls.

    Collapses the identical "build the notifier, no-op if unconfigured,
    thread the blocking send off the event loop, swallow ``NotifierError``
    with a warning" shape that used to be a private ``_notify`` helper
    duplicated in the wake-alarm, blind-schedule and presence-automation
    engines. ``label`` names the caller in the log lines (e.g.
    ``"Wake-alarm"``, ``"Blind failure"``) — never raises, so a delivery
    failure can never break the automation loop that called it.

    ``presence_refresher._notify`` stays its own function: it runs inside a
    worker thread already handed off by its caller's own ``asyncio.to_thread``
    (not the event loop), so it sends directly rather than hopping threads
    again, which this async helper cannot do from there.
    """
    notifier = notifier_factory()
    if notifier is None:
        logger.info("ℹ️ %s not sent to Telegram: notifier not configured", label)
        return
    try:
        # notifier.send_text is blocking network I/O; keep it off the event loop.
        await asyncio.to_thread(notifier.send_text, text)
    except NotifierError as exc:  # delivery must never break the caller's loop
        logger.warning("⚠️ %s Telegram notify failed: %s", label, exc)
