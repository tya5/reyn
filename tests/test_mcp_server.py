"""Tier 2: `reyn mcp serve` server-side surface.

Covers the two tools exposed to outer LLM clients:

  - list_agents — enumerate registered agents
  - send_to_agent — submit one user message, await reply text

The tests drive the backing implementations directly
(``list_agents_impl`` / ``send_to_agent_impl``) rather than the full
stdio JSON-RPC transport — that side is owned by the upstream ``mcp``
SDK. We patch ``reyn.chat.router_loop.call_llm_tools`` so each turn
returns a deterministic fake reply (mirrors the pattern in
``test_chat_router_i18n.py``).
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
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.mcp_server import list_agents_impl, send_to_agent_impl

_EMPTY_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=5)


def _text_result(text: str) -> LLMToolCallResult:
    return LLMToolCallResult(
        content=text,
        tool_calls=[],
        finish_reason="stop",
        usage=_EMPTY_USAGE,
    )


def _make_llm_stub(result_or_factory):
    """Return a real async callable that stands in for ``call_llm_tools``.

    Accepts either a fixed ``LLMToolCallResult``, a list of results to
    return in sequence, or a callable ``(**kw) -> LLMToolCallResult``.
    Using a real callable per testing policy: signature drift surfaces as
    TypeError rather than silently succeeding.
    """
    if callable(result_or_factory) and not isinstance(result_or_factory, LLMToolCallResult):
        _inner = result_or_factory

        async def _stub_fn(**kwargs) -> LLMToolCallResult:
            return await _inner(**kwargs)

        return _stub_fn
    elif isinstance(result_or_factory, list):
        results = list(result_or_factory)
        call_count = [0]

        async def _stub_list(**kwargs) -> LLMToolCallResult:
            idx = call_count[0]
            call_count[0] += 1
            return results[idx] if idx < len(results) else results[-1]

        return _stub_list
    else:
        fixed = result_or_factory

        async def _stub_fixed(**kwargs) -> LLMToolCallResult:
            return fixed

        return _stub_fixed


def _build_registry(
    tmp_path: Path,
    agent_specs: list[tuple[str, str]],
) -> AgentRegistry:
    """Construct an AgentRegistry on tmp_path with the given (name, role) agents.

    Each session is wired with a real BudgetTracker and a snapshot path
    redirected under tmp_path so no global state is touched.
    """
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

    for name, role in agent_specs:
        if name == "default":
            # The registry auto-creates `default`; just refresh its role.
            agent_dir = registry._dir / name
            AgentProfile.new(name, role=role).save(agent_dir)
        else:
            registry.create(name, role=role)

    return registry


# ---------------------------------------------------------------------------
# Tier 2: list_agents
# ---------------------------------------------------------------------------


def test_list_agents_returns_registered_agents(tmp_path):
    """Tier 2: list_agents returns one entry per agent on disk, with the
    role excerpt populated from each profile's role field.

    Pins the contract: the MCP surface enumerates the same names that
    ``reyn agent ls`` would show, no extra filtering.
    """
    registry = _build_registry(tmp_path, [
        ("default", "general assistant"),
        ("planner", "plans things"),
        ("coder", "writes code"),
    ])

    agents = asyncio.run(list_agents_impl(registry))
    names = {a["name"] for a in agents}
    assert names == {"default", "planner", "coder"}

    by_name = {a["name"]: a["role"] for a in agents}
    assert by_name["planner"] == "plans things"
    assert by_name["coder"] == "writes code"


# ---------------------------------------------------------------------------
# Tier 2: send_to_agent — basic reply
# ---------------------------------------------------------------------------


def test_send_to_agent_returns_reply_text(tmp_path, monkeypatch):
    """Tier 2: send_to_agent submits the message, awaits the agent's
    final reply (kind="agent" history entry), and returns it as text.

    The router LLM is faked to return a fixed string; we assert the
    server returns exactly that string in the ``reply`` field.
    """
    monkeypatch.chdir(tmp_path)
    registry = _build_registry(tmp_path, [("default", "")])

    monkeypatch.setattr(
        "reyn.chat.router_loop.call_llm_tools",
        _make_llm_stub(_text_result("Hello from Reyn!")),
    )

    async def go():
        return await send_to_agent_impl(
            registry,
            agent_name="default",
            message="Hi there",
            timeout=5.0,
        )

    result = asyncio.run(go())
    assert result["agent"] == "default"
    assert result["partial"] is False
    assert "Hello from Reyn!" in result["reply"]


# ---------------------------------------------------------------------------
# Tier 2: send_to_agent — unknown agent
# ---------------------------------------------------------------------------


def test_send_to_unknown_agent_errors(tmp_path):
    """Tier 2: send_to_agent on a non-existent name raises ValueError so
    the SDK glue can surface it as an error tool result rather than
    silently auto-creating the agent.
    """
    registry = _build_registry(tmp_path, [("default", "")])

    async def go():
        await send_to_agent_impl(
            registry,
            agent_name="ghost",
            message="hello",
            timeout=1.0,
        )

    with pytest.raises(ValueError, match="ghost"):
        asyncio.run(go())


# ---------------------------------------------------------------------------
# Tier 2: send_to_agent — history persists across calls
# ---------------------------------------------------------------------------


def test_send_to_agent_history_persists_across_calls(tmp_path, monkeypatch):
    """Tier 2: two send_to_agent calls on the same agent share history.

    On the second call we observe (a) the prior user + agent turns are
    in ``session.history``, and (b) only the new agent reply is returned
    (= the implementation slices on baseline = pre-submit history length,
    not the entire history).
    """
    monkeypatch.chdir(tmp_path)
    registry = _build_registry(tmp_path, [("default", "")])

    monkeypatch.setattr(
        "reyn.chat.router_loop.call_llm_tools",
        _make_llm_stub([
            _text_result("I will remember 17."),
            _text_result("You told me 17."),
        ]),
    )

    async def go() -> tuple[dict, dict, list]:
        r1 = await send_to_agent_impl(
            registry,
            agent_name="default",
            message="Remember the number 17.",
            timeout=5.0,
        )
        r2 = await send_to_agent_impl(
            registry,
            agent_name="default",
            message="What number did I just tell you?",
            timeout=5.0,
        )
        # Read history through the registry's cached session — same
        # in-process instance both calls landed on.
        session = registry._agents["default"]
        return r1, r2, list(session.history)

    r1, r2, history = asyncio.run(go())

    # First reply wraps the first faked response.
    assert "17" in r1["reply"]
    # Second reply returns ONLY the new turn — not the previous reply.
    assert "You told me 17." in r2["reply"]
    assert "I will remember 17." not in r2["reply"]

    # History accumulated across calls: both user turns and both assistant
    # turns must be present (issue #383: role rename "agent" → "assistant").
    user_turns = [m for m in history if m.role == "user"]
    agent_turns = [m for m in history if m.role == "assistant"]
    assert user_turns, "expected at least one user turn in history"
    assert agent_turns, "expected at least one assistant turn in history"
    # Both messages must appear in user history (= history is shared across calls).
    user_contents = [m.content for m in user_turns]
    assert any("17" in c for c in user_contents), "first user message must persist"
    assert any("What number" in c for c in user_contents), "second user message must persist"


# ---------------------------------------------------------------------------
# Tier 2: build_server tool registration
# ---------------------------------------------------------------------------


def test_concurrent_send_to_same_agent_does_not_cross_talk(tmp_path, monkeypatch):
    """Tier 2: B16-S2-1 / G25 regression net — two concurrent
    ``send_to_agent_impl`` calls on the SAME agent must each receive only
    their own reply, not the other caller's.

    Discovered in batch 16 S2 dogfood (2026-05-08): with no per-agent
    serialization and no chain_id filter on history harvest, both
    concurrent A2A callers received both replies joined together —
    in 3/5 runs the answers were swapped or duplicated. Fix landed in
    same wave: per-agent ``asyncio.Lock`` in ``send_to_agent_impl`` +
    ``_new_agent_history_entries(... chain_id=...)`` filter.

    This test pins the contract by firing two ``asyncio.gather`` calls
    against the same agent and asserting each reply contains the
    expected per-call marker.
    """
    monkeypatch.chdir(tmp_path)
    registry = _build_registry(tmp_path, [("default", "")])

    # The fake LLM returns the user prompt itself so we can assert
    # which reply each caller received.
    async def echo_llm(*, messages, **kw):
        # Last user message text
        user_text = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_text = m.get("content", "") or ""
                break
        return _text_result(f"echo: {user_text[:40]}")

    monkeypatch.setattr("reyn.chat.router_loop.call_llm_tools", echo_llm)

    async def go() -> tuple[dict, dict]:
        r1, r2 = await asyncio.gather(
            send_to_agent_impl(
                registry, agent_name="default",
                message="ALPHA-MARKER", timeout=5.0,
            ),
            send_to_agent_impl(
                registry, agent_name="default",
                message="BETA-MARKER", timeout=5.0,
            ),
        )
        return r1, r2

    r1, r2 = asyncio.run(go())

    # Each reply must contain its OWN marker, not the other call's.
    # Cross-talk would manifest as both replies containing both markers
    # (= what was observed pre-fix in batch 16 S2).
    assert "ALPHA-MARKER" in r1["reply"], (
        f"r1 should echo ALPHA-MARKER; got: {r1['reply']!r}"
    )
    assert "BETA-MARKER" not in r1["reply"], (
        f"r1 must NOT contain BETA-MARKER (= cross-talk); got: {r1['reply']!r}"
    )
    assert "BETA-MARKER" in r2["reply"], (
        f"r2 should echo BETA-MARKER; got: {r2['reply']!r}"
    )
    assert "ALPHA-MARKER" not in r2["reply"], (
        f"r2 must NOT contain ALPHA-MARKER (= cross-talk); got: {r2['reply']!r}"
    )


def test_send_to_agent_waits_for_plan_terminal_text(tmp_path, monkeypatch):
    """Tier 2: G27 regression net — when the chat router invokes the
    ``plan`` async tool, ``send_to_agent_impl`` must wait for the plan
    task's terminal text (= via spawn_plan_task → outbox + history
    append) before returning to the A2A caller.

    Discovered in batch 17 G24 attractor investigation (2026-05-08):
    plan-mode async dispatch (ADR-0023 §2.1.1) returns from RouterLoop
    on spawn ack only; the terminal text comes asynchronously via
    spawn_plan_task. Pre-fix, send_to_agent_impl harvested history
    immediately after _handle_user_message returned, missing the
    plan's terminal text. A2A callers got empty replies.

    Fix: send_to_agent_impl now awaits all running_plans before
    harvest, within the remaining timeout budget. spawn_plan_task
    appends terminal text to history (= alongside the existing
    put_outbox call) so the harvested entries include the plan reply.

    This test pins the contract by simulating a plan task that
    asynchronously appends a terminal text to history, and asserting
    the A2A caller receives it.
    """
    monkeypatch.chdir(tmp_path)
    registry = _build_registry(tmp_path, [("default", "")])

    # Stub LLM: not invoked because we directly simulate plan dispatch
    # via a fake _handle_user_message that schedules a background task.
    async def _fake_handle_user_message(self, message, *, chain_id):
        from reyn.chat.session import ChatMessage
        # Append the user message (= mirror real path)
        self._append_history(ChatMessage(
            role="user", content=message, ts="2026-05-08T00:00:00",
            meta={"chain_id": chain_id},
        ))
        # Schedule a background "plan task" that appends terminal
        # text after a tiny delay (= simulates async plan dispatch).
        async def _plan_task():
            await asyncio.sleep(0.05)
            self._append_history(ChatMessage(
                role="assistant", content=f"PLAN_RESULT_FOR:{message[:20]}",
                ts="2026-05-08T00:00:01",
                meta={"chain_id": chain_id, "source": "plan"},
            ))
        task = asyncio.create_task(_plan_task())
        # Track in running_plans so send_to_agent_impl awaits it.
        self.running_plans["fake_plan_id"] = task

    from reyn.chat.session import ChatSession
    monkeypatch.setattr(
        ChatSession, "_handle_user_message", _fake_handle_user_message,
    )

    async def go():
        return await send_to_agent_impl(
            registry, agent_name="default",
            message="test_prompt_marker",
            timeout=5.0,
        )

    result = asyncio.run(go())
    assert result["agent"] == "default"
    # The plan terminal text MUST be in the reply (= waited for it)
    assert "PLAN_RESULT_FOR:test_prompt_marker" in result["reply"], (
        f"Expected plan terminal text in reply; got: {result['reply']!r}"
    )
    assert result["partial"] is False  # idle by completion


def test_send_to_agent_drains_skill_completed_inbox(tmp_path, monkeypatch):
    """Tier 2: R-A2A-COMPLETION-DRAIN regression net — FP-0012's
    non-blocking ``invoke_skill`` returns spawn-ack immediately; the
    completion narration is driven by a ``skill_completed`` inbox
    kind that ``session.run()`` consumes. The A2A / MCP bypass path
    does not run ``session.run()`` (asyncio-starvation under stdio),
    so without an explicit drain, completion narration never fires
    for A2A-driven agents.

    Fix: ``send_to_agent_impl`` awaits ``running_skills`` after
    ``_handle_user_message`` returns, then calls
    ``ChatSession.drain_skill_completed_inbox`` to dispatch any queued
    ``skill_completed`` items inline. This test pins the contract by:

    1. Faking ``_handle_user_message`` to spawn a background "skill"
       that enqueues ``skill_completed`` (via the production-shaped
       ``_enqueue_skill_completed`` helper).
    2. Faking ``_handle_skill_completed`` to append a sentinel agent
       reply to history (= proves the drain reached the handler).
    3. Asserting the sentinel appears in the A2A reply text.
    """
    monkeypatch.chdir(tmp_path)
    registry = _build_registry(tmp_path, [("default", "")])

    sentinel = "SKILL_COMPLETION_NARRATION_MARKER"

    async def _fake_handle_user_message(self, message, *, chain_id):
        from reyn.chat.session import ChatMessage
        self._append_history(ChatMessage(
            role="user", content=message, ts="2026-05-11T00:00:00",
            meta={"chain_id": chain_id},
        ))

        async def _skill_task() -> None:
            await asyncio.sleep(0.05)
            await self._enqueue_skill_completed(
                run_id="fake_run_0001",
                skill="fake_skill",
                status="finished",
                chain_id=chain_id,
                data={"hello": "world"},
            )

        task = asyncio.create_task(_skill_task())
        self.running_skills["fake_run_0001"] = task

    async def _fake_handle_skill_completed(self, payload):
        from reyn.chat.session import ChatMessage
        self._append_history(ChatMessage(
            role="assistant",
            content=f"{sentinel}: {payload.get('skill')} {payload.get('status')}",
            ts="2026-05-11T00:00:01",
            meta={
                "chain_id": payload.get("chain_id"),
                "source": "skill_completion_narration",
            },
        ))

    monkeypatch.setattr(
        ChatSession, "_handle_user_message", _fake_handle_user_message,
    )
    monkeypatch.setattr(
        ChatSession, "_handle_skill_completed", _fake_handle_skill_completed,
    )

    async def go():
        return await send_to_agent_impl(
            registry, agent_name="default",
            message="kick off the skill",
            timeout=5.0,
        )

    result = asyncio.run(go())
    assert result["agent"] == "default"
    assert sentinel in result["reply"], (
        f"Completion narration must appear in A2A reply; got: {result['reply']!r}"
    )
    assert result["partial"] is False  # drain completed within budget


def test_drain_skill_completed_inbox_preserves_other_kinds(tmp_path):
    """Tier 2: ``drain_skill_completed_inbox`` only consumes
    ``skill_completed`` kinds; any other inbox kinds remain queued
    (FIFO) so the next consumer / call can still pick them up.
    """
    monkeypatch_chdir = tmp_path  # noqa: F841 — keep tmp scope explicit
    registry = _build_registry(tmp_path, [("default", "")])
    session = registry.get_or_load("default")

    async def go():
        # Manually enqueue: skill_completed + agent_request + skill_completed.
        await session._put_inbox("skill_completed", {
            "run_id": "r1", "skill": "s1", "status": "finished",
            "chain_id": "c1", "data": {},
        })
        await session._put_inbox("agent_request", {
            "from_agent": "peer", "text": "hi",
        })
        await session._put_inbox("skill_completed", {
            "run_id": "r2", "skill": "s2", "status": "error",
            "chain_id": "c2", "data": {"error": "bad"},
        })

        # Replace _handle_skill_completed with a counter so the test
        # doesn't pull in the real router stack.
        dispatched: list[dict] = []

        async def _record(self, payload):
            dispatched.append(payload)

        from reyn.chat.session import ChatSession as _CS
        original = _CS._handle_skill_completed
        _CS._handle_skill_completed = _record  # type: ignore[assignment]
        try:
            import time as _time
            ok = await session.drain_skill_completed_inbox(
                deadline_monotonic=_time.monotonic() + 5.0,
            )
        finally:
            _CS._handle_skill_completed = original  # type: ignore[assignment]

        return ok, dispatched

    drained_ok, dispatched = asyncio.run(go())
    assert drained_ok is True
    # Both skill_completed entries dispatched in FIFO order; agent_request preserved.
    assert [d["run_id"] for d in dispatched] == ["r1", "r2"]
    # agent_request must remain in the queue for the next consumer.
    assert session.inbox.qsize() == 1
    leftover_kind, leftover_payload = session.inbox.get_nowait()
    assert leftover_kind == "agent_request"
    assert leftover_payload.get("from_agent") == "peer"


def test_build_server_exposes_documented_tools(tmp_path):
    """Tier 2: build_server registers exactly the documented tools.
    Acts as a P7 detection net — adding a new tool without refreshing
    the documented contract trips this test.

    issue #270 Phase B added ``answer_intervention`` (= MCP-side
    answer-delivery wire for ivs emitted by send_to_agent skills).
    """
    from reyn.mcp_server import build_server

    registry = _build_registry(tmp_path, [("default", "")])
    server = build_server(registry)

    # The mcp SDK stashes the registered list_tools handler under
    # request_handlers keyed by the request type. We invoke it directly.
    from mcp.types import ListToolsRequest

    handler = server.request_handlers[ListToolsRequest]
    # Build a minimal ListToolsRequest payload. The handler returns a
    # ServerResult whose root is a ListToolsResult.
    req = ListToolsRequest(method="tools/list", params=None)
    result = asyncio.run(handler(req))
    tools = result.root.tools
    names = {t.name for t in tools}
    assert names == {"list_agents", "send_to_agent", "answer_intervention"}
