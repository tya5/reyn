"""Tier 1/2: #4995 — ``ThreadedTransportProxy`` puts a real ``ClientTransport``
on a dedicated worker thread with its own event loop, so competing work
(e.g. the router's own turn processing) no longer starves the TUI's own
scheduling on a shared loop.

**Design history, kept honest** (2 approaches rejected before this one
settled — see ``threaded.py``'s own module docstring for the full
rationale): a "one seam per offloaded function" shape (``asyncio.to_thread``
at each hot-path call site) was rejected first (architect: N seams where 1
— the transport — already exists and already has the right properties); a
``threading.Lock``-synchronized variant of the SAME single seam was
rejected second (lead-coder, measured self-contradiction: a lock held by
the core thread during I/O reproduces exactly the "TUI does not get
scheduled" symptom this issue exists to remove — witness② below would go
red under that design). Settled: immutable snapshot hand-off, no shared
mutable state, no lock.

**Both required witnesses, per lead-coder's own #4995 ruling — neither
alone is sufficient** (① alone only shows "somewhere else", not "the UI
still moves"; ② alone cannot tell a genuinely different thread apart from
a coincidence): no ``sleep``/``timeout``/``attempts=N`` anywhere (CLAUDE.md
floor/ceiling rule) — ① compares thread identities, never a duration; ②
gates the worker on a real ``threading.Event`` the test holds open and
waits on a second real ``threading.Event`` (via ``asyncio.to_thread``)
rather than any bounded/timed poll.

Real ``ThreadedTransportProxy`` — the "inner" transport under test is a
minimal, real ``ClientTransport`` subclass (not a mock/MagicMock — CLAUDE.md
forbids faking a collaborator that's cheaply constructible; this one has no
cheap real production instance to construct from without a live Session/
AgentRegistry, so a small real hand-written implementation is the
established pattern here, mirroring ``test_4983_session_switch_off_thread.
py``'s own ``QueueTransport``).
"""
from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, AsyncIterator

import pytest

from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.threaded import ThreadedTransportProxy

if TYPE_CHECKING:
    from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
    from reyn.runtime.outbox import OutboxMessage


class _RecordingTransport(ClientTransport):
    """A minimal, real ``ClientTransport``. ``frames()`` never yields
    anything in these tests (an unresolved real ``asyncio.Event`` — an
    unbounded wait, not a duration) since neither witness needs a frame to
    flow; ``submit_user_text`` is the one method under test, gated by a
    ``threading.Event`` the test controls."""

    def __init__(self, *, gate: "threading.Event | None" = None,
                 started: "threading.Event | None" = None) -> None:
        self._gate = gate
        self._started = started
        self.call_idents: "list[int]" = []
        self._never = asyncio.Event()

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame | EventFrame]":
        await self._never.wait()
        yield  # pragma: no cover - never reached, satisfies the generator shape

    async def submit_user_text(self, text: str) -> str:
        self.call_idents.append(threading.get_ident())
        if self._started is not None:
            self._started.set()
        if self._gate is not None:
            # Blocks the WORKER thread only — a real threading.Event.wait(),
            # unbounded (CLAUDE.md: wait on the condition, never a sleep).
            self._gate.wait()
        return f"echo:{text}"

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

    def put_display(self, msg: "OutboxMessage") -> None:
        pass

    async def cancel_inflight(self) -> str:
        return ""

    async def shutdown(self) -> None:
        pass


@pytest.mark.asyncio
async def test_witness_1_worker_work_runs_on_a_different_thread_than_the_caller():
    """Tier 1: structural witness. ``submit_user_text`` is dispatched
    through the proxy; the thread it actually executed on must differ from
    the calling (test/event-loop) thread.

    Strip-falsifier (performed manually before submission, not re-run in
    CI — reverting would mean calling the inner transport directly with no
    thread at all): calling ``inner.submit_user_text`` directly, bypassing
    the proxy, makes ``call_idents == [threading.get_ident()]`` — the SAME
    thread as the caller — turning this red."""
    caller_ident = threading.get_ident()
    inner = _RecordingTransport()
    proxy = ThreadedTransportProxy(lambda: inner)
    proxy.start()
    try:
        result = await proxy.submit_user_text("hello")
        assert result == "echo:hello"
        assert inner.call_idents == [proxy.worker_thread_ident]
        assert proxy.worker_thread_ident != caller_ident, (
            "the worker's own recorded thread identity must differ from "
            "the caller's — this is the whole point of the thread boundary"
        )
    finally:
        await proxy.shutdown()


