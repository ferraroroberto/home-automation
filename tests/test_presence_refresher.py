"""Unit tests for the multi-account Find My refresher (issue #478).

No Apple network calls: ``load_presence_configs`` and ``fetch_presence`` are
monkeypatched, so these exercise the merge + per-account degradation logic only.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

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
    R._ALERTED.clear()


def _broken(
    label: str,
    reason: str = "2fa_required",
    detail: str = "stale",
    *,
    failures: int,
) -> R.PresenceAccountStatus:
    """Seed a prior poll's broken status carrying a failure streak (#678).

    Every self-heal/notify decision is gated on ``consecutive_failures`` now, so
    a test that wants the *next* poll to react has to say how long the account
    has already been failing rather than merely that it was broken once.
    """

    return R.PresenceAccountStatus(
        label, False, reason, detail, consecutive_failures=failures
    )


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
# Self-heal retry + Telegram notification (issues #655, #678)
# --------------------------------------------------------------------------


def test_account_display_name_falls_back_to_email_not_bare_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #658: an unset ICLOUD_LABEL must still identify *which* Apple ID
    an alert is about - "account 1"/"account 2" gave no way to tell the
    accounts apart."""

    configs = [_config("1", friendly_name="")]
    monkeypatch.setattr(R, "load_presence_configs", lambda: configs)
    monkeypatch.setattr(R, "invalidate_session", lambda cfg: None)
    monkeypatch.setattr(
        R, "fetch_presence", lambda *, config: (_ for _ in ()).throw(
            PresenceAuthError("iCloud requires 2FA")
        )
    )
    R._CACHE = R.PresenceDiagnosticsCache(
        entities=[],
        accounts=[_broken("1", failures=R._alert_after_failures() - 1)],
    )
    notifier = _FakeNotifier()

    asyncio.run(R.refresh_once(notifier_factory=lambda: notifier))

    assert "1@example.com" in notifier.sent[0]
    assert "account 1" not in notifier.sent[0]


def test_transient_failure_streak_is_silent_and_self_heals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #678 regression: the reported bug, end to end.

    Apple answers a few consecutive polls with a transient 409 and then serves
    normally again - the exact overnight shape that woke the household with a
    "Reconnecting ... approve the sign-in" / "restored" pair for an account
    whose browser trust was never in question. The refresher may quietly force
    one fresh sign-in on the way through, but it must not say a word: the whole
    episode healed below the alert threshold, so there is nothing the user
    could have done and nothing to tell them.
    """

    configs = [_config("1", friendly_name="Ana")]
    monkeypatch.setattr(R, "load_presence_configs", lambda: configs)
    invalidated: list[str] = []
    monkeypatch.setattr(
        R, "invalidate_session", lambda cfg: invalidated.append(cfg.label)
    )
    notifier = _FakeNotifier()

    def failing(*, config: PresenceConfig) -> list[PresenceEntity]:
        raise RuntimeError("Authentication required for Account. (409):")

    monkeypatch.setattr(R, "fetch_presence", failing)
    # Three failed polls - one short of the default alert threshold, i.e. the
    # observed episode (~04:37, 04:52, 05:07).
    for expected in (1, 2, 3):
        cache = asyncio.run(R.refresh_once(notifier_factory=lambda: notifier))
        assert cache.accounts[0].consecutive_failures == expected

    assert notifier.sent == [], "a self-healing hiccup must never notify"

    # Apple comes back (05:22): the streak resets and the recovery stays silent
    # too, because no break was ever announced.
    monkeypatch.setattr(R, "fetch_presence", lambda *, config: [_entity("mine")])
    cache = asyncio.run(R.refresh_once(notifier_factory=lambda: notifier))

    assert cache.accounts[0].available is True
    assert cache.accounts[0].consecutive_failures == 0
    assert notifier.sent == []
    # The silent self-heal is still allowed to have run - it is the *messages*
    # that were the bug, not the handshake.
    assert invalidated in ([], ["1"])


def test_failing_since_is_measured_not_inferred_from_the_poll_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #678: the alert's "has been failing for ~X" must be a real elapsed
    span, not ``streak * PRESENCE_ICLOUD_REFRESH_INTERVAL_S``.

    ``refresh_once`` is also driven on demand by the locate path, so a break the
    user happens to hammer racks up its streak far faster than the background
    cadence - and a duration inferred from that cadence would overstate it by
    an order of magnitude. The episode carries its own start stamp instead,
    pinned on the first failure and cleared only by a success.
    """

    configs = [_config("1", friendly_name="Ana")]
    monkeypatch.setattr(R, "load_presence_configs", lambda: configs)
    monkeypatch.setattr(R, "invalidate_session", lambda cfg: None)
    monkeypatch.setattr(
        R, "fetch_presence", lambda *, config: (_ for _ in ()).throw(
            PresenceAuthError("iCloud requires 2FA")
        )
    )

    cache = asyncio.run(R.refresh_once(notifier_factory=lambda: None))
    started = cache.accounts[0].failing_since
    assert started, "the first failure must pin the episode's start"

    # Two more rapid polls (the locate path's shape): the stamp must not move.
    for _ in range(2):
        cache = asyncio.run(R.refresh_once(notifier_factory=lambda: None))
    assert cache.accounts[0].failing_since == started
    assert cache.accounts[0].consecutive_failures == 3

    # A 30-minute-old break reports ~30m even though only 4 polls happened,
    # which the interval-derived figure would have called an hour.
    status = R.PresenceAccountStatus(
        "1", False, "2fa_required", "stale", consecutive_failures=4,
        failing_since=(
            R.datetime.now(R.timezone.utc) - timedelta(minutes=30)
        ).isoformat(),
    )
    text = R._stuck_alert_text(configs[0], status, now=R.datetime.now(R.timezone.utc))
    assert "~30m" in text
    assert "4 consecutive refreshes" in text

    # Recovery clears the stamp so the next break starts its own episode.
    monkeypatch.setattr(R, "fetch_presence", lambda *, config: [_entity("mine")])
    cache = asyncio.run(R.refresh_once(notifier_factory=lambda: None))
    assert cache.accounts[0].failing_since is None


