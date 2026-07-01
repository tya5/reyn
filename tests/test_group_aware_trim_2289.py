"""Tier 2: #2289 group-aware compaction trim — the pair-split PREVENTION layer.

trim_head/trim_tail used to trim at MESSAGE granularity, so a token boundary could fall between an
assistant's tool_calls and its role=tool results — splitting the pair, which reaches the wire as a
dangling call / orphan result (a provider 400 the Layer-1 wire-repair then has to fix, lossily).

Group-aware trim treats an assistant-with-tool_calls + its adjacent role=tool run as ONE atomic
trim unit, so a normal boundary can never split a pair: the whole cycle is kept or dropped. A
single cycle alone over budget is kept WHOLE (no split, no result loss). For a non-tool history
every turn is a singleton group → byte-identical to the old behavior.
"""
from __future__ import annotations

from reyn.core.events.events import EventLog
from reyn.services.compaction.engine import (
    _group_tool_cycles,
    _is_assistant_with_tool_calls,
    trim_head,
    trim_tail,
)

_M = ""  # model (unused with use_chars4)


def _user(chars: int, seq: int) -> dict:
    return {"role": "user", "text": "a" * chars, "seq": seq}


def _asst_tc(tc_id: str, chars: int, seq: int) -> dict:
    return {
        "role": "assistant", "text": "a" * chars, "seq": seq,
        "tool_calls": [{"id": tc_id, "type": "function", "function": {"name": "f", "arguments": "{}"}}],
    }


def _tool(tc_id: str, chars: int, seq: int) -> dict:
    return {"role": "tool", "tool_call_id": tc_id, "text": "a" * chars, "seq": seq}


def _no_dangling_cycle(turns: list) -> bool:
    """No assistant-with-tool_calls is left WITHOUT its immediately-following role=tool result."""
    for i, t in enumerate(turns):
        if _is_assistant_with_tool_calls(t):
            if i + 1 >= len(turns) or turns[i + 1].get("role") != "tool":
                return False
    return True


# ── grouping ──────────────────────────────────────────────────────────────────────────────────


def test_group_tool_cycles_groups_assistant_with_its_results():
    """Tier 2: an assistant+tool_calls + its adjacent role=tool run is ONE group; others singletons."""
    turns = [_user(40, 1), _asst_tc("t1", 40, 2), _tool("t1", 40, 3), _tool("t1b", 40, 4), _user(40, 5)]
    groups = _group_tool_cycles(turns)
    assert [[t["seq"] for t in g] for g in groups] == [[1], [2, 3, 4], [5]]


def test_group_non_tool_history_all_singletons():
    """Tier 2: with no tool_calls, every turn is its own singleton group (→ old behavior)."""
    turns = [_user(40, 1), _user(40, 2), _user(40, 3)]
    assert _group_tool_cycles(turns) == [[turns[0]], [turns[1]], [turns[2]]]


# ── the atomicity guarantee (the prevention) ──────────────────────────────────────────────────


def test_trim_head_excludes_cycle_rather_than_splitting_it():
    """Tier 2: THE prevention — at a budget where MESSAGE-level trim would keep [user, assistant]
    (splitting the pair, leaving the result out), group-aware trim excludes the whole cycle instead:
    head = [user] only. RED against message-level (which would leave the assistant dangling)."""
    turns = [_user(40, 1), _asst_tc("t1", 40, 2), _tool("t1", 40, 3), _user(40, 4)]
    # message-level @25: user(10)+assistant(10)=20 fits, +tool(10)=30 over → [user, assistant] SPLIT.
    head = trim_head(turns, max_tokens=25, model=_M, use_chars4=True)
    assert _no_dangling_cycle(head), "no assistant tool_calls without its result"
    assert not any(_is_assistant_with_tool_calls(t) for t in head), (
        "the cycle that doesn't fit is excluded WHOLE, not split (the dangling assistant is absent)"
    )
    assert [t["seq"] for t in head] == [1]


