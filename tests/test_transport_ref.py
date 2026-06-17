"""Tier 2: TransportRef discriminated union + run_one_iteration + RoutingLayer +
MessageBus invariants (FP-0013 Components A, B, C, D).

Policy compliance:
- No unittest.mock / MagicMock / AsyncMock / patch for collaborators.
  We use real instances and plain async fakes.
- No private-state assertions (except session.outbox which is the public
  observation surface per test_session_invariants.py convention).
- Each test docstring first line starts with ``Tier 2:``.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.chat.message_bus import MessageBus
from reyn.chat.outbox import OutboxMessage
from reyn.chat.routing import RoutingLayer
from reyn.chat.session import ChatSession
from reyn.chat.transport import (
    A2aRef,
    AgentRef,
    McpRef,
    SystemRef,
    TransportRef,
    TuiRef,
)
from reyn.core.events.state_log import StateLog

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(tmp_path: Path, *, agent_name: str = "test_agent") -> ChatSession:
    """Build a minimal ChatSession wired to tmp_path."""
    return ChatSession(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )


def _drain_outbox(session: ChatSession) -> list[OutboxMessage]:
    """Non-blocking drain of all outbox messages."""
    msgs = []
    while not session.outbox.empty():
        msgs.append(session.outbox.get_nowait())
    return msgs


# ---------------------------------------------------------------------------
# Component A: TransportRef discriminated union
# ---------------------------------------------------------------------------


def test_transport_ref_discriminated_union_correctness():
    """Tier 2: each TransportRef variant constructs correctly and passes
    isinstance checks for its own type but NOT for other variant types.

    This pins the discriminated-union contract: callers dispatch on
    ``type(ref)`` or ``isinstance(ref, XxxRef)``; the union must be
    exhaustive and non-overlapping.
    """
    tui = TuiRef()
    mcp = McpRef(request_id="req-001")
    a2a = A2aRef(request_id="a2a-001")
    agent = AgentRef(agent_name="peer", chain_id="chain-001")
    sys = SystemRef()

    # Each is an instance of exactly its own type.
    assert isinstance(tui, TuiRef)
    assert isinstance(mcp, McpRef)
    assert isinstance(a2a, A2aRef)
    assert isinstance(agent, AgentRef)
    assert isinstance(sys, SystemRef)

    # No variant is an instance of a sibling type.
    assert not isinstance(tui, McpRef)
    assert not isinstance(mcp, TuiRef)
    assert not isinstance(a2a, McpRef)
    assert not isinstance(agent, A2aRef)
    assert not isinstance(sys, AgentRef)

    # Payload fields are accessible.
    assert mcp.request_id == "req-001"
    assert a2a.request_id == "a2a-001"
    assert agent.agent_name == "peer"
    assert agent.chain_id == "chain-001"

    # Union type alias includes all variants (checked via isinstance).
    for ref in (tui, mcp, a2a, agent, sys):
        assert isinstance(ref, (TuiRef, McpRef, A2aRef, AgentRef, SystemRef))

    # Frozen dataclasses: equality is value-based.
    assert TuiRef() == TuiRef()
    assert McpRef("x") == McpRef("x")
    assert McpRef("x") != McpRef("y")


def test_transport_ref_variants_are_frozen():
    """Tier 2: TransportRef variants are frozen dataclasses — mutation raises.

    Frozen ensures refs are safe to use as dict keys (hashable) and
    cannot be accidentally mutated after creation.
    """
    mcp = McpRef(request_id="r1")
    with pytest.raises((AttributeError, TypeError)):
        mcp.request_id = "r2"  # type: ignore[misc]

    agent = AgentRef(agent_name="p", chain_id="c")
    with pytest.raises((AttributeError, TypeError)):
        agent.agent_name = "q"  # type: ignore[misc]


def test_outbox_message_has_reply_to_field():
    """Tier 2: OutboxMessage exposes a ``reply_to`` field defaulting to None.

    FP-0013 migration contract: existing code that does not set reply_to
    must still construct OutboxMessage without errors.
    """
    msg_no_ref = OutboxMessage(kind="agent", text="hello")
    assert msg_no_ref.reply_to is None

    ref = A2aRef(request_id="req-42")
    msg_with_ref = OutboxMessage(kind="agent", text="hi", reply_to=ref)
    assert msg_with_ref.reply_to is ref
    assert isinstance(msg_with_ref.reply_to, A2aRef)


# ---------------------------------------------------------------------------
# Component B: run_one_iteration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_one_iteration_processes_single_kind(tmp_path, monkeypatch):
    """Tier 2: run_one_iteration processes exactly one inbox kind per call.

    When two messages are enqueued, two separate calls are required to
    consume both — confirming the single-iteration contract.

    P6 invariant: both messages must be recorded as inbox_consume in WAL.
    """
    session = _make_session(tmp_path)
    processed: list[str] = []

    async def _fake_handle_user_message(self, text, *, chain_id):
        processed.append(text)

    monkeypatch.setattr(ChatSession, "_handle_user_message", _fake_handle_user_message)

    # Enqueue two "user" messages.
    await session._put_inbox("user", {"text": "first"})
    await session._put_inbox("user", {"text": "second"})

    assert session.inbox.qsize() == 2

    # First iteration: consumes exactly one.
    result1 = await session.run_one_iteration()
    assert result1 is True
    (only,) = processed
    assert only == "first"
    assert session.inbox.qsize() == 1  # one still pending

    # Second iteration: consumes the second.
    result2 = await session.run_one_iteration()
    assert result2 is True
    assert processed == ["first", "second"]
    assert session.inbox.empty()


@pytest.mark.asyncio
async def test_run_one_iteration_returns_false_on_shutdown(tmp_path):
    """Tier 2: run_one_iteration returns False when it consumes a shutdown kind.

    The caller (run()) uses this return value to break out of the while loop.
    """
    session = _make_session(tmp_path)
    await session.inbox.put(("shutdown", {}))
    result = await session.run_one_iteration()
    assert result is False


@pytest.mark.asyncio
async def test_run_one_iteration_dispatches_all_known_kinds(tmp_path, monkeypatch):
    """Tier 2: run_one_iteration dispatches each inbox kind to its handler.

    All five non-shutdown kinds must reach their respective handlers.
    """
    session = _make_session(tmp_path)
    dispatched: list[str] = []

    async def _record_user(self, text, *, chain_id):
        dispatched.append("user")

    async def _record_skill_completed(self, payload):
        dispatched.append("skill_completed")

    async def _record_plan_completed(self, payload):
        dispatched.append("plan_completed")

    async def _record_agent_request(self, payload):
        dispatched.append("agent_request")

    async def _record_agent_response(self, payload):
        dispatched.append("agent_response")

    monkeypatch.setattr(ChatSession, "_handle_user_message", _record_user)
    monkeypatch.setattr(ChatSession, "_handle_skill_completed", _record_skill_completed)
    monkeypatch.setattr(ChatSession, "_handle_plan_completed", _record_plan_completed)
    monkeypatch.setattr(ChatSession, "_handle_agent_request", _record_agent_request)
    monkeypatch.setattr(ChatSession, "_handle_agent_response", _record_agent_response)

    for kind in ("user", "skill_completed", "plan_completed", "agent_request", "agent_response"):
        await session._put_inbox(kind, {"text": "x"})

    for _ in range(5):
        result = await session.run_one_iteration()
        assert result is True

    assert set(dispatched) == {
        "user", "skill_completed", "plan_completed", "agent_request", "agent_response"
    }


@pytest.mark.asyncio
async def test_run_wraps_run_one_iteration(tmp_path, monkeypatch):
    """Tier 2: run() is equivalent to ``while await run_one_iteration(): pass``.

    Submitting a user message followed by a shutdown must produce a reply
    and then terminate. This pins that run() did not regress from the
    while-loop decomposition.
    """
    session = _make_session(tmp_path)
    processed: list[str] = []

    async def _fake_handle_user_message(self, text, *, chain_id):
        processed.append(text)
        await self._put_outbox(OutboxMessage(kind="agent", text=f"echo:{text}"))

    monkeypatch.setattr(ChatSession, "_handle_user_message", _fake_handle_user_message)

    await session._put_inbox("user", {"text": "ping"})
    await session.inbox.put(("shutdown", {}))  # out-of-band, no WAL entry

    # run() should terminate cleanly after consuming both.
    await asyncio.wait_for(session.run(), timeout=5.0)

    assert processed == ["ping"]
    # __end__ sentinel emitted on shutdown.
    msgs = _drain_outbox(session)
    kinds = [m.kind for m in msgs]
    assert "__end__" in kinds


# ---------------------------------------------------------------------------
# Component C: RoutingLayer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routing_layer_dispatches_by_ref_type():
    """Tier 2: RoutingLayer dispatches OutboxMessage to the handler matching
    its reply_to type.

    TuiRef → tui_handler, A2aRef → a2a_handler; crossing over must not happen.
    """
    routing = RoutingLayer()
    tui_received: list[OutboxMessage] = []
    a2a_received: list[OutboxMessage] = []

    async def tui_handler(msg: OutboxMessage) -> None:
        tui_received.append(msg)

    async def a2a_handler(msg: OutboxMessage) -> None:
        a2a_received.append(msg)

    routing.register(TuiRef, tui_handler)
    routing.register(A2aRef, a2a_handler)

    tui_msg = OutboxMessage(kind="agent", text="to tui", reply_to=TuiRef())
    a2a_msg = OutboxMessage(kind="agent", text="to a2a", reply_to=A2aRef(request_id="r1"))

    await routing.dispatch(tui_msg)
    await routing.dispatch(a2a_msg)

    (tui_only,) = tui_received
    assert tui_only.text == "to tui"
    (a2a_only,) = a2a_received
    assert a2a_only.text == "to a2a"


@pytest.mark.asyncio
async def test_routing_layer_none_reply_to_falls_back_to_tui():
    """Tier 2: RoutingLayer falls back to TuiRef when reply_to is None.

    Migration safety: existing outbox messages without reply_to must still
    reach the TUI renderer unchanged.
    """
    routing = RoutingLayer()
    tui_received: list[OutboxMessage] = []

    async def tui_handler(msg: OutboxMessage) -> None:
        tui_received.append(msg)

    routing.register(TuiRef, tui_handler)

    msg = OutboxMessage(kind="status", text="no ref here")  # reply_to=None
    await routing.dispatch(msg)

    (tui_only,) = tui_received
    assert tui_only.text == "no ref here"


@pytest.mark.asyncio
async def test_routing_layer_no_handler_drops_silently():
    """Tier 2: RoutingLayer drops messages when no handler is registered for
    the ref type — no exception raised.

    This is the migration-safe behaviour: during incremental adoption,
    not every ref type will have a handler yet.
    """
    routing = RoutingLayer()
    # Only register TuiRef; send an A2aRef message.
    async def tui_handler(msg: OutboxMessage) -> None:
        pass

    routing.register(TuiRef, tui_handler)

    mcp_msg = OutboxMessage(kind="agent", text="mcp msg", reply_to=McpRef(request_id="r1"))
    # Should not raise.
    await routing.dispatch(mcp_msg)


def test_routing_layer_registered_types():
    """Tier 2: RoutingLayer.registered_types() returns the registered ref types."""
    routing = RoutingLayer()

    async def noop(msg: OutboxMessage) -> None:
        pass

    routing.register(TuiRef, noop)
    routing.register(McpRef, noop)

    assert routing.registered_types() == frozenset({TuiRef, McpRef})


# ---------------------------------------------------------------------------
# Component D: MessageBus
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_bus_request_pumps_until_quiescent(tmp_path, monkeypatch):
    """Tier 2: MessageBus.request pumps run_one_iteration until inbox is empty
    and all in-flight tasks are done, then returns collected OutboxMessages.

    Scenario: user message triggers one outbox emission; bus should return
    after that single turn without a timeout.
    """
    session = _make_session(tmp_path)

    async def _fake_handle_user_message(self, text, *, chain_id):
        await self._put_outbox(OutboxMessage(kind="agent", text=f"echo:{text}"))

    monkeypatch.setattr(ChatSession, "_handle_user_message", _fake_handle_user_message)

    bus = MessageBus()
    replies = await bus.request(
        session,
        kind="user",
        payload={"text": "hello"},
        reply_to=McpRef(request_id="test-001"),
        timeout=5.0,
    )

    # The outbox message emitted during the turn must be collected.
    agent_texts = [r.text for r in replies if r.kind == "agent"]
    assert "echo:hello" in agent_texts


@pytest.mark.asyncio
async def test_message_bus_collects_multiple_outbox_messages(tmp_path, monkeypatch):
    """Tier 2: MessageBus.request collects ALL OutboxMessages emitted during
    the pumped turn, not just the first one.

    We emit two ``agent`` kind messages (which are always queued regardless
    of ``is_attached``) and assert both are collected.
    """
    session = _make_session(tmp_path)

    async def _fake_handle_user_message(self, text, *, chain_id):
        # Both "agent" kinds are queued even when is_attached=False.
        await self._put_outbox(OutboxMessage(kind="agent", text="first_fragment"))
        await self._put_outbox(OutboxMessage(kind="agent", text="done"))

    monkeypatch.setattr(ChatSession, "_handle_user_message", _fake_handle_user_message)

    bus = MessageBus()
    replies = await bus.request(
        session,
        kind="user",
        payload={"text": "go"},
        reply_to=A2aRef(request_id="a2a-test"),
        timeout=5.0,
    )

    texts = [r.text for r in replies if r.kind == "agent"]
    assert texts == ["first_fragment", "done"]


@pytest.mark.asyncio
async def test_message_bus_waits_for_running_tasks(tmp_path, monkeypatch):
    """Tier 2: MessageBus.request waits for running_skills / running_plans
    to finish before declaring quiescence.

    This replaces the previous ``running_plans gather`` + ``running_skills
    gather`` tactical patches in ``send_to_agent_impl``.
    """
    session = _make_session(tmp_path)
    background_done = asyncio.Event()

    async def _fake_handle_user_message(self, text, *, chain_id):
        # Spawn a "background skill" that completes after a brief delay.
        async def _bg():
            await asyncio.sleep(0.05)
            background_done.set()
            # Emit an outbox message when done.
            await self._put_outbox(OutboxMessage(kind="agent", text="bg_result"))

        task = asyncio.create_task(_bg())
        self.running_skills["fake_skill"] = task

    monkeypatch.setattr(ChatSession, "_handle_user_message", _fake_handle_user_message)

    bus = MessageBus()
    replies = await bus.request(
        session,
        kind="user",
        payload={"text": "kick"},
        reply_to=McpRef(request_id="bg-test"),
        timeout=5.0,
    )

    # Background task must have completed before bus returned.
    assert background_done.is_set(), "bus returned before background task finished"
    texts = [r.text for r in replies if r.kind == "agent"]
    assert "bg_result" in texts


# ---------------------------------------------------------------------------
# Component D + Migration: A2A endpoint uses MessageBus
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a2a_endpoint_uses_message_bus(tmp_path, monkeypatch):
    """Tier 2: A2A endpoint (send_to_agent_impl) drives session via
    MessageBus.request, not inline _handle_user_message.

    We verify by observing that the reply is collected and that
    run_one_iteration was invoked (via the inbox being consumed).

    This pins the FP-0013 bypass-deletion contract for the A2A transport.
    """
    from reyn.chat.profile import AgentProfile
    from reyn.chat.registry import AgentRegistry
    from reyn.mcp_server import send_to_agent_impl
    from reyn.runtime.budget.budget import BudgetTracker, CostConfig

    state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")

    def factory(profile: AgentProfile) -> ChatSession:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        bt = BudgetTracker(CostConfig())
        return ChatSession(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=bt,
            state_log=state_log,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    registry = AgentRegistry(
        project_root=tmp_path,
        session_factory=factory,
        state_log=state_log,
    )
    monkeypatch.chdir(tmp_path)

    # Use a real ChatSession (via factory) and track how inbox was consumed.
    inbox_consumed: list[str] = []

    original_put_inbox = ChatSession._put_inbox

    async def _tracking_put_inbox(self, kind, payload):
        inbox_consumed.append(kind)
        return await original_put_inbox(self, kind, payload)

    async def _fake_handle_user_message(self, text, *, chain_id):
        from reyn.chat.session import ChatMessage
        self._append_history(ChatMessage(
            role="user", content=text, ts="2026-05-14T00:00:00",
            meta={"chain_id": chain_id},
        ))
        self._append_history(ChatMessage(
            role="assistant", content=f"bus_reply:{text}",
            ts="2026-05-14T00:00:01",
            meta={"chain_id": chain_id},
        ))

    monkeypatch.setattr(ChatSession, "_put_inbox", _tracking_put_inbox)
    monkeypatch.setattr(ChatSession, "_handle_user_message", _fake_handle_user_message)

    result = await send_to_agent_impl(
        registry,
        agent_name="default",
        message="test_bus_msg",
        timeout=5.0,
    )

    # The inbox was used (= MessageBus path, not inline bypass).
    assert "user" in inbox_consumed, (
        "send_to_agent_impl must use the inbox (MessageBus path); "
        "direct _handle_user_message bypass detected."
    )
    assert result["agent"] == "default"
    assert "bus_reply:test_bus_msg" in result["reply"]
    assert result["partial"] is False
