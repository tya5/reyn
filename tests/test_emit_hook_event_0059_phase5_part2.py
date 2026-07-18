"""Tests for the Hook-Event Redesign Phase 5 part 2 — ``emit_hook_event``
Control-IR op (LLM-emit, proposal ``docs/deep-dives/proposals/
0059-hook-event-redesign.md`` §8/§8.4). This is the security-crux arc: the
LLM gains the ability to publish onto a live ``HookBus``, and the autonomy
boundary here is enforced in TWO SEPARATE dimensions by
``reyn.core.op_runtime.emit_hook_event.handle`` (see its module docstring):

Issue #2887 (architect post-#2885-review F2, audit band): the production
LLM path is the router TOOL (``reyn.tools.emit_hook_event._handle_emit_hook_event``),
which calls ``op_runtime.emit_hook_event.handle`` DIRECTLY — not through
``execute_op`` (whose except-``PermissionError`` branch is what emits the
``permission_denied`` P6 audit-event in the tests above). Verified (primary
data, not inferred): a denied emit on the router-tool path raises
``EmitHookEventDenied`` uncaught out of the tool handler, propagates through
``reyn.tools.dispatch.invoke_tool`` (a thin pass-through, no try/except), and
is caught by ``reyn.core.dispatch.dispatcher.dispatch_tool``'s generic
``except PermissionError`` (``dispatcher.py:128-141``) — the SAME shared
chokepoint every router tool call funnels through. That branch emits a
``tool_failed`` P6 audit-event with ``error_kind="permission_denied"`` (a
different event NAME/shape than ``execute_op``'s ``permission_denied`` event,
but not a missing one) — VERDICT: WHITE, already audited, no code change.
``test_denied_emit_via_production_router_tool_path_is_audited`` below pins
this on the REAL router-tool dispatch path so it can't silently regress.

Coverage plan
-------------
Tier 1 (contract): ``reyn.hooks.schema_registry.is_emittable_llm_kind`` — the
  static OUT-set whitelist predicate itself, real function, no fakes.
Tier 2 (OS invariant, ②A KIND-dimension bound-test-must-flip): a non-
  whitelisted kind (``webhook:github:push``, another session's ``llm:*``)
  is REJECTED via the REAL ``execute_op`` dispatch path — never reaches
  ``HookBus.publish``. Falsified by hand (documented in the test): neutralize
  the handler's whitelist gate (comment out the ``is_emittable_llm_kind``
  check) → the SAME emit succeeds and the event lands on the bus → restore.
Tier 2 (OS invariant, ②B SESSION-dimension structural-impossibility): an
  emit whose (defense-in-depth) ``target_kind`` names a FOREIGN session's
  ``llm:*`` namespace never reaches that foreign session's bus — observed
  via two REAL, independent ``HookBus`` instances (one per OpContext), not
  a private-state assertion.
Tier 2 (OS invariant, ③ emit-origin self-wake force-close — the
  STRENGTHENED loop-valve pin): extends ``tests/
  test_hook_composer_reachability_phase5.py``'s external-``file_changed``-
  origin self-stimulating chain to an ``emit_hook_event``-OP origin: each
  driven turn's "LLM" calls the real ``emit_hook_event`` handler again,
  which a Composer correlates into a ``composed:*`` wake — a genuinely
  unbounded chain that force-closes at ``max_hook_driven_turns`` with ZERO
  new bounding logic (the SAME inbox ``kind="hook"`` E-path every other
  hook-driven wake uses).

Policy (docs/deep-dives/contributing/testing.md): real ``OpContext`` / real
``HookBus`` / real ``execute_op`` / real ``Session`` — no
``unittest.mock``/``MagicMock``/``AsyncMock``/``patch``. Only the LLM
boundary (``session._loop_driver.run_turn``) is replaced with a plain async
recorder that itself calls the REAL ``emit_hook_event`` handler — the same
substitution class ``tests/test_hook_composer_reachability_phase5.py`` and
``tests/test_hook_loop_valve_1800_7.py`` already establish as compliant.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pydantic
import pytest

from reyn.config.chat import LoopConfig, OnLimitConfig, SafetyConfig
from reyn.core.dispatch import DispatchContext, dispatch_tool
from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog
from reyn.core.op_runtime import execute_op
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.emit_hook_event import handle as emit_handle
from reyn.hooks.bus import HookBus
from reyn.hooks.schema_registry import is_emittable_llm_kind
from reyn.runtime.session import Session
from reyn.runtime.session_params import ReactivityConfig
from reyn.schemas.models import EmitHookEventIROp
from reyn.security.permissions.permissions import PermissionDecl
from reyn.tools.emit_hook_event import EMIT_HOOK_EVENT
from reyn.tools.types import RouterCallerState, ToolContext

_POLL_TIMEOUT = 3.0
_POLL_INTERVAL = 0.01


async def _wait_until(predicate, *, timeout: float = _POLL_TIMEOUT) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(_POLL_INTERVAL)


def _op_context(*, session_id: str, hook_bus: HookBus, events: EventLog) -> OpContext:
    return OpContext(
        workspace=None,
        events=events,
        permission_decl=PermissionDecl(),
        session_id=session_id,
        hook_bus=hook_bus,
    )


# ---------------------------------------------------------------------------
# Tier 1: is_emittable_llm_kind — the static whitelist predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,session_id,expected",
    [
        ("llm:sess-A:deploy_ready", "sess-A", True),
        ("llm:sess-A:", "sess-A", False),  # empty event_name suffix
        ("llm:sess-B:deploy_ready", "sess-A", False),  # another session
        ("builtin:lifecycle:turn_end", "sess-A", False),  # spoofs Reyn lifecycle
        ("composed:deploy_approved", "sess-A", False),  # spoofs Composer output
        ("webhook:github:push", "sess-A", False),  # spoofs external ingress
        ("mcp:github:resource_updated", "sess-A", False),  # spoofs MCP ingress
        ("llm:sess-A:deploy_ready", "", False),  # no session identity at all
    ],
)
def test_is_emittable_llm_kind_whitelist_shape(kind, session_id, expected):
    """Tier 1: the OUT-set whitelist is an ALLOW-list (self llm:* only) —
    every other namespace, and any other session's llm:*, is False."""
    assert is_emittable_llm_kind(kind, session_id) is expected


