"""A due-but-failed Tailscale cert renewal must be visible (issue #914).

Before this fix, ``gen_tailscale_cert.py --check`` always exited 0 and
``WebappManager._renew_tailscale_cert`` discarded both the return code and
the ``[WARN]``/stdout output of that subprocess — so under the ``pythonw``
tray a renewal that never happened left no trace anywhere until the
certificate actually expired. These tests pin: (1) ``_check_and_renew()``
returns ``False`` exactly when a due renewal didn't happen, never raises,
and (2) the manager logs a warning with the exit code and output when the
subprocess it spawns comes back non-zero.
"""

from __future__ import annotations

import logging
import subprocess
import sys

import pytest

from app.webapp import manager
from scripts import gen_tailscale_cert


# --------------------------------------------------------------------------- #
# scripts/gen_tailscale_cert.py::_check_and_renew                             #
# --------------------------------------------------------------------------- #
def _due_cert(tmp_path, monkeypatch) -> None:
    """Point CERT_DIR at a fake, existing, due-for-renewal Tailscale cert."""
    cert_dir = tmp_path / "certificates"
    cert_dir.mkdir()
    (cert_dir / "cert.pem").write_bytes(b"fake")
    monkeypatch.setattr(gen_tailscale_cert, "CERT_DIR", cert_dir)
    monkeypatch.setattr(
        gen_tailscale_cert, "_tailscale_hostname_from_cert", lambda _p: "tower.example.ts.net"
    )
    monkeypatch.setattr(gen_tailscale_cert, "_expiring_within", lambda _p, _d: True)


def test_check_and_renew_true_when_nothing_due(tmp_path, monkeypatch) -> None:
    cert_dir = tmp_path / "certificates"
    monkeypatch.setattr(gen_tailscale_cert, "CERT_DIR", cert_dir)  # cert.pem absent

    assert gen_tailscale_cert._check_and_renew() is True


def test_check_and_renew_false_when_due_and_binary_missing(tmp_path, monkeypatch) -> None:
    _due_cert(tmp_path, monkeypatch)
    monkeypatch.setattr(gen_tailscale_cert, "tailscale_binary", lambda: None)

    assert gen_tailscale_cert._check_and_renew() is False


def test_check_and_renew_false_when_provision_fails(tmp_path, monkeypatch) -> None:
    _due_cert(tmp_path, monkeypatch)
    monkeypatch.setattr(gen_tailscale_cert, "tailscale_binary", lambda: "tailscale")

    def _fail_provision(_hostname, _binary):
        raise SystemExit("tailscale cert failed")

    monkeypatch.setattr(gen_tailscale_cert, "_provision", _fail_provision)

    assert gen_tailscale_cert._check_and_renew() is False


def test_check_and_renew_true_when_provision_succeeds(tmp_path, monkeypatch) -> None:
    _due_cert(tmp_path, monkeypatch)
    monkeypatch.setattr(gen_tailscale_cert, "tailscale_binary", lambda: "tailscale")
    monkeypatch.setattr(gen_tailscale_cert, "_provision", lambda _h, _b: None)

    assert gen_tailscale_cert._check_and_renew() is True


def test_main_check_exits_nonzero_when_renewal_due_and_failed(monkeypatch) -> None:
    monkeypatch.setattr(gen_tailscale_cert.sys, "argv", ["gen_tailscale_cert.py", "--check"])
    monkeypatch.setattr(gen_tailscale_cert, "_check_and_renew", lambda: False)

    try:
        gen_tailscale_cert.main()
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("main() did not call sys.exit")


# --------------------------------------------------------------------------- #
# app/webapp/manager.py::_renew_tailscale_cert                               #
# --------------------------------------------------------------------------- #
def test_renew_tailscale_cert_logs_warning_on_nonzero_exit(tmp_path, monkeypatch, caplog) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "gen_tailscale_cert.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(manager, "PROJECT_ROOT", tmp_path)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 1, stdout="[WARN] tailscale not found; skipping cert renewal.\n", stderr=""
        )

    monkeypatch.setattr(manager.subprocess, "run", fake_run)

    with caplog.at_level(logging.WARNING, logger=manager.logger.name):
        manager._renew_tailscale_cert()

    assert any(
        "Tailscale cert renewal failed" in r.message and "exit 1" in r.message
        for r in caplog.records
    )
    assert any("not found" in r.message for r in caplog.records)


