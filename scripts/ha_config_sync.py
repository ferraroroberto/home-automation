"""Deploy this repo's Home Assistant config to the HA VM over SSH (issue #243).

The code-driven replacement for the browser / File-editor workflow: keep the
source-of-truth voice-PE config in this repo, push it into the HA VM's
``/config`` over SSH, validate it with ``ha core check``, reload or (guarded)
restart, and run HA API text probes — using the browser only as a fallback.

The SSH channel is the **Terminal & SSH add-on** shell (mounts ``/config`` +
the ``ha`` CLI), *not* HAOS host SSH on ``:22222`` (that is break-glass and out
of scope). A one-time HA-side bootstrap (enable the add-on, paste this PC's
public key, expose a LAN-only port) is required before any of this connects —
see ``docs/voice-pe-config/README.md``.

Config comes from ``.env`` (non-secret SSH/API settings only; real HA secrets
stay live-only on the VM and are never read, printed, copied, or committed):

    HA_SSH_HOST   HA VM LAN IP            (e.g. 192.168.0.4)
    HA_SSH_PORT   add-on SSH port         (e.g. 2222)
    HA_SSH_USER   add-on SSH user         (e.g. root)
    HA_SSH_KEY    path to the private key (e.g. C:/Users/you/.ssh/ha_ed25519)
    HA_URL        HA frontend base URL    (e.g. http://192.168.0.4:8123)
    HA_TOKEN      long-lived access token (for the API probes)

Subcommands:
    preflight   report connectivity + readiness with a distinct message per
                failure mode (no writes)
    deploy      idempotently push the repo-owned files + the managed
                configuration.yaml block; --dry-run shows a diff, --restart
                guards the full restart
    rollback    restore the most recent backup of the managed files + recheck
    probe       run HA API conversation text probes (read-only by default)

Usage (Windows):
    & .\\.venv\\Scripts\\python.exe -m scripts.ha_config_sync preflight
    & .\\.venv\\Scripts\\python.exe -m scripts.ha_config_sync deploy --dry-run
    & .\\.venv\\Scripts\\python.exe -m scripts.ha_config_sync deploy --restart
    & .\\.venv\\Scripts\\python.exe -m scripts.ha_config_sync rollback
    & .\\.venv\\Scripts\\python.exe -m scripts.ha_config_sync probe
"""
from __future__ import annotations

import argparse
import datetime
import difflib
import json
import logging
import os
import re
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlsplit

from dotenv import load_dotenv

# stdout under capture/redirect falls back to cp1252 on Windows; force UTF-8 so
# the status glyphs below don't throw UnicodeEncodeError under a piped run.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # pragma: no cover - non-reconfigurable stream
    pass

logger = logging.getLogger("ha_config_sync")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Repo-owned source files → their live destinations on the HA VM.
SNIPPET_FILE = PROJECT_ROOT / "docs" / "voice-pe-config" / "configuration.snippet.yaml"
ALARM_SENTENCES_FILE = (
    PROJECT_ROOT / "docs" / "voice-pe-config" / "custom_sentences" / "en" / "alarm.yaml"
)
REMOTE_CONFIG = "/config/configuration.yaml"
REMOTE_ALARM_SENTENCES = "/config/custom_sentences/en/alarm.yaml"
REMOTE_SECRETS = "/config/secrets.yaml"
BACKUP_DIR = "/config/backups/home-automation"

# Marker comments that delimit the block this tool owns inside the otherwise
# hand-managed configuration.yaml. Everything outside the markers is preserved.
BLOCK_BEGIN = (
    "# >>> home-automation:voice-pe-alarm (managed — edit "
    "docs/voice-pe-config/configuration.snippet.yaml, deploy via "
    "scripts/ha_config_sync.py) >>>"
)
BLOCK_END = "# <<< home-automation:voice-pe-alarm <<<"

# The pre-markers header the #88 install pasted into live configuration.yaml.
# The first marker-aware deploy migrates from it (start-of-header → EOF) so the
# managed block doesn't get appended as a duplicate of rest_command/intent_script.
LEGACY_BLOCK_HEADER = "# --- Voice PE deterministic alarm action bridge"

# Secret KEY NAMES the live secrets.yaml must define for the bridge to work. We
# only ever check for the presence of these names — never their values.
REQUIRED_SECRET_KEYS = ("app_api_authorization", "voice_disarm_pin")

