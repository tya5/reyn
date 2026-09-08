"""Tier 2: `tool_returned`'s `tool_call_id` field (#5891 (c), architect
ruling) — the ONE tool call's own `tc["id"]`, distinct from `call_id`
(which only identifies the litellm ROUND, shared by every `tool_calls`
entry in it).

## Why this exists (architect's own correction)

The original #5891 design ("audit event carries an excerpt/hash, the
history row already has a `content_ref`, a reader can JOIN the two on
data both sides already have") turned out to rest on a false premise: a
`tool_returned` event's `call_id` and a history row's `tool_call_id` are
different GRANULARITIES — the former is shared by every tool call in one
litellm round, the latter is per-call — so calling the SAME tool twice in
one round makes `(call_id, tool)` ambiguous between the two resulting
history rows. `DispatchContext.tool_call_id` (the SAME `tc["id"]`
`RouterLoop.feedback` already writes onto its own history row) is the
key that actually disambiguates them, with no new storage — see
`tests/runtime/test_5891_tool_call_id_history_join.py` for the join
witness through both real halves.

These tests pin the dispatcher-level half only: the field is present and
distinct across two calls, and a caller with no litellm tool_calls entry
at all (the DispatchContext default) gets `tool_call_id: None` plus a
NAMED absence reason — never a silently omitted key, which would make
"the caller doesn't have one" indistinguishable from "it was forgotten"
(the same discipline `_persist_tool_returned`'s own
`content_ref_unavailable` already uses one layer down, #5936)."""
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


def _make_ctx(*, tool_call_id: "str | None" = None) -> "tuple[DispatchContext, _FakeEventEmitter]":
    ev = _FakeEventEmitter()
    return (
        DispatchContext(
            caller_kind="router",
            caller_id="test_agent",
            chain_id="c1",
            tool_catalog=_CATALOG,
            events=ev,
            contextual=None,
            call_id="round_1",
            tool_call_id=tool_call_id,
        ),
        ev,
    )


async def _echo_invoker(args: dict) -> dict:
    return {"echoed": args}


def _tool_returned_events(ev: _FakeEventEmitter) -> "list[dict]":
    return [data for etype, data in ev.events if etype == "tool_returned"]


def test_tool_call_id_reaches_the_tool_returned_event():
    """Tier 2: a caller-supplied `tool_call_id` is threaded onto the
    emitted `tool_returned` event, verbatim."""
    async def main():
        ctx, ev = _make_ctx(tool_call_id="call_abc123")
        await dispatch_tool(name="echo", args={}, ctx=ctx, invoker=_echo_invoker)
        (returned,) = _tool_returned_events(ev)
        assert returned["tool_call_id"] == "call_abc123"
        assert "tool_call_id_absent_reason" not in returned, (
            "the reason field must be ABSENT (not even present as None) "
            "when a real id was supplied -- only the None case adds it"
        )
    asyncio.run(main())


def test_two_calls_in_one_round_get_distinct_tool_call_ids():
    """Tier 2: LOAD-BEARING — the exact shape `call_id` alone cannot
    disambiguate (architect's own falsification of the original #5891
    design). Two dispatches sharing the SAME `call_id` (one litellm
    round) but different `tool_call_id`s (two tool_calls entries) produce
    two `tool_returned` events whose `tool_call_id`s are themselves
    distinct and match their own caller-supplied id.

    strip: drop `tool_call_id=ctx.tool_call_id` from dispatcher.py's emit
    call -- both events would carry only the shared `call_id`, making them
    indistinguishable by call identity alone."""
    async def main():
        # Both contexts share one emitter (one round's own event stream)
        # and one call_id (the SAME litellm round), differing ONLY in
        # tool_call_id -- the one variable this test isolates.
        ev = _FakeEventEmitter()
        ctx1 = DispatchContext(
            caller_kind="router", caller_id="test_agent", chain_id="c1",
            tool_catalog=_CATALOG, events=ev, contextual=None,
            call_id="round_1", tool_call_id="call_1",
        )
        ctx2 = DispatchContext(
            caller_kind="router", caller_id="test_agent", chain_id="c1",
            tool_catalog=_CATALOG, events=ev, contextual=None,
            call_id="round_1", tool_call_id="call_2",
        )
        await dispatch_tool(name="echo", args={}, ctx=ctx1, invoker=_echo_invoker)
        await dispatch_tool(name="echo", args={}, ctx=ctx2, invoker=_echo_invoker)

        first, second = _tool_returned_events(ev)  # exactly 2, or unpacking itself raises
        returned = [first, second]
        assert {r["call_id"] for r in returned} == {"round_1"}, (
            "both calls really do share one round -- call_id alone cannot "
            "tell them apart, which is exactly why tool_call_id exists"
        )
        assert {r["tool_call_id"] for r in returned} == {"call_1", "call_2"}, (
            "tool_call_id must disambiguate what call_id cannot"
        )
    asyncio.run(main())


def test_absent_tool_call_id_is_null_with_a_named_reason_not_omitted():
    """Tier 2: LOAD-BEARING — a caller with no litellm tool_calls entry at
    all (CodeAct `tool()`, `/exec`/`/tasks` slash, a pipeline `tool:`
    step — modeled here by simply not threading one, the
    `DispatchContext` default) gets `tool_call_id: None` PLUS
    `tool_call_id_absent_reason` naming `caller_kind` — the key is never
    silently dropped, which would make "no id to give" indistinguishable
    from "a real id was forgotten"."""
    async def main():
        ctx, ev = _make_ctx(tool_call_id=None)
        await dispatch_tool(name="echo", args={}, ctx=ctx, invoker=_echo_invoker)
        (returned,) = _tool_returned_events(ev)
        assert "tool_call_id" in returned, "the key must never be omitted"
        assert returned["tool_call_id"] is None
        assert returned["tool_call_id_absent_reason"] == "router"
    asyncio.run(main())


def test_operator_caller_kind_absence_reason_names_operator():
    """Tier 2: accept-side variety for the absence reason — a slash-driven
    operator call (no litellm round behind it at all, `caller_kind=
    "operator"`) names ITS OWN caller_kind, not a hardcoded "router"."""
    async def main():
        ev = _FakeEventEmitter()
        ctx = DispatchContext(
            caller_kind="operator", caller_id="cli", chain_id=None,
            tool_catalog=_CATALOG, events=ev, contextual=None,
        )
        await dispatch_tool(name="echo", args={}, ctx=ctx, invoker=_echo_invoker)
        (returned,) = _tool_returned_events(ev)
        assert returned["tool_call_id"] is None
        assert returned["tool_call_id_absent_reason"] == "operator"
    asyncio.run(main())
