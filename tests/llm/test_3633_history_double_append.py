"""Tier 3a / Tier 2: #3633 — a tool-turn's assistant TEXT must persist to
history exactly once, not twice.

Root cause: ``RouterLoop``'s Execute arm emits the tool-turn's accompanying
text via ``host.put_outbox(kind="agent", text=..., source="router_tool_turn_text")``
so it renders ahead of the tool-call rows (#1642). ``RouterHostAdapter.put_outbox``
has a side effect: any ``kind=="agent"`` non-empty text is unconditionally
appended to persistent history. A few lines later, ``RouterLoop.feedback()``
(via the active scheme's ``format_feedback``) independently persists the SAME
text as the canonical ``source="router_tool_turn"`` record (complete with
``tool_calls``) through ``append_history_entry`` — a call path that does
NOT go through the outbox and so was never gated by the first append. Net
effect (measured in a real session, issue #3633): the identical assistant
text landed in ``history.jsonl`` twice, 24/283 records in the sampled
session.

The fix threads an explicit ``persist: bool = True`` kwarg through
``put_outbox`` (Protocol + ``RouterHostAdapter`` impl) and sets
``persist=False`` at the ``router_tool_turn_text`` call site — the coupling
between ``kind=="agent"`` and the history-append side effect is now an
explicit per-call-site choice rather than an implicit blanket rule.

Tier 3a test drives the real ``RouterLoop`` via the ``call_llm_tools``
scripted-injection seam + ``FakeRouterHost`` (a real Fake — no mocks — now
mirroring BOTH of RouterHostAdapter's persist paths: the ``put_outbox``
side effect, gated by ``persist``, and ``append_history_entry``). Tier 2
test drives the real ``RouterHostAdapter`` directly (no Fake) to pin the
``persist=False`` contract at its source.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from tests._support.router_loop import (
    FakeRouterHost,
    make_loop,
    text_result,
)
from tests._support.router_loop import (
    ScriptedLLM as _ScriptedLLM,
)

_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=5)


def _text_and_tool(content: str) -> LLMToolCallResult:
    return LLMToolCallResult(
        content=content,
        tool_calls=[
            {
                "id": "t1",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": "notes.txt"}),
                },
            }
        ],
        finish_reason="tool_calls",
        usage=_USAGE,
    )


@pytest.mark.asyncio
async def test_tool_turn_text_persists_to_history_exactly_once(monkeypatch):
    """Tier 3a: a turn with content + tool_calls persists ``content`` to
    history ONE time (the canonical ``router_tool_turn`` record with
    ``tool_calls``), not twice (#3633 — pre-fix it was also persisted via
    the display-only ``router_tool_turn_text`` outbox emit)."""
    host = FakeRouterHost()
    host._files["notes.txt"] = "file body"
    loop = make_loop(host)
    script = [
        _text_and_tool("Let me run your skill first."),  # Execute turn
        text_result("All done."),                        # terminal turn
    ]
    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", _ScriptedLLM(script))
    await loop.run("run my skill", [])

    adjacent_dupes = _count_adjacent_exact_duplicates(host.history)
    assert adjacent_dupes == 0, (
        f"expected 0 adjacent exact-duplicate history entries, got "
        f"{adjacent_dupes}: {host.history}"
    )

    matches = [
        e for e in host.history
        if e["role"] == "assistant" and e["content"] == "Let me run your skill first."
    ]
    # Tuple-unpack: raises ValueError if there isn't EXACTLY one match — a
    # behavioural assertion (the surviving record's shape), not a length pin.
    (surviving,) = matches
    # The surviving record is the canonical one — it carries tool_calls.
    assert surviving["tool_calls"], "surviving record must be the complete turn"

    # The display bubble still renders (outbox unaffected by persist=False).
    agent_outbox_texts = [m["text"] for m in host.outbox if m["kind"] == "agent"]
    assert "Let me run your skill first." in agent_outbox_texts


@pytest.mark.asyncio
async def test_no_tool_calls_terminal_reply_still_persists(monkeypatch):
    """Tier 3a: regression guard for the OTHER path #3633's wrong comment was
    actually reasoning about — a plain no-tool_calls text reply (the common
    case) must still persist to history exactly once. ``persist=False`` is
    scoped to the tool-turn-with-text call site only."""
    host = FakeRouterHost()
    loop = make_loop(host)
    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools", _ScriptedLLM([text_result("just text")])
    )
    await loop.run("hi", [])

    matches = [e for e in host.history if e["content"] == "just text"]
    (surviving,) = matches  # exactly one — ValueError otherwise
    assert surviving["role"] == "assistant"


def _count_adjacent_exact_duplicates(history: list[dict]) -> int:
    """Count adjacent pairs with identical (role, content) — the shape
    measured in #3633 (24/283 in the owner's real history.jsonl)."""
    count = 0
    for prev, cur in zip(history, history[1:]):
        if prev["role"] == cur["role"] and prev["content"] == cur["content"]:
            count += 1
    return count


