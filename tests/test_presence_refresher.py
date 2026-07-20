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


def _config(label: str) -> PresenceConfig:
    return PresenceConfig(
        email=f"{label}@example.com",
        password="secret",
        home_radius_m=200.0,
        label=label,
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