# ---------------------------------------------------------------------------
# Tier 1: EmitHookEventIROp.event_name schema constraint (#2890 F6)
# ---------------------------------------------------------------------------


def test_event_name_accepts_ordinary_names():
    """Tier 1: (#2890 F6) the normal shape (letters/digits/._-) is unaffected
    by the new pattern/max_length constraint."""
    op = EmitHookEventIROp(kind="emit_hook_event", event_name="deploy-ready.v2")
    assert op.event_name == "deploy-ready.v2"


@pytest.mark.parametrize(
    "event_name",
    [
        "deploy\nready",  # newline
        "deploy\x00ready",  # control char (NUL)
        "deploy ready",  # space is outside the allowed charset
        "deploy/ready",  # slash — no namespace-separator smuggling
        "x" * 201,  # over max_length
    ],
)
def test_event_name_rejects_control_chars_and_over_length(event_name):
    """Tier 1: (#2890 F6) control characters / newlines / disallowed chars /
    over-length values are rejected at schema-validation time (pydantic
    ``ValidationError``) — they can no longer flow into the constructed
    ``kind`` and from there into the P6 audit-event.

    Strip-falsify: remove the ``pattern=``/``max_length=`` constraint from
    ``EmitHookEventIROp.event_name`` in ``reyn/schemas/models.py`` and every
    case in this parametrization constructs successfully (RED)."""
    with pytest.raises(pydantic.ValidationError):
        EmitHookEventIROp(kind="emit_hook_event", event_name=event_name)


