"""Shared paths and build identity used by more than one router module."""

from __future__ import annotations

from pathlib import Path

from src.static_versioning import BuildInfo

# app/webapp/routers/_helpers.py → parents[3] is the repo root.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"

# Build identity, computed once at import — the tray restarts on every
# code edit, so a fresh process always reflects the deployed code.
BUILD_INFO = BuildInfo(STATIC_DIR, PROJECT_ROOT)
