"""Unit tests for the pure logic in :mod:`scripts.ha_config_sync` (issue #243).

No SSH, no network: only the managed-block injection, diffing, backup-path
naming, secret-key-NAME detection, and conversation-reply parsing — the parts
that must be correct before the VM is even reachable for a live deploy.
"""

from __future__ import annotations

import datetime

from scripts import ha_config_sync as H


# ----------------------------------------------------------- managed block
def test_build_managed_block_wraps_in_markers() -> None:
    block = H.build_managed_block("rest_command:\n  foo: bar\n")
    assert block.startswith(H.BLOCK_BEGIN)
    assert block.rstrip().endswith(H.BLOCK_END)
    assert "rest_command:" in block


def test_append_when_no_markers_present() -> None:
    existing = "default_config:\nautomation: !include automations.yaml\n"
    block = H.build_managed_block("intent_script:\n  X: {}\n")
    result = H.replace_or_append_block(existing, block)
    # original content preserved, block appended after a blank-line separator
    assert result.startswith(existing)
    assert H.BLOCK_BEGIN in result
    assert "\n\n" + H.BLOCK_BEGIN in result


def test_replace_existing_block_preserves_surroundings() -> None:
    block_v1 = H.build_managed_block("rest_command:\n  v: 1\n")
    head = "default_config:\n\n"
    tail = "\nlogger:\n  default: info\n"
    existing = head + block_v1 + tail

    block_v2 = H.build_managed_block("rest_command:\n  v: 2\n")
    result = H.replace_or_append_block(existing, block_v2)

    assert result.startswith("default_config:")
    assert "logger:" in result
    assert "v: 2" in result
    assert "v: 1" not in result
    # exactly one managed block remains
    assert result.count(H.BLOCK_BEGIN) == 1
    assert result.count(H.BLOCK_END) == 1


def test_deploy_is_idempotent() -> None:
    """Applying the same block twice yields a stable, identical result."""
    existing = "default_config:\n"
    block = H.build_managed_block("rest_command:\n  v: 1\n")
    once = H.replace_or_append_block(existing, block)
    twice = H.replace_or_append_block(once, block)
    assert once == twice
    assert once.count(H.BLOCK_BEGIN) == 1


def test_build_managed_block_idempotent_when_markers_present() -> None:
    """A snippet that already carries the markers is normalised, not re-wrapped."""
    snippet = f"{H.BLOCK_BEGIN}\nrest_command:\n  v: 1\n{H.BLOCK_END}\n"
    block = H.build_managed_block(snippet)
    assert block.count(H.BLOCK_BEGIN) == 1
    assert block.count(H.BLOCK_END) == 1


def test_legacy_header_is_migrated_not_duplicated() -> None:
    """First deploy over the pre-markers #88 install replaces the legacy section
    (header → EOF) instead of appending a duplicate rest_command/intent_script."""
    legacy = (
        "default_config:\n\n"
        f"{H.LEGACY_BLOCK_HEADER} (issue #88, Phase 4) ---\n"
        "rest_command:\n  alarm_arm:\n    url: x\n"
        "intent_script:\n  AlarmDisarmPrompt:\n    speech:\n      text: y\n"
    )
    block = H.build_managed_block("rest_command:\n  v: 2\n")
    result = H.replace_or_append_block(legacy, block)
    assert result.startswith("default_config:")
    assert H.LEGACY_BLOCK_HEADER not in result   # legacy header gone
    assert result.count("rest_command:") == 1    # no duplicate key
    assert result.count(H.BLOCK_BEGIN) == 1
    assert "v: 2" in result
    # and a second deploy is now a stable no-op against the markers
    assert H.replace_or_append_block(result, block) == result


# ----------------------------------------------------------------- diffing
def test_compute_diff_empty_when_identical() -> None:
    assert H.compute_diff("a\nb\n", "a\nb\n", "configuration.yaml") == ""


def test_compute_diff_shows_change() -> None:
    diff = H.compute_diff("v: 1\n", "v: 2\n", "configuration.yaml")
    assert "-v: 1" in diff
    assert "+v: 2" in diff
    assert "configuration.yaml" in diff


