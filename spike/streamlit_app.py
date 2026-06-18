r"""
Streamlit control UI — POC SPIKE (not the product)
===================================================
A lightweight, throwaway way to eyeball the live MELCloud Home data and
poke a single unit's controls (power, operation mode, target temperature,
fan speed). It is **independent** from the product: the real control
surface is the FastAPI + PWA webapp under ``app/webapp/``. This spike
shares nothing with it beyond :mod:`src.melcloud_client`, and is kept
around only as a fast data/debug view.

Run with::

    .\launch_app.bat                                                   # Windows
    & .\.venv\Scripts\python.exe -m streamlit run spike/streamlit_app.py  # Windows (direct)
    ./.venv/bin/python -m streamlit run spike/streamlit_app.py            # POSIX

All MELCloud logic lives in ``src/`` — this file only renders + dispatches.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Ensure project root is on sys.path so ``src.*`` imports resolve.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st

from src.melcloud_client import (
    DeviceInfo,
    MelCloudConfigError,
    fetch_devices,
    set_device_state,
)

st.set_page_config(
    page_title="Home Automation — MELCloud Home",
    page_icon="🌡",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("🌡 MELCloud Home control")
st.caption(
    "Read and control the Mitsubishi Electric units on your MELCloud Home "
    "account. Proof-of-concept ahead of solar load-balancing automation."
)

_DEFAULT_RANGE = (16.0, 31.0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_devices() -> None:
    """Fetch live state and stash it in session_state."""
    try:
        st.session_state.devices = asyncio.run(fetch_devices())
        st.session_state.load_error = None
    except MelCloudConfigError as exc:
        st.session_state.devices = None
        st.session_state.load_error = str(exc)
    except Exception as exc:  # noqa: BLE001 — surface any API/network error
        st.session_state.devices = None
        st.session_state.load_error = f"Failed to fetch units: {exc}"


def _replace_device(updated: DeviceInfo) -> None:
    """Swap a freshly-written snapshot back into the cached unit list."""
    devices = st.session_state.get("devices") or []
    st.session_state.devices = [
        updated if d.unit_id == updated.unit_id else d for d in devices
    ]


def _power_label(value: bool | None) -> str:
    if value is None:
        return "n/a"
    return "ON" if value else "OFF"


def _temp_range(device: DeviceInfo) -> tuple[float, float]:
    """(min, max) target range for the unit's current mode, with fallbacks."""
    rng = device.temp_ranges.get(device.operation_mode or "")
    if rng is None and device.temp_ranges:
        rng = next(iter(device.temp_ranges.values()))
    return rng or _DEFAULT_RANGE


# ---------------------------------------------------------------------------
# Sidebar — connection
# ---------------------------------------------------------------------------
with st.sidebar:
    st.subheader("Connection")
    if st.button("🔄 Fetch / refresh units", type="primary", key="fetch_devices"):
        _load_devices()
    st.caption("Each action re-authenticates with MELCloud Home.")

if "devices" not in st.session_state:
    st.session_state.devices = None
    st.session_state.load_error = None

if st.session_state.get("load_error"):
    st.error(st.session_state.load_error)

devices = st.session_state.get("devices")

if not devices:
    st.info("Press **Fetch / refresh units** in the sidebar to connect.")
    st.stop()

# ---------------------------------------------------------------------------
# Unit selection
# ---------------------------------------------------------------------------
labels = [f"{d.building} — {d.name}" for d in devices]
choice = st.selectbox("Unit", options=range(len(devices)),
                      format_func=lambda i: labels[i], key="device_select")
device = devices[choice]

# ---------------------------------------------------------------------------
# Current state
# ---------------------------------------------------------------------------
st.subheader("Current state")
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Room", f"{device.room_temperature:.1f} °C"
          if device.room_temperature is not None else "n/a")
c2.metric("Target", f"{device.set_temperature:.1f} °C"
          if device.set_temperature is not None else "n/a")
c3.metric("Mode", device.operation_mode or "n/a")
c4.metric("Power", _power_label(device.power))
c5.metric("Fan", device.fan_speed or "n/a")

# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------
st.subheader("Controls")

modes = device.operation_modes or ([device.operation_mode] if device.operation_mode else [])
fan_speeds = device.fan_speeds or ([device.fan_speed] if device.fan_speed else [])

tmin, tmax = _temp_range(device)
tmin, tmax = float(tmin), float(tmax)
step = float(device.temp_step) or 0.5
if tmax <= tmin:  # guard against a degenerate range
    tmax = tmin + step
current_target = device.set_temperature if device.set_temperature else tmin
current_target = min(max(float(current_target), tmin), tmax)

with st.form("controls", border=True):
    power = st.toggle("Power", value=bool(device.power), key="ctl_power")

    mode = device.operation_mode
    if modes:
        mode_index = modes.index(device.operation_mode) if device.operation_mode in modes else 0
        mode = st.selectbox("Operation mode", options=modes, index=mode_index, key="ctl_mode")

    target = st.slider(
        "Target temperature (°C)",
        min_value=tmin, max_value=tmax, value=current_target, step=step,
        key="ctl_target",
        help="Bounds reflect the unit's current operation mode. Re-fetch "
             "after switching modes to update them.",
    )

    fan = device.fan_speed
    if fan_speeds:
        fan_index = fan_speeds.index(device.fan_speed) if device.fan_speed in fan_speeds else 0
        fan = st.selectbox("Fan speed", options=fan_speeds, index=fan_index, key="ctl_fan")

    submitted = st.form_submit_button("✅ Apply", type="primary")

if submitted:
    try:
        with st.spinner("Sending command to MELCloud Home…"):
            updated = asyncio.run(
                set_device_state(
                    device.unit_id,
                    power=power,
                    operation_mode=mode if modes else None,
                    set_temperature=float(target),
                    fan_speed=fan if fan_speeds else None,
                )
            )
        _replace_device(updated)
        st.success(
            f"Applied — power {_power_label(updated.power)}, "
            f"mode {updated.operation_mode}, target "
            f"{updated.set_temperature} °C, fan {updated.fan_speed or 'n/a'}."
        )
        st.rerun()
    except Exception as exc:  # noqa: BLE001 — surface any API/network error
        st.error(f"Failed to apply: {exc}")

st.caption(
    "Note: while a unit is OFF, MELCloud Home may defer mode/temperature "
    "changes until it is powered on. Re-fetch to confirm the live state."
)