# ── RouterHostAdapter.put_outbox persist=False contract ─────────────────


def test_adapter_put_outbox_persist_false_skips_history_append(tmp_path):
    """Tier 2: RouterHostAdapter.put_outbox with ``persist=False`` emits the
    outbox message but does NOT append to history — the fix's load-bearing
    contract, pinned directly against the real adapter (no Fake)."""
    from reyn.core.events.events import EventLog
    from reyn.llm.model_resolver import ModelResolver
    from reyn.runtime.services import (
        LiveSessionIdInputs,
        McpGatewayInputs,
        MemoryService,
        PutOutboxInputs,
        RouterHostAdapter,
    )
    from tests._support.router_host_adapter import (
        make_op_context_source,
        null_file_delete,
        null_file_read,
        null_file_regen,
        null_file_write,
        null_mcp_call_tool,
    )

    outbox: list[dict] = []
    history: list = []

    async def _put_outbox(msg) -> None:
        outbox.append({"kind": msg.kind, "text": msg.text})

    def _append_history(msg) -> None:
        history.append(msg)

    events = EventLog(subscribers=[])
    workspace = tmp_path / "agents" / "alpha"
    adapter = RouterHostAdapter(
        agent_name="alpha",
        agent_role="role",
        output_language=None,
        op_context_source=make_op_context_source(events=events),
        permission_resolver=None,
        mcp_servers=None,
        project_context="",
        events=events,
        resolver=ModelResolver({}),
        memory=MemoryService(
            agent_workspace_dir=workspace,
            events=events,
            file_write=null_file_write,
            file_read=null_file_read,
            file_delete=null_file_delete,
            file_regenerate_index=null_file_regen,
        ),
        journal=None,
        agent_registry=None,
        agent_workspace_dir=workspace,
        mcp_call_tool=null_mcp_call_tool,
        mcp_gateway_inputs=McpGatewayInputs(
            mcp_connection_service=None, mcp_agent_id=None, ephemeral_fn=None,
        ),
        put_outbox_inputs=PutOutboxInputs(
            put_outbox=_put_outbox, agent_replies_tracker=lambda: None,
        ),
        append_history=_append_history,
        live_session_id_inputs=LiveSessionIdInputs(
            session_id=None, live_session_id_fn=None,
        ),
    )

    asyncio.run(adapter.put_outbox(
        kind="agent", text="display only", meta={"chain_id": "c1"}, persist=False,
    ))
    assert [m["text"] for m in outbox] == ["display only"]
    assert history == [], "persist=False must not append to history"

    asyncio.run(adapter.put_outbox(
        kind="agent", text="final reply", meta={"chain_id": "c1"},
    ))
    assert [m["text"] for m in outbox] == ["display only", "final reply"]
    # Tuple-unpack: default persist=True must append EXACTLY the new entry —
    # not zero (regression) and not a second copy of "display only".
    (only_entry,) = history
    assert only_entry.content == "final reply"
