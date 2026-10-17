"""Tests for the shared background-task helpers in ``app.webapp._task_loop``."""

from __future__ import annotations

import asyncio

from app.webapp._task_loop import BACKGROUND_TASKS, spawn_background_task


def test_spawn_background_task_holds_strong_ref_until_done() -> None:
    """Issue #703: ``asyncio.create_task``'s return value used to be
    discarded outright, leaving only a weak reference — eligible for GC
    mid-flight, which would leave an event scan's overlap guard wedged
    closed forever since the task's ``finally`` never runs. The task must
    be tracked (a strong ref) while in flight and untracked once done."""

    async def _slow() -> None:
        await asyncio.sleep(0)

    async def _run() -> None:
        task = spawn_background_task(_slow(), name="test-task")
        assert task in BACKGROUND_TASKS
        await task
        assert task not in BACKGROUND_TASKS

    asyncio.run(_run())