# ---------------------------------------------------------------------------
# Tier 2: ②A KIND dimension — real reject via execute_op, before bus.publish
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nonwhitelisted_kind_rejected_before_bus_publish():
    """Tier 2: bound-test-must-flip — emitting a non-whitelisted kind via the
    REAL ``execute_op`` dispatch path is REJECTED — the event never reaches
    ``HookBus.publish`` (observed: the bus's own subscriber sees nothing).

    FALSIFICATION (performed by hand against this exact op+ctx, not committed
    as a second test): commenting out the
    ``if not is_emittable_llm_kind(kind, session_id): raise ...`` block in
    ``reyn/core/op_runtime/emit_hook_event.py`` flips this test — the SAME
    op call returns ``status: "ok"`` and the event actually lands on
    ``sub.get_nowait()`` — proving the assertion below is load-bearing on
    the whitelist gate actually running, not a tautology. Restoring the
    check reproduces the RED->GREEN flip verified here.
    """
    bus = HookBus()
    sub = bus.subscribe()
    events = EventLog(run_id="r1")
    ctx = _op_context(session_id="sess-A", hook_bus=bus, events=events)

    op = EmitHookEventIROp(kind="emit_hook_event", target_kind="webhook:github:push")
    result = await execute_op(op, ctx)

    assert result["status"] == "denied"
    with pytest.raises(asyncio.QueueEmpty):
        sub.get_nowait()  # nothing was ever published to the bus


@pytest.mark.asyncio
async def test_composed_kind_rejected_before_bus_publish():
    """Tier 2: the SAME reject as above, specifically for ``composed:*`` — an
    LLM forging a Composer's output kind must be denied (an unforged
    ``composed:*`` only ever comes from a real Composer's correlation
    logic, ``reyn.hooks.composer``)."""
    bus = HookBus()
    sub = bus.subscribe()
    events = EventLog(run_id="r1")
    ctx = _op_context(session_id="sess-A", hook_bus=bus, events=events)

    op = EmitHookEventIROp(kind="emit_hook_event", target_kind="composed:deploy_approved")
    result = await execute_op(op, ctx)

    assert result["status"] == "denied"
    with pytest.raises(asyncio.QueueEmpty):
        sub.get_nowait()


@pytest.mark.asyncio
async def test_own_session_llm_kind_is_accepted():
    """Tier 2: the positive control the two reject tests above are
    contrasted against — this session's OWN llm:* kind DOES reach the bus,
    proving the whitelist is a real allow/deny gate, not a blanket deny."""
    bus = HookBus()
    sub = bus.subscribe()
    events = EventLog(run_id="r1")
    ctx = _op_context(session_id="sess-A", hook_bus=bus, events=events)

    op = EmitHookEventIROp(kind="emit_hook_event", event_name="deploy_ready", payload={"x": 1})
    result = await execute_op(op, ctx)

    assert result["status"] == "ok"
    assert result["emitted_kind"] == "llm:sess-A:deploy_ready"
    published = sub.get_nowait()
    assert published.kind == "llm:sess-A:deploy_ready"
    assert published.payload == {"x": 1}


# ---------------------------------------------------------------------------
# Tier 2: ②B SESSION dimension — structural cross-session impossibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_session_target_never_reaches_foreign_bus():
    """Tier 2: an emit whose (defense-in-depth) ``target_kind`` names a
    DIFFERENT session's ``llm:*`` namespace does NOT reach that foreign
    session's bus/inbox — demonstrated with TWO REAL, INDEPENDENT HookBus
    instances (one per OpContext, mirroring real per-Session isolation,
    proposal §3.3), not a private-attribute assertion. This is the
    structural guarantee: ``ctx.hook_bus`` is a single fixed reference to
    THIS OpContext's own bus — the handler has no lookup-by-session-id
    routing path at all, so a foreign-session ``target_kind`` is denied by
    the whitelist AND has nowhere to structurally route to even if it
    weren't."""
    events = EventLog(run_id="r1")
    bus_a = HookBus()
    sub_a = bus_a.subscribe()
    ctx_a = _op_context(session_id="sess-A", hook_bus=bus_a, events=events)

    bus_b = HookBus()
    sub_b = bus_b.subscribe()

    op = EmitHookEventIROp(kind="emit_hook_event", target_kind="llm:sess-B:evil")
    result = await execute_op(op, ctx_a)

    assert result["status"] == "denied"
    with pytest.raises(asyncio.QueueEmpty):
        sub_a.get_nowait()  # ctx_a's OWN bus got nothing either
    with pytest.raises(asyncio.QueueEmpty):
        sub_b.get_nowait()  # the foreign session's bus was never touched


