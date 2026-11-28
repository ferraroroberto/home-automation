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


class _FakeWinApi:
    """``kernel32`` and ``psapi`` in one: fills the out-parameters, or refuses both reads."""

    def __init__(self, *, handles: int = 0, working_set: int = 0, refuse: bool = False) -> None:
        def get_current_process() -> int:
            return -1

        def get_process_handle_count(_process, count_ref) -> int:
            if refuse:
                return 0
            count_ref._obj.value = handles
            return 1

        def get_process_memory_info(_process, counters_ref, _size) -> int:
            if refuse:
                return 0
            counters_ref._obj.WorkingSetSize = working_set
            return 1

        # Plain functions, so the probe can set ``restype`` / ``argtypes`` on them.
        self.GetCurrentProcess = get_current_process
        self.GetProcessHandleCount = get_process_handle_count
        self.GetProcessMemoryInfo = get_process_memory_info


@pytest.mark.parametrize(
    ("api", "expected"),
    [
        (_FakeWinApi(handles=612, working_set=190 * 1024 * 1024), "handles=612 rss=190MB"),
        (_FakeWinApi(refuse=True), "handles=? rss=?"),
    ],
    ids=["readable", "refused"],
)
def test_win32_handles_and_rss_come_from_the_process_apis(
    monkeypatch: pytest.MonkeyPatch, api: _FakeWinApi, expected: str
) -> None:
    loaded: list[str] = []

    def load(name: str, **_kwargs) -> _FakeWinApi:
        loaded.append(name)
        return api

    monkeypatch.setattr(process_stats.sys, "platform", "win32")
    # ``ctypes.WinDLL`` exists only on Windows; the fake stands in on every host.
    monkeypatch.setattr(process_stats.ctypes, "WinDLL", load, raising=False)

    assert process_stats.describe_process().endswith(expected)
    assert loaded == ["kernel32", "psapi"]


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_macos_and_linux_report_unknown_without_loading_a_windows_dll(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    loaded: list[str] = []
    monkeypatch.setattr(process_stats.sys, "platform", platform)
    monkeypatch.setattr(process_stats.ctypes, "WinDLL", lambda name, **_: loaded.append(name), raising=False)

    assert re.fullmatch(r"threads=\d+ handles=\? rss=\?", process_stats.describe_process())
    assert loaded == []


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