def test_alert_only_claims_a_fresh_sign_in_that_actually_happened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #678: "A fresh sign-in did not fix it" must be an observed fact.

    The handshake is throttled to one per ``_retry_backoff_s`` (4h), far longer
    than the alert threshold, so a break that re-opens inside that window never
    gets a sign-in attempt of its own - and the message must not claim one.
    """

    config = _config("1", friendly_name="Ana")
    now = R.datetime.now(R.timezone.utc)
    status = R.PresenceAccountStatus(
        "1", False, "2fa_required", "stale", consecutive_failures=4,
        failing_since=(now - timedelta(minutes=45)).isoformat(),
    )

    # No forced retry recorded at all (or one from before this episode began):
    # the remedy stands on its own, with no claim about a sign-in.
    assert "A fresh sign-in" not in R._stuck_alert_text(config, status, now=now)
    R._LAST_RETRY_ATTEMPT["1"] = now - timedelta(hours=3)  # a *previous* episode
    assert "A fresh sign-in" not in R._stuck_alert_text(config, status, now=now)
    # Renewing trust is still offered either way - that is the actionable part.
    assert "Renew trust" in R._stuck_alert_text(config, status, now=now)

    # A retry that did land inside this episode is reported honestly.
    R._LAST_RETRY_ATTEMPT["1"] = now - timedelta(minutes=30)
    assert "A fresh sign-in did not fix it." in R._stuck_alert_text(
        config, status, now=now
    )


def test_single_failed_poll_does_not_force_a_handshake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #678: one transient 409 must not throw away a working session.

    ``invalidate_session`` costs a full password sign-in on the next build,
    which Apple throttles hard once browser trust has lapsed (#659) - far too
    expensive to spend on a single blip.
    """

    configs = [_config("1", friendly_name="Ana")]
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
    R._CACHE = R.PresenceDiagnosticsCache(entities=[], accounts=[_broken("1", failures=1)])
    notifier = _FakeNotifier()

    asyncio.run(R.refresh_once(notifier_factory=lambda: notifier))

    assert invalidated == []
    assert notifier.sent == []


