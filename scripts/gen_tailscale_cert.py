"""Provision a Tailscale HTTPS certificate (real Let's Encrypt) for this machine.

This is the HTTPS-provisioning path for the webapp: a real Let's Encrypt leaf
issued for the tailnet MagicDNS name, so every device already on the tailnet
trusts it with **zero** per-device steps — no CA install, no mobileconfig, no
Certificate Trust toggle. It replaces the old self-signed-CA dance.

Output goes to ``webapp/certificates/`` (the dir uvicorn already reads):

    cert.pem    server cert  (uvicorn --ssl-certfile)
    key.pem     server key   (uvicorn --ssl-keyfile)

Prerequisites:
  1. Enable HTTPS in the Tailscale admin console (one-time, per tailnet):
     https://login.tailscale.com/admin/dns  (scroll to "HTTPS Certificates")
  2. tailscale must be running and authenticated on this machine.

Usage:
    # Provision or force-renew (auto-detects the MagicDNS name):
    & .\\.venv\\Scripts\\python.exe scripts\\gen_tailscale_cert.py
    & .\\.venv\\Scripts\\python.exe scripts\\gen_tailscale_cert.py tower.tail1121fd.ts.net

    # Check and auto-renew if expiring within 30 days (run by webapp.bat /
    # the tray on startup so a stale cert self-heals before uvicorn binds):
    & .\\.venv\\Scripts\\python.exe scripts\\gen_tailscale_cert.py --check
"""
from __future__ import annotations

import datetime
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CERT_DIR = PROJECT_ROOT / "webapp" / "certificates"
RENEW_WITHIN_DAYS = 30


def _tailscale_hostname() -> str:
    result = subprocess.run(
        ["tailscale", "status", "--json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit("tailscale status failed. Is tailscale running?")
    data = json.loads(result.stdout)
    name = data.get("Self", {}).get("DNSName", "").rstrip(".")
    if not name:
        raise SystemExit("Could not detect Tailscale hostname from 'tailscale status'.")
    return name


def _tailscale_hostname_from_cert(cert_path: Path) -> Optional[str]:
    """Return the .ts.net DNS SAN from the cert, or None if not a Tailscale cert."""
    try:
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        for name in san.value.get_values_for_type(x509.DNSName):
            if ".ts.net" in name:
                return name
    except Exception:
        pass
    return None


def _expiring_within(cert_path: Path, days: int) -> bool:
    """Return True if the cert expires within `days` days."""
    try:
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        try:
            expiry = cert.not_valid_after_utc
        except AttributeError:  # cryptography < 42
            expiry = cert.not_valid_after.replace(tzinfo=datetime.timezone.utc)
        threshold = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)
        return expiry < threshold
    except Exception:
        return False


def _provision(hostname: str) -> None:
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    cert_path = CERT_DIR / "cert.pem"
    key_path = CERT_DIR / "key.pem"

    result = subprocess.run(
        [
            "tailscale", "cert",
            "--cert-file", str(cert_path),
            "--key-file", str(key_path),
            hostname,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        msg = (result.stderr or result.stdout).strip()
        print(msg)
        raise SystemExit(
            "\ntailscale cert failed.\n"
            "Make sure HTTPS certificates are enabled in the Tailscale admin console:\n"
            "  https://login.tailscale.com/admin/dns"
        )

    print(f"[OK] cert.pem -> {cert_path}")
    print(f"[OK] key.pem  -> {key_path}")


def _check_and_renew() -> None:
    """Renew the cert if it is a Tailscale cert expiring within RENEW_WITHIN_DAYS days.
    Always exits cleanly — startup must not be blocked by cert errors."""
    cert_path = CERT_DIR / "cert.pem"
    if not cert_path.exists():
        return

    hostname = _tailscale_hostname_from_cert(cert_path)
    if hostname is None:
        return  # self-signed / non-tailnet cert; leave it alone

    if not _expiring_within(cert_path, RENEW_WITHIN_DAYS):
        return

    print(f"[INFO] Tailscale cert for {hostname} expires within {RENEW_WITHIN_DAYS} days — renewing.")
    if shutil.which("tailscale") is None:
        print("[WARN] tailscale not found on PATH; skipping cert renewal.")
        return
    try:
        _provision(hostname)
        print("[OK] Tailscale cert renewed.")
    except SystemExit as exc:
        print(f"[WARN] Cert renewal failed: {exc}")


def main() -> None:
    args = sys.argv[1:]

    if args and args[0] == "--check":
        _check_and_renew()
        return

    if shutil.which("tailscale") is None:
        raise SystemExit("tailscale not found on PATH.")

    hostname = args[0] if args else _tailscale_hostname()
    print(f"Provisioning Tailscale cert for: {hostname}")
    _provision(hostname)
    print()
    print("Restart the app (tray.bat --restart, or webapp.bat), then open:")
    print(f"  https://{hostname}:8447")
    print()
    print("Note: https://localhost:8447 will show a cert hostname-mismatch warning")
    print("because this cert is issued only for the Tailscale domain.")
    print("Use http://localhost:8447 for plain local desktop access.")


if __name__ == "__main__":
    main()
