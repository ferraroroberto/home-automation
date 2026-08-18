"""Unit tests for the multi-account Find My refresher (issue #478).

No Apple network calls: ``load_presence_configs`` and ``fetch_presence`` are
monkeypatched, so these exercise the merge + per-account degradation logic only.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.webapp import presence_refresher as R
from src.presence_client import (
    PresenceAuthError,
    PresenceConfig,
    PresenceConfigError,
    PresenceEntity,
)


def _entity(entity_id: str) -> PresenceEntity:
    return PresenceEntity(
        entity_id=entity_id,
        name=entity_id,
        model="iPhone",
        device_class="iPhone",
        latitude=0.0,
        longitude=0.0,
        horizontal_accuracy_m=5.0,
        last_seen=None,
        battery_level_pct=50,
        battery_status="NotCharging",
        distance_from_home_m=0.0,
        at_home=True,
    )


def _config(label: str, *, friendly_name: str = "") -> PresenceConfig:
    return PresenceConfig(
        email=f"{label}@example.com",
        password="secret",
        home_radius_m=200.0,
        label=label,
        friendly_name=friendly_name,
    )


class _FakeNotifier:
    """Collects sent messages instead of hitting the network (issue #655)."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_text(self, text: str) -> None:
        self.sent.append(text)


@pytest.fixture(autouse=True)
def _reset_presence_refresher_state() -> None:
    """Each test starts from a clean cache + retry-backoff tracker.

    Both are module-level globals shared across the whole test process; without
    this, one test's leftover ``_CACHE.accounts`` would silently become the next
    test's ``prev_status`` input for the backoff-retry/notify logic (#655).
    """

    R._CACHE = R.PresenceDiagnosticsCache(entities=[])
    R._LAST_RETRY_ATTEMPT.clear()


def _run_refresh(
    monkeypatch: pytest.MonkeyPatch,
    configs: list[PresenceConfig],
    fetch_map: dict[str, object],
) -> R.PresenceDiagnosticsCache:
    """Drive ``refresh_once`` with fake configs and a per-account fetch behavior.

    ``fetch_map`` maps an account label to either a list of entities to return or
    an exception instance to raise.
    """

    monkeypatch.setattr(R, "load_presence_configs", lambda: configs)

    def fake_fetch(*, config: PresenceConfig) -> list[PresenceEntity]:
        outcome = fetch_map[config.label]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(R, "fetch_presence", fake_fetch)
    return asyncio.run(R.refresh_once())


def test_refresh_merges_two_healthy_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _run_refresh(
        monkeypatch,
        [_config("1"), _config("2")],
        {"1": [_entity("mine")], "2": [_entity("anna")]},
    )

    assert cache.available is True
    assert cache.reason == "ok"
    assert {e.entity_id for e in cache.entities} == {"mine", "anna"}
    assert cache.home_radius_m == 200.0
    assert [(a.label, a.available, a.entity_count) for a in cache.accounts] == [
        ("1", True, 1),
        ("2", True, 1),
    ]


def test_refresh_partial_failure_keeps_healthy_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #478: one account needing 2FA must not blank the other's entities."""

    cache = _run_refresh(
        monkeypatch,
        [_config("1"), _config("2")],
        {"1": [_entity("mine")], "2": PresenceAuthError("iCloud requires 2FA")},
    )

    # The healthy account still populates the cache.
    assert cache.available is True
    assert [e.entity_id for e in cache.entities] == ["mine"]
    # Top-level reason flags a partial outage without pretending the source is up.
    assert cache.reason == "partial"
    assert "account 2" in cache.detail
    statuses = {a.label: a for a in cache.accounts}
    assert statuses["1"].available is True
    assert statuses["2"].available is False
    assert statuses["2"].reason == "2fa_required"


def test_refresh_all_accounts_failing_reports_worst_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _run_refresh(
        monkeypatch,
        [_config("1"), _config("2")],
        {
            "1": RuntimeError("boom"),
            "2": PresenceAuthError("iCloud requires 2FA"),
        },
    )

    assert cache.available is False
    assert cache.entities == []
    # 2fa_required outranks error so the UI/voice prompts a re-auth.
    assert cache.reason == "2fa_required"


def test_refresh_single_account_preserves_legacy_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one-account setup keeps the pre-#478 reason/detail verbatim."""

    cache = _run_refresh(
        monkeypatch,
        [_config("1")],
        {"1": PresenceAuthError("iCloud requires 2FA")},
    )

    assert cache.available is False
    assert cache.reason == "2fa_required"
    assert cache.detail == "iCloud requires 2FA"


def test_refresh_not_configured_when_primary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_config_error() -> list[PresenceConfig]:
        raise PresenceConfigError("Missing iCloud credentials.")

    monkeypatch.setattr(R, "load_presence_configs", raise_config_error)
    cache = asyncio.run(R.refresh_once())

    assert cache.available is False
    assert cache.reason == "not_configured"
    assert cache.accounts == []


def test_refresh_fetches_accounts_concurrently_not_sequentially(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for #491: two accounts must share the caller's timeout budget
    instead of splitting it serially. Each fake fetch blocks for ``DELAY_S`` on
    its own thread; if the refresher still looped sequentially, total wall time
    would be roughly ``2 * DELAY_S`` instead of roughly ``DELAY_S``."""

    configs = [_config("1"), _config("2")]
    monkeypatch.setattr(R, "load_presence_configs", lambda: configs)

    DELAY_S = 0.3

    def fake_fetch(*, config: PresenceConfig) -> list[PresenceEntity]:
        time.sleep(DELAY_S)
        return [_entity(config.label)]

    monkeypatch.setattr(R, "fetch_presence", fake_fetch)

    started = time.monotonic()
    cache = asyncio.run(R.refresh_once())
    elapsed = time.monotonic() - started

    assert {e.entity_id for e in cache.entities} == {"1", "2"}
    # Sequential fetches would take >= 2 * DELAY_S; concurrent ones stay near 1x.
    assert elapsed < DELAY_S * 1.8, f"expected concurrent fetch, took {elapsed:.2f}s"


# --------------------------------------------------------------------------
# Self-heal retry + Telegram notification (issue #655)
# --------------------------------------------------------------------------


def test_broken_account_retries_and_notifies_reconnect_and_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A previously-broken account due for retry gets a forced fresh session
    build (announced beforehand) and, once the fetch succeeds, a recovery
    notification — both in the same poll."""

    configs = [_config("1", friendly_name="Roberto")]
    monkeypatch.setattr(R, "load_presence_configs", lambda: configs)
    invalidated: list[str] = []
    monkeypatch.setattr(
        R, "invalidate_session", lambda cfg: invalidated.append(cfg.label)
    )
    monkeypatch.setattr(R, "fetch_presence", lambda *, config: [_entity("mine")])
    R._CACHE = R.PresenceDiagnosticsCache(
        entities=[],
        accounts=[R.PresenceAccountStatus("1", False, "2fa_required", "stale")],
    )
    notifier = _FakeNotifier()

    cache = asyncio.run(R.refresh_once(notifier_factory=lambda: notifier))

    assert invalidated == ["1"]
    assert cache.accounts[0].available is True
    assert len(notifier.sent) == 2
    assert "Reconnecting Roberto" in notifier.sent[0]
    assert "Roberto" in notifier.sent[1] and "restored" in notifier.sent[1]
    # Issue #656: NOT cleared on recovery - a flapping account must stay
    # backoff-throttled instead of every recovery resetting the clock to zero.
    assert "1" in R._LAST_RETRY_ATTEMPT


def test_flapping_account_stays_backoff_throttled_after_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #656 regression: a break -> recover -> break-again cycle within
    the backoff window must not force a second handshake/notification pair.

    This is the exact shape of the reported bug: pyicloud's FindMy sub-service
    can leave a freshly-rebuilt session's ``requires_2fa`` stuck true again a
    poll or two after a successful self-heal, with no exception logged. If the
    backoff timestamp were cleared on recovery (as it used to be), this second
    break would look like a brand-new first-time failure and retry/notify
    immediately - reproducing the every-~30-minutes handshake spam.
    """

    configs = [_config("1", friendly_name="Roberto")]
    monkeypatch.setattr(R, "load_presence_configs", lambda: configs)
    invalidated: list[str] = []
    monkeypatch.setattr(
        R, "invalidate_session", lambda cfg: invalidated.append(cfg.label)
    )
    monkeypatch.setattr(R, "fetch_presence", lambda *, config: [_entity("mine")])
    R._CACHE = R.PresenceDiagnosticsCache(
        entities=[],
        accounts=[R.PresenceAccountStatus("1", False, "2fa_required", "stale")],
    )
    notifier = _FakeNotifier()

    # First poll: broken -> self-heal fires, recovers.
    asyncio.run(R.refresh_once(notifier_factory=lambda: notifier))
    assert invalidated == ["1"]
    assert len(notifier.sent) == 2

    # Account flaps broken again immediately (e.g. pyicloud's internal
    # sub-service reauth silently poisoned requires_2fa) - still well within
    # the default 4h backoff window.
    monkeypatch.setattr(
        R, "fetch_presence", lambda *, config: (_ for _ in ()).throw(
            PresenceAuthError("iCloud requires 2FA")
        )
    )
    asyncio.run(R.refresh_once(notifier_factory=lambda: notifier))

    # Third poll: still broken, still within backoff - must stay quiet.
    cache = asyncio.run(R.refresh_once(notifier_factory=lambda: notifier))

    assert invalidated == ["1"]  # no second forced reconnect
    assert len(notifier.sent) == 2  # no additional Telegram traffic
    assert cache.accounts[0].available is False
    assert cache.accounts[0].reason == "2fa_required"


def test_broken_account_does_not_retry_before_backoff_elapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No repeated handshake/notification spam within the backoff window."""

    configs = [_config("1", friendly_name="Roberto")]
    monkeypatch.setattr(R, "load_presence_configs", lambda: configs)
    invalidated: list[str] = []
    monkeypatch.setattr(
        R, "invalidate_session", lambda cfg: invalidated.append(cfg.label)
    )
    monkeypatch.setattr(
        R, "fetch_presence", lambda *, config: (_ for _ in ()).throw(
            PresenceAuthError("iCloud requires 2FA")
        )
    )
    R._CACHE = R.PresenceDiagnosticsCache(
        entities=[],
        accounts=[R.PresenceAccountStatus("1", False, "2fa_required", "stale")],
    )
    R._LAST_RETRY_ATTEMPT["1"] = R.datetime.now(R.timezone.utc)
    notifier = _FakeNotifier()

    cache = asyncio.run(R.refresh_once(notifier_factory=lambda: notifier))

    assert invalidated == []
    assert notifier.sent == []
    assert cache.accounts[0].available is False


def test_first_observed_failure_does_not_retry_or_notify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh process's first-ever poll (no prior status) must not treat a
    brand-new failure as a retry-worthy break — that would notify on every
    tray restart, not only on a genuinely stuck session."""

    configs = [_config("1", friendly_name="Roberto")]
    monkeypatch.setattr(R, "load_presence_configs", lambda: configs)
    invalidated: list[str] = []
    monkeypatch.setattr(
        R, "invalidate_session", lambda cfg: invalidated.append(cfg.label)
    )
    monkeypatch.setattr(
        R, "fetch_presence", lambda *, config: (_ for _ in ()).throw(
            PresenceAuthError("iCloud requires 2FA")
        )
    )
    notifier = _FakeNotifier()

    cache = asyncio.run(R.refresh_once(notifier_factory=lambda: notifier))

    assert invalidated == []
    assert notifier.sent == []
    assert cache.accounts[0].available is False


def test_healthy_account_never_retries_or_notifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = [_config("1", friendly_name="Roberto")]
    monkeypatch.setattr(R, "load_presence_configs", lambda: configs)
    invalidated: list[str] = []
    monkeypatch.setattr(
        R, "invalidate_session", lambda cfg: invalidated.append(cfg.label)
    )
    monkeypatch.setattr(R, "fetch_presence", lambda *, config: [_entity("mine")])
    R._CACHE = R.PresenceDiagnosticsCache(
        entities=[_entity("mine")],
        accounts=[R.PresenceAccountStatus("1", True, "ok", "", entity_count=1)],
    )
    notifier = _FakeNotifier()

    asyncio.run(R.refresh_once(notifier_factory=lambda: notifier))

    assert invalidated == []
    assert notifier.sent == []
