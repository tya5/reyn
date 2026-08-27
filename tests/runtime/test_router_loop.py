"""Unit tests for RouterLoop (PR35 wave-2 task D).

Uses FakeRouterHost and a scripted callable (_ScriptedLLM) to return scripted
LLMToolCallResult sequences without hitting the network.

No unittest.mock.AsyncMock / MagicMock / patch(new_callable=AsyncMock) are
used. patch() is only called with real callables (policy: Mock vs Fake).
"""
from __future__ import annotations

import pytest

from reyn.runtime.router_loop import RouterLoop

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
    # #4691 Phase B ③: each of the 3 tool-calls rounds also emits its own
    # (content-less, since always_tool has no text) tree-parent placeholder
    # row now — filter to the one kind this test is actually about.
    (msg,) = [m for m in host.outbox if m["kind"] == "error"]
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
    # #2425 案B: a dispatch error renders the plain ``Error (<kind>): <message>`` string, not JSON.
    assert tool_msg["content"].startswith("Error (unknown_tool): ")
    assert "bogus" in tool_msg["content"]
    # #4691 Phase B ③: round 1's own (content-less) tree-parent placeholder
    # row lands first now — the terminal reply is still the LAST row.
    assert host.outbox[-1]["text"] == "Recovered."


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

    # the write landed at the layer's own resolved path
    expected_path = host.memory.memory_path("shared", "user_role")
    written_paths = [path for path, _ in host.file_writes]
    assert expected_path in written_paths

    # Check frontmatter in written content
    written_content = dict(host.file_writes)[expected_path]
    assert "name: User Role" in written_content
    assert "type: user" in written_content
    assert "The user is a senior developer." in written_content

    # the listing index was regenerated for that same layer
    (regen,) = host.index_regenerations
    assert regen["output_path"] == host.memory.memory_path("shared", "MEMORY")

    # #4691 Phase B ③: the tool-calls round's own tree-parent placeholder
    # row lands first now — the terminal reply is still the LAST row.
    assert host.outbox[-1]["text"] == "Saved."


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
async def test_async_tool_dispatch_exits_the_loop(monkeypatch):
    """Tier 2: OS invariant — RouterLoop exits after first async-dispatch-kind
    tool call and does not iterate further in the same turn.

    delegate_to_agent (the original async tool this test drove) retired in
    proposal 0067 P6 (#3978) — spawn_session is the surviving async tool
    (dispatch_kind="async") and exercises the SAME generic RouterLoop
    mechanism this test targets: after a successful async dispatch,
    RouterLoop returns without consuming further rounds; the actual
    completion arrives via a separate later mechanism.
    """
    host = FakeRouterHost()
    loop = make_loop(host)

    rounds = [
        tool_result([{
            "name": "spawn_session",
            "args": {"request": "please process the data", "mode": "ephemeral"},
        }]),
        # Subsequent rounds intentionally not consumed — loop must exit
        # after the async dispatch.
        text_result("Should not reach this round."),
    ]
    scripted = _ScriptedLLM(rounds)

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", scripted)
    await loop.run("spawn a session", [])

    (spawn_call,) = host.spawn_calls
    assert spawn_call["request"] == "please process the data"
    assert spawn_call["mode"] == "ephemeral"
    assert spawn_call["chain_id"] == "chain-test"
    # Only the first LLM call ran; the second round was never consumed.
    assert scripted.call_count == 1
    # B55 R-7: outbox shows a `[task_spawned] kind=prompt ...`
    # structured spawn_ack (= parity with skill / plan spawn_ack),
    # not a generic "awaiting peer reply" status row. (proposal 0067 P4,
    # #3978, architect ruling 2026-08-10: kind=agent -> kind=prompt —
    # `m["kind"]` below is the UNRELATED outbox-display-frame axis, byte-
    # identical, not touched by this migration.)
    assert any(
        m["kind"] == "agent"
        and m.get("meta", {}).get("source") == "agent_spawn_ack"
        and "[task_spawned] kind=prompt" in m["text"]
        for m in host.outbox
    ), f"Expected agent_spawn_ack; got: {host.outbox}"