@pytest.mark.asyncio
async def test_event_name_path_has_no_session_field_to_supply():
    """Tier 2: the NORMAL (router-tool-exposed) path — ``event_name`` only —
    structurally cannot express a foreign session at all: two OpContexts
    with different ``session_id``s, given the IDENTICAL op (same
    ``event_name``), each produce a kind scoped to THEIR OWN session. There
    is no field on ``EmitHookEventIROp`` an LLM could set to make session
    A's call land under session B's kind."""
    events = EventLog(run_id="r1")
    bus_a = HookBus()
    sub_a = bus_a.subscribe()
    ctx_a = _op_context(session_id="sess-A", hook_bus=bus_a, events=events)

    bus_b = HookBus()
    sub_b = bus_b.subscribe()
    ctx_b = _op_context(session_id="sess-B", hook_bus=bus_b, events=events)

    op = EmitHookEventIROp(kind="emit_hook_event", event_name="ping")
    result_a = await execute_op(op, ctx_a)
    result_b = await execute_op(op, ctx_b)

    assert result_a["emitted_kind"] == "llm:sess-A:ping"
    assert result_b["emitted_kind"] == "llm:sess-B:ping"
    assert sub_a.get_nowait().kind == "llm:sess-A:ping"
    assert sub_b.get_nowait().kind == "llm:sess-B:ping"


# ---------------------------------------------------------------------------
# Tier 2: fail-closed preconditions (no session identity / no bus wired)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_session_identity_is_denied():
    """Tier 2: no bound session identity → denied outright (fail-closed;
    there is nothing to scope the kind to)."""
    events = EventLog(run_id="r1")
    bus = HookBus()
    ctx = OpContext(
        workspace=None, events=events, permission_decl=PermissionDecl(),
        session_id=None, hook_bus=bus,
    )
    op = EmitHookEventIROp(kind="emit_hook_event", event_name="ping")
    result = await execute_op(op, ctx)
    assert result["status"] == "denied"


@pytest.mark.asyncio
async def test_no_hook_bus_wired_is_denied():
    """Tier 2: no HookBus wired onto this OpContext (e.g. a non-chat/
    preprocessor context) → denied outright, never a silent no-op."""
    events = EventLog(run_id="r1")
    ctx = OpContext(
        workspace=None, events=events, permission_decl=PermissionDecl(),
        session_id="sess-A", hook_bus=None,
    )
    op = EmitHookEventIROp(kind="emit_hook_event", event_name="ping")
    result = await execute_op(op, ctx)
    assert result["status"] == "denied"


# ---------------------------------------------------------------------------
# Tier 2: ③ emit-origin self-wake force-close — the STRENGTHENED valve pin
# ---------------------------------------------------------------------------


def _make_session(
    tmp_path: Path, *, hooks_config: list, composers_config: list, cap: int,
) -> Session:
    safety = SafetyConfig(
        loop=LoopConfig(max_hook_driven_turns=cap),
        on_limit=OnLimitConfig(mode="unattended"),  # deny deterministically, no bus
    )
    return Session(
        agent_name="emit-hook-event-agent",
        session_id="emit-sess",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        reactivity=ReactivityConfig(hooks_config=hooks_config, composers_config=composers_config),
        safety=safety,
    )


def _collect_events(session: Session) -> list[dict]:
    collected: list[dict] = []

    def _sub(event) -> None:
        collected.append({"type": event.type, **event.data})

    session._chat_events.add_subscriber(_sub)
    return collected


