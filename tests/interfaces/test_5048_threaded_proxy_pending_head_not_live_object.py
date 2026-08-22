"""Tier 2: #5048 pre-cutover finding (architect) — ``ThreadedTransportProxy``
must never hand the LIVE ``UserIntervention`` object across the thread
boundary, only its ``id``.

Real measurement, not a hypothetical: ``InProcessTransport.pending_
intervention_head()`` returns ``Session.interventions.head()`` VERBATIM —
the actual ``UserIntervention``, which carries a mutable ``future:
asyncio.Future`` bound to the WORKER loop. Before this fix,
``ThreadedTransportProxy`` copied that live reference straight into its
own ``_ThreadedSnapshot`` slot, contradicting ``threaded.py``'s own module
docstring ("nothing on the caller (TUI) thread ever reads their live,
mutable attributes directly" / "every value ... is refreshed into ONE
overwriting slot"). No consumer reads more than ``.id`` today (``stream_
client.py``'s own ``_pending_head_id``), so the leak was latent, not the
"value not the owned object" contract #5044 already ruled on for this
same class family — this is what makes it the right shape to fix BEFORE
#5048's cutover promotes this class to the TUI's default transport and
gains consumers that might reach for ``.prompt``/``.choices``/``.future``.

Real ``ThreadedTransportProxy`` + a real (non-mock) ``ClientTransport``
stand-in whose ``pending_intervention_head()`` returns an object shaped
exactly like the real ``UserIntervention`` (an ``id`` plus a genuinely
live, unresolved ``asyncio.Future`` bound to ITS OWN loop) — the same
"small real hand-written implementation" pattern
``test_4995_threaded_transport_proxy.py`` already establishes for this
class, not a mock.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, AsyncIterator

import pytest

from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.threaded import ThreadedTransportProxy

if TYPE_CHECKING:
    from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
    from reyn.runtime.outbox import OutboxMessage


class _FakeIntervention:
    """Shaped like the real ``UserIntervention`` in exactly the ways this
    fix cares about: a stable ``id`` plus a genuinely live, unresolved
    ``asyncio.Future`` — the mutable, thread-affine attribute that must
    never reach the caller thread. Not the real dataclass itself (that
    would require constructing it on the worker's own loop just to prove a
    point already provable with a lighter stand-in) — but real enough that
    ``getattr(head, "id", None)`` and "does this carry a live Future" are
    both genuine, not simulated."""

    def __init__(self, iv_id: str, future: "asyncio.Future") -> None:
        self.id = iv_id
        self.future = future


class _InterveningTransport(ClientTransport):
    """A minimal, real ``ClientTransport`` whose ``pending_intervention_
    head()`` returns a live ``_FakeIntervention`` — mirroring ``InProcess
    Transport``'s own real behaviour (``s.interventions.head()`` returned
    verbatim), not a made-up shape."""

    def __init__(self, head: "_FakeIntervention | None") -> None:
        self._head = head
        self._never = asyncio.Event()

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame | EventFrame]":
        from reyn.interfaces.transport.frames import DisplayFrame
        from reyn.runtime.outbox import OutboxMessage

        yield DisplayFrame(OutboxMessage(kind="status", text="frame"))
        await self._never.wait()

    async def submit_user_text(self, text: str) -> str:
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
        return self._head

    def put_display(self, msg: "OutboxMessage") -> None:
        pass

    async def cancel_inflight(self) -> str:
        return ""

    async def shutdown(self) -> None:
        pass


@pytest.mark.asyncio
async def test_pending_intervention_head_never_crosses_the_live_object():
    """Tier 2: the snapshot slot carries the bare ``id`` string, never the
    ``_FakeIntervention`` instance (and never its ``.future``) — the caller
    thread genuinely cannot reach the live object through this seam.

    Strip-falsifier: reverting ``_pump_frames`` to
    ``pending_intervention_head=self._inner.pending_intervention_head()``
    (dropping the ``_pending_head_id`` narrowing) makes
    ``proxy.pending_intervention_head()`` return the ``_FakeIntervention``
    instance itself — this assertion (``isinstance(..., str)``) turns red.
    Verified locally by temporarily reverting that one line."""
    worker_future_holder: "list[asyncio.Future]" = []

    class _HeadProducingTransport(_InterveningTransport):
        def start(self) -> None:
            # The Future must belong to the WORKER's own loop (constructed
            # after the worker thread's loop exists) to genuinely mirror
            # the real UserIntervention's thread-affinity — this is what
            # makes "the caller must never touch it" a real hazard, not a
            # simulated one.
            fut = asyncio.get_event_loop().create_future()
            worker_future_holder.append(fut)
            self._head = _FakeIntervention("iv-42", fut)

    inner = _HeadProducingTransport(head=None)
    proxy = ThreadedTransportProxy(lambda: inner)
    proxy.start()
    try:
        frame = await proxy.frames().__anext__()
        assert frame.message.text == "frame"

        head = proxy.pending_intervention_head()
        assert head == "iv-42", f"expected the bare id, got {head!r}"
        assert isinstance(head, str), (
            "the live _FakeIntervention (and its worker-bound Future) must "
            "never reach the caller thread through this seam"
        )
    finally:
        await proxy.shutdown()


@pytest.mark.asyncio
async def test_no_pending_intervention_stays_none():
    """Tier 2: regression guard — the ordinary "nothing pending" case
    (every pre-#5048 caller, including today's #4995 tests) is unaffected:
    ``None`` in, ``None`` out."""
    inner = _InterveningTransport(head=None)
    proxy = ThreadedTransportProxy(lambda: inner)
    proxy.start()
    try:
        frame = await proxy.frames().__anext__()
        assert frame.message.text == "frame"
        assert proxy.pending_intervention_head() is None
    finally:
        await proxy.shutdown()
