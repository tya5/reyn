"""Tier 2: #5895 — the sent-queue gate seeds from the values the snapshot
frame CARRIES, never from the live read model. The local-shaped witness.

#5886's first fix moved the seed from "the first non-status frame" to "the
snapshot frame's own position in the stream" but still read the read model
LIVE at that moment — which only narrowed the window. Between the instant a
snapshot frame is enqueued and the instant the pump processes it, the live
value can move: remotely, ``_pump_sse`` applies a later delta onto
``RemoteStatusView``; locally, a submit advances ``queue_seq``. Either way a
live read seeds a baseline AHEAD of the submission the gate must admit, and
the operator's own message is rejected as "already reflected" — the same
silence #5886 was reported as. Architect ruling: the frame carries the
values captured at snapshot time; the seed reads the frame and nothing else.

This file constructs that gap deterministically in its LOCAL form: a REAL
registry and session, a REAL ``RegistryReadModel`` (so "the live value" is
the real session's own ``queue_seq``), a real submit that advances it, and a
snapshot frame carrying the value captured BEFORE the submit — exactly the
state the local producer leaves when a submit lands between its capture and
the pump. The submit happens before the frame is pushed, so by the time the
pump seeds, the live counter is already one ahead of what the frame says —
the gap is a fact this test controls, not a race it hopes for (the producer
itself is exercised by ``test_3310_n2_reset_hydrate.py``'s switch witness).
The auto-driver is stopped so nothing else moves the counter.

The sessions here carry NO WAL on purpose: ``Session.queued_user_messages()``
is journal-backed and stays empty without one, while ``queue_seq`` is
in-memory and advances regardless. That makes the operator's row reachable
by exactly ONE path — the ``user_submitted`` echo passing the gate. With a
WAL, a build that seeds from the live read model would ALSO pick the queued
item up from the live queue and draw the same row, and the accept-side
assert could not tell the two apart (that is how this file's first form
stayed green under its own strip).

Strip (verified): making ``_seed_queue_view`` read ``self._snapshot()``
instead of ``frame.snapshot`` turns the accept-side test red — the real,
advanced ``queue_seq`` becomes the baseline and the submission is rejected.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import pytest

from reyn.core.events.events import Event
from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.sent_queue import SentQueue
from reyn.interfaces.repl.read_model import RegistryReadModel
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import EventFrame, QueueSnapshot, StatusApplied
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import _DEFAULT_SID, AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session

_MINE = "the operator's own submission after the snapshot was captured"


class _FrameTransport(ClientTransportStub):
    """A real, minimal ``ClientTransport`` fed one item at a time (the
    ``_EventOnlyTransport`` shape ``test_3338_tui_status_chrome_liveness.py``
    established)."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()

    async def push(self, item: object) -> None:
        await self._queue.put(item)

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def frames(self) -> "AsyncIterator[object]":
        while True:
            yield await self._queue.get()

    async def submit_user_text(self, text: str, *, client_ref: "str | None" = None) -> str:
        return ""

    async def run_slash_command(self, name: str, args: str) -> bool:
        return True

    async def answer_intervention_text(
        self, text: str, *, intervention_id: "str | None" = None,
    ) -> bool:
        return False

    async def answer_intervention_choice(
        self, choice_id: str, *, intervention_id: "str | None" = None,
    ) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg) -> None:
        pass

    async def cancel_inflight(self) -> str:
        return ""

    async def shutdown(self) -> None:
        pass


def _registry_without_wal(tmp_path: Path) -> AgentRegistry:
    """A real registry whose sessions carry NO ``StateLog`` — see the module
    docstring: the live queue must stay empty so the only way a row can
    appear is the echo passing the gate."""

    def factory(profile: AgentProfile) -> Session:
        return make_session(agent_name=profile.name, agent_role=profile.role)

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    reg.create("alpha")
    return reg


