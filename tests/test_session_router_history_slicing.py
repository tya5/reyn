"""Tier 2: ChatSession._build_history_for_router slicing correctness.

Pins the contract that the messages array fed to the chat router LLM
mirrors session.history exactly when the history fits within the head
+ tail window (= no duplication), and applies head/tail slicing only
when the history exceeds the window.

Discovered as the ROOT CAUSE of the Q4 ``list_skills`` empty-stop
attractor in dogfood trace v6: the prior implementation
unconditionally concatenated ``turns[:head_size] + turns[-tail_size:]``,
so any history with ``len(turns) <= head_size + tail_size`` produced a
fully-duplicated messages array. The LLM saw the same user query twice
with history reset between them and silently exited.

Tests also pin the per-turn invariant Reyn relies on for pathological
inputs: even if a user sends two messages in quick succession, each
turn's messages array reflects the snapshot of session.history at that
point, with no extra duplication.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.budget.budget import BudgetTracker, CostConfig
from reyn.chat.session import ChatMessage, ChatSession
from reyn.config import CompactionConfig
from reyn.events.state_log import StateLog


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _make_session(
    tmp_path: Path,
    *,
    head_size: int = 12,
    tail_size: int = 12,
) -> ChatSession:
    state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")
    bt = BudgetTracker(CostConfig(router_invocations_per_turn=3))
    cfg = CompactionConfig(
        trigger_total_tokens=100_000,  # never trigger compaction in unit tests
        head_size=head_size,
        tail_size=tail_size,
        body_token_cap=1500,
    )
    return ChatSession(
        agent_name="default",
        agent_role="",
        output_language="en",
        budget_tracker=bt,
        state_log=state_log,
        compaction_config=cfg,
        snapshot_path=tmp_path / ".reyn" / "agents" / "default" / "state" / "snapshot.json",
    )


def _push_turn(session, role: str, text: str) -> None:
    session.history.append(ChatMessage(role=role, text=text, ts=_now()))


# ── No-overlap branch (= len(turns) <= head_size + tail_size) ────────────────


def test_history_fits_in_window_returns_unique_turns(tmp_path):
    """Tier 2: when history.length <= head_size + tail_size, the
    messages array equals the history one-for-one — no head+tail
    overlap duplication.

    This is the contract that Q4 dogfood violated pre-fix.
    """
    session = _make_session(tmp_path, head_size=12, tail_size=12)
    # 7 turns (= 4 user + 3 agent), well under head+tail=24
    _push_turn(session, "user", "hello")
    _push_turn(session, "agent", "Hi there!")
    _push_turn(session, "user", "what can you do?")
    _push_turn(session, "agent", "I can help with...")
    _push_turn(session, "user", "tell me about yourself")
    _push_turn(session, "agent", "I am a Reyn agent.")
    _push_turn(session, "user", "list available skills")

    msgs = session._build_history_for_router()
    # Must equal exactly 7 entries — pre-fix would have produced 14.
    assert len(msgs) == 7, (
        f"expected 7 messages (= 1-for-1 with history), got {len(msgs)}; "
        "head+tail overlap duplication regression"
    )
    # No duplicates by content.
    user_texts = [m["content"] for m in msgs if m["role"] == "user"]
    assert user_texts == ["hello", "what can you do?", "tell me about yourself", "list available skills"]
    assert len(set(user_texts)) == len(user_texts), (
        "user texts should be unique; duplication detected"
    )


def test_empty_history_returns_empty_messages(tmp_path):
    """Tier 2: empty history → empty messages, no spurious duplication."""
    session = _make_session(tmp_path)
    msgs = session._build_history_for_router()
    assert msgs == []


def test_single_turn_returns_single_message(tmp_path):
    """Tier 2: 1 turn → 1 message, not 2."""
    session = _make_session(tmp_path)
    _push_turn(session, "user", "hello")
    msgs = session._build_history_for_router()
    assert len(msgs) == 1
    assert msgs[0] == {"role": "user", "content": "hello"}


# ── Boundary: exactly at head + tail ────────────────────────────────────────


def test_exactly_head_plus_tail_no_duplication(tmp_path):
    """Tier 2: len(turns) == head_size + tail_size is the boundary case.
    Both branches would produce the same result (= all turns, no
    duplication). Pre-fix would also have worked here because
    head[:N] + tail[-N:] of a 2N-list IS the full list. Pinning this
    boundary case explicitly so a future refactor that flips the
    inequality (= ``<`` vs ``<=``) doesn't silently regress.
    """
    session = _make_session(tmp_path, head_size=2, tail_size=2)
    for i in range(4):
        _push_turn(session, "user" if i % 2 == 0 else "agent", f"msg-{i}")
    msgs = session._build_history_for_router()
    assert len(msgs) == 4, f"expected 4, got {len(msgs)}"
    assert [m["content"] for m in msgs] == ["msg-0", "msg-1", "msg-2", "msg-3"]


# ── Compaction branch (= len(turns) > head + tail) ──────────────────────────


def test_history_exceeds_window_applies_head_tail_slice(tmp_path):
    """Tier 2: when history exceeds head+tail, slicing kicks in and the
    middle turns are elided. With NO summary bridge, head+tail are
    concatenated directly.
    """
    session = _make_session(tmp_path, head_size=2, tail_size=2)
    for i in range(8):
        _push_turn(session, "user" if i % 2 == 0 else "agent", f"msg-{i}")
    # 8 turns > 2+2=4 → head=msg-0..1, tail=msg-6..7
    msgs = session._build_history_for_router()
    assert len(msgs) == 4
    assert [m["content"] for m in msgs] == ["msg-0", "msg-1", "msg-6", "msg-7"]


def test_zero_tail_size_no_overlap_in_unfit_branch(tmp_path):
    """Tier 2: tail_size=0 in the unfit branch yields head only, no
    spurious empty tail to cause off-by-one.
    """
    session = _make_session(tmp_path, head_size=2, tail_size=0)
    for i in range(5):
        _push_turn(session, "user", f"msg-{i}")
    msgs = session._build_history_for_router()
    assert len(msgs) == 2
    assert [m["content"] for m in msgs] == ["msg-0", "msg-1"]
