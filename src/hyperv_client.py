"""Hyper-V VM status + lifecycle control for the Home Assistant VM (issue #240).

UI-free core. Shells out to the Hyper-V PowerShell cmdlets (``Get-VM`` /
``Start-VM`` / ``Stop-VM``) for **exactly one** VM named by ``HA_VM_NAME`` and
flattens the result into a frozen :class:`HyperVState`. Mirrors
``src/ups_client.py``'s "shell out to a local Windows tool, parse
``ConvertTo-Json``, flatten" shape, including the ``CREATE_NO_WINDOW`` guard so
no console window pops on each Home-view poll.

The VM is always addressed **by name** — this never enumerates or acts on any
other VM (the host also runs WSL2's hidden utility VM, deliberately out of scope
per issue #240). The VM name is passed to the child process via an environment
variable, never string-interpolated into the script, so a name can't inject
PowerShell.

Status reads degrade gracefully: a powered-off VM, a missing name, or an
unprivileged read return an ``available=False`` :class:`HyperVState` with a
distinct ``error`` rather than raising — matching the UPS/network "partial data
stays 200" idiom. The state-changing ``start_vm`` / ``stop_vm`` raise distinct
exceptions instead, so the router can map each cause to its own HTTP status.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from dotenv import load_dotenv

logger = logging.getLogger("hyperv")

load_dotenv()

# Hide the console window each subprocess would otherwise pop on Windows (these
# run on every Home-view poll). No-op off Windows. Mirrors ``ups_client``.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0

# The env var naming the single VM this module manages.
_VM_NAME_ENV = "HA_VM_NAME"


class HyperVConfigError(RuntimeError):
    """``HA_VM_NAME`` is unset/empty — the core can't know which VM to target."""


class HyperVNotFoundError(RuntimeError):
    """No VM with the configured name exists on this host."""


class HyperVPermissionError(RuntimeError):
    """The current user lacks the Hyper-V rights to run the requested cmdlet."""


class HyperVStateError(RuntimeError):
    """The VM is already in the requested state (start-when-on / stop-when-off)."""


class HyperVCommandError(RuntimeError):
    """A Hyper-V cmdlet failed for some other reason."""


@dataclass(frozen=True)
class HyperVState:
    """Flattened Hyper-V VM status for the dashboard."""

    available: bool
    name: Optional[str] = None
    state: str = "unknown"
    uptime_seconds: Optional[int] = None
    ip_address: Optional[str] = None
    mac_address: Optional[str] = None
    error: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# PowerShell scripts (the VM name is read from $env:HA_VM_NAME, never inlined)  #
