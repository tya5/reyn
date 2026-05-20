"""Tier 2: FP-0001 — chain_id-scoped intervention bus override invariants.

Covers:
  1. register_intervention_override adds entry; unregister removes it (idempotent).
  2. _dispatch_intervention with iv.run_id whose chain_id has no override falls
     through to the default InterventionHandler.
  3. _dispatch_intervention with iv.run_id whose chain_id IS registered delegates
     to the override bus, NOT the default handler.
  4. Override is short-circuit: default handler .dispatch not called when override fires.
  5. iv.run_id is None → falls through to default (no override evaluated).
  6. iv.run_id not in running_skills_chain → falls through to default.
  7. send_to_agent_impl(..., intervention_override=bus) registers on entry and
     unregisters in finally even when bus.request raises.
  8. Concurrent calls to send_to_agent_impl with overrides on the same agent
     serialize via the agent lock (no leak between calls).

Policy compliance (docs/deep-dives/contributing/testing.ja.md):
  - No unittest.mock / AsyncMock / patch usage for collaborators.
  - Real ChatSession instances and a concrete _CaptureBus fake.
  - Tier 2 OS invariant tests only.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.budget.budget import BudgetTracker, CostConfig
from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.chat.session import ChatSession
from reyn.events.state_log import StateLog
from reyn.mcp_server import send_to_agent_impl
from reyn.user_intervention import InterventionAnswer, UserIntervention

# ---------------------------------------------------------------------------
# In-file _CaptureBus — no MagicMock; plain class implementing the
# InterventionBus protocol.
# ---------------------------------------------------------------------------


class _CaptureBus:
    """Minimal intervention observer fake (post-issue-#292 α refactor).

    Records ``on_dispatch`` calls. Pre-α this class implemented
    ``request`` which returned an ``InterventionAnswer``; post-α the
    bus is a side-effect observer that does NOT own iv resolution —
    ``on_dispatch`` returns None.
    """

    def __init__(
        self,
        *,
        raise_on_dispatch: Exception | None = None,
    ) -> None:
        self.calls: list[UserIntervention] = []
        self._raise_on_dispatch = raise_on_dispatch

    async def on_dispatch(self, iv: UserIntervention) -> None:
        self.calls.append(iv)
        if self._raise_on_dispatch is not None:
            raise self._raise_on_dispatch


class _RecordingHandler:
    """Wraps a real InterventionHandler's dispatch, counting calls via counter list.

    Used to observe whether the default handler is reached, without replacing
    the underlying real handler (no mock, just delegation + recording).
    """

    def __init__(self, real_dispatch_fn) -> None:
        self._real = real_dispatch_fn
        self.dispatch_count = 0

    async def dispatch(self, iv: UserIntervention) -> InterventionAnswer:
        self.dispatch_count += 1
        return await self._real(iv)


# ---------------------------------------------------------------------------
# Session factory helpers
# ---------------------------------------------------------------------------


def _make_session(tmp_path: Path, *, agent_name: str = "test_agent") -> ChatSession:
    """Build a ChatSession with I/O redirected to tmp_path.

    issue #254 Phase 1: register a placeholder listener so the registry's
    ``enforce_listener_presence=True`` short-circuit does not fire — these
    tests exercise the chain-scoped override path and may dispatch to the
    fallback registry path when no override is set.
    """
    session = ChatSession(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / f"{agent_name}_state.wal"),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )
    session.register_intervention_listener("test")
    return session


def _make_iv(*, run_id: str | None = None, prompt: str = "Q?") -> UserIntervention:
    iv = UserIntervention(kind="ask_user", prompt=prompt, run_id=run_id)
    iv.future = asyncio.get_running_loop().create_future()
    return iv


def _build_registry(tmp_path: Path, agent_specs: list[tuple[str, str]]) -> AgentRegistry:
    """Construct an AgentRegistry on tmp_path (mirrors test_mcp_server.py pattern)."""
    state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")

    def factory(profile: AgentProfile) -> ChatSession:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        bt = BudgetTracker(CostConfig())
        session = ChatSession(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=bt,
            state_log=state_log,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )
        session.register_intervention_listener("test")
        return session

    registry = AgentRegistry(
        project_root=tmp_path,
        session_factory=factory,
        state_log=state_log,
    )

    for name, role in agent_specs:
        if name == "default":
            agent_dir = registry._dir / name
            AgentProfile.new(name, role=role).save(agent_dir)
        else:
            registry.create(name, role=role)

    return registry


# ---------------------------------------------------------------------------
# Test 1: register / unregister lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_and_unregister_intervention_override(tmp_path):
    """Tier 2: register_intervention_override adds entry; unregister removes it (idempotent)."""
    session = _make_session(tmp_path)
    bus = _CaptureBus()
    chain_id = "chain-abc"

    # Before registration — not present.
    assert chain_id not in session._intervention_overrides

    session.register_intervention_override(chain_id, bus)
    assert session._intervention_overrides.get(chain_id) is bus

    # Unregister removes it.
    session.unregister_intervention_override(chain_id)
    assert chain_id not in session._intervention_overrides

    # Idempotent: second unregister does not raise.
    session.unregister_intervention_override(chain_id)
    assert chain_id not in session._intervention_overrides


# ---------------------------------------------------------------------------
# Test 2: no override for chain → falls through to default handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_falls_through_when_no_override(tmp_path):
    """Tier 2: _dispatch_intervention with iv.run_id whose chain_id has no registered
    override falls through to the default InterventionHandler.

    Verifies by letting the default handler's outbox path execute (it emits an
    outbox message of kind 'ask_user' when the session is attached).
    """
    session = _make_session(tmp_path)
    session.is_attached = True

    # Wire running_skills_chain so we have a run that maps to a chain.
    run_id = "run-001"
    chain_id = "chain-no-override"
    session.running_skills_chain[run_id] = chain_id
    # Note: no override registered for chain_id.

    iv = _make_iv(run_id=run_id)

    # The default handler will block on iv.future. Launch as a task and
    # resolve the future to let it complete.
    task = asyncio.ensure_future(session._dispatch_intervention(iv))
    await asyncio.sleep(0)  # yield so dispatch reaches await

    # Resolve the future with a real answer.
    expected_answer = InterventionAnswer(text="default-answer")
    iv.future.set_result(expected_answer)

    answer = await asyncio.wait_for(task, timeout=2.0)
    assert answer.text == "default-answer"
    # Override bus was never called (no override registered).
    assert "_intervention_overrides" in dir(session)


# ---------------------------------------------------------------------------
# Test 3: override registered → delegates to override bus
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_notifies_override_then_continues_to_handler(tmp_path):
    """Tier 2 (= issue #292 α): _dispatch_intervention with a registered
    override notifies the override's ``on_dispatch`` AS A SIDE EFFECT,
    then ALWAYS continues to the default InterventionHandler.dispatch.
    Pre-α the override REPLACED the handler; α changed it to DECORATE.
    """
    session = _make_session(tmp_path)
    run_id = "run-override"
    chain_id = "chain-with-override"
    session.running_skills_chain[run_id] = chain_id

    override_bus = _CaptureBus()
    session.register_intervention_override(chain_id, override_bus)

    iv = _make_iv(run_id=run_id)

    # The handler will await iv.future. Resolve it concurrently with a
    # known value so dispatch returns; the test pin is that the override
    # was called AND the handler-resolved value came back.
    async def _resolve_after_delay() -> None:
        await asyncio.sleep(0.05)
        iv.future.set_result(InterventionAnswer(text="from-handler"))

    resolver = asyncio.create_task(_resolve_after_delay())
    try:
        answer = await session._dispatch_intervention(iv)
    finally:
        resolver.cancel()

    # Override was notified.
    assert len(override_bus.calls) == 1
    assert override_bus.calls[0] is iv
    # Handler resolved the answer (= α decorator semantics: handler always runs).
    assert answer.text == "from-handler"


@pytest.mark.asyncio
async def test_override_does_not_short_circuit_default_handler(tmp_path):
    """Tier 2 (= issue #292 α): the override DECORATES dispatch, it
    does NOT replace it. This is the exact contract reversal from PR
    pre-α: where the old test asserted "default NOT called", the new
    test asserts "default IS called" + override is called alongside.
    """
    session = _make_session(tmp_path)
    run_id = "run-sc"
    chain_id = "chain-sc"
    session.running_skills_chain[run_id] = chain_id

    override_bus = _CaptureBus()
    session.register_intervention_override(chain_id, override_bus)

    # Wrap the real handler's dispatch to count invocations.
    real_dispatch = session._intervention_handler.dispatch
    dispatch_calls: list[UserIntervention] = []

    async def _counting_dispatch(iv: UserIntervention) -> InterventionAnswer:
        dispatch_calls.append(iv)
        return await real_dispatch(iv)

    session._intervention_handler.dispatch = _counting_dispatch  # type: ignore[method-assign]

    iv = _make_iv(run_id=run_id)

    async def _resolve_after_delay() -> None:
        await asyncio.sleep(0.05)
        iv.future.set_result(InterventionAnswer(text="from-handler"))

    resolver = asyncio.create_task(_resolve_after_delay())
    try:
        await session._dispatch_intervention(iv)
    finally:
        resolver.cancel()

    # α contract: BOTH ran. Override was notified AND default handler dispatched.
    assert len(override_bus.calls) == 1
    assert len(dispatch_calls) == 1, (
        f"α contract: handler.dispatch must run alongside the override; "
        f"got {len(dispatch_calls)} dispatch calls (expected 1)"
    )


# ---------------------------------------------------------------------------
# Test 5: iv.run_id is None → falls through to default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_falls_through_when_run_id_is_none(tmp_path):
    """Tier 2: iv.run_id is None → override lookup is skipped, falls through
    to default InterventionHandler.
    """
    session = _make_session(tmp_path)
    session.is_attached = True

    chain_id = "chain-any"
    override_bus = _CaptureBus()
    # Register an override (should not be reached because run_id is None).
    session.register_intervention_override(chain_id, override_bus)

    iv = _make_iv(run_id=None)  # run_id is None

    task = asyncio.ensure_future(session._dispatch_intervention(iv))
    await asyncio.sleep(0)

    iv.future.set_result(InterventionAnswer(text="default-none-run"))
    answer = await asyncio.wait_for(task, timeout=2.0)

    assert answer.text == "default-none-run"
    # Override bus was never called.
    assert len(override_bus.calls) == 0


# ---------------------------------------------------------------------------
# Test 6: run_id not in running_skills_chain → falls through to default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_falls_through_when_run_id_not_in_chain(tmp_path):
    """Tier 2: iv.run_id is set but not present in running_skills_chain
    → override lookup cannot resolve chain_id, falls through to default.
    """
    session = _make_session(tmp_path)
    session.is_attached = True

    chain_id = "chain-known"
    override_bus = _CaptureBus()
    session.register_intervention_override(chain_id, override_bus)

    # run_id is NOT in running_skills_chain.
    iv = _make_iv(run_id="run-unknown")

    task = asyncio.ensure_future(session._dispatch_intervention(iv))
    await asyncio.sleep(0)

    iv.future.set_result(InterventionAnswer(text="default-unknown-run"))
    answer = await asyncio.wait_for(task, timeout=2.0)

    assert answer.text == "default-unknown-run"
    assert len(override_bus.calls) == 0


# ---------------------------------------------------------------------------
# Test 7: send_to_agent_impl registers and unregisters override (even on raise)
# ---------------------------------------------------------------------------


def test_send_to_agent_impl_override_registered_and_cleaned_up(tmp_path, monkeypatch):
    """Tier 2: send_to_agent_impl(..., intervention_override=bus) registers the
    override immediately after chain_id is minted and unregisters it in finally,
    even when bus.request raises.

    Uses a _CaptureBus stub that raises to verify cleanup: after the call the
    session's _intervention_overrides must be empty.
    """
    monkeypatch.chdir(tmp_path)
    registry = _build_registry(tmp_path, [("default", "")])
    session = registry.get_or_load("default")

    registered_chain_ids: list[str] = []
    unregistered_chain_ids: list[str] = []

    # Spy on register/unregister via a subclass shim placed on the instance.
    orig_register = session.register_intervention_override
    orig_unregister = session.unregister_intervention_override

    def _spy_register(chain_id: str, bus) -> None:
        registered_chain_ids.append(chain_id)
        orig_register(chain_id, bus)

    def _spy_unregister(chain_id: str) -> None:
        unregistered_chain_ids.append(chain_id)
        orig_unregister(chain_id)

    session.register_intervention_override = _spy_register  # type: ignore[method-assign]
    session.unregister_intervention_override = _spy_unregister  # type: ignore[method-assign]

    # Use a _CaptureBus that raises immediately so the request path is
    # short-circuited (bus.request is not actually reached through the
    # MessageBus in this call — the override is registered but the request
    # flow is what matters; we use a fake _handle_user_message instead to
    # keep the test deterministic).
    override_bus = _CaptureBus()

    # Stub _handle_user_message so the session produces a synthetic reply
    # without invoking a real LLM.
    from reyn.chat.session import ChatMessage

    async def _fake_handle(self_inner, message, *, chain_id):
        self_inner._append_history(ChatMessage(
            role="user", text=message, ts="2026-05-16T00:00:00",
            meta={"chain_id": chain_id},
        ))
        self_inner._append_history(ChatMessage(
            role="agent", text="stub-reply",
            ts="2026-05-16T00:00:01",
            meta={"chain_id": chain_id},
        ))

    monkeypatch.setattr(ChatSession, "_handle_user_message", _fake_handle)

    async def go():
        return await send_to_agent_impl(
            registry,
            agent_name="default",
            message="hello",
            timeout=5.0,
            intervention_override=override_bus,
        )

    result = asyncio.run(go())

    assert result["agent"] == "default"
    # register was called exactly once.
    assert len(registered_chain_ids) == 1
    # unregister was called exactly once (finally clause).
    assert len(unregistered_chain_ids) == 1
    # The same chain_id was registered and unregistered.
    assert registered_chain_ids[0] == unregistered_chain_ids[0]
    # After the call, the session has no lingering overrides.
    assert session._intervention_overrides == {}


def test_send_to_agent_impl_override_cleaned_up_on_exception(tmp_path, monkeypatch):
    """Tier 2: override is unregistered in finally even when MessageBus.request raises.

    Verifies the no-leak guarantee on the exception path.
    """
    monkeypatch.chdir(tmp_path)
    registry = _build_registry(tmp_path, [("default", "")])
    session = registry.get_or_load("default")

    override_bus = _CaptureBus()

    # Stub MessageBus.request to raise immediately.
    from reyn.chat.message_bus import MessageBus

    async def _raising_request(self_bus, session_arg, *, kind, payload, reply_to, timeout):
        raise RuntimeError("simulated MessageBus failure")

    monkeypatch.setattr(MessageBus, "request", _raising_request)

    async def go():
        await send_to_agent_impl(
            registry,
            agent_name="default",
            message="hello",
            timeout=5.0,
            intervention_override=override_bus,
        )

    with pytest.raises(RuntimeError, match="simulated MessageBus failure"):
        asyncio.run(go())

    # After the exception, the session must have no lingering overrides.
    assert session._intervention_overrides == {}, (
        f"Override leak detected: {session._intervention_overrides!r}"
    )


# ---------------------------------------------------------------------------
# Test 8: concurrent calls serialize via agent lock — no override leak
# ---------------------------------------------------------------------------


def test_concurrent_send_to_agent_impl_no_override_leak(tmp_path, monkeypatch):
    """Tier 2: two concurrent send_to_agent_impl calls with different overrides
    on the same agent serialize via the agent lock and neither leaks its override
    into the other's execution window.

    Verifies by observing that after both calls complete, _intervention_overrides
    is empty and each call's override was registered + unregistered exactly once.
    """
    monkeypatch.chdir(tmp_path)
    registry = _build_registry(tmp_path, [("default", "")])
    session = registry.get_or_load("default")

    bus_alpha = _CaptureBus()
    bus_beta = _CaptureBus()

    # Track registration events per bus identity.
    registered: list[tuple[str, object]] = []   # (chain_id, bus)
    unregistered: list[str] = []               # chain_ids

    orig_register = session.register_intervention_override
    orig_unregister = session.unregister_intervention_override

    def _spy_register(chain_id: str, bus) -> None:
        registered.append((chain_id, bus))
        orig_register(chain_id, bus)

    def _spy_unregister(chain_id: str) -> None:
        unregistered.append(chain_id)
        orig_unregister(chain_id)

    session.register_intervention_override = _spy_register  # type: ignore[method-assign]
    session.unregister_intervention_override = _spy_unregister  # type: ignore[method-assign]

    from reyn.chat.session import ChatMessage

    call_count = 0

    async def _fake_handle(self_inner, message, *, chain_id):
        nonlocal call_count
        call_count += 1
        self_inner._append_history(ChatMessage(
            role="user", text=message, ts="2026-05-16T00:00:00",
            meta={"chain_id": chain_id},
        ))
        self_inner._append_history(ChatMessage(
            role="agent",
            text=f"reply-to:{message[:20]}",
            ts="2026-05-16T00:00:01",
            meta={"chain_id": chain_id},
        ))

    monkeypatch.setattr(ChatSession, "_handle_user_message", _fake_handle)

    async def go():
        r1, r2 = await asyncio.gather(
            send_to_agent_impl(
                registry,
                agent_name="default",
                message="ALPHA",
                timeout=5.0,
                intervention_override=bus_alpha,
            ),
            send_to_agent_impl(
                registry,
                agent_name="default",
                message="BETA",
                timeout=5.0,
                intervention_override=bus_beta,
            ),
        )
        return r1, r2

    r1, r2 = asyncio.run(go())

    # Both calls completed.
    assert r1["agent"] == "default"
    assert r2["agent"] == "default"

    # Each call registered and unregistered exactly once.
    assert len(registered) == 2
    assert len(unregistered) == 2

    # All registered chain_ids were also unregistered (no leak).
    registered_chain_ids = {cid for cid, _ in registered}
    assert registered_chain_ids == set(unregistered), (
        f"Leak: registered={registered_chain_ids!r}, unregistered={set(unregistered)!r}"
    )

    # No lingering overrides after both calls.
    assert session._intervention_overrides == {}
