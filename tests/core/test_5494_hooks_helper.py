"""Tier 2: #5494 — ``tests/_support/hooks.py``'s ``collect_hook_events``/
``run_one_turn`` test helpers.

Same skeleton as ``test_3868_collect_events_helper.py``'s own #5467-phase-1
Session-acceptance tests — architect's own bar here is the same: this seam
works with the exact object a caller who only holds a ``Session`` (no inbox
access, no internal ``HookBus``/``RouterLoopDriver`` reference) actually
has. Real ``Session``/``HookBus``/``RouterLoop`` throughout, no mocks.

Load-bearing-ness (lead-coder BLOCKING, PR #5500): NOT pinned via a
name-absence assert on ``Session`` (``not hasattr(session, "...")``) — that
shape is inverted (turns red the day a legitimate public seam is added for
a real consumer) and incomplete (blind to a differently-named seam). Both
helpers' private reach is instead shown load-bearing by STRIP-FALSIFY,
performed BY HAND: neutralizing ``collect_hook_events``'s body (raise
instead of subscribing) turns 3 tests below RED; neutralizing
``run_one_turn``'s delegate call turns ``test_run_one_turn_drives_the_
real_router_loop`` RED. Both restored, confirmed GREEN again. See each
removed test's own comment, at its former location below, for the
detailed reasoning (same family #5449 already closed the opposite
direction of — see ``test_5447_doctor_hook_env_single_source.py:24-30``).
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.emit_hook_event import handle as emit_handle
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import EmitHookEventIROp
from reyn.security.permissions.permissions import PermissionDecl
from tests._support.agent_session import make_session
from tests._support.events import collect_events, settle
from tests._support.hooks import collect_hook_events, run_one_turn

# ---------------------------------------------------------------------------
# collect_hook_events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collect_hook_events_observes_a_real_publish() -> None:
    """Tier 2: witness ① — a subscription obtained via the helper observes
    a REAL event published onto this session's own hook bus. Effect
    through the seam (a real op dispatch), never a private peek at
    ``_hook_bus`` at the assertion site — the private reach lives only
    inside ``collect_hook_events`` itself."""
    session = make_session(agent_name="hooks-helper-witness")
    sub = collect_hook_events(session)

    # #5739: a real Workspace, not None — emit_hook_event never reads
    # ctx.workspace, but the field is declared non-Optional.
    ctx = OpContext(
        workspace=Workspace(session._audit_events),
        events=session._audit_events, permission_decl=PermissionDecl(),
        session_id=session.session_id, hook_bus=session._hook_bus,
    )
    op = EmitHookEventIROp(kind="emit_hook_event", event_name="ping")
    result = await emit_handle(op, ctx)
    assert result["status"] == "ok"

    event = sub.get_nowait()
    assert event.kind == f"llm:{session.session_id}:ping"


@pytest.mark.asyncio
async def test_collect_hook_events_strip_falsify_wrong_session_sees_nothing() -> None:
    """Tier 2: witness ② — strip-falsify by construction (not by editing
    source) — a subscription obtained for a DIFFERENT session's bus does
    not see this session's publish, proving the assertion above is
    load-bearing on the subscription actually being wired to the SAME bus
    the publish landed on, not merely "some queue got something"."""
    session_a = make_session(agent_name="hooks-helper-witness-a")
    session_b = make_session(agent_name="hooks-helper-witness-b")
    sub_b = collect_hook_events(session_b)

    ctx = OpContext(
        workspace=Workspace(session_a._audit_events),
        events=session_a._audit_events, permission_decl=PermissionDecl(),
        session_id=session_a.session_id, hook_bus=session_a._hook_bus,
    )
    op = EmitHookEventIROp(kind="emit_hook_event", event_name="ping")
    await emit_handle(op, ctx)

    with pytest.raises(asyncio.QueueEmpty):
        sub_b.get_nowait()


def test_collect_hook_events_returns_the_hook_bus_own_subscription_type() -> None:
    """Tier 1: the returned object is ``HookBus``'s own native subscription
    shape (the same one ``reyn.hooks.composer``/``composed_consumer``
    already use internally as real production subscribers) — not adapted
    into a callback-list shape ``collect_events`` happens to use for a
    DIFFERENT underlying mechanism (``EventLog.add_subscriber``)."""
    from reyn.hooks.bus import HookBusSubscription

    session = make_session(agent_name="hooks-helper-witness-type")
    sub = collect_hook_events(session)
    assert isinstance(sub, HookBusSubscription)


# load-bearing-ness witness (lead-coder BLOCKING, PR #5500): a prior version
# of this file pinned NON-EXISTENCE of a specific method name
# (`assert not hasattr(session, "subscribe_hook_events")`) as the load-
# bearing witness for `collect_hook_events`'s private reach. Wrong on two
# counts (same family #5449 already closed — see test_5447_doctor_hook_
# env_single_source.py:24-30 — for the opposite direction of the same
# "what absence means" mistake): (1) inverted — the day a public seam is
# legitimately added for a real production consumer (architect's own
# rejection condition was "no consumer TODAY", not "never"), this test
# turns red and punishes the fixer, not the regression; (2) incomplete —
# a public seam added under any OTHER name (`hook_events()`,
# `observe_hooks()`, ...) leaves this assert green while the claim it
# encodes goes false. Deleted; load-bearing-ness is shown by
# STRIP-FALSIFY instead (performed by hand, #5500): replacing
# `collect_hook_events`'s body with `raise RuntimeError(...)` turns the
# three tests above RED (confirmed); restoring makes them green again.
# `test_collect_hook_events_returns_the_hook_bus_own_subscription_type`
# above already covers the "what exists" half safely (asserts the
# returned object's type, which cannot go stale the way a name-absence
# check does).


# ---------------------------------------------------------------------------
# run_one_turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_one_turn_drives_the_real_router_loop() -> None:
    """Tier 2: ``run_one_turn`` reaches the REAL router loop for real — not
    merely "the stub was called". A private `run_turn` replacement records
    what it was invoked with (the same substitution class this repo's
    testing policy already sanctions — replacing ONLY the LLM-adjacent
    boundary, not the turn machinery around it), proving `run_one_turn`
    genuinely drove the call rather than being a no-op wrapper."""
    session = make_session(agent_name="run-one-turn-helper-witness")
    events = collect_events(session._audit_events)

    calls: list[str] = []

    async def _record_run_turn(text: str, chain_id: str) -> None:
        calls.append(text)

    session._loop_driver.run_turn = _record_run_turn  # type: ignore[method-assign]

    await run_one_turn(session, "hello", "run-one-turn-chain")
    await settle(session._audit_events)

    assert calls == ["hello"]
    assert any(e.type == "turn_completed" for e in events)


# load-bearing-ness witness: same correction as collect_hook_events's own
# above (lead-coder BLOCKING, PR #5500) — a name-absence assert on Session
# would be inverted (turns red the day a legitimate public seam is added)
# and incomplete (blind to a differently-named one). Load-bearing-ness of
# `run_one_turn`'s private reach is already shown by strip-falsify, BY
# HAND, earlier in this arc (documented in the module docstring above):
# neutralizing the delegate call inside `run_one_turn` turned
# `test_run_one_turn_drives_the_real_router_loop` RED; restored, GREEN.
