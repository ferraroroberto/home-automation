"""Presence engine arm and staleness blocks: notification dwell, iCloud
corroboration and state-file write churn."""

from datetime import datetime, timedelta, timezone

from src.presence import presence_blocks as B
from src.presence import presence_engine as P
from tests.presence.test_presence_engine import _person


# --- arm-block notification dwell (issue #599) ---
#
# Production incident 2026-08-01: ana's arrival webhook landed at 17:51:07 and
# roberto's at 17:51:39. The block diagnostic fired a Telegram "FAILED" at
# 17:51:27 - inside the 32 s gap between two people walking in together - and
# cleared itself 12 s later. Nothing had failed; nothing was even attempted.


_ANA_IN = datetime(2026, 8, 1, 17, 51, 7, 924162, tzinfo=timezone.utc)
_ROB_IN = datetime(2026, 8, 1, 17, 51, 39, 885749, tzinfo=timezone.utc)
_ALERTED_AT = datetime(2026, 8, 1, 17, 51, 27, 903472, tzinfo=timezone.utc)


def _ana_block() -> P.PresenceBlock:
    return P.PresenceBlock(
        key=f"block:ana:{_ANA_IN.isoformat()}",
        blocking_person_ids=("ana",),
        since=_ANA_IN,
    )


def test_two_arrivals_32s_apart_do_not_notify(monkeypatch, tmp_path):
    """The exact production window: no notification inside the arrival gap."""
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    block = _ana_block()

    first = B.set_arm_block(block, dwell_s=900, at=_ANA_IN + timedelta(seconds=10))
    assert first.changed is True      # still worth logging + showing in the UI
    assert first.notify is False      # ...but not worth paging about

    # The tick that actually paged in production.
    assert B.set_arm_block(block, dwell_s=900, at=_ALERTED_AT).notify is False

    # roberto arrives; the block evaporates on its own.
    cleared = B.set_arm_block(None, dwell_s=900, at=_ROB_IN)
    assert cleared.changed is True and cleared.notify is False


def test_block_notifies_once_after_dwell(monkeypatch, tmp_path):
    """A genuinely stuck presence still pages - once - after the dwell."""
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    block = _ana_block()

    assert B.set_arm_block(block, dwell_s=900, at=_ANA_IN).notify is False
    assert B.set_arm_block(
        block, dwell_s=900, at=_ANA_IN + timedelta(seconds=899)
    ).notify is False
    assert B.set_arm_block(
        block, dwell_s=900, at=_ANA_IN + timedelta(seconds=900)
    ).notify is True
    # A confirmed send (#601) is what latches "already notified" - without it
    # the episode would keep reporting `notify=True` forever.
    B.mark_arm_block_notified(block.key)
    # And not again on later ticks of the same episode.
    for extra in (901, 1200, 7200):
        assert B.set_arm_block(
            block, dwell_s=900, at=_ANA_IN + timedelta(seconds=extra)
        ).notify is False


# --- a send that's due but not (yet) confirmed sent (issue #601) ---
#
# #527 fixed the per-day error de-dupe to only latch once a Telegram send was
# *confirmed* delivered. The arm-block path had the same eager-latch bug:
# `set_arm_block` used to mark `arm_blocked_notified` the moment a send became
# due, regardless of whether the caller's send actually went through - a
# failed/unconfigured notifier silently burned the episode's one-and-only
# alert with `block.key` staying stable for as long as the presence itself
# stayed stuck.


def test_notify_due_is_not_marked_notified_until_confirmed(monkeypatch, tmp_path):
    """A send that never happens (declined, failed, no notifier) must not burn
    the episode's alert - `set_arm_block` alone must never latch `notified`."""
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    block = _ana_block()
    B.set_arm_block(block, dwell_s=900, at=_ANA_IN)  # establishes first_seen

    at = _ANA_IN + timedelta(seconds=900)
    assert B.set_arm_block(block, dwell_s=900, at=at).notify is True
    # Nothing marked it delivered - still due on a later tick, once the retry
    # cooldown (untouched here) has elapsed.
    assert B.set_arm_block(
        block, dwell_s=900, at=at + timedelta(seconds=B._ARM_BLOCK_RETRY_COOLDOWN_S)
    ).notify is True