def test_stuck_account_alerts_once_then_notifies_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #678: a genuinely stuck account gets exactly one actionable alert
    per episode - never one per poll - and its recovery closes that alert.

    The message must name the account, its reason, and the remedy that actually
    exists (in-app trust renewal / the password), and must not tell the user to
    approve a sign-in: since #658 the unattended refresher fetches with
    ``request_2fa_push=False`` and can no longer make Apple push anything.
    """

    configs = [_config("1", friendly_name="Roberto")]
    monkeypatch.setattr(R, "load_presence_configs", lambda: configs)
    invalidated: list[str] = []
    monkeypatch.setattr(
        R, "invalidate_session", lambda cfg: invalidated.append(cfg.label)
    )
    monkeypatch.setattr(
        R, "fetch_presence", lambda *, config: (_ for _ in ()).throw(
            PresenceAuthError("iCloud Find My refused the session")
        )
    )
    R._CACHE = R.PresenceDiagnosticsCache(
        entities=[],
        accounts=[_broken("1", failures=R._alert_after_failures() - 1)],
    )
    notifier = _FakeNotifier()

    cache = asyncio.run(R.refresh_once(notifier_factory=lambda: notifier))

    assert len(notifier.sent) == 1
    alert = notifier.sent[0]
    assert "Roberto" in alert
    assert "2fa_required" in alert
    assert "Renew trust" in alert
    assert "approve the sign-in" not in alert
    assert cache.accounts[0].consecutive_failures == R._alert_after_failures()

    # Still stuck on the following polls: the latch holds, no repeat spam.
    asyncio.run(R.refresh_once(notifier_factory=lambda: notifier))
    asyncio.run(R.refresh_once(notifier_factory=lambda: notifier))
    assert len(notifier.sent) == 1

    # Recovery closes the announced episode with exactly one message.
    monkeypatch.setattr(R, "fetch_presence", lambda *, config: [_entity("mine")])
    cache = asyncio.run(R.refresh_once(notifier_factory=lambda: notifier))

    assert cache.accounts[0].available is True
    assert len(notifier.sent) == 2
    assert "Roberto" in notifier.sent[1] and "restored" in notifier.sent[1]
    assert "1" not in R._ALERTED
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

    Issue #678 keeps the same guarantee against a *longer* flap: the second
    break is seeded straight past the alert threshold, so only the backoff
    stands between it and a second handshake.
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
        accounts=[_broken("1", failures=R._alert_after_failures())],
    )
    R._ALERTED.add("1")  # this episode was announced, so its recovery closes it
    notifier = _FakeNotifier()

    # First poll: broken -> self-heal fires, recovers.
    asyncio.run(R.refresh_once(notifier_factory=lambda: notifier))
    assert invalidated == ["1"]
    assert len(notifier.sent) == 1  # the recovery message

    # Account flaps broken again immediately (e.g. pyicloud's internal
    # sub-service reauth silently poisoned requires_2fa) - still well within
    # the default 4h backoff window.
    monkeypatch.setattr(
        R, "fetch_presence", lambda *, config: (_ for _ in ()).throw(
            PresenceAuthError("iCloud requires 2FA")
        )
    )
    for _ in range(R._alert_after_failures()):
        asyncio.run(R.refresh_once(notifier_factory=lambda: notifier))

    # Still broken, still within backoff - must not force a second handshake.
    cache = asyncio.run(R.refresh_once(notifier_factory=lambda: notifier))

    assert invalidated == ["1"]  # no second forced reconnect
    # The new break is a new episode, so it earns its one alert - but exactly
    # one, no matter how many polls it spans.
    assert len(notifier.sent) == 2
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
        # Well past the self-heal streak (#678), so the backoff is the only
        # thing that can be holding the handshake back.
        accounts=[_broken("1", failures=R._self_heal_after_failures() + 1)],
    )
    R._LAST_RETRY_ATTEMPT["1"] = R.datetime.now(R.timezone.utc)
    R._ALERTED.add("1")  # already announced, so no second alert is due either
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


def test_refresher_fetches_with_2fa_push_disabled_and_logs_broken_polls(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Issue #658: the unattended refresher must never let pyicloud ask Apple to
    push a 2FA code (nobody in the tray can enter it), and every poll that ends
    broken must leave a log breadcrumb (those polls used to be invisible)."""

    configs = [_config("1"), _config("2")]
    monkeypatch.setattr(R, "load_presence_configs", lambda: configs)
    seen: list[tuple[str, bool]] = []

    def fake_fetch(*, config: PresenceConfig) -> list[PresenceEntity]:
        seen.append((config.label, config.request_2fa_push))
        if config.label == "2":
            raise PresenceAuthError("iCloud Find My refused the session")
        return [_entity("mine")]

    monkeypatch.setattr(R, "fetch_presence", fake_fetch)
    R._CACHE = R.PresenceDiagnosticsCache(entities=[])

    with caplog.at_level("WARNING", logger=R.logger.name):
        cache = asyncio.run(R.refresh_once(notifier_factory=lambda: None))

    assert sorted(seen) == [("1", False), ("2", False)]
    # The configs handed to the caller keep their attended default - only the
    # refresher's own fetch opts out of the push.
    assert all(cfg.request_2fa_push is True for cfg in configs)
    assert cache.reason == "partial"
    breadcrumbs = [r for r in caplog.records if "needs re-auth" in r.getMessage()]
    assert len(breadcrumbs) == 1
    assert "account 2" in breadcrumbs[0].getMessage()


def test_account_status_carries_display_name_and_session_trust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #659: every per-account status names the account (friendly name
    or Apple ID) and reports whether its cached session still holds browser
    trust - on healthy *and* broken outcomes alike, and ``None`` when no
    session has been built - so the PWA rows can render from the cache."""

    configs = [_config("1", friendly_name="Fixture One"), _config("2"), _config("3")]
    monkeypatch.setattr(R, "load_presence_configs", lambda: configs)
    trust_by_label = {"1": True, "2": False, "3": None}
    monkeypatch.setattr(R, "session_trust_state", lambda cfg: trust_by_label[cfg.label])

    def fake_fetch(*, config: PresenceConfig) -> list[PresenceEntity]:
        if config.label == "2":
            raise PresenceAuthError("iCloud Find My refused the session")
        return [_entity(config.label)]

    monkeypatch.setattr(R, "fetch_presence", fake_fetch)

    cache = asyncio.run(R.refresh_once(notifier_factory=lambda: None))

    by_label = {a.label: a for a in cache.accounts}
    assert by_label["1"].display_name == "Fixture One"
    assert by_label["1"].trusted is True
    assert by_label["1"].available is True
    assert by_label["2"].display_name == "2@example.com"
    assert by_label["2"].trusted is False
    assert by_label["2"].available is False  # broken and untrusted are separate facts
    assert by_label["3"].trusted is None