# Default read-only conversation probe (safe — a status read never actuates).
DEFAULT_PROBE_TEXT = "what is the alarm status"


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class HaConfig:
    """Non-secret connection settings, loaded from ``.env``."""

    ssh_host: str
    ssh_port: int
    ssh_user: str
    ssh_key: str
    url: str
    token: str

    @property
    def ui_host(self) -> str:
        return urlsplit(self.url).hostname or self.ssh_host

    @property
    def ui_port(self) -> int:
        return urlsplit(self.url).port or 8123


def load_config() -> HaConfig:
    load_dotenv(PROJECT_ROOT / ".env")
    return HaConfig(
        ssh_host=os.getenv("HA_SSH_HOST", "").strip(),
        ssh_port=int(os.getenv("HA_SSH_PORT", "22") or "22"),
        ssh_user=os.getenv("HA_SSH_USER", "root").strip(),
        ssh_key=os.getenv("HA_SSH_KEY", "").strip(),
        url=os.getenv("HA_URL", "").strip().rstrip("/"),
        token=os.getenv("HA_TOKEN", "").strip(),
    )


# --------------------------------------------------------------------------- #
# Pure logic (no SSH / no network — unit-tested)
# --------------------------------------------------------------------------- #
def build_managed_block(snippet_text: str) -> str:
    """Return the canonical managed block for *snippet_text*.

    Idempotent: if the snippet already carries the markers (the committed
    ``configuration.snippet.yaml`` does, so a hand-paste matches the script
    output byte-for-byte) it is just normalised; otherwise it is wrapped.
    """
    body = snippet_text.strip("\n")
    if BLOCK_BEGIN in body and BLOCK_END in body:
        return body + "\n"
    return f"{BLOCK_BEGIN}\n{body}\n{BLOCK_END}\n"


def replace_or_append_block(existing_text: str, block: str) -> str:
    """Return *existing_text* with *block* inserted idempotently.

    Precedence: (1) if the markers are present, the marked region (inclusive) is
    replaced; (2) else if the legacy ``# --- Voice PE …`` header is present, the
    region from that header to EOF is replaced (one-time migration off the
    pre-markers #88 install); (3) else the block is appended at EOF after a
    blank-line separator. Content outside the replaced region is preserved.
    """
    block = block if block.endswith("\n") else block + "\n"
    begin = existing_text.find(BLOCK_BEGIN)
    end = existing_text.find(BLOCK_END)
    if begin != -1 and end != -1 and end > begin:
        end_full = end + len(BLOCK_END)
        # absorb a single trailing newline after the end marker so re-runs are stable
        if end_full < len(existing_text) and existing_text[end_full] == "\n":
            end_full += 1
        return existing_text[:begin] + block + existing_text[end_full:]

    legacy = existing_text.find(LEGACY_BLOCK_HEADER)
    if legacy != -1:
        head = existing_text[:legacy].rstrip("\n")
        head = head + "\n\n" if head else ""
        return head + block

    prefix = existing_text
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    if prefix and not prefix.endswith("\n\n"):
        prefix += "\n"
    return prefix + block


def compute_diff(old: str, new: str, path: str) -> str:
    """Unified diff of *old* → *new*, labelled with *path*. '' if identical."""
    if old == new:
        return ""
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(diff)


def backup_remote_path(remote_path: str, stamp: str) -> str:
    """Timestamped backup path under BACKUP_DIR for a live file."""
    return f"{BACKUP_DIR}/{Path(remote_path).name}.{stamp}.bak"


