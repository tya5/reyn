"""Unit tests for RouterLoop (PR35 wave-2 task D).

Uses FakeRouterHost and a scripted callable (_ScriptedLLM) to return scripted
LLMToolCallResult sequences without hitting the network.

No unittest.mock.AsyncMock / MagicMock / patch(new_callable=AsyncMock) are
used. patch() is only called with real callables (policy: Mock vs Fake).
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from reyn.runtime.router_loop import _UNIVERSAL_WRAPPER_NAMES, RouterLoop, _build_hot_list_aliases

# Shared RouterLoop fakes/builders now live in tests/_support (stable, location-
# independent import path). Aliased back to the original module-local names so
# the tests below are unchanged.
from tests._support.router_loop import (  # noqa: E402
    FakeRouterHost,
    make_loop,
    text_result,
    tool_result,
)
from tests._support.router_loop import (
    ScriptedLLM as _ScriptedLLM,
)

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chitchat_no_tools(monkeypatch):
    """Tier 1: RouterLoop text-reply path puts one agent message in outbox."""
    host = FakeRouterHost()
    loop = make_loop(host)

    scripted = _ScriptedLLM([text_result("hello")])

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", scripted)
    await loop.run("hi", [])

    (msg,) = host.outbox
    assert msg["kind"] == "agent"
    assert msg["text"] == "hello"
    assert not host.skill_calls
    assert scripted.call_count == 1


@pytest.mark.asyncio
async def test_max_iterations_exhausted(monkeypatch):
    """Tier 2: OS invariant — RouterLoop emits error outbox message after exceeding max_iterations cap. Loop never runs more iterations than configured."""
    host = FakeRouterHost()
    loop = make_loop(host, max_iterations=3)

    # Always return a tool call (unknown tool to avoid side effects)
    always_tool = tool_result([{"name": "bogus_tool", "args": {}}])
    scripted = _ScriptedLLM([always_tool] * 3)

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", scripted)
    await loop.run("do stuff", [])

    assert scripted.call_count == 3
    (msg,) = host.outbox
    assert msg["kind"] == "error"
    assert "max iterations" in msg["text"]
    assert "3" in msg["text"]


@pytest.mark.asyncio
async def test_unknown_tool_returns_error_in_result(monkeypatch):
    """Tier 1: unknown tool name produces error tool result with kind=unknown_tool; loop continues to next round."""
    host = FakeRouterHost()
    loop = make_loop(host)

    rounds = [
        tool_result([{"name": "bogus", "args": {}}]),
        text_result("Recovered."),
    ]

    messages_captured: list[list[dict]] = []

    async def mock_llm(*, messages, **kwargs):
        messages_captured.append(list(messages))
        return rounds[len(messages_captured) - 1]

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", mock_llm)
    await loop.run("try bogus", [])

    # Find the tool result message from round 1
    round2_messages = messages_captured[1]
    tool_msgs = [m for m in round2_messages if m.get("role") == "tool"]
    (tool_msg,) = tool_msgs
    result_data = json.loads(tool_msg["content"])
    # PR36: unknown tools now return {status: "error", error: {kind: "unknown_tool", ...}}
    assert result_data.get("status") == "error"
    error = result_data.get("error", {})
    assert error.get("kind") == "unknown_tool"
    assert "bogus" in error.get("message", "")
    assert host.outbox[0]["text"] == "Recovered."


@pytest.mark.asyncio
async def test_remember_shared_writes_file_and_regenerates_index(monkeypatch):
    """Tier 1: remember_shared tool writes memory file with correct frontmatter and triggers index regeneration."""
    host = FakeRouterHost(file_permissions={"read": ["/memory"], "write": ["/memory"]})
    loop = make_loop(host)

    rounds = [
        tool_result([{
            "name": "remember_shared",
            "args": {
                "slug": "user_role",
                "name": "User Role",
                "description": "User is a developer",
                "type": "user",
                "body": "The user is a senior developer.",
            },
        }]),
        text_result("Saved."),
    ]
    scripted = _ScriptedLLM(rounds)

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", scripted)
    await loop.run("remember: I'm a developer", [])

    # file_write should have been called with the right path
    written_paths = [path for path, _ in host.file_writes]
    assert "/memory/shared/user_role.md" in written_paths

    # Check frontmatter in written content
    written_content = dict(host.file_writes)["/memory/shared/user_role.md"]
    assert "name: User Role" in written_content
    assert "type: user" in written_content
    assert "The user is a senior developer." in written_content

    # file_regenerate_index should have been called
    (regen,) = host.index_regenerations
    assert regen["output_path"] == "/memory/shared/MEMORY.md"

    assert host.outbox[0]["text"] == "Saved."


@pytest.mark.asyncio
async def test_list_memory_top_level():
    """Tier 1: list_memory('') returns layer+count entries from memory index. Tests tool API output shape without LLM involvement."""
    memory_content = (
        "# Memory Index (shared)\n\n"
        "- [User Role](user_role.md) — Developer\n"
        "- [Project Goal](project_goal.md) — Build OS\n"
        "\n"
        "# Memory Index (agent: chat_20240101)\n\n"
        "- [Feedback tone](feedback_tone.md) — Prefer formal\n"
    )
    host = FakeRouterHost(
        memory_index={"status": "ok", "content": memory_content}
    )
    loop = make_loop(host)

    result = loop._list_memory("")

    result_by_path = {r["path"]: r["count"] for r in result}
    assert result_by_path["shared"] == 2
    assert result_by_path["agent"] == 1


@pytest.mark.asyncio
async def test_delegate_to_agent(monkeypatch):
    """Tier 2: OS invariant — RouterLoop exits after first delegate_to_agent dispatch and emits awaiting-peer-reply status; does not iterate further in the same turn.

    Earlier (pre-PR-tui-4-followup) the loop continued and the LLM
    re-delegated each iteration until the cap was exhausted. Fix:
    after a successful delegate dispatch, RouterLoop emits a status
    note and returns; PR14 pending_chain re-invokes router with the
    peer reply later.
    """
    host = FakeRouterHost(agents=[{"name": "peer_agent", "role": "data agent"}])
    loop = make_loop(host)

    rounds = [
        tool_result([{
            "name": "delegate_to_agent",
            "args": {"to": "peer_agent", "request": "please process the data"},
        }]),
        # Subsequent rounds intentionally not consumed — loop must exit
        # after the delegate dispatch.
        text_result("Should not reach this round."),
    ]
    scripted = _ScriptedLLM(rounds)

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", scripted)
    await loop.run("send to peer", [])

    (agent_send,) = host.agent_sends
    assert agent_send["to"] == "peer_agent"
    assert agent_send["request"] == "please process the data"
    assert agent_send["chain_id"] == "chain-test"
    # Only the first LLM call ran; the second round was never consumed.
    assert scripted.call_count == 1
    # B55 R-7: outbox shows a `[task_spawned] kind=agent ...`
    # structured spawn_ack (= parity with skill / plan spawn_ack),
    # not a generic "awaiting peer reply" status row.
    assert any(
        m["kind"] == "agent"
        and m.get("meta", {}).get("source") == "agent_spawn_ack"
        and "[task_spawned] kind=agent" in m["text"]
        for m in host.outbox
    ), f"Expected agent_spawn_ack; got: {host.outbox}"


@pytest.mark.asyncio
async def test_delegate_does_not_re_delegate_in_same_turn(monkeypatch):
    """Tier 2: OS invariant — RouterLoop.run() exits after first delegate dispatch even if LLM keeps emitting delegate calls; exactly one dispatch occurs regardless of max_iterations.

    Real LLM behavior (dogfood verify): with the old code the LLM saw
    `{status: dispatched}`, didn't realize the peer reply would arrive
    asynchronously, and re-delegated each iteration until the cap fired.
    Now we exit after the first delegate so pending_chain can take over.
    """
    host = FakeRouterHost(agents=[{"name": "peer", "role": "x"}])
    loop = make_loop(host, max_iterations=5)

    # If the loop kept iterating it would call delegate 5 times.
    delegate_round = tool_result([{
        "name": "delegate_to_agent",
        "args": {"to": "peer", "request": "do work"},
    }])
    scripted = _ScriptedLLM([delegate_round] * 5)

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", scripted)
    await loop.run("delegate", [])

    # Exactly one delegate dispatch; loop exited after the first iteration.
    (only_send,) = host.agent_sends
    assert scripted.call_count == 1


@pytest.mark.asyncio
async def test_dedupe_duplicate_async_tool_calls_in_same_round(monkeypatch):
    """Tier 2: OS invariant — duplicate async tool_calls (same name, same
    args) in a single LLM round are deduped before dispatch (F5 fix).

    Weak models (e.g. gemini-2.5-flash-lite) sometimes emit
    `delegate_to_agent` twice with identical arguments in one tool_calls
    list. Without dedupe, the peer's inbox would receive the same
    request twice, doubling cost and confusing the chain. After dedupe,
    exactly one send_to_agent runs and a `tool_call_deduped` audit event
    is emitted for the suppressed call.
    """
    host = FakeRouterHost(agents=[{"name": "peer", "role": "x"}])
    loop = make_loop(host)

    # Two identical delegate_to_agent calls in the same round.
    duplicate_round = tool_result([
        {"id": "tc_a", "name": "delegate_to_agent",
         "args": {"to": "peer", "request": "do work"}},
        {"id": "tc_b", "name": "delegate_to_agent",
         "args": {"to": "peer", "request": "do work"}},
    ])
    scripted = _ScriptedLLM([duplicate_round])

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", scripted)
    await loop.run("send", [])

    # Only one send_to_agent — duplicate suppressed.
    (agent_send,) = host.agent_sends
    assert agent_send["to"] == "peer"
    assert agent_send["request"] == "do work"
    # Audit event records the suppressed call.
    deduped_events = [
        e for e in host.events.emitted  # type: ignore[attr-defined]
        if e["type"] == "tool_call_deduped"
    ]
    (deduped_evt,) = deduped_events
    assert deduped_evt["name"] == "delegate_to_agent"
    assert deduped_evt["reason"] == "duplicate_async_in_round"


@pytest.mark.asyncio
async def test_dedupe_does_not_collapse_distinct_async_args(monkeypatch):
    """Tier 2: OS invariant — async tool_calls with different args are
    NOT deduped (F5 false-positive guard).

    Two `delegate_to_agent` calls to the same peer with different
    `request` payloads must both dispatch — they're legitimately distinct
    work items.
    """
    host = FakeRouterHost(agents=[{"name": "peer", "role": "x"}])
    loop = make_loop(host)

    distinct_round = tool_result([
        {"id": "tc_a", "name": "delegate_to_agent",
         "args": {"to": "peer", "request": "task A"}},
        {"id": "tc_b", "name": "delegate_to_agent",
         "args": {"to": "peer", "request": "task B"}},
    ])
    scripted = _ScriptedLLM([distinct_round])

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", scripted)
    await loop.run("send two tasks", [])

    # Both dispatch — different args.
    requests = sorted(s["request"] for s in host.agent_sends)
    assert requests == ["task A", "task B"]
    # No dedupe events.
    deduped_events = [
        e for e in host.events.emitted  # type: ignore[attr-defined]
        if e["type"] == "tool_call_deduped"
    ]
    assert not deduped_events


@pytest.mark.asyncio
async def test_dedupe_does_not_apply_to_non_invoke_sync_tool_calls(monkeypatch):
    """Tier 2: OS invariant — duplicate SYNC tool_calls in the same round are
    NOT deduped.

    Sync tool dupes are wasteful but correctness-preserving (same args →
    same result), and deduping them risks tool_call_id mismatches in the
    follow-up assistant message. Only async tools (delegate_to_agent) get the
    dedupe treatment; sync tools do not.
    """
    host = FakeRouterHost(skills=[{"name": "my_skill", "category": "general"}])
    loop = make_loop(host)

    rounds = [
        tool_result([
            {"id": "tc_a", "name": "describe_skill",
             "args": {"name": "my_skill"}},
            {"id": "tc_b", "name": "describe_skill",
             "args": {"name": "my_skill"}},
        ]),
        text_result("done"),
    ]
    scripted = _ScriptedLLM(rounds)

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", scripted)
    await loop.run("describe", [])

    # No dedupe events for non-invoke_skill sync tools.
    deduped_events = [
        e for e in host.events.emitted  # type: ignore[attr-defined]
        if e["type"] == "tool_call_deduped"
    ]
    assert not deduped_events


@pytest.mark.asyncio
async def test_forget_memory_deletes_file_and_regenerates_index(monkeypatch):
    """Tier 1: forget_memory tool deletes the memory file and triggers index regeneration."""
    host = FakeRouterHost(file_permissions={"read": ["/memory"], "write": ["/memory"]})
    host._files["/memory/shared/user_role.md"] = "# old memory"
    loop = make_loop(host)

    rounds = [
        tool_result([{
            "name": "forget_memory",
            "args": {"layer": "shared", "slug": "user_role"},
        }]),
        text_result("Forgotten."),
    ]
    scripted = _ScriptedLLM(rounds)

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", scripted)
    await loop.run("forget my role", [])

    assert "/memory/shared/user_role.md" in host.file_deletes
    (only_regen,) = host.index_regenerations
    assert host.outbox[0]["text"] == "Forgotten."


@pytest.mark.asyncio
async def test_history_appended_to_messages(monkeypatch):
    """Tier 1: prior history turns appear in LLM messages before the current user utterance, in correct role order."""
    host = FakeRouterHost()
    loop = make_loop(host)

    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]

    messages_seen: list[list[dict]] = []

    async def mock_llm(*, messages, **kwargs):
        messages_seen.append(list(messages))
        return text_result("reply")

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", mock_llm)
    await loop.run("new message", history)

    first_call_messages = messages_seen[0]
    roles = [m["role"] for m in first_call_messages]
    # system, history[0], history[1], user
    assert roles == ["system", "user", "assistant", "user"]
    assert first_call_messages[-1]["content"] == "new message"


# ---------------------------------------------------------------------------
# PR36 Layer 1: tool name validation tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_tool_name_returns_error_not_dispatched(monkeypatch):
    """Tier 2: OS invariant — tool_call for a name absent from the current catalog returns status=error/kind=unknown_tool; underlying host method is never called.

    LLM emits tool_call with name='read_file' (not in catalog for no-file host).
    """
    # Host with no file_permissions → read_file not in catalog.
    host = FakeRouterHost(
        skills=[{"name": "list_skills", "category": "general"}],
        file_permissions=None,
        mcp_servers=[],
    )
    loop = make_loop(host)

    rounds = [
        tool_result([{"name": "read_file", "args": {"path": "/some/file.txt"}}]),
        text_result("Sorry, let me try differently."),
    ]

    messages_captured: list[list[dict]] = []

    async def mock_llm(*, messages, **kwargs):
        messages_captured.append(list(messages))
        return rounds[len(messages_captured) - 1]

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", mock_llm)
    await loop.run("read README.md", [])

    # host.file_read must NOT have been called.
    assert not host.file_reads, "file_read must not be called for unknown tool"

    # The tool result fed back to the LLM should carry status=error, kind=unknown_tool
    round2_messages = messages_captured[1]
    tool_msgs = [m for m in round2_messages if m.get("role") == "tool"]
    (tool_msg,) = tool_msgs
    result_data = json.loads(tool_msg["content"])
    assert result_data.get("status") == "error"
    error = result_data.get("error", {})
    assert error.get("kind") == "unknown_tool"
    assert "read_file" in error.get("message", "")

    # Loop recovered and produced a reply
    assert host.outbox[0]["text"] == "Sorry, let me try differently."


@pytest.mark.asyncio
async def test_tool_names_populated_per_run(monkeypatch):
    """Tier 1: tool catalog reflects host configuration.

    File-class tools (list_directory, read_file, write_file,
    delete_file) are gated on the operator's `permissions.file.*`
    declaration — they touch the user's project files, which sit
    behind the permission boundary.

    Reyn-source tools (reyn_src_list, reyn_src_read) are unconditional
    by design — they read Reyn's own public OSS repository, not the
    user's files, so no permission gate applies.
    """
    host_no_file = FakeRouterHost(file_permissions=None, mcp_servers=[])
    loop = RouterLoop(host=host_no_file, chain_id="chain-test")

    scripted1 = _ScriptedLLM([text_result("ok")])
    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", scripted1)
    await loop.run("hello", [])

    names_no_file = frozenset(loop._tool_names)
    # File tools all gated.
    assert "read_file" not in names_no_file
    assert "list_directory" not in names_no_file
    assert "write_file" not in names_no_file
    assert "delete_file" not in names_no_file
    # Reyn-source tools always present.
    assert "reyn_src_list" in names_no_file
    assert "reyn_src_read" in names_no_file
    # Other always-on baseline.
    assert "list_agents" in names_no_file

    # Second run with a host that has file permissions.
    host_with_file = FakeRouterHost(
        file_permissions={"read": ["/docs"], "write": []},
        mcp_servers=[],
    )
    loop2 = RouterLoop(host=host_with_file, chain_id="chain-test-2")

    scripted2 = _ScriptedLLM([text_result("ok")])
    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", scripted2)
    await loop2.run("hello", [])

    names_with_file = frozenset(loop2._tool_names)
    assert "read_file" in names_with_file
    assert "list_directory" in names_with_file
    assert "write_file" not in names_with_file  # write scope empty

    # Third: with write scope.
    host_with_write = FakeRouterHost(
        file_permissions={"read": ["/docs"], "write": ["/tmp"]},
        mcp_servers=[],
    )
    loop3 = RouterLoop(host=host_with_write, chain_id="chain-test-3")

    scripted3 = _ScriptedLLM([text_result("ok")])
    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", scripted3)
    await loop3.run("hello", [])

    names_with_write = frozenset(loop3._tool_names)
    assert "write_file" in names_with_write
    assert "delete_file" in names_with_write


# ---------------------------------------------------------------------------
# PR37 Wave 2D: dispatch_tool integration + S13b skill-name validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_tool_emits_tool_failed_on_unknown_tool(monkeypatch):
    """Tier 2: P6 invariant — dispatch_tool emits tool_failed event with error_kind=unknown_tool when tool name is not in catalog."""
    host = FakeRouterHost()
    loop = make_loop(host)

    rounds = [
        tool_result([{"name": "bogus_unknown_tool", "args": {}}]),
        text_result("Recovered."),
    ]

    messages_captured: list[list[dict]] = []

    async def mock_llm(*, messages, **kwargs):
        messages_captured.append(list(messages))
        return rounds[len(messages_captured) - 1]

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", mock_llm)
    await loop.run("try bogus", [])

    event_types = [e["type"] for e in host.events.emitted]
    assert "tool_failed" in event_types
    failed = next(e for e in host.events.emitted if e["type"] == "tool_failed")
    assert failed["error_kind"] == "unknown_tool"


@pytest.mark.asyncio
async def test_session_spawn_dispatches_to_host_not_unhandled():
    """Tier 2: #2120 — _invoke_router_tool('session_spawn') reaches the registry handler
    and the host's spawn_session, NOT the {"error": "unhandled tool"} fall-through.

    The tui live-probe found session_spawn advertised but undispatched: the LLM called it
    and got {"error": "unhandled tool: session_spawn"}, no spawn. This drives the real
    dispatch path (REGISTRY_DISPATCH_TOOLS → _invoke_via_registry → SESSION_SPAWN._handle
    → RouterCallerState.spawn_session_fn → host.spawn_session). Drop session_spawn from
    REGISTRY_DISPATCH_TOOLS → the bare name falls through → result is the unhandled-tool
    error and host.spawn_calls stays empty → RED."""
    host = FakeRouterHost()
    loop = RouterLoop(host=host, chain_id="chain-test")

    from reyn.runtime.router_tools import build_tools
    tools = build_tools(host.list_available_agents())
    loop._catalog = {t["function"]["name"]: t for t in tools}
    loop._tool_names = frozenset(loop._catalog.keys())

    result = await loop._invoke_router_tool(
        "session_spawn", {"request": "do a task", "mode": "persistent"}
    )

    assert not (isinstance(result, dict) and "unhandled tool" in str(result.get("error", ""))), (
        f"session_spawn hit the unhandled-tool fall-through (#2120 dispatch gap): {result}"
    )
    assert host.spawn_calls, "session_spawn did not reach host.spawn_session"
    spawned = host.spawn_calls[-1]
    assert spawned["request"] == "do a task"
    assert spawned["mode"] == "persistent"


@pytest.mark.asyncio
async def test_no_events_attribute_needed_for_unknown_tool_path(monkeypatch):
    """Tier 2: P6 invariant — unknown tool error emits tool_failed via host.events through dispatch_tool; event routing is not bypassed on the error path."""
    host = FakeRouterHost()
    loop = make_loop(host)

    rounds = [
        tool_result([{"name": "nonexistent_tool", "args": {}}]),
        text_result("Recovered."),
    ]

    messages_captured: list[list[dict]] = []

    async def mock_llm(*, messages, **kwargs):
        messages_captured.append(list(messages))
        return rounds[len(messages_captured) - 1]

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", mock_llm)
    await loop.run("try nonexistent", [])

    round2_messages = messages_captured[1]
    tool_msgs = [m for m in round2_messages if m.get("role") == "tool"]
    result_data = json.loads(tool_msgs[0]["content"])
    assert result_data.get("status") == "error"
    assert result_data["error"]["kind"] == "unknown_tool"
    # events were emitted
    assert any(e["type"] == "tool_failed" for e in host.events.emitted)


# ---------------------------------------------------------------------------
# B27-C1: _build_hot_list_aliases must filter universal wrapper names
# ---------------------------------------------------------------------------


def test_build_hot_list_aliases_filters_universal_wrappers() -> None:
    """Tier 2: universal wrapper names are excluded from hot-list alias output.

    When the input list contains universal wrapper names (list_actions,
    describe_action, invoke_action, search_actions) alongside a real action
    name, only the real action appears in the returned alias list.

    This is the OS-level invariant that prevents duplicate function
    declarations when ActionUsageTracker.get_top_n() returns a wrapper name
    that was recorded as usage (B27-C1 regression).
    """
    input_names = ["list_actions", "file__read", "describe_action"]
    result = _build_hot_list_aliases(input_names)
    returned_names = [entry["function"]["name"] for entry in result]
    assert returned_names == ["file__read"], (
        f"Expected only ['file__read'] but got {returned_names}; "
        "universal wrappers must be filtered before alias construction"
    )


def test_build_hot_list_aliases_all_wrappers_returns_empty() -> None:
    """Tier 2: when all inputs are universal wrapper names the result is empty.

    All four wrapper names must be present in _UNIVERSAL_WRAPPER_NAMES and
    all must be filtered, leaving an empty list.
    """
    all_wrappers = list(_UNIVERSAL_WRAPPER_NAMES)
    result = _build_hot_list_aliases(all_wrappers)
    assert result == [], (
        f"Expected empty list but got {result}; "
        "_UNIVERSAL_WRAPPER_NAMES must cover all four wrapper names"
    )


def test_build_hot_list_aliases_no_wrappers_passes_through() -> None:
    """Tier 2: when no universal wrapper names are present all names pass through.

    Smoke test: the filter must not affect non-wrapper action names.
    """
    names = ["skill__summarise", "file__write", "agent__planner"]
    result = _build_hot_list_aliases(names)
    returned_names = [entry["function"]["name"] for entry in result]
    assert returned_names == names, (
        f"Expected {names} but got {returned_names}; "
        "non-wrapper names must not be filtered"
    )


def test_universal_wrapper_names_constant_covers_all_four() -> None:
    """Tier 2: _UNIVERSAL_WRAPPER_NAMES contains exactly the four wrapper names.

    Guards against accidental shrinkage of the constant if the set is later
    edited without updating the filter logic.
    """
    expected = {"list_actions", "search_actions", "describe_action", "invoke_action"}
    assert _UNIVERSAL_WRAPPER_NAMES == expected, (
        f"_UNIVERSAL_WRAPPER_NAMES mismatch: got {_UNIVERSAL_WRAPPER_NAMES}"
    )