async def _stop_auto_driver(reg: AgentRegistry, name: str) -> None:
    """Cancel the background ``session.run()`` ``attach`` booted, so a
    submission stays QUEUED (its seq is the next after the baseline) and
    nothing but this test moves the counter."""
    task = reg._tasks.get((name, _DEFAULT_SID))
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _settle(pilot, n: int = 3) -> None:
    for _ in range(n):
        await pilot.pause()


@pytest.mark.asyncio
async def test_a_submit_between_snapshot_capture_and_seed_is_not_rejected(tmp_path, monkeypatch):
    """Tier 2: the accept side. The snapshot frame carries ``queue_seq`` as
    it was when captured; a REAL submit then advances the session's live
    counter before the pump seeds. The operator's own ``user_submitted``
    (the very next seq) must be admitted into the sent-queue region."""
    monkeypatch.chdir(tmp_path)
    reg = _registry_without_wal(tmp_path)
    try:
        session = await reg.attach("alpha")
        await _stop_auto_driver(reg, "alpha")
        captured_seq = session.queue_seq  # what the producer would have captured
        # The gap: a real submit advances the LIVE counter after the frame's
        # values were captured and before the pump seeds (the frame is
        # pushed below, after this). The live queue itself stays empty (no
        # WAL) — only ``queue_seq`` moves.
        msg_id = await session.submit_user_text(_MINE)
        assert session.queue_seq == captured_seq + 1
        read_model = RegistryReadModel(reg)
        assert read_model.snapshot()["queue_seq"] == captured_seq + 1
        assert not session.queued_user_messages(), "premise: no WAL, no live queue"

        transport = _FrameTransport()
        app = TextualChatApp(transport=transport, read_model=read_model, agent_name="alpha")
        async with app.run_test(size=(100, 30)) as pilot:
            await transport.push(StatusApplied(
                kind="snapshot",
                snapshot=QueueSnapshot(queue=(), turn_active=False, queue_seq=captured_seq),
            ))
            # The echo the local forwarder would deliver for that submit.
            await transport.push(EventFrame(Event(type="user_submitted", data={
                "msg_id": msg_id, "chain_id": None, "text": _MINE, "seq": captured_seq + 1,
            })))
            await _settle(pilot)

            rendered = list(app.query_one(SentQueue).rendered_texts())
            assert any(_MINE in t for t in rendered), (
                "the operator's own submission was rejected by the gate — the seed "
                f"read the LIVE queue_seq ({captured_seq + 1}) instead of the frame's "
                f"captured one ({captured_seq}); sent-queue: {rendered!r}"
            )
    finally:
        await reg.shutdown()


@pytest.mark.asyncio
async def test_a_delta_older_than_the_captured_snapshot_is_still_rejected(tmp_path, monkeypatch):
    """Tier 2: the discriminating control — the gate still gates. A delta
    whose seq is AT the captured baseline (already reflected in the
    snapshot) is rejected, so "seed from the frame" cannot be satisfied by a
    build that stopped seeding, or seeded 0, altogether."""
    monkeypatch.chdir(tmp_path)
    reg = _registry_without_wal(tmp_path)
    try:
        session = await reg.attach("alpha")
        await _stop_auto_driver(reg, "alpha")
        await session.submit_user_text("already in the snapshot")
        captured_seq = session.queue_seq  # >= 1: the snapshot reflects that submit

        transport = _FrameTransport()
        app = TextualChatApp(
            transport=transport, read_model=RegistryReadModel(reg), agent_name="alpha",
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await transport.push(StatusApplied(
                kind="snapshot",
                snapshot=QueueSnapshot(queue=(), turn_active=False, queue_seq=captured_seq),
            ))
            await transport.push(EventFrame(Event(type="user_submitted", data={
                "msg_id": "stale", "chain_id": None, "text": "a stale replay",
                "seq": captured_seq,
            })))
            await _settle(pilot)

            rendered = list(app.query_one(SentQueue).rendered_texts())
            assert not any("a stale replay" in t for t in rendered), rendered
    finally:
        await reg.shutdown()