# --------------------------------------------------------------------------- #
_STATUS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$name = $env:HA_VM_NAME
$vm = Get-VM -ComputerName localhost -Name $name
$net = Get-VMNetworkAdapter -VM $vm -ErrorAction SilentlyContinue
$ipv4 = $null
$mac = $null
if ($net) {
  $first = $net | Select-Object -First 1
  $mac = $first.MacAddress
  $ips = @($net | ForEach-Object { $_.IPAddresses } | Where-Object { $_ -and ($_ -notmatch ':') })
  if ($ips.Count -gt 0) { $ipv4 = $ips[0] }
}
[pscustomobject]@{
  Name = $vm.Name
  State = [string]$vm.State
  UptimeSeconds = [int]$vm.Uptime.TotalSeconds
  Ip = $ipv4
  Mac = $mac
} | ConvertTo-Json -Compress
"""

# ``Start-VM`` on a running VM is a silent no-op (so "already running" can't come
# from it — we pre-check). ``Stop-VM -Force`` is a graceful ACPI shutdown without
# the confirmation prompt — NOT a hard power-off (that is ``-TurnOff``, never used).
_START_SCRIPT = "$ErrorActionPreference='Stop'; Start-VM -ComputerName localhost -Name $env:HA_VM_NAME"
_STOP_SCRIPT = "$ErrorActionPreference='Stop'; Stop-VM -ComputerName localhost -Name $env:HA_VM_NAME -Force"


def fetch_hyperv_state() -> HyperVState:
    """Read the configured VM's live status. Raises only :class:`HyperVConfigError`.

    Any other failure (VM not found, unprivileged read, Hyper-V module absent,
    VM powered off) returns an ``available=False`` state carrying a distinct
    ``error`` — never a raise — so the Home card can render a useful message.
    """
    name = vm_name()  # HyperVConfigError propagates → router maps to 503
    result = _run(_STATUS_SCRIPT, name)
    if result.returncode == 0 and result.stdout.strip():
        try:
            return parse_vm_status(result.stdout.strip(), name=name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️  Failed to parse Hyper-V status: %s", exc)
            return HyperVState(
                available=False, name=name,
                error=f"could not parse VM status: {exc}", updated_at=_now(),
            )

    err = (result.stderr or result.stdout or "Get-VM failed").strip()
    category = classify_powershell_error(err)
    if category == "not_found":
        return HyperVState(
            available=False, name=name, state="not_found",
            error=f"VM '{name}' was not found on this host.", updated_at=_now(),
        )
    if category == "permission":
        return HyperVState(
            available=False, name=name,
            error=(
                "Insufficient Hyper-V rights to read VM status — add the webapp "
                "user to the local 'Hyper-V Administrators' group."
            ),
            updated_at=_now(),
        )
    logger.warning("⚠️  Hyper-V status read failed: %s", err)
    return HyperVState(available=False, name=name, error=_short(err), updated_at=_now())


def start_vm() -> HyperVState:
    """Power the VM on (graceful boot). Returns the read-back state."""
    return _set_power(start=True)


def stop_vm() -> HyperVState:
    """Gracefully shut the VM down (ACPI, never a hard power-off). Returns state."""
    return _set_power(start=False)


def _set_power(*, start: bool) -> HyperVState:
    name = vm_name()
    # Pre-check so "already in that state" is a deterministic signal: ``Start-VM``
    # on a running VM succeeds silently, so the cmdlet can't tell us. When the read
    # itself fails we skip the guard and let the action cmdlet return the
    # authoritative (e.g. permission) error, which is classified below.
    current = fetch_hyperv_state()
    if current.available:
        running = current.state == "running"
        if start and running:
            raise HyperVStateError(f"VM '{name}' is already running.")
        if not start and not running:
            raise HyperVStateError(f"VM '{name}' is already stopped.")
    elif current.state == "not_found":
        raise HyperVNotFoundError(f"VM '{name}' was not found on this host.")

    script = _START_SCRIPT if start else _STOP_SCRIPT
    # A graceful stop waits for the guest OS to come down, so allow generous time.
    result = _run(script, name, timeout=120)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "cmdlet failed").strip()
        category = classify_powershell_error(err)
        if category == "not_found":
            raise HyperVNotFoundError(f"VM '{name}' was not found on this host.")
        if category == "permission":
            raise HyperVPermissionError(
                "Insufficient Hyper-V rights — add the webapp user to the local "
                "'Hyper-V Administrators' group to start or stop the VM."
            )
        logger.warning("⚠️  Hyper-V %s failed: %s", "start" if start else "stop", err)
        raise HyperVCommandError(_short(err))
    return fetch_hyperv_state()


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested without a VM)                                      #
# --------------------------------------------------------------------------- #
def vm_name() -> str:
    """The configured VM name, or raise :class:`HyperVConfigError` if unset."""
    name = (os.getenv(_VM_NAME_ENV) or "").strip()
    if not name:
        raise HyperVConfigError(
            f"{_VM_NAME_ENV} is not set — add the Hyper-V VM name to .env."
        )
    return name


def parse_vm_status(raw: str, name: Optional[str] = None) -> HyperVState:
    """Parse the ``ConvertTo-Json`` status payload into a :class:`HyperVState`."""
    payload = json.loads(raw)
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    return HyperVState(
        available=True,
        name=_clean(payload.get("Name")) or name,
        state=_normalize_state(payload.get("State")),
        uptime_seconds=_to_int(payload.get("UptimeSeconds")),
        ip_address=_clean(payload.get("Ip")),
        mac_address=_format_mac(payload.get("Mac")),
        updated_at=_now(),
    )


def classify_powershell_error(text: str) -> str:
    """Bucket a cmdlet's stderr into ``not_found`` / ``permission`` / ``unknown``."""
    low = (text or "").lower()
    if any(
        s in low
        for s in (
            "unable to find a virtual machine",
            "find a virtual machine with name",
            "no virtual machine",
        )
    ):
        return "not_found"
    if any(
        s in low
        for s in (
            "access denied",
            "access is denied",
            "do not have the required permission",
            "not authorized",
            "hyper-v administrators",
        )
    ):
        return "permission"
    return "unknown"


def _normalize_state(raw: object) -> str:
    """Lower-case the Hyper-V ``State`` enum (Running/Off/Saved/…) for the UI."""
    text = str(raw or "").strip().lower()
    return text or "unknown"


def _format_mac(raw: object) -> Optional[str]:
    """Colon-format a Hyper-V MAC (``00155D012A0B``); drop the all-zero placeholder.

    A dynamic-MAC adapter reads ``000000000000`` until the VM has booted once, so
    that is treated as "no MAC yet" rather than displayed.
    """
    s = str(raw or "").strip().replace(":", "").replace("-", "").upper()
    if len(s) != 12 or s == "000000000000":
        return None
    return ":".join(s[i : i + 2] for i in range(0, 12, 2))


def _clean(value: object) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_int(value: object) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(round(float(str(value).strip())))
    except (TypeError, ValueError):
        return None


def _short(text: str, limit: int = 200) -> str:
    return " ".join((text or "").split())[:limit]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _powershell() -> str:
    return shutil.which("powershell.exe") or (
        "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    )


def _run(script: str, name: str, timeout: int = 15) -> "subprocess.CompletedProcess[str]":
    env = os.environ.copy()
    env[_VM_NAME_ENV] = name  # explicit so the child never depends on inherited env
    return subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-Command", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=_NO_WINDOW,
        env=env,
    )