def test_failed_attempt_backs_off_instead_of_retrying_every_tick(monkeypatch, tmp_path):
    """A declined/failed send must retry on a *later* tick, not every ~10s
    poll tick - `mark_arm_block_attempted` is what backs this off."""
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    block = _ana_block()
    B.set_arm_block(block, dwell_s=900, at=_ANA_IN)  # establishes first_seen

    at = _ANA_IN + timedelta(seconds=900)
    assert B.set_arm_block(block, dwell_s=900, at=at).notify is True
    B.mark_arm_block_attempted(block.key, at=at)  # attempted, but not confirmed sent

    # A tick shortly after (the ~10s presence poll cadence) must not retry yet.
    assert B.set_arm_block(
        block, dwell_s=900, at=at + timedelta(seconds=10)
    ).notify is False
    assert B.set_arm_block(
        block, dwell_s=900, at=at + timedelta(seconds=B._ARM_BLOCK_RETRY_COOLDOWN_S - 1)
    ).notify is False
    # Once the cooldown window elapses, a retry is due again.
    assert B.set_arm_block(
        block, dwell_s=900, at=at + timedelta(seconds=B._ARM_BLOCK_RETRY_COOLDOWN_S)
    ).notify is True


def test_mark_arm_block_notified_ignores_a_stale_key(monkeypatch, tmp_path):
    """A confirmed send for an episode that has since moved on must not mark
    the wrong (current) episode as notified."""
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    block = _ana_block()
    B.set_arm_block(block, dwell_s=900, at=_ANA_IN)  # establishes first_seen
    at = _ANA_IN + timedelta(seconds=900)
    B.set_arm_block(block, dwell_s=900, at=at)

    B.mark_arm_block_notified("some-other-episode-key")

    assert B.set_arm_block(
        block, dwell_s=900, at=at + timedelta(seconds=B._ARM_BLOCK_RETRY_COOLDOWN_S)
    ).notify is True


def test_mark_arm_block_attempted_ignores_a_stale_key(monkeypatch, tmp_path):
    """An attempt timestamp for a superseded episode must not throttle the
    current one."""
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    block = _ana_block()
    B.set_arm_block(block, dwell_s=900, at=_ANA_IN)  # establishes first_seen
    at = _ANA_IN + timedelta(seconds=900)
    B.set_arm_block(block, dwell_s=900, at=at)

    B.mark_arm_block_attempted("some-other-episode-key", at=at)

    # The (misdirected) attempt must not have started this episode's cooldown.
    assert B.set_arm_block(
        block, dwell_s=900, at=at + timedelta(seconds=1)
    ).notify is True


def test_dwell_is_anchored_to_first_seen_not_to_since(monkeypatch, tmp_path):
    """`since` can be hours old for someone legitimately home all day.

    Anchoring the dwell to it would fire instantly the moment anyone else left
    - reintroducing the very false alert this dwell exists to prevent.
    """
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    home_all_day = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)
    other_leaves = datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc)  # 9 h later
    block = P.PresenceBlock(
        key=f"block:ana:{home_all_day.isoformat()}",
        blocking_person_ids=("ana",),
        since=home_all_day,
    )

    assert B.set_arm_block(block, dwell_s=900, at=other_leaves).notify is False
    assert B.set_arm_block(
        block, dwell_s=900, at=other_leaves + timedelta(seconds=900)
    ).notify is True


