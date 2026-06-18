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

import contextlib
from datetime import datetime, timezone
from pathlib import Path

import reyn.llm.model_budget as _mb
from reyn.config import CompactionConfig
from reyn.core.events.state_log import StateLog
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.session import ChatMessage, Session


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextlib.contextmanager
def _synthetic_t_max(t_max: int):
    """Monkeypatch get_max_input_tokens for the duration of the with-block.

    Uses direct module-level replacement (the same pattern used in
    test_chat_compaction_engine_11axis.py) — no unittest.mock.
    """
    original = _mb.get_max_input_tokens
    _mb.get_max_input_tokens = lambda model, **kw: t_max  # type: ignore[assignment]
    try:
        yield
    finally:
        _mb.get_max_input_tokens = original


def _make_session(tmp_path: Path, *, t_max: int = 1_000_000) -> Session:
    """Create a Session whose compaction engine uses a synthetic T_max.

    ``use_chars4_estimate=True`` makes token estimation deterministic:
    each character counts as 1/4 token.

    ``t_max`` is injected via monkeypatch so effective_trigger is
    predictable in tests.  The default (1_000_000) is large enough that
    any realistic test conversation fits and no elide fires, unless a
    smaller t_max is passed.
    """
    state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")
    bt = BudgetTracker(CostConfig())
    cfg = CompactionConfig(
        body_token_cap=1500,
        use_chars4_estimate=True,  # deterministic: chars // 4
        section_caps_spec_tokens=0,  # keeps B_M positive for small T_max values
    )
    # Monkeypatch covers the engine's compute_budgets() call at Session init.
    with _synthetic_t_max(t_max):
        return Session(
            agent_name="default",
            agent_role="",
            output_language="en",
            budget_tracker=bt,
            state_log=state_log,
            compaction_config=cfg,
            snapshot_path=tmp_path / ".reyn" / "agents" / "default" / "state" / "snapshot.json",
        )


def _push(session: Session, role: str, text: str) -> None:
    if role == "agent":
        role = "assistant"
    session.history.append(ChatMessage(role=role, content=text, ts=_now()))


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

    msgs = session._build_history_for_router()
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
    msgs = session._build_history_for_router()
    assert msgs == [], f"expected empty result for empty history, got {msgs!r}"


def test_single_turn_returns_single_message(tmp_path):
    """Tier 2: single turn → exactly one message, no duplication."""
    session = _make_session(tmp_path)
    _push(session, "user", "hello")
    msgs = session._build_history_for_router()
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

    msgs = session._build_history_for_router()
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

    msgs = session._build_history_for_router()
    bridge_msgs = [m for m in msgs if isinstance(m.get("content"), str)
                   and m["content"].startswith("[summary")]
    assert bridge_msgs, (
        "expected a summary bridge message when summary exists and elide fires"
    )
