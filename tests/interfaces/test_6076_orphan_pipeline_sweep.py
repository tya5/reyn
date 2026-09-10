"""Force-settle an orphaned pipeline row at the turn boundary (#6076 ②).

:meth:`_sweep_orphaned_running_tools` (#72) already force-settles an orphaned
TOOL row at a turn boundary — but it only ever walked ``_running_tools``.
``_pipeline_runs`` is a SEPARATE dict (keyed by ``run_id``, populated by a
``run_pipeline`` tool call's own progress frames), and nothing cleared it when
a turn ended before the run's completion frame arrived: the reported symptom
("終わってるはずなのに表示が上の方に張り付いてたり" — a row stuck as if
still running, after the turn is long over). This is independent of #6076 ①
(a display-only off-by-one in the SAME row's ``done/total`` text, fixed in
presenter.py; that fix does not touch lifecycle at all).

Same #72/#3296 discipline reused here: the row goes CANCELLED, never
SUCCESS/ERROR — the run's own report simply never arrived, which is neither
success nor failure.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import EntryState, FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat._meta_keys import RUNNING_SINCE_KEY
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.schemas.models import Event


class QueueTransport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport` fed one frame at a time from a
    queue — display OR event frames — so a test can push a pipeline-started
    frame, inspect its live RUNNING row, THEN push a turn-end event and
    inspect the sweep, with the stream staying open throughout. Mirrors
    ``test_textual_chat_orphan_sweep_72.py``'s own ``QueueTransport``."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()

    async def push_display(self, msg: OutboxMessage) -> None:
        await self._queue.put(DisplayFrame(msg))

    async def push_event(self, event_type: str) -> None:
        await self._queue.put(EventFrame(Event(type=event_type)))

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[object]":
        while True:
            yield await self._queue.get()

    async def submit_user_text(self, text: str, *, client_ref: "str | None" = None) -> None:  # pragma: no cover
        pass

    async def answer_intervention_text(self, text: str) -> bool:  # pragma: no cover
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:  # pragma: no cover
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:  # pragma: no cover
        pass

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


def _pipeline_started(run_id: str, index: int = 0) -> OutboxMessage:
    return OutboxMessage(
        kind="status",
        text="[pipeline step]",
        meta={
            "source": "pipeline",
            "run_id": run_id,
            "pipeline_name": "probe",
            "step_index": index,
            "total_steps": 4,
            "step_kind": "transform",
            "step_event": "pipeline_step_started",
        },
    )


def _entries(app: TextualChatApp):
    return app.query_one(FlowView).entries


@pytest.mark.asyncio
async def test_orphaned_pipeline_run_settles_cancelled_on_turn_settled() -> None:
    """Tier 2b: a pipeline row still open (live-animated, per
    :data:`RUNNING_SINCE_KEY`) when the turn settles is force-closed
    CANCELLED — never left spinning, and popped from ``_pipeline_runs``
    (verified separately below)."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_pipeline_started("run-orphan"))
        await pilot.pause()
        entry = next(
            e for e in _entries(app) if e.item.meta.get("run_id") == "run-orphan"
        )
        assert (entry.item.meta or {}).get(RUNNING_SINCE_KEY) is not None

        await transport.push_event("turn_settled")
        await pilot.pause()

        assert entry.state is EntryState.CANCELLED


@pytest.mark.asyncio
async def test_without_the_sweep_the_pipeline_row_would_stay_running() -> None:
    """Tier 2b: non-vacuity witness — with NO turn-end event delivered, the
    orphaned pipeline row stays open (still live-animated, never CANCELLED),
    proving the sweep (not some other mechanism) is what closes the gap in
    the paired test above."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_pipeline_started("run-still-open"))
        await pilot.pause()
        await pilot.pause()
        entry = next(
            e for e in _entries(app) if e.item.meta.get("run_id") == "run-still-open"
        )
        assert (entry.item.meta or {}).get(RUNNING_SINCE_KEY) is not None
        assert entry.state is not EntryState.CANCELLED


@pytest.mark.asyncio
async def test_a_new_run_after_the_sweep_gets_its_own_fresh_row() -> None:
    """Tier 2b: the swept run is popped from ``_pipeline_runs`` (not just
    settled in place) — observed via the PUBLIC surface: a later frame
    reusing the same ``run_id`` (the dict-not-cleared failure mode) creates a
    SECOND, freshly-RUNNING entry rather than reanimating the CANCELLED one
    in place. Mirrors ``_running_tools.clear()`` in the #72 sweep."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_pipeline_started("run-reused-id"))
        await pilot.pause()
        await transport.push_event("turn_settled")
        await pilot.pause()
        before = [
            e for e in _entries(app) if e.item.meta.get("run_id") == "run-reused-id"
        ]
        settled = before[0]
        assert settled.state is EntryState.CANCELLED

        await transport.push_display(_pipeline_started("run-reused-id"))
        await pilot.pause()

        after = [
            e for e in _entries(app) if e.item.meta.get("run_id") == "run-reused-id"
        ]
        fresh = [e for e in after if e not in before]
        assert fresh, (
            "a cleared _pipeline_runs must not reanimate the settled row in "
            "place — it has no live handle to it any more, so a same-id "
            "frame appends fresh instead"
        )
        assert settled.state is EntryState.CANCELLED, (
            "the settled row itself must stay untouched by the new frame"
        )
        assert (fresh[0].item.meta or {}).get(RUNNING_SINCE_KEY) is not None


@pytest.mark.asyncio
async def test_turn_completed_and_turn_cancelled_also_sweep_pipeline_runs() -> None:
    """Tier 2b: the belt-and-suspenders turn-end events (``turn_completed`` /
    ``turn_cancelled``) trigger the same pipeline sweep as ``turn_settled``."""
    for event_type in ("turn_completed", "turn_cancelled"):
        transport = QueueTransport()
        app = TextualChatApp(transport=transport)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await transport.push_display(_pipeline_started(f"run-{event_type}"))
            await pilot.pause()
            entry = next(
                e
                for e in _entries(app)
                if e.item.meta.get("run_id") == f"run-{event_type}"
            )
            assert (entry.item.meta or {}).get(RUNNING_SINCE_KEY) is not None

            await transport.push_event(event_type)
            await pilot.pause()

            assert entry.state is EntryState.CANCELLED, event_type
