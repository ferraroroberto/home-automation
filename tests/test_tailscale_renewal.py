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