def test_trim_head_includes_whole_cycle_when_it_fits():
    """Tier 2: when the budget fits the cycle, the whole cycle is kept intact (call + result)."""
    turns = [_user(40, 1), _asst_tc("t1", 40, 2), _tool("t1", 40, 3), _user(40, 4)]
    head = trim_head(turns, max_tokens=35, model=_M, use_chars4=True)  # fits user+cycle (30), not +user
    assert [t["seq"] for t in head] == [1, 2, 3], "whole cycle kept intact"
    assert _no_dangling_cycle(head)


def test_trim_tail_excludes_cycle_rather_than_splitting_it():
    """Tier 2: symmetric for the tail — a boundary that would keep [tool_result, user] (orphan
    result, its call trimmed away) instead excludes the whole cycle."""
    turns = [_user(40, 1), _asst_tc("t1", 40, 2), _tool("t1", 40, 3), _user(40, 4)]
    # message-level tail @25: user(10)+tool(10)=20 fits, +assistant(10)=30 over → [tool, user] ORPHAN.
    tail = trim_tail(turns, max_tokens=25, model=_M, use_chars4=True)
    assert _no_dangling_cycle(tail), "no orphan result (the trimmed-away call would orphan it)"
    assert all(t.get("role") != "tool" for t in tail), "the split cycle is excluded whole from the tail"
    assert [t["seq"] for t in tail] == [4]


def test_trim_tail_includes_whole_cycle_when_it_fits():
    """Tier 2: the tail keeps the whole cycle intact when it fits."""
    turns = [_user(40, 1), _asst_tc("t1", 40, 2), _tool("t1", 40, 3), _user(40, 4)]
    tail = trim_tail(turns, max_tokens=35, model=_M, use_chars4=True)  # fits cycle(20)+user(10)
    assert [t["seq"] for t in tail] == [2, 3, 4]
    assert _no_dangling_cycle(tail)


# ── over-budget single cycle: kept WHOLE (keep-whole, not truncated) ───────────────────────────


def test_over_budget_single_cycle_kept_whole_with_new_event():
    """Tier 2: a single tool cycle alone exceeding the budget is kept WHOLE (no split, no result
    loss) and emits ``tool_cycle_kept_whole_over_budget`` — NOT ``turn_too_large_truncated`` (it is
    not a truncation)."""
    turns = [_asst_tc("t1", 80, 1), _tool("t1", 80, 2)]  # 20 + 20 = 40 tokens, alone > 25
    events = EventLog()
    head = trim_head(turns, max_tokens=25, model=_M, use_chars4=True, events=events)
    assert [t["seq"] for t in head] == [1, 2], "the whole cycle survives (call + result), over budget"
    kinds = [e.type for e in events.all()]
    assert "tool_cycle_kept_whole_over_budget" in kinds, "the keep-whole event is emitted"
    assert "turn_too_large_truncated" not in kinds, "it is NOT reported as a truncation (no loss)"


def test_over_budget_non_cycle_turn_keeps_legacy_truncated_event():
    """Tier 2: a single non-cycle turn over budget keeps the existing ``turn_too_large_truncated``
    event (backward-compat — the group change only adds the cycle case)."""
    turns = [_user(200, 1)]  # 50 tokens alone > 25
    events = EventLog()
    trim_head(turns, max_tokens=25, model=_M, use_chars4=True, events=events)
    kinds = [e.type for e in events.all()]
    assert "turn_too_large_truncated" in kinds
    assert "tool_cycle_kept_whole_over_budget" not in kinds


# ── non-tool history is byte-identical to the old message-level behavior ───────────────────────


def test_non_tool_history_is_unchanged():
    """Tier 2: with no tool_calls, group-aware trim is byte-identical to message-level — head is the
    budget-bounded prefix, tail the budget-bounded suffix."""
    turns = [_user(40, i + 1) for i in range(8)]  # 10 tokens each
    head = trim_head(turns, max_tokens=25, model=_M, use_chars4=True)
    tail = trim_tail(turns, max_tokens=25, model=_M, use_chars4=True)
    assert head == turns[:2], "head = first two turns (20 ≤ 25, +10 would exceed)"
    assert tail == turns[-2:], "tail = last two turns"