def utc_stamp(now: Optional[datetime.datetime] = None) -> str:
    """Lexically-sortable UTC stamp, e.g. 20260628T141502Z."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ")


def required_secret_keys_present(
    secrets_text: str, keys: Tuple[str, ...] = REQUIRED_SECRET_KEYS
) -> Tuple[list, list]:
    """Check which of *keys* are defined as top-level keys in *secrets_text*.

    Parses key NAMES only (``^name:``); never reads, returns, or logs a value.
    Returns ``(present, missing)``.
    """
    defined = set(re.findall(r"^([A-Za-z0-9_]+)\s*:", secrets_text, flags=re.MULTILINE))
    present = [k for k in keys if k in defined]
    missing = [k for k in keys if k not in defined]
    return present, missing


def parse_conversation_reply(payload: dict) -> Tuple[str, str, bool]:
    """Extract ``(speech, response_type, matched_locally)`` from an HA
    ``/api/conversation/process`` reply.

    ``matched_locally`` is a heuristic: a local intent match returns
    ``response_type == 'action_done'``; an LLM fallthrough returns ``error`` or a
    generic "no tools" reply. Reported alongside the raw type, not trusted blindly.
    """
    response = (payload or {}).get("response", {}) or {}
    response_type = response.get("response_type", "")
    speech = ((response.get("speech", {}) or {}).get("plain", {}) or {}).get("speech", "")
    matched_locally = response_type == "action_done"
    return speech, response_type, matched_locally


# --------------------------------------------------------------------------- #
# Failure taxonomy
# --------------------------------------------------------------------------- #
class HaSyncError(Exception):
    """A preflight/deploy failure carrying a stable category for distinct reporting."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


# --------------------------------------------------------------------------- #
# SSH transport (paramiko — imported lazily so pure logic needs no dep)
# --------------------------------------------------------------------------- #
class HaSsh:
    """Thin paramiko wrapper: connect with a distinct error per failure, plus
    SFTP read / atomic write / exists and remote command execution."""

    def __init__(self, cfg: HaConfig) -> None:
        self.cfg = cfg
        self._client = None
        self._sftp = None

    def connect(self) -> None:
        try:
            import paramiko
        except ImportError as exc:  # pragma: no cover - dep guaranteed in .venv
            raise HaSyncError(
                "dependency",
                "paramiko is not installed — run "
                "`& .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt`.",
            ) from exc

        if not self.cfg.ssh_host:
            raise HaSyncError("config", "HA_SSH_HOST is not set in .env.")
        if not self.cfg.ssh_key or not Path(self.cfg.ssh_key).expanduser().exists():
            raise HaSyncError(
                "config",
                f"HA_SSH_KEY not found: {self.cfg.ssh_key!r}. Set it to your private key path.",
            )

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=self.cfg.ssh_host,
                port=self.cfg.ssh_port,
                username=self.cfg.ssh_user,
                key_filename=str(Path(self.cfg.ssh_key).expanduser()),
                timeout=8,
                allow_agent=False,
                look_for_keys=False,
            )
        except paramiko.AuthenticationException as exc:
            raise HaSyncError(
                "ssh_auth",
                f"SSH auth failed for {self.cfg.ssh_user}@{self.cfg.ssh_host}:{self.cfg.ssh_port}. "
                "Is this PC's public key in the add-on's authorized_keys?",
            ) from exc
        except (paramiko.SSHException, socket.timeout, OSError) as exc:
            raise HaSyncError(
                "ssh_conn",
                f"SSH connection to {self.cfg.ssh_host}:{self.cfg.ssh_port} failed: {exc}",
            ) from exc
        self._client = client

    @property
    def sftp(self):
        if self._sftp is None:
            self._sftp = self._client.open_sftp()
        return self._sftp

    def exists(self, remote_path: str) -> bool:
        try:
            self.sftp.stat(remote_path)
            return True
        except IOError:
            return False

    def read(self, remote_path: str) -> Optional[str]:
        """Return file contents, or None if it does not exist."""
        try:
            with self.sftp.open(remote_path, "r") as fh:
                return fh.read().decode("utf-8")
        except IOError:
            return None

    def write(self, remote_path: str, content: str) -> None:
        """Atomic write: stage to a temp sibling, then posix-rename into place."""
        parent = str(Path(remote_path).parent).replace("\\", "/")
        self.run(f"mkdir -p {parent}")
        tmp = f"{remote_path}.tmp-{utc_stamp()}"
        with self.sftp.open(tmp, "w") as fh:
            fh.write(content)
        self.sftp.posix_rename(tmp, remote_path)

    def run(self, command: str) -> Tuple[int, str, str]:
        """Run a remote command; return (exit_status, stdout, stderr)."""
        _, stdout, stderr = self._client.exec_command(command, timeout=120)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()
        return rc, out, err

    def has_ha_cli(self) -> bool:
        rc, _, _ = self.run("command -v ha")
        return rc == 0

    def list_backups(self, basename: str) -> list:
        """Most-recent-first backup paths for *basename* under BACKUP_DIR."""
        rc, out, _ = self.run(f"ls -1 {BACKUP_DIR} 2>/dev/null")
        if rc != 0:
            return []
        names = [n for n in out.splitlines() if n.startswith(f"{basename}.") and n.endswith(".bak")]
        names.sort(reverse=True)
        return [f"{BACKUP_DIR}/{n}" for n in names]

    def close(self) -> None:
        try:
            if self._sftp is not None:
                self._sftp.close()
        finally:
            if self._client is not None:
                self._client.close()


