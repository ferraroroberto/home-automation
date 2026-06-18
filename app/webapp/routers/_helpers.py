"""Shared paths used by more than one router module."""

from __future__ import annotations

from pathlib import Path

# app/webapp/routers/_helpers.py → parents[3] is the repo root.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
