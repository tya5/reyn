"""Tier 2: `dispatch_id` (#6213) — reyn's own per-dispatch correlation id.

## The defect this closes

`app.py`'s `_running_tools` used to key a tool call's RUNNING row by
`op_id` (== `dispatcher.py`'s own `args_hash` — a deterministic CONTENT
fingerprint, `_compute_args_hash`'s own docstring: "collision risk is
acceptable for resume memoization" — collision is the DESIGN, not a
bug). Calling the SAME tool with the SAME args twice in one turn
therefore produced the SAME `op_id` twice: the second `started` row
silently overwrote the first row's handle in `_running_tools`, so the
first call's completion settled the WRONG (second) row, and the first
row stayed RUNNING forever (#6213's own repro).

## Two rejected fixes (both measured, both wrong — see #6213's own
## issue thread for the full record)

1. Key by `ctx.tool_call_id` (`dispatcher.py`'s own #5891 field, litellm's
   `tc["id"]`) instead. Rejected: that id is a THIRD PARTY's (architect's
   own classification: "借り物の一意性" — borrowed uniqueness), and it is
   `None` by DECLARATION for CodeAct/`/exec`/`/tasks`/pipeline callers —
   any fallback for that population reintroduces the exact same defect,
   just less visible.
2. Key by `tool_call_id`, falling back to `op_id` only when `tool_call_id`
   is `None`. Rejected: the fallback population is EXACTLY the one that
   most needs a real fix (declared-absent callers), and falling back to
   the same content fingerprint leaves that population's defect
   unchanged AND unnoticed (a correlation gap that stays invisible reads
   as fixed to a casual reader).

## The actual fix

`dispatch_tool` (`dispatcher.py`) mints its OWN id, unconditionally, once
per dispatch (`new_dispatch_id()` — same minting DISCIPLINE
`session_pure.py`'s `new_chain_id()` established, a distinct function
since this is a different id KIND) — the SAME scope that already computes
`args_hash` and emits all 3 lifecycle events (`tool_called` /
`tool_failed` / `tool_returned`), so one mint reaches all 3, no branch,
no fallback, no declared-absence population left over.

`ctx.tool_call_id` (#5891's own field, a DIFFERENT consumer's key for a
DIFFERENT purpose — joining `tool_returned` to its own history row) is
UNTOUCHED here — co-exists, never replaced (accept ⑥,
`test_5891_tool_call_id_audit_field.py` pins that field's own contract
separately and is NOT modified by this file).

`lifecycle_forwarder.py`'s own producer-side threading of this value
into `OutboxMessage.meta` has no dedicated test file (its own
`_enqueue_tool_call` docstring records the change) — it is exercised
end-to-end by `tests/interfaces/test_6213_running_tools_correlation.py`
(the actual `app.py` `_running_tools` fix this dispatcher-level id
exists to serve) and `tests/runtime/test_6213_derive_id_dispatch.py`
(`outbox.py`'s own derivation of `id`/`parent_id` from it).
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


def _make_ctx(*, tool_call_id: "str | None" = None) -> "tuple[DispatchContext, _FakeEventEmitter]":
    ev = _FakeEventEmitter()
    return (
        DispatchContext(
            caller_kind="router", caller_id="test_agent", chain_id="c1",
            tool_catalog=_CATALOG, events=ev, contextual=None,
            call_id="round_1", tool_call_id=tool_call_id,
        ),
        ev,
    )


async def _ok_invoker(args: dict) -> dict:
    return {"status": "ok"}


async def _raising_invoker(args: dict) -> dict:
    raise RuntimeError("boom")


async def _declared_error_invoker(args: dict) -> dict:
    return {"error": "cancelled"}


def test_dispatch_id_propagates_to_every_emitted_event_kind():
    """Tier 2: dispatch_id appears on tool_called AND its outcome, across
    all 3 ways a call can end (success, raised exception, handler-declared
    error) — mirrors call_id's own equivalent test in test_dispatcher.py,
    same shape, different field."""
    async def main():
        for invoker, expect_kind in (
            (_ok_invoker, "tool_returned"),
            (_raising_invoker, "tool_failed"),
            (_declared_error_invoker, "tool_failed"),
        ):
            ctx, ev = _make_ctx()
            await dispatch_tool(name="echo", args={}, ctx=ctx, invoker=invoker)
            types = [e[0] for e in ev.events]
            assert types == ["tool_called", expect_kind]
            # Unpacking into a single-element set IS the "same value on
            # both events" check (raises on 2+ distinct values) — not a
            # `len(...) == N` size pin.
            (only,) = {data["dispatch_id"] for _, data in ev.events}
            assert only, "dispatch_id must not be empty/None"
    asyncio.run(main())


def test_same_tool_same_args_called_twice_get_different_dispatch_ids():
    """Tier 2: LOAD-BEARING — the exact #6213 repro, at the dispatcher
    level. Two dispatches of the SAME tool with the SAME (empty) args
    share the SAME args_hash (accept ③'s own premise) but get DISTINCT
    dispatch_ids — this is the root-cause fix `_running_tools` keys off.

    strip: revert dispatch_id to a value derived from args (or drop the
    mint, reusing args_hash) -- both calls would get the SAME
    dispatch_id, reproducing the exact defect this field exists to
    close."""
    async def main():
        ev = _FakeEventEmitter()
        ctx1, _ = _make_ctx()
        ctx1.events = ev
        ctx2, _ = _make_ctx()
        ctx2.events = ev
        await dispatch_tool(name="echo", args={}, ctx=ctx1, invoker=_ok_invoker)
        await dispatch_tool(name="echo", args={}, ctx=ctx2, invoker=_ok_invoker)

        # Unpacking into exactly 2 named events IS the "one tool_called
        # per dispatch" check (raises on 0/1/3+), not a `len(...) == N`
        # size pin.
        first, second = (data for etype, data in ev.events if etype == "tool_called")
        # Same content fingerprint (accept③'s own premise: empty args are
        # deterministic) -- this is exactly what op_id/args_hash keying
        # could NOT tell apart.
        assert first["args_hash"] == second["args_hash"]
        # But dispatch_id DOES tell them apart.
        assert first["dispatch_id"] != second["dispatch_id"]
    asyncio.run(main())


def test_dispatch_id_is_present_even_when_tool_call_id_is_declared_none():
    """Tier 2: accept ④'s own dispatcher-level half — a caller with NO
    litellm tool_calls round behind it (tool_call_id declared None, the
    CodeAct/slash/pipeline population) still gets a real, non-None
    dispatch_id. Unconditional minting means this population is no
    longer a special case at all."""
    async def main():
        ctx, ev = _make_ctx(tool_call_id=None)
        await dispatch_tool(name="echo", args={}, ctx=ctx, invoker=_ok_invoker)
        called = [data for etype, data in ev.events if etype == "tool_called"][0]
        assert "tool_call_id" not in called, (
            "tool_call_id stays off tool_called regardless (see the "
            "dedicated test below) -- this test is only about dispatch_id"
        )
        assert called["dispatch_id"], "dispatch_id must be minted regardless"
    asyncio.run(main())


def test_tool_call_id_is_unaffected_and_stays_returned_only():
    """Tier 2: accept ⑥'s own dispatcher-level half — dispatch_id's
    addition does not touch tool_call_id's existing contract: still
    present on tool_returned (with its own caller-supplied value,
    verbatim), still absent from tool_called/tool_failed's own emitted
    keys (this field's pre-existing shape, #5891 — unchanged)."""
    async def main():
        ctx, ev = _make_ctx(tool_call_id="call_xyz")
        await dispatch_tool(name="echo", args={}, ctx=ctx, invoker=_ok_invoker)
        called = [data for etype, data in ev.events if etype == "tool_called"][0]
        returned = [data for etype, data in ev.events if etype == "tool_returned"][0]
        assert "tool_call_id" not in called, (
            "tool_call_id must stay OFF tool_called -- dispatch_id is the "
            "new field there, not an extension of tool_call_id's own population"
        )
        assert returned["tool_call_id"] == "call_xyz"
    asyncio.run(main())
