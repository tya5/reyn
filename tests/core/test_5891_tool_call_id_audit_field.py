"""Tier 2: `tool_returned`'s `tool_call_id` field (#5891 (c), architect
ruling, corrected twice) — the ONE tool call's own `tc["id"]`, distinct
from `call_id` (which only identifies the litellm ROUND, shared by every
`tool_calls` entry in it).

## Why this exists (architect's first correction)

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

## Why there is no `tool_call_id_absent_reason` field (architect's SECOND
## correction, lead-coder BLOCKING on PR #5970)

The first fix gave `tool_call_id` a `None` default and added a sibling
`tool_call_id_absent_reason` naming `caller_kind` to explain a `None`.
Rejected: `caller_kind` cannot actually distinguish "no id to give" from
"one was dropped in transit", because a CodeAct in-snippet `tool()` call
has the SAME `caller_kind="router"` an Execute-round tool_calls dispatch
does — the reason field repeated information the event already carried
(`caller_kind` itself) while claiming to explain something it could not.

The fix instead: `tool_call_id` is a REQUIRED keyword-only field, no
default (the SAME shape #5960's `hydrate` flip and `DispatchContext.
contextual` already use — `test_dispatcher.py`'s own `test_dispatch_
context_construction_without_contextual_raises_type_error` is the
precedent this mirrors). Omitting it is a real Python `TypeError`, not a
silent default, so `tool_call_id: None` in an emitted event is now
ALWAYS the caller's own DECLARATION of absence — self-describing, no
reason field needed. These tests pin the dispatcher-level half only (the
key is never omitted either way, present or absent).

⚠️ There is deliberately no gate proving "every caller_kind='router'
event has a real id" — architect's own first attempt at one (a census:
"caller_kind='router' + tool_call_id: null events are 0") was proposed
and then FALSIFIED by this codebase's own existing code: CodeAct's
`_os_gate` (`router_loop.py`) is `caller_kind="router"` AND declares
`None` by design, so that census would have flagged a correct, intended
line as though it were a bug. The real safeguard is structural, not
data-derived: `_dispatch_resolved` is the ONE place a `caller_kind=
"router"` `DispatchContext` is ever built, so its 3 real callers are
enumerable by reading `router_loop.py` directly — see `DispatchContext.
tool_call_id`'s own docstring in `dispatcher.py` for the full table."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from reyn.core.dispatch import DispatchContext, dispatch_tool

_CATALOG = {"echo": {"function": {"name": "echo"}}}


class _FakeEventEmitter:
    def __init__(self) -> None:
        self.events: "list[tuple[str, dict]]" = []

    def emit(self, event_type: str, **data: Any) -> None:
        self.events.append((event_type, data))


def _make_ctx(*, tool_call_id: "str | None") -> "tuple[DispatchContext, _FakeEventEmitter]":
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
            "no reason field exists any more -- tool_call_id being a "
            "required field is what makes one unnecessary"
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


def test_a_caller_that_has_no_id_declares_none_explicitly():
    """Tier 2: a caller with no litellm tool_calls entry at all (CodeAct
    `tool()`, `/exec`/`/tasks` slash, a pipeline `tool:` step — modeled
    here by an operator-kind caller, one of the real such shapes)
    DECLARES `tool_call_id=None` explicitly. The event still carries the
    key (never omitted -- `None` is a real, meaningful value, distinct
    from "no key at all"), and there is no sibling reason field: the
    None itself, on a required field, already IS the declaration."""
    async def main():
        ev = _FakeEventEmitter()
        ctx = DispatchContext(
            caller_kind="operator", caller_id="cli", chain_id=None,
            tool_catalog=_CATALOG, events=ev, contextual=None,
            tool_call_id=None,
        )
        await dispatch_tool(name="echo", args={}, ctx=ctx, invoker=_echo_invoker)
        (returned,) = _tool_returned_events(ev)
        assert "tool_call_id" in returned, "the key must never be omitted"
        assert returned["tool_call_id"] is None
        assert "tool_call_id_absent_reason" not in returned
    asyncio.run(main())


def test_omitting_tool_call_id_raises_type_error_not_a_silent_default():
    """Tier 2: LOAD-BEARING — the structural guarantee itself. Omitting
    `tool_call_id` at DispatchContext construction is a real TypeError
    (no default exists to silently fall back to), so a caller can no
    longer "forget" to declare absence -- every `tool_call_id: None` an
    event ever carries was a deliberate choice, never an accident.
    strip: give `tool_call_id` a `None` default back in dispatcher.py --
    this construction stops raising (see test_dispatcher.py's own
    dedicated field-introspection test for the fuller version of this
    witness, mirroring `contextual`'s identical precedent)."""
    with pytest.raises(TypeError):
        DispatchContext(
            caller_kind="router", caller_id="test_agent", chain_id="c1",
            tool_catalog=_CATALOG, events=_FakeEventEmitter(), contextual=None,
            # tool_call_id= deliberately omitted
        )
