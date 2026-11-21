"""Unit tests for :mod:`src.network.lan_discovery` — the shared host-list parser."""

from __future__ import annotations

import pytest

from src.network.lan_discovery import parse_host_port


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("192.168.1.5", ("192.168.1.5", 80)),
        ("192.168.1.5:9123", ("192.168.1.5", 9123)),
        ("http://meter.local/", ("meter.local", 80)),
        ("https://meter.local:8443/x/y", ("meter.local", 8443)),
        ("  host  ", ("host", 80)),
        ("host:notaport", ("host:notaport", 80)),
    ],
)
def test_parse_host_port(raw: str, expected: tuple[str, int]) -> None:
    assert parse_host_port(raw, 80) == expected


@pytest.mark.parametrize("raw", ["", "   ", "http://", ":80"])
def test_parse_host_port_rejects_blank(raw: str) -> None:
    assert parse_host_port(raw, 80) is None
