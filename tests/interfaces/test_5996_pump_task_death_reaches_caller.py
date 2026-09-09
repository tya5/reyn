"""Tier 2: #5996 — ``ThreadedTransportProxy``'s own worker-thread pump task
(``_pump_frames``, run on a dedicated OS thread's own event loop) used to
die UNTRACKED on an unhandled exception: no ``add_done_callback``, so
asyncio's own default handler only logs "Task exception was never
retrieved" at teardown/GC — never to this app's own logger — and the
caller's own :meth:`ThreadedTransportProxy.frames` waited on
``self._caller_queue.get()`` forever, since nothing else would ever put
anything there again.

lead-coder's own ruling (issue #5996): keep the task, and deliver the
exception to ``frames()``'s own waiter — not "make the pump not die",
which is a different, unrelated question this issue does not answer.
Scope: ``ThreadedTransportProxy`` only, per lead-coder's explicit
instruction — this issue was surfaced by #5990's own census as a
structurally similar (but NOT identical, and NOT fixed by) shape to
#5989/#5990/#5995.

Real ``ThreadedTransportProxy`` + a real worker thread + a real, minimal
``ClientTransport`` throughout (mirrors #5329's own ``_RaisingTransport``
idiom for injecting a wire-layer failure) — no mocks. No duration
anywhere: the caller-side ``await proxy.frames().__anext__()`` genuinely
blocks on a real ``asyncio.Queue.get()`` until the worker thread's own
``call_soon_threadsafe`` wakes it — CI's own ``--timeout`` is the only
ceiling, matching every other unbounded-wait test in this file's own
sibling (``test_4995_threaded_transport_proxy.py``).
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, AsyncIterator

import pytest

from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.threaded import ThreadedPumpFailed, ThreadedTransportProxy

if TYPE_CHECKING:
    from reyn.runtime.outbox import OutboxMessage


class _InjectedPumpFailure(RuntimeError):
    """Stands in for a real inner-transport failure — what actually raises
    is irrelevant to the property under test (#5329's own explicit
    scoping, reused here for the same reason)."""


class _RaisingAfterOneFrameTransport(ClientTransportStub):
    """A real, minimal ``ClientTransport`` whose ``frames()`` yields ONE
    genuine frame (proving normal flow still works — accept criterion ②)
    then raises (accept criterion ①'s own trigger)."""

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[object]":
        from reyn.interfaces.transport.frames import DisplayFrame
        from reyn.runtime.outbox import OutboxMessage

        yield DisplayFrame(OutboxMessage(kind="agent", text="before the failure"))
        raise _InjectedPumpFailure("simulated inner-transport failure")

    async def submit_user_text(self, text: str, *, client_ref: "str | None" = None) -> str:  # pragma: no cover
        return ""

    async def answer_intervention_text(
        self, text: str, *, intervention_id: "str | None" = None
    ) -> bool:  # pragma: no cover
        return False

    async def answer_intervention_choice(
        self, choice_id: str, *, intervention_id: "str | None" = None
    ) -> bool:  # pragma: no cover
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:  # pragma: no cover
        pass

    async def cancel_inflight(self) -> str:  # pragma: no cover
        return ""

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


class _NeverYieldsTransport(ClientTransportStub):
    """Regression-guard sibling: ``frames()`` never yields anything and
    never raises on its own — the pump task only ever ends via a real
    ``shutdown()``/cancel, never a death. Confirms
    :meth:`ThreadedTransportProxy._on_pump_task_done`'s cancellation
    branch (``task.cancelled()`` → return, no forwarding) — the
    LEGITIMATE shutdown path must never be mistaken for a death."""

    def __init__(self) -> None:
        self._never = asyncio.Event()
        self.shutdown_calls = 0

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[object]":
        await self._never.wait()
        return
        yield  # pragma: no cover - unreachable, satisfies the generator shape

    async def submit_user_text(self, text: str, *, client_ref: "str | None" = None) -> str:  # pragma: no cover
        return ""

    async def answer_intervention_text(
        self, text: str, *, intervention_id: "str | None" = None
    ) -> bool:  # pragma: no cover
        return False

    async def answer_intervention_choice(
        self, choice_id: str, *, intervention_id: "str | None" = None
    ) -> bool:  # pragma: no cover
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:  # pragma: no cover
        pass

    async def cancel_inflight(self) -> str:  # pragma: no cover
        return ""

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


@pytest.mark.asyncio
async def test_pump_task_death_unblocks_frames_with_a_typed_exception() -> None:
    """Tier 2: #5996's own accept criterion ① — when the worker thread's
    pump task dies, the caller's ``await`` on ``frames()`` resolves via a
    raised exception, not a permanent hang.

    Strip-falsifier (verified by hand: ``_on_pump_task_done`` never
    attached via ``add_done_callback``, matching the pre-fix shape): this
    test hangs — the assertion below is never reached, and CI's own
    ``--timeout`` is what would eventually kill it (per CLAUDE.md's own
    "wait on the condition unboundedly" ceiling policy — a stripped fix
    correctly manifests as an unbounded wait that never resolves, not as
    a quick red)."""
    proxy = ThreadedTransportProxy(lambda: _RaisingAfterOneFrameTransport())
    proxy.start()
    try:
        stream = proxy.frames()
        first = await stream.__anext__()
        assert first.message.text == "before the failure", (
            "accept criterion ②: a real frame must flow normally BEFORE "
            "the failure — proves this is testing the real pump, not a "
            "pump that never started"
        )
        with pytest.raises(ThreadedPumpFailed) as excinfo:
            await stream.__anext__()
        assert isinstance(excinfo.value.__cause__, _InjectedPumpFailure), (
            "the real exception must be chained as __cause__, not lost — "
            f"got {excinfo.value.__cause__!r}"
        )
    finally:
        proxy.close()


@pytest.mark.asyncio
async def test_shutdown_of_a_never_finishing_pump_does_not_raise_threaded_pump_failed() -> None:
    """Tier 2: regression guard — #5996's own accept criterion, the OTHER
    direction: :meth:`ThreadedTransportProxy._cancel_pump_on_worker`
    (the ``shutdown()`` path) cancels a still-running pump task, and that
    CANCELLATION must not be mistaken for a death by the new
    ``_on_pump_task_done`` callback.

    #5996 (lead-coder review, BLOCKING on PR #6004, self-corrected here):
    an earlier version of this test called ``shutdown()`` WITHOUT ever
    consuming ``frames()`` — ``ThreadedPumpFailed`` is only ever raised
    from INSIDE ``frames()``'s own ``isinstance`` check (see that
    method's own docstring), never from ``shutdown()`` itself, which
    never reads the queue at all. A build that forwarded on cancellation
    too would have merely left an unread ``_PumpDied`` sentinel sitting
    in ``_caller_queue`` — the old test stayed green regardless, since
    nothing ever looked. Fixed: a real consumer task is started on
    ``frames()`` BEFORE ``shutdown()`` runs, so a wrongly-forwarded
    sentinel has a real reader to reach.

    No arbitrary wait: ``shutdown()`` itself is the real synchronization
    point, not a chosen duration — it structurally requires several real
    cross-thread round trips (``_call_on_worker("shutdown")``, then
    ``_cancel_pump_on_worker`` via ``wrap_future``, then
    ``asyncio.to_thread(self._thread.join)``), each of which yields the
    CALLER loop back to the scheduler. ``_on_pump_task_done`` is
    registered via ``add_done_callback`` at ``_run_worker`` time — before
    ``_cancel_pump_on_worker``'s own ``await self._pump_task`` is ever
    reached — so in a build that forwards on cancellation, the sentinel
    would already have been pushed AND already have been read by
    ``consumer`` by the time ``await proxy.shutdown()`` returns; nothing
    here depends on picking a sleep long enough.

    Strip-falsifier, verified by hand — NOT a bare deletion of ``if
    task.cancelled(): return``: ``Task.exception()`` on an already-
    CANCELLED task raises ``CancelledError`` itself (confirmed
    interactively), so simply removing that guard makes
    ``_on_pump_task_done`` raise INSIDE the ``add_done_callback``
    machinery instead — asyncio's own callback invocation swallows that
    (logged via the loop's default exception handler, never reaching
    ``call_soon_threadsafe``), so a bare deletion leaves this test green
    for an unrelated reason (nothing forwards, but also nothing is
    diagnosed — a real, distinct gap, just not the one this test
    targets). The strip that DOES turn this test red, and the one
    verified by hand: replacing the cancellation guard with a mistake a
    careless rewrite could plausibly make —
    ``try: exc = task.exception() except asyncio.CancelledError as ce:
    exc = ce`` — which treats the cancellation itself as a forwardable
    failure. That reproduces the exact "an ordinary /quit raises
    ThreadedPumpFailed" shape this test exists to catch."""
    transport = _NeverYieldsTransport()
    proxy = ThreadedTransportProxy(lambda: transport)
    proxy.start()
    consumer = asyncio.ensure_future(proxy.frames().__anext__())
    try:
        await proxy.shutdown()
        assert transport.shutdown_calls == 1, (
            "sanity: shutdown must have actually reached the inner transport"
        )
        assert not consumer.done(), (
            "#5996 REGRESSION: an ordinary /quit must not deliver "
            "ThreadedPumpFailed to a live frames() consumer — got "
            f"{consumer.exception() if consumer.done() else 'n/a'!r}"
        )
    finally:
        consumer.cancel()
        try:
            await consumer
        except asyncio.CancelledError:
            pass