def _checkpoint_kinds(events: list[dict]) -> list:
    return [e.get("kind") for e in events if e["type"] == "safety_limit_checkpoint"]


@pytest.mark.asyncio
async def test_emit_origin_self_stimulating_chain_force_closes_at_cap(tmp_path):
    """Tier 2: STRENGTHENED loop-valve pin — the emit-ORIGIN variant of
    ``tests/test_hook_composer_reachability_phase5.py``'s external-
    ``file_changed``-origin chain): each hook-driven turn's "LLM" calls the
    REAL ``emit_hook_event`` handler (``llm:<session_id>:ping``), which a
    Composer (``op=count``, ``threshold=1``) correlates into
    ``composed:tick``; a Sync ``on: composed:tick`` wake hook fires, pushing
    a new turn — which emits again. This is a genuinely SELF-STIMULATING,
    UNBOUNDED chain driven entirely through the ``emit_hook_event`` OP path
    (proposal §8.4 item 3's "LLM 自己覚醒 loop": emit_hook_event -> Composer
    -> wake:true hook -> a new turn -> that turn emits again -> ...).

    With ``max_hook_driven_turns=2``: exactly 2 hook-driven ("tick!") turns
    run before the 3rd is suppressed by the EXISTING ``_hook_driven_turns``
    cap check (session.py) and a ``hook_driven_turns`` safety_limit_checkpoint
    fires — proving the emit-origin wake path traverses the SAME inbox
    ``kind="hook"`` E-path every other hook-driven wake uses, with ZERO new
    bounding logic added for this op.

    FALSIFICATION (performed by hand against this exact fixture, not
    committed as a second test to avoid a wall-clock race in CI): raising
    ``max_hook_driven_turns`` from 2 to a much larger value and re-running
    with the SAME bounded wait window flips the checkpoint assertion — the
    chain keeps running instead of stopping at turn 3, proving the
    assertion below is load-bearing on the cap actually binding."""
    session_id = "emit-sess"
    hooks_config = [
        {"on": "composed:tick", "template_push": {"message": "tick!", "wake": True}},
    ]
    composers_config = [
        {
            "name": "tick",
            "op": "count",
            "count": 1,
            "inputs": [{"kind": f"llm:{session_id}:ping"}],
            "emit": {"kind": "composed:tick"},
        }
    ]
    cap = 2
    session = _make_session(
        tmp_path, hooks_config=hooks_config, composers_config=composers_config, cap=cap,
    )
    ran: list[str] = []

    async def _run_turn_that_emits(user_text: str, chain_id: str) -> None:
        ran.append(user_text)
        # Simulate the LLM, THIS turn, calling the real emit_hook_event
        # handler — the self-stimulating step of the chain.
        ctx = OpContext(
            workspace=None, events=session._chat_events, permission_decl=PermissionDecl(),
            session_id=session._session_id, hook_bus=session._hook_bus,
        )
        op = EmitHookEventIROp(kind="emit_hook_event", event_name="ping")
        await emit_handle(op, ctx)

    session._loop_driver.run_turn = _run_turn_that_emits  # type: ignore[method-assign]
    events = _collect_events(session)

    run_task = asyncio.ensure_future(session.run())
    try:
        await _wait_until(lambda: session._hook_bus.subscriber_count >= 2)
        await session._put_inbox("user", {"text": "go", "wake": True, "chain_id": "c"})
        await _wait_until(lambda: "hook_driven_turns" in _checkpoint_kinds(events))
    finally:
        await session.shutdown()
        try:
            await asyncio.wait_for(run_task, timeout=_POLL_TIMEOUT)
        except asyncio.TimeoutError:
            run_task.cancel()

    assert ran == ["go"] + ["tick!"] * cap
    assert "hook_driven_turns" in _checkpoint_kinds(events)


