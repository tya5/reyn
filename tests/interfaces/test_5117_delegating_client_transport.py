"""Tier 2: #5117 (architect ruling, class B) — ``DelegatingClientTransport``
forwards to its inner transport by DEFAULT, for every method, even ones a
subclass never mentions.

The defect this closes: ``ClientTransportStub``'s 9 convenience defaults
answer with a FIXED value (``False``/``[]``/``None``/…) — correct for a
self-contained ``tests/`` fixture with nothing behind it, wrong for a
wrapper that DOES have an inner transport, because the fixed value is a
claim the wrapper cannot actually back (a no-op ``clear_pending_command_ui``
asserts "nothing to clear" — only the layer that owns that state can say
that). Acceptance (lead-coder): a wrapper that implements NOT A SINGLE
method must still reach the inner transport.

Real ``ClientTransportStub`` subclass as the inner transport throughout —
no mocks. Distinguishing values (not ``ClientTransportStub``'s own
defaults) prove genuine forwarding rather than two defaults coincidentally
matching.
"""
from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

import pytest

from reyn.interfaces.transport.client_transport import (
    ClientTransportStub,
    DelegatingClientTransport,
)
from reyn.runtime.outbox import OutboxMessage


class _RecordingInner(ClientTransportStub):
    """A real inner transport whose answers are DISTINCTIVE, not
    ``ClientTransportStub``'s own defaults — so a passing assertion proves
    the wrapper actually reached this instance, not that two unrelated
    defaults happened to agree. Fills in the remaining (always-abstract on
    ``ClientTransportStub`` too) methods with the same minimal shape
    ``tests/_support/slash.py``'s own ``RecordingTransport`` uses."""

    def __init__(self) -> None:
        self.cleared = False
        self.state_ready_called = False
        self.displayed: "list[OutboxMessage]" = []

    def start(self) -> None: ...
    def close(self) -> None: ...

    def frames(self) -> "AsyncIterator[object]":
        raise NotImplementedError("not exercised by this test file")

    async def submit_user_text(self, text: str) -> str:
        return ""

    async def answer_intervention_text(self, text: str, *, intervention_id=None) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str, *, intervention_id=None) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    async def cancel_inflight(self) -> str:
        return ""

    async def shutdown(self) -> None: ...

    def put_display(self, msg: "OutboxMessage") -> None:
        self.displayed.append(msg)

    async def clear_pending_command_ui(self) -> None:
        self.cleared = True

    async def state_ready(self) -> None:
        self.state_ready_called = True

    def reyn_state_root(self) -> "Path | None":
        return Path("/distinctive-marker")

    async def request_session_list(self) -> "list[dict]":
        return [{"sid": "distinctive-marker"}]


class _ZeroOverrideWrapper(DelegatingClientTransport):
    """Implements NOT A SINGLE method — the exact acceptance shape
    (lead-coder): every call must still reach ``self._inner``."""


@pytest.mark.asyncio
async def test_zero_override_wrapper_reaches_the_inner_transport_command() -> None:
    """Tier 2: the COMMAND case #5117 exists for — a wrapper that overrides
    nothing still delivers ``clear_pending_command_ui`` to the inner
    transport, rather than silently no-op'ing (``ClientTransportStub``'s
    own default, which is right for a bare fixture, wrong here).

    Strip-falsifier: reverting ``DelegatingClientTransport.clear_pending_
    command_ui`` to ``return None`` (``ClientTransportStub``'s own body)
    turns this red (``inner.cleared`` stays ``False``) — verified locally."""
    inner = _RecordingInner()
    wrapper = _ZeroOverrideWrapper(inner)

    await wrapper.clear_pending_command_ui()

    assert inner.cleared is True


@pytest.mark.asyncio
async def test_zero_override_wrapper_reaches_the_inner_transport_query() -> None:
    """Tier 2: the QUERY case (``state_ready``) — same shape, the other
    axis #5117 separates (architect: a query and a command are different
    classes, both need forwarding for a wrapper specifically)."""
    inner = _RecordingInner()
    wrapper = _ZeroOverrideWrapper(inner)

    await wrapper.state_ready()

    assert inner.state_ready_called is True


@pytest.mark.asyncio
async def test_zero_override_wrapper_forwards_a_returned_value() -> None:
    """Tier 2: forwarding also carries the inner transport's RETURN value
    back out — not just a fire-and-forget call. Uses a distinctive value
    (not ``ClientTransportStub``'s own ``[]`` default) so a pass cannot be
    two unrelated defaults agreeing by coincidence."""
    inner = _RecordingInner()
    wrapper = _ZeroOverrideWrapper(inner)

    assert await wrapper.request_session_list() == [{"sid": "distinctive-marker"}]
    assert wrapper.reyn_state_root() == Path("/distinctive-marker")


@pytest.mark.asyncio
async def test_a_subclass_overriding_one_method_still_forwards_the_rest() -> None:
    """Tier 2: the real-world shape (mirrors ``_ErrorWatchingTransport``,
    the existing hand-written wrapper this class is meant to relieve) — a
    subclass that overrides put_display to watch for errors must still get
    every OTHER method forwarded for free, with no need to mention them."""

    class _WatchingWrapper(DelegatingClientTransport):
        def __init__(self, inner) -> None:
            super().__init__(inner)
            self.saw_error = False

        def put_display(self, msg: "OutboxMessage") -> None:
            if msg.kind == "error":
                self.saw_error = True
            self._inner.put_display(msg)

    inner = _RecordingInner()
    wrapper = _WatchingWrapper(inner)
    msg = OutboxMessage(kind="error", text="boom")

    wrapper.put_display(msg)
    assert wrapper.saw_error is True
    assert inner.displayed == [msg], "the overridden method must still forward to inner"

    # The method it did NOT override still reaches the inner transport.
    await wrapper.clear_pending_command_ui()
    assert inner.cleared is True