def test_new_episode_restarts_the_dwell(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    first = _ana_block()
    B.set_arm_block(first, dwell_s=900, at=_ANA_IN)
    assert B.set_arm_block(
        first, dwell_s=900, at=_ANA_IN + timedelta(seconds=900)
    ).notify is True

    # A different set of blocking people is a different episode.
    second = P.PresenceBlock(
        key="block:ana,roberto:x", blocking_person_ids=("ana", "roberto"), since=_ANA_IN,
    )
    at = _ANA_IN + timedelta(seconds=1000)
    assert B.set_arm_block(second, dwell_s=900, at=at).notify is False
    assert B.set_arm_block(
        second, dwell_s=900, at=at + timedelta(seconds=900)
    ).notify is True


def test_recurring_block_after_clear_notifies_again(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    block = _ana_block()
    B.set_arm_block(block, dwell_s=900, at=_ANA_IN)
    assert B.set_arm_block(
        block, dwell_s=900, at=_ANA_IN + timedelta(seconds=900)
    ).notify is True
    B.set_arm_block(None, dwell_s=900, at=_ANA_IN + timedelta(hours=1))

    back = _ANA_IN + timedelta(hours=2)
    assert B.set_arm_block(block, dwell_s=900, at=back).notify is False
    assert B.set_arm_block(
        block, dwell_s=900, at=back + timedelta(seconds=900)
    ).notify is True


def test_block_is_visible_in_the_api_before_the_dwell_elapses(monkeypatch, tmp_path):
    """The dwell gates the *notification*, not the diagnostic (#599)."""
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    B.set_arm_block(_ana_block(), dwell_s=900, at=_ANA_IN + timedelta(seconds=10))
    assert B.load_arm_block() == {
        "blocked": True,
        "person_ids": ["ana"],
        "since": _ANA_IN.isoformat(),
    }


def test_arm_block_dwell_config_round_trips(tmp_path):
    path = tmp_path / "presence_automation.json"
    path.write_text('{"arm_block_notify_after_s": 120}', encoding="utf-8")
    assert P.load_automation_config(path).arm_block_notify_after_s == 120
    # Absent / malformed -> the default dwell, never an instant page.
    for body in ('{}', '{"arm_block_notify_after_s": null}', '{"arm_block_notify_after_s": "x"}'):
        path.write_text(body, encoding="utf-8")
        assert P.load_automation_config(path).arm_block_notify_after_s == 900


# --- iCloud staleness corroboration (issue #653) ---
#
# Production incident 2026-08-11: roberto's "leave home" webhook fired
# correctly, but auto-arm never fired because ana's webhook record hadn't
# updated in ~4 days (she simply hadn't crossed her geofence) - the freshness
# gate silently refused every decision for the whole household, with zero
# trace. Corroboration lets a fresher, agreeing iCloud/Find My read stand in
# for the missing webhook heartbeat.


def test_icloud_corroboration_window_config_round_trips(tmp_path):
    path = tmp_path / "presence_automation.json"
    path.write_text('{"icloud_corroboration_window_s": 3600}', encoding="utf-8")
    assert P.load_automation_config(path).icloud_corroboration_window_s == 3600
    for body in ('{}', '{"icloud_corroboration_window_s": null}', '{"icloud_corroboration_window_s": "x"}'):
        path.write_text(body, encoding="utf-8")
        assert P.load_automation_config(path).icloud_corroboration_window_s == 21600


def test_stale_webhook_arms_when_icloud_corroboration_agrees(monkeypatch, tmp_path):
    """The exact production shape: ana's webhook is stale, but her iCloud
    entity is fresh and agrees she's away - the arm must still fire."""
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(
        auto_arm_enabled=True, arm_away_after_s=60, stale_after_s=3600,
        icloud_corroboration_window_s=21600,
    )
    ana_stale = _person("ana", "away", t0 - timedelta(days=4))  # far past stale_after_s
    roberto_left = _person("roberto", "away", t0)
    corroboration = {
        "ana": P.PresenceCorroboration(last_seen=t0 - timedelta(hours=1), at_home=False),
    }
    decision = P.evaluate_alarm_decision(
        [ana_stale, roberto_left],
        security_mode="disarmed",
        config=cfg,
        at=t0 + timedelta(minutes=5),
        corroboration=corroboration,
    )
    assert decision is not None
    assert decision.kind == "arm"


def test_stale_webhook_still_blocks_when_icloud_also_stale(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(
        auto_arm_enabled=True, arm_away_after_s=60, stale_after_s=3600,
        icloud_corroboration_window_s=21600,
    )
    ana_stale = _person("ana", "away", t0 - timedelta(days=4))
    roberto_left = _person("roberto", "away", t0)
    corroboration = {
        # Also outside the corroboration window - can't vouch for her either.
        "ana": P.PresenceCorroboration(last_seen=t0 - timedelta(days=2), at_home=False),
    }
    assert P.evaluate_alarm_decision(
        [ana_stale, roberto_left],
        security_mode="disarmed",
        config=cfg,
        at=t0 + timedelta(minutes=5),
        corroboration=corroboration,
    ) is None


def test_stale_webhook_still_blocks_when_icloud_disagrees(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(
        auto_arm_enabled=True, arm_away_after_s=60, stale_after_s=3600,
        icloud_corroboration_window_s=21600,
    )
    ana_stale = _person("ana", "away", t0 - timedelta(days=4))
    roberto_left = _person("roberto", "away", t0)
    corroboration = {
        # Fresh, but says she's actually home - disagrees with the stale webhook.
        "ana": P.PresenceCorroboration(last_seen=t0 - timedelta(hours=1), at_home=True),
    }
    assert P.evaluate_alarm_decision(
        [ana_stale, roberto_left],
        security_mode="disarmed",
        config=cfg,
        at=t0 + timedelta(minutes=5),
        corroboration=corroboration,
    ) is None


def test_fresh_webhook_is_unaffected_by_absent_corroboration(monkeypatch, tmp_path):
    """A person with no corroboration entry at all (no linked iCloud entity)
    is unaffected as long as their webhook data is itself fresh."""
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(auto_arm_enabled=True, arm_away_after_s=300, stale_after_s=3600)
    decision = P.evaluate_alarm_decision(
        [_person("roberto", "away", t0), _person("ana", "away", t0 + timedelta(seconds=30))],
        security_mode="disarmed",
        config=cfg,
        at=t0 + timedelta(minutes=6),
        corroboration={},
    )
    assert decision is not None and decision.kind == "arm"


def test_evaluate_staleness_block_fires_when_uncorroborated(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(auto_arm_enabled=True, stale_after_s=3600)
    block = P.evaluate_staleness_block(
        [_person("ana", "away", t0 - timedelta(days=4)), _person("roberto", "away", t0)],
        config=cfg,
        at=t0 + timedelta(minutes=5),
    )
    assert block is not None
    assert block.stale_person_ids == ("ana",)


def test_evaluate_staleness_block_does_not_fire_when_corroborated(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(auto_arm_enabled=True, stale_after_s=3600)
    corroboration = {
        "ana": P.PresenceCorroboration(last_seen=t0 - timedelta(hours=1), at_home=False),
    }
    assert P.evaluate_staleness_block(
        [_person("ana", "away", t0 - timedelta(days=4)), _person("roberto", "away", t0)],
        config=cfg,
        at=t0 + timedelta(minutes=5),
        corroboration=corroboration,
    ) is None


# issue #696 - a webhook record that is FRESH by raw age is still trusted
# outright today, even when a strictly newer iCloud/Find My read disagrees
# with it: #653's corroboration only ever rescues an already-stale record
# when iCloud agrees, it never revokes trust from a fresh-but-wrong one. Live
# case: a stuck iOS Shortcut "Arrive" automation leaves someone's webhook
# state "away" long after they're actually home, but well inside a generous
# stale_after_s - the house must not treat that as "confirmed away".


def test_fresh_webhook_still_blocks_when_newer_icloud_disagrees(monkeypatch, tmp_path):
    """roberto's webhook ping is fresh by the clock (well under stale_after_s),
    but a strictly newer iCloud read says he's actually home - the engine must
    refuse to arm on his stale-but-clock-fresh "away", not trust it outright."""
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 8, 25, 6, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(
        auto_arm_enabled=True, arm_away_after_s=60, stale_after_s=216000,
        icloud_corroboration_window_s=21600,
    )
    roberto_stuck_away = _person("roberto", "away", t0)
    ana_left = _person("ana", "away", t0 + timedelta(days=1))
    corroboration = {
        # Newer than roberto's webhook ping, and disagrees: iCloud says home.
        "roberto": P.PresenceCorroboration(last_seen=t0 + timedelta(hours=22), at_home=True),
    }
    assert P.evaluate_alarm_decision(
        [roberto_stuck_away, ana_left],
        security_mode="disarmed",
        config=cfg,
        at=t0 + timedelta(days=1, minutes=5),
        corroboration=corroboration,
    ) is None


def test_fresh_webhook_unaffected_when_disagreeing_icloud_read_is_older(monkeypatch, tmp_path):
    """A disagreeing iCloud read that predates the webhook ping has nothing to
    say - the webhook is the more recent information and stays authoritative."""
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 8, 25, 6, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(
        auto_arm_enabled=True, arm_away_after_s=60, stale_after_s=216000,
        icloud_corroboration_window_s=21600,
    )
    roberto_left = _person("roberto", "away", t0)
    ana_left = _person("ana", "away", t0 + timedelta(seconds=30))
    corroboration = {
        # Older than roberto's webhook ping - stale news, must not override it.
        "roberto": P.PresenceCorroboration(last_seen=t0 - timedelta(hours=1), at_home=True),
    }
    decision = P.evaluate_alarm_decision(
        [roberto_left, ana_left],
        security_mode="disarmed",
        config=cfg,
        at=t0 + timedelta(minutes=5),
        corroboration=corroboration,
    )
    assert decision is not None
    assert decision.kind == "arm"


def test_evaluate_staleness_block_surfaces_contradicted_person_ids(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 8, 25, 6, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(
        auto_arm_enabled=True, stale_after_s=216000, icloud_corroboration_window_s=21600,
    )
    corroboration = {
        "roberto": P.PresenceCorroboration(last_seen=t0 + timedelta(hours=22), at_home=True),
    }
    block = P.evaluate_staleness_block(
        [_person("roberto", "away", t0), _person("ana", "away", t0 + timedelta(days=1))],
        config=cfg,
        at=t0 + timedelta(days=1, minutes=5),
        corroboration=corroboration,
    )
    assert block is not None
    assert block.contradicted_person_ids == ("roberto",)
    assert block.stale_person_ids == ()
    assert block.all_person_ids == ("roberto",)


def test_evaluate_staleness_block_does_not_fire_when_everyone_fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(auto_arm_enabled=True, stale_after_s=3600)
    assert P.evaluate_staleness_block(
        [_person("ana", "away", t0), _person("roberto", "away", t0)],
        config=cfg,
        at=t0,
    ) is None


def test_set_staleness_block_notifies_once_after_dwell(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc)
    block = P.StalePresenceBlock(key="stale:ana", stale_person_ids=("ana",))

    assert B.set_staleness_block(block, dwell_s=900, at=t0).notify is False
    assert B.set_staleness_block(
        block, dwell_s=900, at=t0 + timedelta(seconds=899)
    ).notify is False
    assert B.set_staleness_block(
        block, dwell_s=900, at=t0 + timedelta(seconds=900)
    ).notify is True
    B.mark_staleness_block_notified(block.key)
    assert B.set_staleness_block(
        block, dwell_s=900, at=t0 + timedelta(seconds=1200)
    ).notify is False

    # Independent state namespace: the arm-block diagnostic must be unaffected.
    assert B.load_arm_block() == {"blocked": False, "person_ids": [], "since": None}
    assert B._STALE_BLOCK.load() == {"blocked": True, "person_ids": ["ana"]}


def test_set_staleness_block_clears(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    block = P.StalePresenceBlock(key="stale:ana", stale_person_ids=("ana",))
    B.set_staleness_block(block, dwell_s=0)
    assert B._STALE_BLOCK.load()["blocked"] is True

    cleared = B.set_staleness_block(None)
    assert cleared.changed is True and cleared.notify is False
    assert B._STALE_BLOCK.load() == {"blocked": False, "person_ids": []}


# --- write churn on the shared state file (issue #689) ---
#
# Both block diagnostics ran every ~10s tick and saved unconditionally, so
# `presence_state.json` was rewritten four times a tick to persist bytes
# identical to the ones just read. That churn is what kept the file permanently
# mid-`os.replace` and made a concurrent reader's sharing violation - the read
# that came back empty and wiped the roster - near-certain.


def test_unchanged_block_tick_does_not_rewrite_the_state_file(monkeypatch, tmp_path):
    state = tmp_path / "presence_state.json"
    monkeypatch.setattr(P, "STATE_PATH", state)
    since = datetime(2026, 8, 25, 3, 40, tzinfo=timezone.utc)
    block = P.PresenceBlock(
        key="block:roberto:" + since.isoformat(),
        blocking_person_ids=("roberto",),
        since=since,
    )

    B.set_arm_block(block, dwell_s=900, at=since)
    settled = state.read_bytes()
    mtime = state.stat().st_mtime_ns

    # Same episode, same tick shape, nothing new to record.
    B.set_arm_block(block, dwell_s=900, at=since + timedelta(seconds=10))
    B.set_arm_block(block, dwell_s=900, at=since + timedelta(seconds=20))

    assert state.read_bytes() == settled
    assert state.stat().st_mtime_ns == mtime


def test_an_already_clear_block_does_not_rewrite_the_state_file(monkeypatch, tmp_path):
    state = tmp_path / "presence_state.json"
    monkeypatch.setattr(P, "STATE_PATH", state)
    B.set_arm_block(None)
    settled = state.read_bytes()
    mtime = state.stat().st_mtime_ns

    B.set_arm_block(None)
    B.set_arm_block(None)

    assert state.read_bytes() == settled
    assert state.stat().st_mtime_ns == mtime


def test_a_real_block_change_still_persists(monkeypatch, tmp_path):
    """The skip must not swallow an episode that genuinely moved."""

    state = tmp_path / "presence_state.json"
    monkeypatch.setattr(P, "STATE_PATH", state)
    since = datetime(2026, 8, 25, 3, 40, tzinfo=timezone.utc)
    first = P.PresenceBlock(key="block:ana", blocking_person_ids=("ana",), since=since)
    second = P.PresenceBlock(
        key="block:roberto", blocking_person_ids=("roberto",), since=since
    )

    B.set_arm_block(first, dwell_s=900, at=since)
    assert B.load_arm_block()["person_ids"] == ["ana"]
    assert B.set_arm_block(second, dwell_s=900, at=since).changed is True
    assert B.load_arm_block()["person_ids"] == ["roberto"]
