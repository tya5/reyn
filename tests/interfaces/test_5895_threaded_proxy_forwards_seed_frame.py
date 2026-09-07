"""Tier 2: #5895 (architect container ruling) — ``ThreadedTransportProxy``
forwards a ``StatusApplied(kind="snapshot")`` seed frame UNCHANGED, and an
app behind the proxy is seeded by it.

The local frame stream now carries ``StatusApplied`` (the sent-queue seed
behind every ``session_attached`` barrier, ``in_process.py``). The threaded
proxy sits between that stream and the TUI in the threaded configuration
(#4995) and is a pass-through consumer: it must forward the seed, not drop
it — a proxy that dropped it would reproduce the #5886 regression in
exactly ONE configuration, silently (the architect's own named hazard).

Both tests use a real minimal ``ClientTransport`` (the ``test_5048_
threaded_proxy_pending_head_not_live_object.py`` shape) whose ``frames()``
yields one seed frame and then holds, so "the seed reached the caller
side" is a fact the stream itself shows; the proxy is the REAL
``ThreadedTransportProxy`` with its real worker thread. No registry is
built here: the property under test is the proxy's forwarding, not the
producer (``test_3310_n2_reset_hydrate.py`` covers the producer).

Strip (verified): making ``ThreadedTransportProxy._pump_frames`` skip
``StatusApplied`` items turns BOTH tests red — as a HANG that CI's
``--timeout`` kills (the stream yields nothing before the hold, so
``__anext__`` / the app's pump wait forever), not as an assertion; said
here because a hang wears no colour of its own.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.sent_queue import SentQueue
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import QueueSnapshot, StatusApplied
from reyn.interfaces.transport.threaded import ThreadedTransportProxy

_QUEUED = {"msg_id": "m1", "chain_id": "c1", "text": "seeded through the proxy", "meta": {}}


class _SeedThenHoldTransport(ClientTransportStub):
    """Yields ONE ``StatusApplied(kind="snapshot")`` seed frame, then holds
    forever — the smallest stream in which "the seed was forwarded" is
    observable on the caller side."""

    def __init__(self) -> None:
        self._never: "asyncio.Event | None" = None

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def frames(self) -> "AsyncIterator[StatusApplied]":
        yield StatusApplied(
            kind="snapshot",
            snapshot=QueueSnapshot(queue=(dict(_QUEUED),), turn_active=False, queue_seq=1),
        )
        # Created lazily ON the worker loop (the proxy runs this generator
        # there), never on the caller's loop.
        self._never = asyncio.Event()
        await self._never.wait()

    async def submit_user_text(self, text: str, *, client_ref: "str | None" = None) -> str:
        return ""

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

    def pending_intervention_head(self):
        return None

    def put_display(self, msg) -> None:
        pass

    async def cancel_inflight(self) -> str:
        return ""

    async def shutdown(self) -> None:
        pass


@pytest.mark.asyncio
async def test_the_proxy_forwards_the_seed_frame_unchanged() -> None:
    """Tier 2: the first item out of ``proxy.frames()`` IS the inner
    stream's ``StatusApplied`` seed — same kind, same carried values."""
    proxy = ThreadedTransportProxy(_SeedThenHoldTransport)
    proxy.start()
    try:
        frame = await proxy.frames().__anext__()
        assert isinstance(frame, StatusApplied), (
            f"the proxy did not forward the seed frame as-is: {frame!r}"
        )
        assert frame.kind == "snapshot" and frame.snapshot is not None
        assert frame.snapshot.queue_seq == 1
        assert [i["msg_id"] for i in frame.snapshot.queue] == ["m1"]
    finally:
        await proxy.shutdown()


@pytest.mark.asyncio
async def test_an_app_behind_the_proxy_is_seeded_by_the_forwarded_frame() -> None:
    """Tier 2: one layer up — the TUI fed through the proxy shows the
    snapshot's queued item in its sent-queue region, i.e. the seed point
    survives the threaded configuration."""
    proxy = ThreadedTransportProxy(_SeedThenHoldTransport)
    proxy.start()  # the runner's act in production (``repl.py``), not the app's
    app = TextualChatApp(transport=proxy)
    try:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.pause()
            rendered = list(app.query_one(SentQueue).rendered_texts())
            assert any("seeded through the proxy" in t for t in rendered), (
                f"the app behind the proxy was not seeded; sent-queue: {rendered!r}"
            )
    finally:
        await proxy.shutdown()
