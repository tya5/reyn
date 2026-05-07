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
from unittest.mock import patch

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
        cost = CostConfig(router_invocations_per_turn=3)
        bt = BudgetTracker(cost)
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

    async def fake_llm_tools(**kw):
        return _text_result("Hello from Reyn!")

    async def go():
        with patch(
            "reyn.chat.router_loop.call_llm_tools",
            side_effect=fake_llm_tools,
        ):
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

    replies = iter([
        "I will remember 17.",
        "You told me 17.",
    ])

    async def fake_llm_tools(**kw):
        return _text_result(next(replies))

    async def go() -> tuple[dict, dict, list]:
        with patch(
            "reyn.chat.router_loop.call_llm_tools",
            side_effect=fake_llm_tools,
        ):
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

    # History accumulated across calls: 2 user turns + 2 agent turns.
    user_turns = [m for m in history if m.role == "user"]
    agent_turns = [m for m in history if m.role == "agent"]
    assert len(user_turns) == 2
    assert len(agent_turns) == 2


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

    async def go() -> tuple[dict, dict]:
        with patch(
            "reyn.chat.router_loop.call_llm_tools",
            side_effect=echo_llm,
        ):
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


def test_build_server_exposes_two_tools(tmp_path):
    """Tier 2: build_server registers exactly the two documented tools
    (list_agents, send_to_agent). Acts as a P7 detection net — adding a
    third tool here without refreshing the documented contract trips
    this test.
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
    assert names == {"list_agents", "send_to_agent"}
