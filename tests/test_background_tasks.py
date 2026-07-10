"""Tests for the fire-and-forget task helper in app.main."""

from __future__ import annotations

import asyncio

import pytest
from structlog.testing import capture_logs

from app.main import _background_tasks, _fire_and_forget


@pytest.mark.asyncio
async def test_fire_and_forget_runs_the_coroutine() -> None:
    ran = False

    async def work() -> None:
        nonlocal ran
        ran = True

    task = _fire_and_forget(work())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert ran is True
    assert task.done()
    # Cleaned up from the module-level keep-alive set once finished.
    assert task not in _background_tasks


@pytest.mark.asyncio
async def test_fire_and_forget_logs_failure_and_does_not_propagate() -> None:
    """A failing coroutine must be logged with context, never raised at us."""

    async def boom() -> None:
        raise RuntimeError("disk full")

    with capture_logs() as logs:
        task = _fire_and_forget(boom(), session_id="sid-xyz", turn="assistant")
        # Give the event loop enough turns to run the task and fire the
        # add_done_callback (which is scheduled via call_soon once the
        # task's coroutine raises).
        for _ in range(5):
            await asyncio.sleep(0)

    assert task.done()
    # No exception propagated out of _fire_and_forget / the callback — if it
    # had, this test would have errored above instead of reaching here.
    assert task not in _background_tasks

    failures = [entry for entry in logs if entry.get("event") == "background_task_failed"]
    assert len(failures) == 1
    failure = failures[0]
    assert failure["session_id"] == "sid-xyz"
    assert failure["turn"] == "assistant"
    assert failure["error_type"] == "RuntimeError"
    assert "disk full" in failure["error"]


@pytest.mark.asyncio
async def test_fire_and_forget_keeps_a_reference_until_done() -> None:
    """Guards against the classic asyncio footgun: GC before completion."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow() -> None:
        started.set()
        await release.wait()

    task = _fire_and_forget(slow())
    await started.wait()
    assert task in _background_tasks

    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert task not in _background_tasks
