"""Tier 3: external tool-result → context-auto capability narrowing (#1909).

The #1827 S4 context-auto defense (capability narrowing while untrusted external
content is live in context) worked for the external-peer-answer seam
(``intervention_handler.py``) but NOT for external tool-results: ``router_loop.py``
already tagged a ``returns_external_content`` result with ``_external_source``
(FP-0050/#1822 S2, used by the fence) and already extracted it in ``feedback()``,
but the extracted flag was discarded instead of reaching the persisted history
entry's ``meta`` — so ``metas_have_untrusted``/``_effective_contextual_for_turn``
(``session.py``) never saw it and narrowing never engaged for this seam.

#1909 fixes the ONE gap: ``feedback()``'s tool-result ``append_history_entry`` call
now includes ``meta["external_source"] = True`` when the dispatched tool declared
``returns_external_content`` — the SAME marker convention the S4 answer seam uses
(``UNTRUSTED_META_KEY`` in ``capability_profile.py``). No new narrowing mechanism is
added: ``_effective_contextual_for_turn`` already live-scans ``self.history`` meta
on every call (not cached), so once the marker lands, the very next time a
``RouterLoop`` is constructed (``RouterLoopDriver.execute`` calls
``contextual_for_turn_fn()`` fresh before each turn) the narrowed profile is live.

**Grounding note on timing** (verified empirically, not assumed): ``RouterLoop``'s
``_contextual_permission`` is resolved ONCE per ``RouterLoop.__init__`` (i.e. once
per ``Session._handle_user_message`` turn) and stays fixed for every LLM/tool
round WITHIN that same turn's ``run()`` — so a tool result that taints history
mid-turn does not retroactively narrow a LATER dispatch in the SAME multi-round
turn. The narrowing takes effect starting the turn's window: NEXT turn boundary
(the next ``_handle_user_message`` call, which is the same "no separate
re-narrowing" story the #1827 S4 answer seam already relies on — an answer is
appended to history by the intervention handler and only the NEXT turn's fresh
``contextual_for_turn_fn()`` picks it up too). These tests pin THAT boundary, not
a within-a-single-multi-round-loop re-narrow (which the codebase does not do for
either seam).

Real ``Session`` + real history + real tool registry throughout — no
``_FakeMessage`` / hand-rolled host stand-in (#2957 PR-A precedent: a fake here
would hide exactly the "tag set but not propagated" bug this PR fixes).
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.session import Session
from tests._support.agent_session import make_session
from tests._support.untrusted_narrowing import narrowing_on

_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=5)

_REMEMBER_ARGS = {
    "slug": "y", "name": "n", "description": "d", "type": "user", "body": "x",
}


def _tool_call_result(calls: list[dict]) -> LLMToolCallResult:
    tool_calls = [
        {
            "id": c.get("id", f"tc_{i}"),
            "type": "function",
            "function": {"name": c["name"], "arguments": json.dumps(c.get("args", {}))},
        }
        for i, c in enumerate(calls)
    ]
    return LLMToolCallResult(
        content=None, tool_calls=tool_calls, finish_reason="tool_calls", usage=_USAGE,
    )


def _text_result(text: str) -> LLMToolCallResult:
    return LLMToolCallResult(content=text, tool_calls=[], finish_reason="stop", usage=_USAGE)


def _scripted_llm(rounds: list[LLMToolCallResult]):
    state = {"n": 0}

    async def _call(**kwargs: Any) -> LLMToolCallResult:
        r = rounds[state["n"]]
        state["n"] += 1
        return r
    return _call


def _make(tmp_path) -> Session:
    # #3501: the narrowing is opt-in. The taint-STAMPING arms below are
    # independent of it (the marker is stamped whether or not anything consumes
    # it); the narrowing arms are not, so the whole file opts in.
    return make_session(agent_name="test_agent", safety=narrowing_on())


@pytest.mark.asyncio
async def test_external_tool_result_meta_carries_taint_marker(tmp_path, monkeypatch):
    """Tier 3: the persisted tool-result history entry for an
    ``returns_external_content`` tool (``list_memory``) carries
    ``meta["external_source"] = True`` — the propagation this PR adds at
    ``router_loop.py`` ``feedback()``. A trusted-internal tool call in the
    same turn does NOT carry the marker (source-scoped, not blanket)."""
    monkeypatch.chdir(tmp_path)
    session = _make(tmp_path)
    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools",
        _scripted_llm([
            _tool_call_result([
                {"name": "list_memory", "args": {"path": ""}, "id": "tc_ext"},
                {"name": "list_agents", "args": {}, "id": "tc_trusted"},
            ]),
            _text_result("ok"),
        ]),
    )
    await session._handle_inbox_text("look things up", chain_id="c1")

    tool_msgs = {m.tool_call_id: m for m in session.history if m.role == "tool"}
    assert tool_msgs["tc_ext"].meta.get("external_source") is True
    assert not tool_msgs["tc_trusted"].meta.get("external_source")


@pytest.mark.asyncio
async def test_baseline_without_taint_remember_shared_succeeds(tmp_path, monkeypatch):
    """Tier 3: control — with NO external content ever in context,
    ``remember_shared`` (a tool the ``_untrusted`` floor denies) succeeds
    normally. This is the control the negative witness below is measured
    against — proves the later denial is CAUSED by narrowing, not by some
    unrelated dispatch failure."""
    monkeypatch.chdir(tmp_path)
    session = _make(tmp_path)
    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools",
        _scripted_llm([
            _tool_call_result([{"name": "remember_shared", "args": _REMEMBER_ARGS, "id": "tc_r"}]),
            _text_result("done"),
        ]),
    )
    await session._handle_inbox_text("remember this", chain_id="c1")

    (tool_msg,) = [m for m in session.history if m.role == "tool"]
    assert "tool_excluded" not in str(tool_msg.content)


@pytest.mark.asyncio
async def test_next_turn_dispatch_denied_negative_witness(tmp_path, monkeypatch):
    """Tier 3: NEGATIVE + TIMING witness. Turn 1 dispatches an external tool
    (``list_memory``). Turn 2 — the VERY NEXT ``_handle_user_message`` call on
    the SAME session, no compaction in between — attempts ``remember_shared``
    (denied by the built-in ``_untrusted`` floor). Without the meta-propagation
    fix this call SUCCEEDS (see the control test above); with it, it is
    rejected with ``tool_excluded`` — the operation that would otherwise have
    gone through is actually blocked, not just "profile has an attribute".

    **Scope note (what this does NOT verify):** this pins the TURN boundary
    (``RouterLoopDriver.run_turn`` constructs a fresh ``RouterLoop`` per
    ``_handle_user_message`` call and resolves ``contextual_for_turn_fn()``
    once at that construction — see its ``#1827 S4b: per-turn effective
    contextual`` comment). It does **not** exercise — and does not claim
    coverage of — narrowing WITHIN a single turn's multi-round tool loop
    (list_memory and a would-be-denied call in the SAME ``_handle_user_message``
    call): verified empirically that a same-turn later round is NOT narrowed,
    because ``RouterLoop._contextual_permission`` is fixed once per
    construction and is not re-derived per iteration. That intra-turn gap is
    tracked as a separate follow-up issue, not closed by this PR."""
    monkeypatch.chdir(tmp_path)
    session = _make(tmp_path)
    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools",
        _scripted_llm([
            _tool_call_result([{"name": "list_memory", "args": {"path": ""}, "id": "tc_ext"}]),
            _text_result("ok fetched"),
            _tool_call_result([{"name": "remember_shared", "args": _REMEMBER_ARGS, "id": "tc_denied"}]),
            _text_result("done"),
        ]),
    )
    await session._handle_inbox_text("look something up", chain_id="c1")
    await session._handle_inbox_text("now remember it", chain_id="c2")

    (denied_msg,) = [m for m in session.history if m.tool_call_id == "tc_denied"]
    assert "tool_excluded" in str(denied_msg.content)
    assert "remember_shared" in str(denied_msg.content)


@pytest.mark.asyncio
async def test_self_clears_after_tainted_entry_compacted_out(tmp_path, monkeypatch):
    """Tier 3: SELF-CLEAR witness. After the tainted tool-result entry is
    removed from active history (simulating compaction — same technique
    ``test_context_auto_1827_s4b.py`` uses for the S4 seam), a THIRD turn's
    ``remember_shared`` call succeeds again — narrowing is until-compaction
    scope, not a permanent lock, for the tool-result seam same as the answer
    seam.

    **Scope note**: like the negative/timing witness above, the taint→deny and
    deny→clear transitions this pins are both observed at TURN boundaries
    (separate ``_handle_user_message`` calls), not within one turn's
    multi-round tool loop. Does not verify intra-turn self-clear (out of
    scope — see the negative-witness test's scope note)."""
    monkeypatch.chdir(tmp_path)
    session = _make(tmp_path)
    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools",
        _scripted_llm([
            _tool_call_result([{"name": "list_memory", "args": {"path": ""}, "id": "tc_ext"}]),
            _text_result("ok fetched"),
            _tool_call_result([{"name": "remember_shared", "args": _REMEMBER_ARGS, "id": "tc_denied"}]),
            _text_result("done"),
            _tool_call_result([{"name": "remember_shared", "args": _REMEMBER_ARGS, "id": "tc_allowed"}]),
            _text_result("done again"),
        ]),
    )
    await session._handle_inbox_text("look something up", chain_id="c1")
    await session._handle_inbox_text("now remember it", chain_id="c2")
    (denied_msg,) = [m for m in session.history if m.tool_call_id == "tc_denied"]
    assert "tool_excluded" in str(denied_msg.content)

    # #5276: simulate compaction retiring the tainted tool-result entry via a
    # real watermark advance (a role="summary" entry through
    # session._append_history, the actual production mechanism — same shape
    # test_3380_tool_tab_ephemeral_narrowing.py's own
    # test_narrowing_self_clears_when_a_real_compaction_covers_the_taint
    # uses), not a raw self.history reassignment: Session._untrusted_taint_
    # active is maintained incrementally by _append_history and only
    # re-derived in full on an actual watermark advance, so a raw
    # reassignment bypasses that hook entirely and leaves the taint latched.
    tainted_seq = max(
        m.seq for m in session.history if (m.meta or {}).get("external_source")
    )
    session._append_history(
        ChatMessage(
            role="summary", content="summarised",
            meta={
                "structured": {}, "covers_from_seq": 1,
                "covers_through_seq": tainted_seq,
            },
        )
    )

    await session._handle_inbox_text("try again", chain_id="c3")
    (allowed_msg,) = [m for m in session.history if m.tool_call_id == "tc_allowed"]
    assert "tool_excluded" not in str(allowed_msg.content)