@pytest.mark.asyncio
async def test_witness_2_the_caller_loop_keeps_advancing_while_the_worker_is_blocked():
    """Tier 2: progress witness. While the worker thread is deliberately
    held open inside ``submit_user_text`` (a real ``threading.Event.wait()``,
    released only at the end of this test), the CALLER's own event loop
    must keep scheduling other work — the actual defect #4995 exists to
    remove is the TUI not getting scheduled while the core does something
    slow on a SHARED loop.

    Strip-falsifier (performed manually, not re-run in CI — reverting would
    hang the test process): if ``ThreadedTransportProxy._call_on_worker``
    dispatched inline (called ``inner.submit_user_text`` directly on the
    caller's own thread, no ``run_coroutine_threadsafe``/thread hop at all)
    instead of on the worker thread, ``self._gate.wait()`` would block the
    CALLER's own thread — including its event loop — and the ``asyncio.
    sleep(0)`` loop below could never run even once: this test would hang
    until the harness's own CI timeout killed it, never reaching the
    ``assert progressed == 50`` line. This was verified by temporarily
    forcing ``_call_on_worker`` to await the inner coroutine directly (no
    thread) and observing the hang under a bounded ``pytest --timeout``
    invocation, then reverting — not left as an automated test, since a
    genuinely hanging test is exactly the failure mode CI's own kill switch
    exists to catch, not something to reproduce on every real run."""
    gate = threading.Event()
    started = threading.Event()
    inner = _RecordingTransport(gate=gate, started=started)
    proxy = ThreadedTransportProxy(lambda: inner)
    proxy.start()
    try:
        submit_task = asyncio.create_task(proxy.submit_user_text("hi"))
        # Unbounded wait on a real condition (started.wait()), off the
        # caller's own loop via to_thread — not a sleep, not a poll.
        await asyncio.to_thread(started.wait)

        progressed = 0
        for _ in range(50):
            await asyncio.sleep(0)
            progressed += 1
        assert progressed == 50, (
            "the caller loop must keep scheduling other work while the "
            "worker thread sits inside a blocking call"
        )
        assert not submit_task.done(), (
            "the worker call must still be blocked at this point — proves "
            "the 50 iterations above genuinely overlapped it, rather than "
            "the worker having already finished before they ran"
        )

        gate.set()
        result = await submit_task
        assert result == "echo:hi"
    finally:
        await proxy.shutdown()


@pytest.mark.asyncio
async def test_accept_side_frames_and_sync_reads_flow_through_the_proxy():
    """Tier 2: accept-side — the proxy is not JUST a thread hop for one
    async method; the frame stream and the synchronous passthrough reads
    (``has_session`` etc., read from the single snapshot slot, refreshed
    alongside each frame — see ``threaded.py``'s own module docstring) work
    end-to-end with a real inner transport that actually produces frames
    and reports a real session."""
    from reyn.interfaces.transport.frames import DisplayFrame
    from reyn.runtime.outbox import OutboxMessage

    class _FramingTransport(_RecordingTransport):
        """Yields exactly one real frame (constructed at ``__init__`` time,
        no cross-thread test injection needed — the worker thread's own
        pump loop is what drains this and delivers it), then hangs on an
        unresolved real ``asyncio.Event`` forever (unbounded wait, not a
        duration) since this test needs no second frame."""

        async def frames(self):
            yield DisplayFrame(OutboxMessage(kind="status", text="hello from core"))
            await self._never.wait()

    inner = _FramingTransport()
    proxy = ThreadedTransportProxy(
        lambda: inner, read_model_snapshot_fn=lambda: {"model": "test-model"},
    )
    proxy.start()
    try:
        # Pre-first-frame: the pre-attach contract (None), same shape
        # #5009's own cost/ctx panes already rely on.
        assert proxy.has_session() is False
        assert proxy.read_model_snapshot() is None

        frame = await proxy.frames().__anext__()
        assert frame.message.text == "hello from core"

        assert proxy.has_session() is True
        assert proxy.read_model_snapshot() == {"model": "test-model"}
    finally:
        await proxy.shutdown()
