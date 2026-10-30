"""The periodic ``threads=… handles=… rss=…`` breadcrumb."""

from __future__ import annotations

import asyncio
import logging
import re

import pytest

from app.webapp import process_stats

_FRAGMENT = re.compile(r"threads=\d+ handles=(\d+|\?) rss=(\d+MB|\?)")


def test_describe_process_reports_all_three_counts() -> None:
    assert _FRAGMENT.fullmatch(process_stats.describe_process())


def test_describe_process_survives_an_unreadable_process_api(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise OSError("no psapi")

    monkeypatch.setattr(process_stats.sys, "platform", "win32")
    monkeypatch.setattr(process_stats, "_windows_handles_and_rss", boom)

    assert re.fullmatch(r"threads=\d+ handles=\? rss=\?", process_stats.describe_process())


def test_interval_zero_starts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_stats, "load_dotenv", lambda **_: None)
    monkeypatch.setenv("PROCESS_STATS_INTERVAL_S", "0")

    async def scenario() -> None:
        assert process_stats.start_process_stats() is None

    asyncio.run(scenario())


def test_task_logs_the_line_at_info(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(process_stats, "load_dotenv", lambda **_: None)
    monkeypatch.setenv("PROCESS_STATS_INTERVAL_S", "300")

    async def scenario() -> None:
        task = process_stats.start_process_stats()
        assert task is not None
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with caplog.at_level(logging.INFO, logger=process_stats.logger.name):
        asyncio.run(scenario())

    assert any(
        r.levelno == logging.INFO and "Process health" in r.getMessage() and _FRAGMENT.search(r.getMessage())
        for r in caplog.records
    )
