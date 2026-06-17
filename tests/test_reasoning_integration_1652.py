"""Tier 2: #1652 — host-centralised reasoning gating (the no-double-inject guard
+ the two independent opt-out toggles, exercised through the REAL
RouterHostAdapter.put_outbox chokepoint).

This is the load-bearing correctness gate lead reviews:
- DISPLAY (toggle2): a discrete kind="reasoning" OutboxMessage is emitted BEFORE
  the agent reply when display is on, and NOT when off. The agent OutboxMessage
  never carries reasoning in meta (channels render only the discrete signal) →
  no double-render.
- PERSIST (toggle1): reasoning rides the persisted history ChatMessage's meta
  only when continuity is on (so the replay section can read it); never on the
  wire-shape (built from content+tool_calls) → no native double-inject on gemini.

Real RouterHostAdapter + real collaborators + recording callbacks (no mocks).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.chat.services import MemoryService, RouterHostAdapter
from reyn.chat.session import ChatMessage, Session
from reyn.config import ReasoningConfig
from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog
from reyn.llm.model_resolver import ModelResolver

_REASONING = "REYN_1652_THOUGHTS: 17*23 = 391."


async def _noop(*a, **k):
    return {}


def _mk_host(reasoning_config, *, outbox: list, history: list, section: str = "") -> RouterHostAdapter:
    events = EventLog(subscribers=[])
    workspace = Path(".reyn") / "agents" / "t"
    return RouterHostAdapter(
        agent_name="t", agent_role="r", output_language="en",
        allowed_skills=None, allowed_mcp=None, permission_resolver=None,
        mcp_servers=None, project_context="", events=events,
        resolver=ModelResolver({}),
        memory=MemoryService(
            agent_workspace_dir=workspace, events=events,
            file_write=_noop, file_read=_noop, file_delete=_noop,
            file_regenerate_index=_noop,
        ),
        journal=None, agent_registry=None, skill_enumerate_fn=lambda exclude: [],
        agent_workspace_dir=workspace, plan_registry_getter=lambda: None,
        file_read=_noop, file_write=_noop, file_delete=_noop,
        file_list_directory=_noop, file_regenerate_index=_noop,
        mcp_list_servers=_noop, mcp_list_tools=_noop, mcp_call_tool=_noop,
        run_skill_awaitable=_noop, spawn_skill=_noop, send_to_agent=_noop,
        put_outbox=lambda msg: outbox.append(msg) or _noop(),
        append_history=lambda msg: history.append(msg),
        spawn_plan_task=_noop, delegation_tracker=lambda: [],
        agent_replies_tracker=lambda: [], turn_budget_engine=None,
        environment_backend=None,
        reasoning_config=reasoning_config,
        reasoning_continuity_section_fn=lambda: section,
    )


def _put(host, *, text="answer", reasoning=_REASONING):
    outbox_async = host.put_outbox(
        kind="agent", text=text,
        meta={"chain_id": "c1", "reasoning": reasoning},
    )
    asyncio.run(outbox_async)


def test_display_on_emits_discrete_reasoning_signal_and_strips_agent_meta():
    """Tier 2: #1652 — display ON → a kind="reasoning" OutboxMessage precedes the
    agent reply (carrying the text), and the agent OutboxMessage meta has NO
    reasoning (channels render only the discrete signal → no double-render)."""
    outbox: list = []
    host = _mk_host(ReasoningConfig(continuity=True, display=True), outbox=outbox, history=[])
    _put(host)
    kinds = [m.kind for m in outbox]
    assert kinds == ["reasoning", "agent"], kinds  # reasoning BEFORE reply
    assert outbox[0].text == _REASONING
    assert "reasoning" not in outbox[1].meta  # stripped from the agent message


def test_display_off_suppresses_signal_but_persists_when_continuity_on():
    """Tier 2: #1652 — display OFF → NO kind="reasoning" emit, but continuity ON
    still persists reasoning to history (independent toggles)."""
    outbox: list = []
    history: list = []
    host = _mk_host(ReasoningConfig(continuity=True, display=False), outbox=outbox, history=history)
    _put(host)
    assert [m.kind for m in outbox] == ["agent"]  # no discrete reasoning signal
    assert "reasoning" not in outbox[0].meta
    # continuity on → persisted history ChatMessage keeps reasoning
    assert history and history[-1].meta.get("reasoning") == _REASONING


def test_continuity_off_does_not_persist_reasoning_but_display_still_emits():
    """Tier 2: #1652 — continuity OFF → persisted history ChatMessage has NO
    reasoning (no replay source), while display ON still emits the signal."""
    outbox: list = []
    history: list = []
    host = _mk_host(ReasoningConfig(continuity=False, display=True), outbox=outbox, history=history)
    _put(host)
    assert "reasoning" in [m.kind for m in outbox]  # display still emits
    assert history and "reasoning" not in history[-1].meta  # not persisted


def test_no_reasoning_is_inert():
    """Tier 2: #1652 — a turn with no reasoning emits only the agent reply and
    persists no reasoning, regardless of toggles."""
    outbox: list = []
    history: list = []
    host = _mk_host(ReasoningConfig(continuity=True, display=True), outbox=outbox, history=history)
    _put(host, reasoning=None)
    assert [m.kind for m in outbox] == ["agent"]
    assert "reasoning" not in outbox[0].meta
    assert history and "reasoning" not in history[-1].meta


def test_no_double_inject_reasoning_only_in_meta_never_in_content():
    """Tier 2: #1652 no-double-inject GUARD — persisted reasoning lives ONLY in
    meta["reasoning"], NEVER in the message content (the wire-shape is built from
    content+tool_calls, so reasoning can't leak to the LLM via the wire; the SP
    text-section is its single replay vehicle on gemini)."""
    outbox: list = []
    history: list = []
    host = _mk_host(ReasoningConfig(continuity=True, display=True), outbox=outbox, history=history)
    _put(host, text="The answer is 391.")
    msg = history[-1]
    assert msg.content == "The answer is 391."  # content is the reply only
    assert _REASONING not in msg.content  # reasoning NOT in content → not on wire
    assert msg.meta.get("reasoning") == _REASONING  # reasoning lives in meta only


def test_continuity_section_surfaces_via_host():
    """Tier 2: #1652 — the host exposes the session-rendered continuity section
    (the SP replay vehicle); display/continuity flags read from config."""
    host = _mk_host(
        ReasoningConfig(continuity=True, display=True),
        outbox=[], history=[], section="PRIOR_REASONING_SECTION",
    )
    assert host.reasoning_continuity_section() == "PRIOR_REASONING_SECTION"
    assert host.reasoning_display_enabled() is True
    assert host.reasoning_continuity_enabled() is True


# ── replay: the session reads persisted reasoning → bounded section ─────────


def _session(reasoning_config, tmp_path):
    return Session(
        agent_name="t",
        state_log=StateLog(tmp_path / "wal.jsonl"),
        snapshot_path=tmp_path / "snap.json",
        reasoning_config=reasoning_config,
    )


def test_replay_section_filters_assistant_reasoning_and_bounds(tmp_path):
    """Tier 2: #1652 replay-into-next-turn — _reasoning_continuity_section reads
    each assistant turn's persisted meta["reasoning"], bounds to recent_turns
    (most recent), and renders the section the next SP injects."""
    s = _session(ReasoningConfig(continuity=True, recent_turns=2), tmp_path)
    s.history.append(ChatMessage(role="user", content="q"))
    s.history.append(ChatMessage(role="assistant", content="a1", meta={"reasoning": "R1"}))
    s.history.append(ChatMessage(role="assistant", content="a2", meta={"reasoning": "R2"}))
    s.history.append(ChatMessage(role="assistant", content="a3", meta={"reasoning": "R3"}))
    sec = s.reasoning_continuity_section()
    # recent_turns=2 → only the last two reasonings; oldest dropped
    assert "R2" in sec and "R3" in sec
    assert "R1" not in sec
    assert sec.index("R2") < sec.index("R3")  # most recent last


def test_replay_section_empty_when_continuity_off(tmp_path):
    """Tier 2: #1652 — continuity OFF → empty section even with persisted
    reasoning in history (opt-out gates the replay)."""
    s = _session(ReasoningConfig(continuity=False), tmp_path)
    s.history.append(ChatMessage(role="assistant", content="a", meta={"reasoning": "R"}))
    assert s.reasoning_continuity_section() == ""


def test_replay_section_unbounded_keeps_all(tmp_path):
    """Tier 2: #1652 — recent_turns<=0 (unbounded) replays all persisted
    reasoning (the 'always-send-all' option)."""
    s = _session(ReasoningConfig(continuity=True, recent_turns=0), tmp_path)
    for i in range(5):
        s.history.append(ChatMessage(role="assistant", content=f"a{i}", meta={"reasoning": f"R{i}"}))
    sec = s.reasoning_continuity_section()
    assert all(f"R{i}" in sec for i in range(5))