def test_renew_tailscale_cert_silent_when_exit_zero(tmp_path, monkeypatch, caplog) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "gen_tailscale_cert.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(manager, "PROJECT_ROOT", tmp_path)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(manager.subprocess, "run", fake_run)

    with caplog.at_level(logging.WARNING, logger=manager.logger.name):
        manager._renew_tailscale_cert()

    assert not any("Tailscale cert renewal failed" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# An unreadable cert is "unknown", never "not expiring"                       #
# --------------------------------------------------------------------------- #
def _write_cert(path, *, dns_names, days_valid):
    """Write a throwaway self-signed cert with the given SAN DNS names and lifetime."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=days_valid))
    )
    if dns_names:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(n) for n in dns_names]), critical=False
        )
    cert = builder.sign(key, hashes.SHA256())
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _cert_dir(tmp_path, monkeypatch):
    cert_dir = tmp_path / "certificates"
    cert_dir.mkdir()
    monkeypatch.setattr(gen_tailscale_cert, "CERT_DIR", cert_dir)
    return cert_dir / "cert.pem"


def test_check_and_renew_false_and_warns_when_cert_is_corrupt(tmp_path, monkeypatch, capsys) -> None:
    cert = _cert_dir(tmp_path, monkeypatch)
    cert.write_bytes(b"-----BEGIN CERTIFICATE-----\nnot a certificate\n-----END CERTIFICATE-----\n")

    assert gen_tailscale_cert._check_and_renew() is False
    assert "expiry is unknown" in capsys.readouterr().out


def test_check_and_renew_false_when_cryptography_is_missing(tmp_path, monkeypatch, capsys) -> None:
    cert = _cert_dir(tmp_path, monkeypatch)
    _write_cert(cert, dns_names=["tower.example.ts.net"], days_valid=60)
    monkeypatch.setitem(sys.modules, "cryptography", None)  # makes `import cryptography` raise ImportError
    monkeypatch.setitem(sys.modules, "cryptography.x509", None)

    assert gen_tailscale_cert._check_and_renew() is False
    assert "expiry is unknown" in capsys.readouterr().out


def test_main_check_exits_nonzero_when_cert_is_unreadable(tmp_path, monkeypatch) -> None:
    cert = _cert_dir(tmp_path, monkeypatch)
    cert.write_bytes(b"garbage")
    monkeypatch.setattr(gen_tailscale_cert.sys, "argv", ["gen_tailscale_cert.py", "--check"])

    with pytest.raises(SystemExit) as exc:
        gen_tailscale_cert.main()
    assert exc.value.code == 1


def test_check_and_renew_true_for_healthy_tailnet_cert(tmp_path, monkeypatch) -> None:
    cert = _cert_dir(tmp_path, monkeypatch)
    _write_cert(cert, dns_names=["tower.example.ts.net"], days_valid=60)

    assert gen_tailscale_cert._check_and_renew() is True


def test_check_and_renew_true_for_readable_non_tailnet_cert(tmp_path, monkeypatch) -> None:
    cert = _cert_dir(tmp_path, monkeypatch)
    _write_cert(cert, dns_names=None, days_valid=1)  # no SAN at all: self-signed, left alone

    assert gen_tailscale_cert._check_and_renew() is True


def test_check_and_renew_attempts_renewal_for_expiring_tailnet_cert(tmp_path, monkeypatch) -> None:
    cert = _cert_dir(tmp_path, monkeypatch)
    _write_cert(cert, dns_names=["tower.example.ts.net"], days_valid=5)
    monkeypatch.setattr(gen_tailscale_cert, "tailscale_binary", lambda: None)

    assert gen_tailscale_cert._check_and_renew() is False  # due, and no binary to renew with