# --------------------------------------------------------------------------- #
# HA REST API (stdlib urllib — no extra dep)
# --------------------------------------------------------------------------- #
def _api_request(cfg: HaConfig, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, dict]:
    if not cfg.url:
        raise HaSyncError("api_config", "HA_URL is not set in .env.")
    if not cfg.token:
        raise HaSyncError("api_token", "HA_TOKEN is not set in .env.")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{cfg.url}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {cfg.token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise HaSyncError("api_token", f"HA token rejected ({exc.code}) — is HA_TOKEN valid?") from exc
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return exc.code, {"error": raw}
    except urllib.error.URLError as exc:
        raise HaSyncError("api_conn", f"HA API at {cfg.url} unreachable: {exc.reason}") from exc


def conversation_process(cfg: HaConfig, text: str) -> Tuple[str, str, bool]:
    status, payload = _api_request(cfg, "POST", "/api/conversation/process",
                                   {"text": text, "language": "en"})
    if status != 200:
        raise HaSyncError("api_probe", f"conversation/process returned HTTP {status}.")
    return parse_conversation_reply(payload)


def conversation_reload(cfg: HaConfig) -> None:
    status, _ = _api_request(cfg, "POST", "/api/services/conversation/reload", {})
    if status != 200:
        raise HaSyncError("api_reload", f"conversation.reload returned HTTP {status}.")


# --------------------------------------------------------------------------- #
# TCP reachability (distinguish "UI up, SSH closed" from "VM down")
# --------------------------------------------------------------------------- #
def tcp_open(host: str, port: int, timeout: float = 4.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Reporting helpers
# --------------------------------------------------------------------------- #
def _ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def _info(msg: str) -> None:
    print(f"  [..]   {msg}")


# --------------------------------------------------------------------------- #
# Subcommand: preflight
# --------------------------------------------------------------------------- #
def cmd_preflight(cfg: HaConfig) -> int:
    print("Preflight — HA config sync readiness\n")
    failures = 0

    ui_up = tcp_open(cfg.ui_host, cfg.ui_port)
    ssh_up = tcp_open(cfg.ssh_host, cfg.ssh_port)

    if ui_up:
        _ok(f"HA UI reachable at {cfg.ui_host}:{cfg.ui_port}")
    else:
        _fail(f"HA UI NOT reachable at {cfg.ui_host}:{cfg.ui_port} — is the VM up?")
        failures += 1

    if not ssh_up:
        if ui_up:
            _fail(
                f"VM/UI reachable but SSH port {cfg.ssh_port} is CLOSED — "
                "complete the one-time Terminal & SSH add-on bootstrap "
                "(see docs/voice-pe-config/README.md)."
            )
        else:
            _fail(f"SSH port {cfg.ssh_host}:{cfg.ssh_port} closed (VM appears down).")
        # Without SSH nothing below can run; stop here.
        return 1

    _ok(f"SSH port open at {cfg.ssh_host}:{cfg.ssh_port}")

    ssh = HaSsh(cfg)
    try:
        try:
            ssh.connect()
            _ok(f"SSH auth OK as {cfg.ssh_user}")
        except HaSyncError as exc:
            _fail(exc.message)
            return 1

        if ssh.exists("/config"):
            _ok("/config is present")
        else:
            _fail("/config is MISSING — is this the Terminal & SSH add-on shell?")
            failures += 1

        if ssh.has_ha_cli():
            rc, out, _ = ssh.run("ha core check")
            _ok("ha CLI present")
            if rc == 0:
                _ok("ha core check passed")
            else:
                _fail(f"ha core check FAILED:\n{out.strip()}")
                failures += 1
        else:
            _fail("ha CLI NOT found on PATH — config check/restart unavailable.")
            failures += 1

        secrets = ssh.read(REMOTE_SECRETS)
        if secrets is None:
            _fail(f"{REMOTE_SECRETS} not readable — required secret keys cannot be verified.")
            failures += 1
        else:
            present, missing = required_secret_keys_present(secrets)
            if missing:
                _fail(f"secrets.yaml missing required key name(s): {', '.join(missing)}")
                failures += 1
            else:
                _ok(f"secrets.yaml defines required key name(s): {', '.join(present)}")
    finally:
        ssh.close()

    # API token + a read-only conversation probe.
    try:
        speech, rtype, matched = conversation_process(cfg, DEFAULT_PROBE_TEXT)
        _ok(f"HA token valid; conversation probe replied ({rtype}): {speech!r}")
        if not matched:
            _info("probe did not match a local intent — sentences may need a deploy/reload.")
    except HaSyncError as exc:
        _fail(f"[{exc.category}] {exc.message}")
        failures += 1

    print()
    if failures:
        print(f"Preflight: {failures} issue(s) found.")
        return 1
    print("Preflight: all checks passed — ready to deploy.")
    return 0


# --------------------------------------------------------------------------- #
# Subcommand: deploy
# --------------------------------------------------------------------------- #
def _plan_changes(ssh: HaSsh) -> list:
    """Return a list of (label, remote_path, new_content, old_content) for files
    whose desired content differs from what's live. Empty = nothing to do."""
    changes = []

    # custom_sentences/en/alarm.yaml — whole file
    alarm_new = ALARM_SENTENCES_FILE.read_text(encoding="utf-8")
    alarm_old = ssh.read(REMOTE_ALARM_SENTENCES) or ""
    if alarm_new != alarm_old:
        changes.append(("sentences", REMOTE_ALARM_SENTENCES, alarm_new, alarm_old))

    # configuration.yaml — managed block only
    config_old = ssh.read(REMOTE_CONFIG)
    if config_old is None:
        raise HaSyncError("config_missing", f"{REMOTE_CONFIG} not found on the VM.")
    block = build_managed_block(SNIPPET_FILE.read_text(encoding="utf-8"))
    config_new = replace_or_append_block(config_old, block)
    if config_new != config_old:
        changes.append(("config", REMOTE_CONFIG, config_new, config_old))

    return changes


def cmd_deploy(cfg: HaConfig, dry_run: bool, restart: bool) -> int:
    ssh = HaSsh(cfg)
    try:
        ssh.connect()
        changes = _plan_changes(ssh)

        if not changes:
            print("Nothing to deploy — the VM already matches the repo.")
            return 0

        labels = {c[0] for c in changes}
        print(f"Planned changes: {', '.join(sorted(labels))}\n")
        for label, path, new, old in changes:
            diff = compute_diff(old, new, path)
            print(f"--- {label}: {path} ---")
            print(diff if diff else "(no textual diff)")
            print()

        if dry_run:
            print("Dry run — no files were written.")
            return 0

        # Backup + write each changed file.
        stamp = utc_stamp()
        ssh.run(f"mkdir -p {BACKUP_DIR}")
        for label, path, new, old in changes:
            if ssh.exists(path):
                backup = backup_remote_path(path, stamp)
                ssh.write(backup, old)
                print(f"Backed up {path} -> {backup}")
            ssh.write(path, new)
            print(f"Wrote {path}")

        # Validate before any restart.
        if ssh.has_ha_cli():
            rc, out, _ = ssh.run("ha core check")
            if rc != 0:
                _fail(f"ha core check FAILED after write:\n{out.strip()}")
                print("\nThe files are deployed but invalid. Run `rollback` to restore.")
                return 1
            _ok("ha core check passed")
        else:
            _info("ha CLI unavailable — skipped config check.")

        # Reload vs restart: a configuration.yaml change needs a full restart;
        # a sentences-only change is applied with the narrow conversation.reload.
        if "config" in labels:
            if restart:
                print("\nconfiguration.yaml changed — restarting Home Assistant (--restart).")
                rc, out, _ = ssh.run("ha core restart")
                if rc != 0:
                    _fail(f"ha core restart failed:\n{out.strip()}")
                    return 1
                _ok("Home Assistant restarted.")
            else:
                print(
                    "\nconfiguration.yaml changed — a FULL HA restart is required to load "
                    "the intent_script / rest_command blocks.\n"
                    "Re-run with --restart, or restart from Settings → System."
                )
        elif "sentences" in labels:
            print("\nSentences changed — applying with conversation.reload (no restart needed).")
            try:
                conversation_reload(cfg)
                _ok("conversation.reload succeeded.")
            except HaSyncError as exc:
                _fail(f"[{exc.category}] {exc.message} (restart HA to apply.)")
                return 1
        return 0
    except HaSyncError as exc:
        _fail(f"[{exc.category}] {exc.message}")
        return 1
    finally:
        ssh.close()


# --------------------------------------------------------------------------- #
# Subcommand: rollback
# --------------------------------------------------------------------------- #
def cmd_rollback(cfg: HaConfig) -> int:
    ssh = HaSsh(cfg)
    try:
        ssh.connect()
        restored = 0
        for remote_path in (REMOTE_CONFIG, REMOTE_ALARM_SENTENCES):
            backups = ssh.list_backups(Path(remote_path).name)
            if not backups:
                _info(f"No backup found for {remote_path} — skipped.")
                continue
            latest = backups[0]
            content = ssh.read(latest)
            if content is None:
                _fail(f"Backup unreadable: {latest}")
                continue
            ssh.write(remote_path, content)
            print(f"Restored {remote_path} <- {latest}")
            restored += 1

        if not restored:
            print("Nothing restored — no backups present.")
            return 1

        if ssh.has_ha_cli():
            rc, out, _ = ssh.run("ha core check")
            if rc == 0:
                _ok("ha core check passed after rollback.")
            else:
                _fail(f"ha core check FAILED after rollback:\n{out.strip()}")
                return 1
        print("\nRollback complete. Restart HA if a configuration.yaml change was reverted.")
        return 0
    except HaSyncError as exc:
        _fail(f"[{exc.category}] {exc.message}")
        return 1
    finally:
        ssh.close()


# --------------------------------------------------------------------------- #
# Subcommand: probe
# --------------------------------------------------------------------------- #
def cmd_probe(cfg: HaConfig, text: str, actuate: bool) -> int:
    if text != DEFAULT_PROBE_TEXT and not actuate:
        _fail(
            f"Custom probe text {text!r} may actuate a device. Re-run with --actuate "
            "to confirm you accept it firing for real."
        )
        return 1
    try:
        speech, rtype, matched = conversation_process(cfg, text)
    except HaSyncError as exc:
        _fail(f"[{exc.category}] {exc.message}")
        return 1
    print(f"Probe text : {text!r}")
    print(f"Reply type : {rtype}")
    print(f"Spoken     : {speech!r}")
    print(f"Matched local intent: {'yes' if matched else 'no (fell through to the LLM?)'}")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ha_config_sync",
        description="Deploy this repo's Home Assistant voice-PE config over SSH (#243).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("preflight", help="report connectivity + readiness (no writes)")

    d = sub.add_parser("deploy", help="push repo-owned files + the managed config block")
    d.add_argument("--dry-run", action="store_true", help="show a diff; write nothing")
    d.add_argument("--restart", action="store_true",
                   help="perform the full HA restart a config change requires")

    sub.add_parser("rollback", help="restore the most recent backup + recheck")

    pr = sub.add_parser("probe", help="run an HA conversation text probe (read-only default)")
    pr.add_argument("--text", default=DEFAULT_PROBE_TEXT, help="phrase to send")
    pr.add_argument("--actuate", action="store_true",
                    help="acknowledge a non-default phrase may actuate a device")
    return p


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # paramiko's transport logs (Connected / Authentication / sftp) are noise here.
    logging.getLogger("paramiko").setLevel(logging.WARNING)
    args = build_parser().parse_args(argv)
    cfg = load_config()

    if args.command == "preflight":
        return cmd_preflight(cfg)
    if args.command == "deploy":
        return cmd_deploy(cfg, dry_run=args.dry_run, restart=args.restart)
    if args.command == "rollback":
        return cmd_rollback(cfg)
    if args.command == "probe":
        return cmd_probe(cfg, text=args.text, actuate=args.actuate)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