# ------------------------------------------------------------- backup path
def test_backup_remote_path_uses_basename_and_stamp() -> None:
    p = H.backup_remote_path("/config/configuration.yaml", "20260628T141502Z")
    assert p == f"{H.BACKUP_DIR}/configuration.yaml.20260628T141502Z.bak"


def test_utc_stamp_is_lexically_sortable() -> None:
    early = H.utc_stamp(datetime.datetime(2026, 6, 28, 1, 0, 0, tzinfo=datetime.timezone.utc))
    late = H.utc_stamp(datetime.datetime(2026, 6, 28, 23, 0, 0, tzinfo=datetime.timezone.utc))
    assert early < late
    assert early.endswith("Z")


# ----------------------------------------------------- secret key NAME check
def test_required_secret_keys_detects_present_and_missing() -> None:
    secrets = "app_api_authorization: Bearer xxx\nsome_other: 1\n"
    present, missing = H.required_secret_keys_present(secrets)
    assert "app_api_authorization" in present
    assert "voice_disarm_pin" in missing


def test_required_secret_keys_never_returns_values() -> None:
    """The check must surface key NAMES only — never the secret value."""
    secret_value = "Bearer super-secret-token-value"
    secrets = (
        f"app_api_authorization: {secret_value}\n"
        "voice_disarm_pin: 1234\n"
        "grocery_api_authorization: Bearer other\n"
    )
    present, missing = H.required_secret_keys_present(secrets)
    assert present == ["app_api_authorization", "voice_disarm_pin", "grocery_api_authorization"]
    assert missing == []
    # neither return list leaks the value
    assert secret_value not in present and secret_value not in missing
    assert "1234" not in present and "1234" not in missing


def test_required_secret_keys_ignores_indented_subkeys() -> None:
    # a nested key with the right name but indented should not count as top-level
    secrets = "outer:\n  app_api_authorization: x\nvoice_disarm_pin: y\n"
    present, missing = H.required_secret_keys_present(secrets)
    assert "app_api_authorization" in missing
    assert "voice_disarm_pin" in present


# ------------------------------------------------ conversation reply parsing
def test_parse_conversation_reply_local_match() -> None:
    payload = {
        "response": {
            "response_type": "action_done",
            "speech": {"plain": {"speech": "The alarm is Disarmed."}},
        }
    }
    speech, rtype, matched = H.parse_conversation_reply(payload)
    assert speech == "The alarm is Disarmed."
    assert rtype == "action_done"
    assert matched is True


def test_parse_conversation_reply_llm_fallthrough() -> None:
    payload = {
        "response": {
            "response_type": "error",
            "speech": {"plain": {"speech": "I don't have the tools for that."}},
        }
    }
    speech, rtype, matched = H.parse_conversation_reply(payload)
    assert rtype == "error"
    assert matched is False


def test_parse_conversation_reply_handles_empty() -> None:
    speech, rtype, matched = H.parse_conversation_reply({})
    assert speech == ""
    assert rtype == ""
    assert matched is False


# ----------------------------------------- committed snippet round-trips clean
def test_committed_snippet_is_a_canonical_block() -> None:
    """The repo's configuration.snippet.yaml carries the markers, so deploying it
    is a stable replace (not an append) and a hand-paste matches script output."""
    snippet = H.SNIPPET_FILE.read_text(encoding="utf-8")
    block = H.build_managed_block(snippet)
    assert block.count(H.BLOCK_BEGIN) == 1
    assert block.count(H.BLOCK_END) == 1
    # injecting it into a bare config then re-injecting is a no-op
    once = H.replace_or_append_block("default_config:\n", block)
    assert H.replace_or_append_block(once, block) == once


# ------------------------------------------------- multi-sentence-file deploy
def test_sentence_files_globs_every_repo_yaml() -> None:
    """The deploy set is globbed from custom_sentences/en/ — every repo-owned
    sentences file must appear with its matching remote path, so a new feature's
    yaml can never be silently skipped (that's how grocery.yaml went missing,
    issue #315)."""
    remotes = {remote for _, remote in H.SENTENCE_FILES}
    for name in ("alarm.yaml", "wake_alarm.yaml", "grocery.yaml"):
        assert f"{H.REMOTE_SENTENCES_DIR}/{name}" in remotes
    for local_file, _ in H.SENTENCE_FILES:
        assert local_file.exists(), f"missing sentence source: {local_file}"
