"""Tier 2: Agent routing logic for intervention requests (issue #254 Phase 4).

Pins the 3-way routing decision the Agent makes in
``ChatSession.handle_intervention(iv)``:

  1. ``self_answer`` (= ``try_self_answer`` hook returns non-None)
  2. ``parent_agent.delegate`` (= ``resolve_parent_agent`` hook returns
     a ChatSession)
  3. ``user_channel.deliver`` (= default, falls through to
     ``_dispatch_intervention``)

Phase 4 ships the routing **scaffold**: hooks return None by default,
so all requests go to the user_channel branch (= behaviour parity with
Phase 3). Subclasses override the hooks to enable per-kind / per-context
policies — Phase 4 verifies the wire works, not specific policies.

Each branch emits an ``intervention_routed`` event for observability;
the Phase 4 sub-issue (= dispatched by tui-coder) will surface the
event in the TUI events tab.

No mocks. Real ChatSession + subclass overrides for the hook tests.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from reyn.chat.session import AgentRequestBus, ChatSession
from reyn.user_intervention import InterventionAnswer, UserIntervention

# ── 1. Hook methods exist with the canonical signatures ────────────────


def test_try_self_answer_hook_exists() -> None:
    """Tier 2: ChatSession exposes ``try_self_answer(iv) -> Answer|None``
    as the self-answer routing hook.
    """
    assert hasattr(ChatSession, "try_self_answer")
    assert inspect.iscoroutinefunction(ChatSession.try_self_answer)
    sig = inspect.signature(ChatSession.try_self_answer)
    assert list(sig.parameters.keys()) == ["self", "iv"]


def test_resolve_parent_agent_hook_exists() -> None:
    """Tier 2: ChatSession exposes ``resolve_parent_agent(iv) -> ChatSession|None``
    as the parent-delegate routing hook.

    Synchronous (not async) — chain-walk lookups don't need to await.
    """
    assert hasattr(ChatSession, "resolve_parent_agent")
    assert not inspect.iscoroutinefunction(ChatSession.resolve_parent_agent)
    sig = inspect.signature(ChatSession.resolve_parent_agent)
    assert list(sig.parameters.keys()) == ["self", "iv"]


def test_default_hooks_return_none() -> None:
    """Tier 2: default implementations return None — Phase 4 ships pure
    scaffold, no kind-specific policies enabled out-of-the-box.
    """
    session = ChatSession(agent_name="t")
    iv = UserIntervention(kind="ask_user", prompt="Q?")

    async def _check_self_answer() -> InterventionAnswer | None:
        return await session.try_self_answer(iv)

    assert asyncio.run(_check_self_answer()) is None
    assert session.resolve_parent_agent(iv) is None


# ── 2. Default routing — user_channel branch fires ─────────────────────


def test_default_routing_fires_user_channel_branch(tmp_path: Path) -> None:
    """Tier 2: with default hooks (= both return None), an unhandled
    intervention falls through to ``_dispatch_intervention``.

    The Phase 1 subscriber guard short-circuits when no listener is
    registered, so the round-trip returns an empty answer — used here
    to verify the branch was actually taken (= the answer's emptiness
    is observable only if dispatch was reached).
    """
    session = ChatSession(agent_name="t")
    iv = UserIntervention(kind="ask_user", prompt="Q?")

    answer = asyncio.run(session.handle_intervention(iv))
    # Phase 1 guard short-circuit reached → user_channel branch fired.
    assert answer.text == ""
    assert answer.choice_id is None


def test_default_routing_emits_user_channel_event(tmp_path: Path) -> None:
    """Tier 2: the user_channel branch emits ``intervention_routed`` with
    ``route="user_channel"`` so observers (= TUI events tab, debug
    traces, future routing-policy analysis) can see the decision.
    """
    session = ChatSession(agent_name="t")
    iv = UserIntervention(kind="ask_user", prompt="Q?")

    asyncio.run(session.handle_intervention(iv))

    # session._chat_events is the EventLog used for chat-side events.
    events = [
        e for e in session._chat_events.to_json()
        if e.get("type") == "intervention_routed"
    ]
    assert events, "intervention_routed event must fire on each handle_intervention call"
    payload = events[-1]["data"]
    assert payload["route"] == "user_channel"
    assert payload["iv_kind"] == "ask_user"
    assert payload["iv_id"] == iv.id


# ── 3. self_answer branch — fires when _try_self_answer returns answer ──


class _SelfAnsweringSession(ChatSession):
    """Subclass that always self-answers with a fixed answer."""

    async def try_self_answer(self, iv: UserIntervention) -> InterventionAnswer | None:
        return InterventionAnswer(text="self-policy", choice_id="self")


def test_self_answer_branch_returns_directly_without_dispatch() -> None:
    """Tier 2: when ``try_self_answer`` returns a non-None answer, the
    handler returns it immediately without invoking
    ``_dispatch_intervention`` — the user surface is never touched.
    """
    session = _SelfAnsweringSession(agent_name="t")
    iv = UserIntervention(kind="permission.shell", prompt="Run ls?")

    answer = asyncio.run(session.handle_intervention(iv))
    assert answer.text == "self-policy"
    assert answer.choice_id == "self"

    # And the registry queue must be untouched — no prompt was enqueued.
    assert session.interventions.queued_count() == 0


def test_self_answer_branch_emits_self_answer_event() -> None:
    """Tier 2: the self_answer branch emits ``intervention_routed`` with
    ``route="self_answer"``.
    """
    session = _SelfAnsweringSession(agent_name="t")
    iv = UserIntervention(kind="permission.shell", prompt="Run ls?")

    asyncio.run(session.handle_intervention(iv))

    events = [
        e for e in session._chat_events.to_json()
        if e.get("type") == "intervention_routed"
    ]
    assert events
    payload = events[-1]["data"]
    assert payload["route"] == "self_answer"
    assert payload["iv_kind"] == "permission.shell"


# ── 4. parent_delegate branch — fires when _resolve_parent_agent
#      returns a session ─────────────────────────────────────────────────


class _DelegatingSession(ChatSession):
    """Subclass that delegates all interventions to a designated parent
    session. Used to verify the parent_delegate routing branch.
    """

    def set_parent(self, parent: ChatSession) -> None:
        self._parent_session = parent

    def resolve_parent_agent(self, iv: UserIntervention) -> ChatSession | None:
        return getattr(self, "_parent_session", None)


def test_parent_delegate_branch_forwards_to_parent() -> None:
    """Tier 2: when ``resolve_parent_agent`` returns a parent session,
    the handler forwards the intervention to ``parent.handle_intervention``
    and returns the parent's decision verbatim.

    Verified via a parent that self-answers — the chain is
    child.handle_intervention → parent.handle_intervention →
    parent._try_self_answer.
    """
    parent = _SelfAnsweringSession(agent_name="parent")
    child = _DelegatingSession(agent_name="child")
    child.set_parent(parent)

    iv = UserIntervention(kind="ask_user", prompt="Q?")
    answer = asyncio.run(child.handle_intervention(iv))

    # Parent self-answered, child received that answer back.
    assert answer.text == "self-policy"
    assert answer.choice_id == "self"


def test_parent_delegate_branch_emits_parent_delegate_event() -> None:
    """Tier 2: the parent_delegate branch on the child emits
    ``intervention_routed`` with ``route="parent_delegate"`` BEFORE
    forwarding. The parent's own routing decision generates a separate
    event on the parent's chat_events log.
    """
    parent = _SelfAnsweringSession(agent_name="parent")
    child = _DelegatingSession(agent_name="child")
    child.set_parent(parent)

    iv = UserIntervention(kind="ask_user", prompt="Q?")
    asyncio.run(child.handle_intervention(iv))

    child_events = [
        e for e in child._chat_events.to_json()
        if e.get("type") == "intervention_routed"
    ]
    assert child_events
    assert child_events[-1]["data"]["route"] == "parent_delegate"

    parent_events = [
        e for e in parent._chat_events.to_json()
        if e.get("type") == "intervention_routed"
    ]
    assert parent_events
    # Parent's self_answer hook fired on its own intervention_routed event.
    assert parent_events[-1]["data"]["route"] == "self_answer"


# ── 5. Branch precedence: self_answer > parent_delegate > user_channel ──


class _SelfAndParentSession(_DelegatingSession):
    """Subclass that BOTH self-answers AND has a parent. Used to verify
    self_answer takes precedence (= the agent decides itself before
    consulting upstream).
    """

    async def try_self_answer(self, iv: UserIntervention) -> InterventionAnswer | None:
        return InterventionAnswer(text="child-self", choice_id="child")


def test_self_answer_takes_precedence_over_parent_delegate() -> None:
    """Tier 2: when both hooks could fire, ``try_self_answer`` runs
    first — the agent decides itself rather than consulting upstream.
    This encodes "the agent has its own will" per the Reyn peer-to-peer
    design (issue #254 design discussion log).
    """
    parent = _SelfAnsweringSession(agent_name="parent")
    child = _SelfAndParentSession(agent_name="child")
    child.set_parent(parent)

    iv = UserIntervention(kind="ask_user", prompt="Q?")
    answer = asyncio.run(child.handle_intervention(iv))

    # Child self-answered, parent was never consulted.
    assert answer.text == "child-self"
    assert answer.choice_id == "child"

    # And the parent's chat_events has NO intervention_routed event
    # (= parent's handle_intervention was never invoked).
    parent_events = [
        e for e in parent._chat_events.to_json()
        if e.get("type") == "intervention_routed"
    ]
    assert not parent_events


# ── 6. Behaviour parity: existing dispatch path still works through
#      the new routing scaffold ──────────────────────────────────────────


def test_chain_override_is_notified_through_user_channel_branch(tmp_path: Path) -> None:
    """Tier 2: the A2A peer override registered via
    ``register_intervention_override`` is notified via ``on_dispatch``
    as a side effect during the user_channel branch. Pre-α the
    override REPLACED the dispatch; α changed it to DECORATE — the
    iv still flows to the default handler.
    """
    session = ChatSession(agent_name="t")
    # The default handler awaits iv.future; register a listener so
    # dispatch doesn't short-circuit on no-listener.
    session.register_intervention_listener("test")

    captured: list[UserIntervention] = []

    class _StubOverrideObserver:
        async def on_dispatch(self, iv: UserIntervention) -> None:
            captured.append(iv)

    session.register_intervention_override("chain-X", _StubOverrideObserver())
    session.running_skills_chain["run-1"] = "chain-X"

    async def _drive() -> InterventionAnswer:
        iv = UserIntervention(kind="ask_user", prompt="Q?", run_id="run-1")

        async def _resolve() -> None:
            await asyncio.sleep(0.05)
            iv.future.set_result(InterventionAnswer(text="from-handler"))

        resolver = asyncio.create_task(_resolve())
        try:
            return await session.handle_intervention(iv)
        finally:
            resolver.cancel()

    answer = asyncio.run(_drive())

    # α: observer was notified (side effect); handler resolved the answer.
    assert captured, "override observer must have been notified"
    assert answer.text == "from-handler"

    # Routing event records the user_channel branch fired.
    events = [
        e for e in session._chat_events.to_json()
        if e.get("type") == "intervention_routed"
    ]
    assert events
    assert events[-1]["data"]["route"] == "user_channel"


# ── 7. RequestBus adapter — Phase 4 routing visible through the adapter ──


def test_self_answer_visible_through_request_bus_adapter() -> None:
    """Tier 2: an OS caller that holds the ``RequestBus`` adapter sees
    the self_answer policy fire — the adapter is fully transparent to
    the routing decision happening behind it.
    """
    session = _SelfAnsweringSession(agent_name="t")
    bus = session.as_request_bus()
    assert isinstance(bus, AgentRequestBus)

    iv = UserIntervention(kind="ask_user", prompt="Q?")
    answer = asyncio.run(bus.request(iv))

    assert answer.text == "self-policy"
    assert answer.choice_id == "self"
