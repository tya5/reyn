"""Tier 2: Session._build_history_for_router token-budget slicing correctness.

#1128 step 3 (Fork B): elide threshold now coincides with effective_trigger
(the existing pre-frame compaction trigger) instead of the old turn-count
head_size/tail_size.  The router view returns ALL turns when total token
estimate <= effective_trigger (window-utilization-first), and elides the
middle only when the conversation exceeds the budget.

Tests pin:
- Small conversation (total < effective_trigger): all turns returned, no
  duplication (the Q4 attractor root cause was exactly this duplication).
- Large conversation (total > effective_trigger): head + tail with the middle
  elided; deduplication guard so no turn appears twice.
- Empty history: empty result.
- Summary bridge inserted when a summary exists and elide fires.
"""
from __future__ import annotations

from reyn.runtime.chat_message import ChatMessage

# Session builders (make_session / push / now / synthetic_t_max) now live in
# tests/_support (stable, location-independent import path). Aliased back to the
# original module-local names so the tests below are unchanged.
from tests._support.session import (  # noqa: E402
    make_session as _make_session,
)
from tests._support.session import (
    now as _now,
)
from tests._support.session import (
    push as _push,
)

# ── No-elide branch (total <= effective_trigger) ─────────────────────────────


def test_history_fits_in_window_returns_all_turns(tmp_path):
    """Tier 2: when total tokens <= effective_trigger, all turns are returned
    in order — no elide, no duplication.

    This pins the window-utilization contract: Fork B shows the full raw
    conversation until it exceeds the trigger.  The Q4 attractor root cause
    was duplicate turns from the old turn-count head+tail overlap — this
    branch can never produce duplicates.
    """
    # Large t_max → effective_trigger large → 7 short turns easily fit.
    session = _make_session(tmp_path, t_max=1_000_000)
    pushed = ["hello", "Hi there!", "what can you do?", "I can help...",
              "tell me about yourself", "I am a Reyn agent.", "list available skills"]
    for text in pushed:
        _push(session, "user", text)

    msgs = session._history_buffer.build_history()
    contents = [m["content"] for m in msgs]
    # All pushed turns returned — no drops, no duplicates.
    assert set(contents) == set(pushed), (
        "window-utilization branch must return all pushed turns"
    )
    assert len(contents) == len(set(contents)), (
        "duplicate messages detected — window-utilization branch must return unique turns"
    )


def test_empty_history_returns_empty_messages(tmp_path):
    """Tier 2: empty history → empty result."""
    session = _make_session(tmp_path)
    msgs = session._history_buffer.build_history()
    assert msgs == [], f"expected empty result for empty history, got {msgs!r}"


def test_single_turn_returns_single_message(tmp_path):
    """Tier 2: single turn → exactly one message, no duplication."""
    session = _make_session(tmp_path)
    _push(session, "user", "hello")
    msgs = session._history_buffer.build_history()
    assert msgs == [{"role": "user", "content": "hello"}]


# ── Elide branch (total > effective_trigger) ─────────────────────────────────

# Each "XXXXXXXXXXX...X" text is 320 chars → 80 tokens (chars4).
# 30 turns × 80 tokens = 2400 tokens. With t_max=2000, effective_trigger is
# always < t_max by construction, so 2400 > effective_trigger regardless of SP
# size — default-independent (hot_list_n changes don't affect this bound).

_LONG_TEXT = "X" * 320  # 80 tokens via chars4; use with t_max=2000


def test_history_exceeds_trigger_elides_middle(tmp_path):
    """Tier 2: when total tokens > effective_trigger, the middle turns are
    elided and head + tail are returned without duplication.

    Uses T_max=2000 with 30 turns of 80-token text (total=2400 tokens).
    2400 > T_max=2000 so the elide branch fires regardless of SP size —
    default-independent: hot_list_n and other SP-affecting defaults don't
    change whether elide fires.
    """
    session = _make_session(tmp_path, t_max=2000)
    texts = [f"turn-{i}:" + _LONG_TEXT for i in range(30)]
    for i, text in enumerate(texts):
        _push(session, "user" if i % 2 == 0 else "assistant", text)

    msgs = session._history_buffer.build_history()
    contents = [m["content"] for m in msgs]

    # The middle turn(s) must be absent.
    present = set(contents)
    assert texts[0] in present, "head turn must be present"
    assert texts[-1] in present, "tail turn must be present"
    # At least one middle turn must be absent (= elide occurred).
    middle_texts = set(texts[1:-1])
    assert not middle_texts.issubset(present), (
        "expected at least one middle turn to be elided, but all middle turns present"
    )
    # No duplicates.
    assert len(contents) == len(set(contents)), (
        "duplicate messages in elide branch — overlap deduplication failed"
    )


def test_elide_inserts_summary_bridge_when_summary_present(tmp_path):
    """Tier 2: when a summary exists and elide fires, a bridge message is
    inserted between head and tail.

    Uses 30 turns to guarantee elide fires regardless of SP size (see
    test_history_exceeds_trigger_elides_middle for the default-independent
    size rationale).
    """
    session = _make_session(tmp_path, t_max=2000)
    # Inject a summary before the turns.
    session.history.append(ChatMessage(
        role="summary",
        content="summary of earlier",
        ts=_now(),
        meta={"structured": {"topic_arc": "test"}, "covers_through_seq": 0},
    ))
    # 30 turns × 80 tokens = 2400 > T_max=2000 → elide fires.
    texts = [f"turn-{i}:" + _LONG_TEXT for i in range(30)]
    for i, text in enumerate(texts):
        _push(session, "user" if i % 2 == 0 else "assistant", text)

    msgs = session._history_buffer.build_history()
    bridge_msgs = [m for m in msgs if isinstance(m.get("content"), str)
                   and m["content"].startswith("[summary")]
    assert bridge_msgs, (
        "expected a summary bridge message when summary exists and elide fires"
    )