@pytest.mark.asyncio
async def test_async_tool_does_not_redispatch_in_same_turn(monkeypatch):
    """Tier 2: OS invariant — RouterLoop.run() exits after first async
    dispatch even if the LLM keeps emitting the same async tool call;
    exactly one dispatch occurs regardless of max_iterations.
    delegate_to_agent (the original async tool this test drove) retired in
    proposal 0067 P6 (#3978) — spawn_session exercises the same generic
    exit-after-async-dispatch mechanism."""
    host = FakeRouterHost()
    loop = make_loop(host, max_iterations=5)

    # If the loop kept iterating it would call spawn_session 5 times.
    spawn_round = tool_result([{
        "name": "spawn_session",
        "args": {"request": "do work", "mode": "ephemeral"},
    }])
    scripted = _ScriptedLLM([spawn_round] * 5)

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", scripted)
    await loop.run("spawn", [])

    # Exactly one spawn dispatch; loop exited after the first iteration.
    (only_spawn,) = host.spawn_calls
    assert scripted.call_count == 1


@pytest.mark.asyncio
async def test_dedupe_duplicate_async_tool_calls_in_same_round(monkeypatch):
    """Tier 2: OS invariant — duplicate async tool_calls (same name, same
    args) in a single LLM round are deduped before dispatch (F5 fix).

    delegate_to_agent (the original async tool this test drove) retired in
    proposal 0067 P6 (#3978) — spawn_session is the surviving async tool
    and exercises the SAME generic dedupe mechanism. Weak models (e.g.
    gemini-2.5-flash-lite) sometimes emit the same async tool call twice
    with identical arguments in one tool_calls list. Without dedupe, the
    dispatch would run twice, doubling cost. After dedupe, exactly one
    dispatch runs and a `tool_call_deduped` audit event is emitted for the
    suppressed call.
    """
    host = FakeRouterHost()
    loop = make_loop(host)

    # Two identical spawn_session calls in the same round.
    duplicate_round = tool_result([
        {"id": "tc_a", "name": "spawn_session",
         "args": {"request": "do work", "mode": "ephemeral"}},
        {"id": "tc_b", "name": "spawn_session",
         "args": {"request": "do work", "mode": "ephemeral"}},
    ])
    scripted = _ScriptedLLM([duplicate_round])

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", scripted)
    await loop.run("send", [])

    # Only one spawn_session dispatch — duplicate suppressed.
    (spawn_call,) = host.spawn_calls
    assert spawn_call["request"] == "do work"
    assert spawn_call["mode"] == "ephemeral"
    # Audit event records the suppressed call.
    deduped_events = [
        e for e in host.events.emitted  # type: ignore[attr-defined]
        if e["type"] == "tool_call_deduped"
    ]
    (deduped_evt,) = deduped_events
    assert deduped_evt["name"] == "spawn_session"
    assert deduped_evt["reason"] == "duplicate_async_in_round"


