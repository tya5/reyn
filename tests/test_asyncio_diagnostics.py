"""Tier 2b: an unhandled exception in a fire-and-forget asyncio task is
durably captured as an `asyncio_unhandled_exception` P6 event, without
losing the event loop's own default exception handling.

Regression context: reyn installed no `asyncio.set_exception_handler`
anywhere, so a background `asyncio.create_task(...)` whose result nobody
awaits/checks would raise into Python's own
`asyncio.BaseEventLoop.default_exception_handler` (stderr/logging only) and
be gone — the exact "Unhandled exception in event loop" / "exception: None"
class an operator cannot investigate after the fact. This test exercises the
real `install_asyncio_exception_handler` + `emit_cli_event` path (no mocks)
end to end: schedule a raising task on a real loop, let the loop process it,
and assert the event landed durably under `.reyn/events/`.

Policy compliance (docs/deep-dives/contributing/testing.md):
- No unittest.mock / MagicMock / AsyncMock / patch.
- Real event loop, real filesystem (pytest tmp_path), real EventStore reader.
- Test docstring first line declares Tier.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from reyn.core.events.asyncio_diagnostics import install_asyncio_exception_handler


def _read_events_of_kind(events_dir: Path, kind: str) -> list[dict]:
    """Read every JSONL event of *kind* from anywhere under *events_dir*."""
    found: list[dict] = []
    if not events_dir.exists():
        return found
    for path in events_dir.rglob("*.jsonl"):
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("type") == kind:
                found.append(rec)
    return found


def test_unhandled_task_exception_is_durably_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2b: a raising fire-and-forget task's exception survives as a P6 event.

    Steps:
    1. Real ``.reyn/`` dir under tmp_path (the durable-emit anchor
       ``emit_cli_event`` walks up from cwd to find).
    2. Real event loop; install the handler; schedule a task that raises
       with NO one awaiting/checking its result (the fire-and-forget class).
    3. Yield control (``asyncio.sleep(0)`` twice) so the loop's own
       call_exception_handler path actually fires for the failed task.
    4. Read back ``.reyn/events/**/*.jsonl`` and assert one
       ``asyncio_unhandled_exception`` event exists with the right fields.
    """
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    async def _boom() -> None:
        raise ValueError("kaboom-from-background-task")

    async def _drive() -> None:
        install_asyncio_exception_handler(asyncio.get_running_loop())
        asyncio.create_task(_boom())  # fire-and-forget: nobody awaits this
        # Give the loop two ticks: one to run _boom() to its raise, one more
        # for the loop to notice the task's exception was never retrieved
        # and invoke the exception handler.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_drive())

    events = _read_events_of_kind(reyn_dir / "events", "asyncio_unhandled_exception")
    [event] = events  # exactly one event captured — unpack raises otherwise
    data = event["data"]
    assert data["exception_type"] == "ValueError"
    assert "kaboom-from-background-task" in data["exception_message"]
    assert "ValueError: kaboom-from-background-task" in data["traceback"]
    assert "Task exception was never retrieved" in data["context_message"]


def test_default_handler_still_invoked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2b: installing our handler does not regress existing stderr/log visibility.

    asyncio's own default_exception_handler logs the failure via the
    ``asyncio`` logger at ERROR level. This asserts that log record still
    fires (our handler wraps, never replaces, the default one).
    """
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    async def _boom() -> None:
        raise RuntimeError("still-visible-in-logs")

    async def _drive() -> None:
        install_asyncio_exception_handler(asyncio.get_running_loop())
        asyncio.create_task(_boom())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    with caplog.at_level(logging.ERROR, logger="asyncio"):
        asyncio.run(_drive())

    assert any(
        "still-visible-in-logs" in str(record.message)
        or (record.exc_info and "still-visible-in-logs" in str(record.exc_info[1]))
        for record in caplog.records
    )
