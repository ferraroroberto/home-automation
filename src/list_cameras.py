r"""
Show configured cameras (CLI / probe)
======================================
Read-only probe for the camera fleet (issue #161): confirm each camera in
``config/cameras.json`` is reachable over ONVIF and report its model + PTZ
capability, mirroring ``list_devices.py`` / ``list_elgato_lights.py``.

Run from the project root with the venv interpreter::

    & .\.venv\Scripts\python.exe -m src.list_cameras                  # Windows
    ./.venv/bin/python -m src.list_cameras                            # POSIX
"""

from __future__ import annotations

import asyncio
import logging

from src.camera_client import (
    CameraConfigError,
    CameraInfo,
    close_all,
    fetch_cameras,
    load_cameras,
)
from src.camera_display_names import load_camera_display_names


def _print_camera(cam: CameraInfo, label: str) -> None:
    print(f"{label} ({cam.id} @ {cam.host})")
    if not cam.reachable:
        print(f"  Unavailable: {cam.error or 'unknown error'}")
        return
    print(f"  Model:    {cam.manufacturer or '?'} {cam.model or '?'}")
    print(f"  Firmware: {cam.firmware or '?'}")
    print(f"  PTZ:      {'yes' if cam.ptz_capable else 'no'}")


async def _main() -> int:
    try:
        if not load_cameras():
            print(
                "No cameras configured. Copy config/cameras.sample.json to "
                "config/cameras.json and fill in your cameras."
            )
            return 1
        names = load_camera_display_names()
        cameras = await fetch_cameras()
    except CameraConfigError as exc:
        print(f"Cameras unavailable: {exc}")
        return 2

    try:
        for idx, cam in enumerate(cameras):
            if idx:
                print()
            _print_camera(cam, names.get(cam.id) or cam.id)
    finally:
        await close_all()
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