@pytest.mark.asyncio
async def test_dedupe_does_not_collapse_distinct_async_args(monkeypatch):
    """Tier 2: OS invariant — async tool_calls with different args are
    NOT deduped (F5 false-positive guard). delegate_to_agent (the original
    async tool this test drove) retired in proposal 0067 P6 (#3978) —
    spawn_session exercises the same generic mechanism: two calls with
    different `request` payloads must both dispatch — they're legitimately
    distinct work items.
    """
    host = FakeRouterHost()
    loop = make_loop(host)

    distinct_round = tool_result([
        {"id": "tc_a", "name": "spawn_session",
         "args": {"request": "task A", "mode": "ephemeral"}},
        {"id": "tc_b", "name": "spawn_session",
         "args": {"request": "task B", "mode": "ephemeral"}},
    ])
    scripted = _ScriptedLLM([distinct_round])

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", scripted)
    await loop.run("send two tasks", [])

    # Both dispatch — different args.
    requests = sorted(s["request"] for s in host.spawn_calls)
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
    existing_path = host.memory.memory_path("shared", "user_role")
    host._files[existing_path] = "# old memory"
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

    assert existing_path in host.file_deletes
    (only_regen,) = host.index_regenerations
    # #4691 Phase B ③: the tool-calls round's own tree-parent placeholder
    # row lands first now — the terminal reply is still the LAST row.
    assert host.outbox[-1]["text"] == "Forgotten."


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
    """Tier 2: OS invariant — a tool_call naming something absent from the
    current catalog returns status=error / kind=unknown_tool, and no host
    method is called.

    #3429 changed the WITNESS this uses, and the reason is worth recording.
    It used to be ``read_file`` under a host with no ``file_permissions``, on
    the reading that a no-file host does not carry the file tools. Measured on
    the base commit, that reading was wrong: the dispatchable catalog for that
    host contained ``file__read`` / ``file__write`` / ``file__delete`` — the
    file capability was fully reachable, and the only thing missing was the
    BARE spelling. The test was pinning "one of the two spellings is absent",
    not "the capability is gated". With one spelling that reading is no longer
    available, so the witness is a name that genuinely does not exist.

    (Whether the ADVERTISEMENT should also gate on file scope — the catalog
    enumerates the file category unconditionally while ``build_tools`` gates it
    — is #3449, and is not what this test is about. Execution is gated by the
    permission resolver on every path either way.)
    """
    host = FakeRouterHost(
        skills=[{"name": "list_skills", "category": "general"}],
        file_permissions=None,
        mcp_servers=[],
    )
    loop = make_loop(host)

    rounds = [
        tool_result([{"name": "no_such_tool_xyz", "args": {"path": "/some/file.txt"}}]),
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
    # #2425 案B: a dispatch error renders the plain ``Error (<kind>): <message>`` string, not JSON.
    assert tool_msg["content"].startswith("Error (unknown_tool): ")
    assert "no_such_tool_xyz" in tool_msg["content"]

    # Loop recovered and produced a reply — #4691 Phase B ③: the tool-calls
    # round's own tree-parent placeholder row lands first now — the
    # terminal reply is still the LAST row.
    assert host.outbox[-1]["text"] == "Sorry, let me try differently."


@pytest.mark.asyncio
async def test_tool_names_populated_per_run(monkeypatch):
    """Tier 1: the dispatchable catalog reflects host configuration.

    ``build_tools`` gates the file-class tools on the operator's
    `permissions.file.*` declaration, while the universal catalog enumerates
    the ``file`` category unconditionally; the default ``enumerate-all`` scheme
    composes both, so the file actions ARE dispatchable under a no-file host
    and are gated at EXECUTION by the permission resolver.

    ★ #3429 did not change that; it made it legible. Measured on the base
    commit, a no-file host's dispatchable catalog already carried
    ``file__read`` / ``file__write`` / ``file__delete`` — the same seven file
    actions it carries now, under the other spelling. The assertions here used
    to read ``"read_file" not in names`` as "the file tools are gated", which
    was true of the BARE SPELLING and false of the capability. (Whether the
    advertisement should gate too is #3449.)

    What IS host-dependent and still asserted: the MCP surface, which has no
    catalog entries when no server is configured.
    """
    host_no_file = FakeRouterHost(file_permissions=None, mcp_servers=[])
    loop = RouterLoop(host=host_no_file, chain_id="chain-test")

    scripted1 = _ScriptedLLM([text_result("ok")])
    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", scripted1)
    await loop.run("hello", [])

    names_no_file = frozenset(loop._tool_names)
    # The file actions are reachable through the catalog regardless of
    # file_permissions — execution, not advertisement, is where the gate is.
    assert {"read_file", "list_directory", "write_file", "delete_file"} <= names_no_file
    # Reyn-source tools always present.
    assert "reyn_repo_list" in names_no_file
    assert "reyn_repo_read" in names_no_file
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
    assert {"read_file", "list_directory", "write_file"} <= names_with_file

    # Third: with write scope — same set, because the catalog is what carries
    # these actions and it does not read file scope (#3449).
    host_with_write = FakeRouterHost(
        file_permissions={"read": ["/docs"], "write": ["/tmp"]},
        mcp_servers=[],
    )
    loop3 = RouterLoop(host=host_with_write, chain_id="chain-test-3")

    scripted3 = _ScriptedLLM([text_result("ok")])
    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", scripted3)
    await loop3.run("hello", [])

    names_with_write = frozenset(loop3._tool_names)
    assert {"write_file", "delete_file"} <= names_with_write
    # The host-dependent half that IS still visible here: no MCP server
    # configured on any of the three hosts, so the mcp actions the CATALOG
    # carries are present while ``build_tools``' per-server rows are not.
    assert names_no_file == names_with_file == names_with_write, (
        "the dispatchable catalog changed with file scope — if that is now "
        "intended, #3449 landed and this assertion is what should record it"
    )


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
async def test_dispatched_tool_call_carries_the_llm_calls_own_call_id(monkeypatch):
    """Tier 2: #4691 Phase B ①(remainder) — a tool_calls round's own
    LLMToolCallResult.call_id (#4725) reaches tool_called/tool_returned's
    audit-event payload — threaded down the actual call graph as an explicit
    parameter (dispatch()/_run_execute_round -> ExecContext.extra ->
    DispatchContext.call_id), not a stored field. This is the key a TUI
    consumer keys a tool row to its parent litellm CALL by — never dispatch
    order (owner ruling B, #4691)."""
    host = FakeRouterHost()
    loop = make_loop(host)

    # list_agents is always in the catalog (build_tools includes it
    # unconditionally, independent of registered agents) — a real,
    # dispatchable tool, so tool_called fires instead of the pre-dispatch
    # unknown_tool short-circuit the other tests in this file exercise.
    rounds = [
        tool_result([{"name": "list_agents", "args": {"path": ""}}], call_id="resp-round-1"),
        text_result("done"),
    ]

    async def mock_llm(**kwargs):
        return rounds.pop(0)

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", mock_llm)
    await loop.run("call a tool", [])

    called = [e for e in host.events.emitted if e["type"] == "tool_called"]
    assert called, "tool_called never fired — nothing to assert call_id on"
    for e in called:
        assert e["call_id"] == "resp-round-1"


@pytest.mark.asyncio
async def test_a_call_id_less_round_never_inherits_a_prior_rounds_call_id(monkeypatch):
    """Tier 2: #4691 Phase B ①(remainder) — lead-coder review (#4734): the
    ORIGINAL implementation stored call_id on ``self._current_call_id``,
    which never reset — after one round ran, EVERY later dispatch (even one
    whose own round genuinely carries no call_id) silently inherited the
    PRIOR round's value. That is worse than a missing key: a stale id LOOKS
    valid while pointing at the wrong call. Two tool_calls rounds back to
    back — round 1 WITH a call_id, round 2 WITHOUT — pins that round 2's own
    dispatch reports None, not round 1's leftover value."""
    host = FakeRouterHost()
    loop = make_loop(host)

    rounds = [
        tool_result([{"name": "list_agents", "args": {"path": ""}}], call_id="resp-round-1"),
        tool_result([{"name": "list_agents", "args": {"path": ""}}], call_id=None),
        text_result("done"),
    ]

    async def mock_llm(**kwargs):
        return rounds.pop(0)

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", mock_llm)
    await loop.run("call a tool twice", [])

    called = [e for e in host.events.emitted if e["type"] == "tool_called"]
    # Unpacking enforces "exactly one tool_called per round" — a structural
    # correlation check, not a pinned figure.
    first_call, second_call = called
    assert first_call["call_id"] == "resp-round-1"
    assert second_call["call_id"] is None, (
        "round 2 genuinely has no call_id — it must NOT inherit round 1's "
        f"'resp-round-1' (got {second_call['call_id']!r})"
    )


@pytest.mark.asyncio
async def test_session_spawn_dispatches_to_host_not_unhandled():
    """Tier 2: #2120 — _invoke_router_tool('spawn_session') reaches the registry handler
    and the host's spawn_session, NOT the {"error": "unhandled tool"} fall-through.

    The tui live-probe found spawn_session advertised but undispatched: the LLM called it
    and got {"error": "unhandled tool: spawn_session"}, no spawn. This drives the real
    dispatch path (REGISTRY_DISPATCH_TOOLS → _invoke_via_registry → SESSION_SPAWN._handle
    → RouterCallerState.spawn_session_fn → host.spawn_session). Drop spawn_session from
    REGISTRY_DISPATCH_TOOLS → the bare name falls through → result is the unhandled-tool
    error and host.spawn_calls stays empty → RED."""
    host = FakeRouterHost()
    loop = RouterLoop(host=host, chain_id="chain-test")

    from reyn.runtime.router_tools import build_tools
    tools = build_tools()
    loop._catalog = {t["function"]["name"]: t for t in tools}
    loop._tool_names = frozenset(loop._catalog.keys())

    result = await loop._invoke_router_tool(
        "spawn_session", {"request": "do a task", "mode": "persistent"}
    )

    assert not (isinstance(result, dict) and "unhandled tool" in str(result.get("error", ""))), (
        f"spawn_session hit the unhandled-tool fall-through (#2120 dispatch gap): {result}"
    )
    assert host.spawn_calls, "spawn_session did not reach host.spawn_session"
    spawned = host.spawn_calls[-1]
    assert spawned["request"] == "do a task"
    assert spawned["mode"] == "persistent"


@pytest.mark.asyncio
async def test_send_to_session_dispatches_to_host_not_unhandled():
    """Tier 2: proposal 0067 P5 (#3978) — _invoke_router_tool('send_to_session')
    reaches the registry handler and the host's send_to_session, NOT the
    {"error": "unhandled tool"} fall-through.

    Same shape as test_session_spawn_dispatches_to_host_not_unhandled above,
    added per #4101 review (lead-coder): that PR's own e2e tests
    (tests/runtime/test_send_to_session_3978_p5.py) hand-build their own
    ToolContext/RouterCallerState with a self-bound send_to_session_fn,
    bypassing RouterLoop._build_router_caller_state's REAL hasattr-guarded
    binding entirely — so they stayed green even with the binding line
    removed. This drives the real dispatch path (REGISTRY_DISPATCH_TOOLS
    → _invoke_via_registry → SEND_TO_SESSION._handle →
    RouterCallerState.send_to_session_fn → host.send_to_session). Drop the
    `send_to_session_fn=_send_to_session_bound` line from
    RouterLoop._build_router_caller_state → the bare name falls through →
    result is the unhandled-tool error and host.send_to_session_calls stays
    empty → RED (falsify-verified during review)."""
    host = FakeRouterHost()
    loop = RouterLoop(host=host, chain_id="chain-test")

    from reyn.runtime.router_tools import build_tools
    tools = build_tools()
    loop._catalog = {t["function"]["name"]: t for t in tools}
    loop._tool_names = frozenset(loop._catalog.keys())

    result = await loop._invoke_router_tool(
        "send_to_session", {"agent": "beta", "session": "main", "text": "hi", "wake": True},
    )

    assert not (isinstance(result, dict) and "unhandled tool" in str(result.get("error", ""))), (
        f"send_to_session hit the unhandled-tool fall-through (#2120-class dispatch gap): {result}"
    )
    assert host.send_to_session_calls, "send_to_session did not reach host.send_to_session"
    sent = host.send_to_session_calls[-1]
    assert sent["agent"] == "beta"
    assert sent["session"] == "main"
    assert sent["text"] == "hi"
    assert sent["wake"] is True


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
    # #2425 案B: a dispatch error renders the plain ``Error (<kind>): <message>`` string, not JSON.
    assert tool_msgs[0]["content"].startswith("Error (unknown_tool): ")
    # events were emitted
    assert any(e["type"] == "tool_failed" for e in host.events.emitted)


# ---------------------------------------------------------------------------
# #4552: B27-C1's _build_hot_list_aliases / _UNIVERSAL_WRAPPER_NAMES filter
# tests lived here. Both symbols were deleted with the hot-list feature
# (owner directive: discarded, its role already gone) — nothing left to pin.
# ---------------------------------------------------------------------------
