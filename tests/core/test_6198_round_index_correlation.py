"""Tier 2: `round_index` (#6198) — reyn's own round fact, threaded through
`DispatchContext`/`dispatch_tool` the SAME explicit-parameter path
`call_id` already uses.

## Why this exists

`app.py`'s `_call_parents` Group-parent key moved off `call_id` (a THIRD
PARTY's identifier — litellm's own response `id`, uniqueness scope
undocumented by either OpenAI or litellm) onto `turn:{chain_id}/round:
{round_index}` (reyn's own facts, #6198's own structural fix). For a
TOOL row (`tool_call_started`/`completed`/`failed`) to build the SAME
key its parent registered under, `round_index` has to reach
`dispatch_tool`'s own emitted events — this file pins that it does,
mirroring `test_dispatcher.py`'s own `test_call_id_propagates_to_every_
emitted_event_kind` and #6213's own `test_6213_dispatch_id_correlation.
py` for the SAME shape (a NEW correlation field, propagated the SAME
way an existing one already is).

## #4734's own lesson, applied here too

`call_id` is an EXPLICIT parameter through `_dispatch_resolved` and
every one of its 3 callers (never a stored `self._current_call_id`) —
a stored field silently inherits a PRIOR round's value for a dispatch
reached outside the exact per-round reassignment path (#4734's own
incident). `round_index` follows the IDENTICAL discipline (`router_
loop.py`'s own docstrings at each new call site) — this file cannot
prove staleness-freedom itself (that needs an actual async-deferred
caller reaching dispatch outside its own round, which no production
caller does today — #6198 issue thread), but it DOES pin that the
value dispatch_tool emits is whatever was explicitly passed, never
silently substituted.
"""
from __future__ import annotations

import asyncio
from typing import Any

from reyn.core.dispatch import DispatchContext, dispatch_tool

_CATALOG = {"echo": {"function": {"name": "echo"}}}


class _FakeEventEmitter:
    def __init__(self) -> None:
        self.events: "list[tuple[str, dict]]" = []

    def emit(self, event_type: str, **data: Any) -> None:
        self.events.append((event_type, data))


def _make_ctx(*, round_index: "int | None" = None) -> "tuple[DispatchContext, _FakeEventEmitter]":
    ev = _FakeEventEmitter()
    return (
        DispatchContext(
            caller_kind="router", caller_id="test_agent", chain_id="c1",
            tool_catalog=_CATALOG, events=ev, contextual=None,
            call_id="resp-1", tool_call_id=None, round_index=round_index,
        ),
        ev,
    )


async def _ok_invoker(args: dict) -> dict:
    return {"status": "ok"}


async def _raising_invoker(args: dict) -> dict:
    raise RuntimeError("boom")


async def _declared_error_invoker(args: dict) -> dict:
    return {"error": "cancelled"}


def test_round_index_propagates_to_every_emitted_event_kind():
    """Tier 2: round_index appears on tool_called AND its outcome,
    across all 3 ways a call can end (success, raised exception,
    handler-declared error) — mirrors call_id's/dispatch_id's own
    equivalent tests, same shape, different field."""
    async def main():
        for invoker, expect_kind in (
            (_ok_invoker, "tool_returned"),
            (_raising_invoker, "tool_failed"),
            (_declared_error_invoker, "tool_failed"),
        ):
            ctx, ev = _make_ctx(round_index=3)
            await dispatch_tool(name="echo", args={}, ctx=ctx, invoker=invoker)
            types = [e[0] for e in ev.events]
            assert types == ["tool_called", expect_kind]
            # Unpacking into a single-element set IS the "same value on
            # both events" check (raises on 2+ distinct values) — not a
            # `len(...) == N` size pin.
            (only,) = {data["round_index"] for _, data in ev.events}
            assert only == 3
    asyncio.run(main())


def test_round_index_is_none_when_the_caller_passes_none():
    """Tier 2: LOAD-BEARING — round_index is NEVER silently substituted.
    A caller that explicitly passes None (the pre-#6198 / declared-
    absent shape) gets None on every emitted event, not some fallback
    value. strip: read `ctx.round_index or 0` instead of `ctx.
    round_index` verbatim at any emit site -- this test would then see
    `0` instead of `None` and fail."""
    async def main():
        ctx, ev = _make_ctx(round_index=None)
        await dispatch_tool(name="echo", args={}, ctx=ctx, invoker=_ok_invoker)
        called = [data for etype, data in ev.events if etype == "tool_called"][0]
        assert called["round_index"] is None
    asyncio.run(main())


def test_different_round_index_values_are_not_collapsed():
    """Tier 2: two dispatches under different rounds keep DISTINCT
    round_index values on their own events — the exact property
    app.py's Group-parent key relies on to tell two rounds apart."""
    async def main():
        ev = _FakeEventEmitter()
        ctx1, _ = _make_ctx(round_index=1)
        ctx1.events = ev
        ctx2, _ = _make_ctx(round_index=2)
        ctx2.events = ev
        await dispatch_tool(name="echo", args={}, ctx=ctx1, invoker=_ok_invoker)
        await dispatch_tool(name="echo", args={}, ctx=ctx2, invoker=_ok_invoker)

        first, second = (data for etype, data in ev.events if etype == "tool_called")
        assert first["round_index"] == 1
        assert second["round_index"] == 2
    asyncio.run(main())