# ---------------------------------------------------------------------------
# Issue #2887: production router-tool dispatch path audits denials
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_denied_emit_via_production_router_tool_path_is_audited():
    """Tier 2: a denied ``emit_hook_event`` on the REAL PRODUCTION router-tool
    dispatch path — ``reyn.core.dispatch.dispatcher.dispatch_tool`` invoking
    the REAL ``EMIT_HOOK_EVENT.handler`` (``reyn.tools.emit_hook_event``),
    NOT ``execute_op`` — still produces a P6 audit-event recording the
    denial (``tool_failed`` / ``error_kind="permission_denied"``), verified
    via ``EventLog``'s real subscriber mechanism (no private-state read).

    This is the #2887 architect-flagged gap: ``_handle_emit_hook_event``
    (``reyn/tools/emit_hook_event.py``) calls ``op_runtime.emit_hook_event.
    handle`` DIRECTLY, bypassing ``execute_op``'s own except-``PermissionError``
    -> ``permission_denied`` audit branch. Verified here that the SHARED
    ``dispatch_tool`` chokepoint every router tool call passes through
    (``reyn/core/dispatch/dispatcher.py``) independently catches the same
    ``EmitHookEventDenied`` (a ``PermissionError`` subclass) and audits it —
    so the production path is NOT a blind spot, just a differently-named
    audit event than the op_runtime path.

    Denial driven here: no bound session identity (``OpContext.session_id
    is None``), the router-exposed-args-reachable fail-closed precondition
    (``op_runtime/emit_hook_event.py`` lines 68-77) — the router tool schema
    exposes only ``event_name``/``payload`` (no ``target_kind``), so a
    live-session-absent OpContext is the realistic router-tool-reachable
    denial shape.

    FALSIFICATION (performed by hand, not committed as a second test):
    commenting out the ``except PermissionError`` branch in
    ``reyn/core/dispatch/dispatcher.py``'s ``dispatch_tool`` (letting
    ``EmitHookEventDenied`` propagate as a bare exception instead) flips
    this test RED — no ``tool_failed``/``permission_denied`` event is
    recorded (the generic ``except Exception`` branch instead records
    ``error_kind="exception"``, not ``"permission_denied"``). Restoring the
    branch reproduces the RED->GREEN flip verified here.
    """
    events = EventLog(run_id="r1")
    collected: list[dict] = []
    events.add_subscriber(lambda e: collected.append({"type": e.type, **e.data}))

    bus = HookBus()
    sub = bus.subscribe()
    # No session_id bound — the OpContext a live router session would build
    # via router_state.op_context_factory() when no chat session identity is
    # available. This is a REAL OpContext, not a mock.
    op_ctx = OpContext(
        workspace=None, events=events, permission_decl=PermissionDecl(),
        session_id=None, hook_bus=bus,
    )
    router_state = RouterCallerState(op_context_factory=lambda: op_ctx)
    tool_ctx = ToolContext(
        events=events,
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=router_state,
    )

    async def invoker(args: dict):
        return await EMIT_HOOK_EVENT.handler(args, tool_ctx)

    catalog = {
        "emit_hook_event": {
            "function": {
                "name": "emit_hook_event",
                "parameters": EMIT_HOOK_EVENT.parameters,
            }
        }
    }
    dispatch_ctx = DispatchContext(
        caller_kind="router", caller_id="test-agent", chain_id="c1",
        tool_catalog=catalog, events=events,
    )

    result = await dispatch_tool(
        name="emit_hook_event",
        args={"event_name": "ping"},
        ctx=dispatch_ctx,
        invoker=invoker,
    )

    assert result["status"] == "error"
    assert result["error"]["kind"] == "permission_denied"
    with pytest.raises(asyncio.QueueEmpty):
        sub.get_nowait()  # the denied emit never reached the bus

    failed_events = [e for e in collected if e["type"] == "tool_failed"]
    (only_failed,) = failed_events  # exactly one tool_failed — unpack asserts the count
    assert only_failed["error_kind"] == "permission_denied"
    assert only_failed["tool"] == "emit_hook_event"
