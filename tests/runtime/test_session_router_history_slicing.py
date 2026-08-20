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


def test_history_fits_in_window_returns_all_turns(tmp_path, monkeypatch):
    """Tier 2: when total tokens <= effective_trigger, all turns are returned
    in order — no elide, no duplication.

    This pins the window-utilization contract: Fork B shows the full raw
    conversation until it exceeds the trigger.  The Q4 attractor root cause
    was duplicate turns from the old turn-count head+tail overlap — this
    branch can never produce duplicates.
    """
    # Large t_max → effective_trigger large → 7 short turns easily fit.
    session = _make_session(tmp_path, t_max=1_000_000, monkeypatch=monkeypatch)
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


def test_watermark_excludes_covered_turns_even_when_conversation_fits_budget(
    tmp_path, monkeypatch,
):
    """Tier 2: #4954(2) — the exact combination the issue's own gap analysis
    named ("a summary exists AND the conversation still fits the token
    budget"; no existing fixture before this PR combined the two). Before
    #4954(2), a covered turn's fate depended ENTIRELY on whether elide
    fired this call — a byte-heavy-but-token-light conversation could stay
    under ``effective_trigger`` forever, resending covered turns raw on
    every single turn (the issue's own real-machine symptom). Now the
    watermark filter runs unconditionally, before the fits-vs-elides
    decision, so a covered turn is excluded here even though this
    conversation is small enough that elide never fires at all.
    """
    session = _make_session(tmp_path, t_max=1_000_000, monkeypatch=monkeypatch)
    session.history.append(ChatMessage(
        role="summary", content="summary of the first exchange", ts=_now(),
        meta={"structured": {"topic_arc": "test"}, "covers_through_seq": 2},
    ))
    covered = ["covered-1", "covered-2"]
    uncovered = ["uncovered-3", "uncovered-4"]
    for i, text in enumerate(covered + uncovered):
        session.history.append(ChatMessage(
            role="user" if i % 2 == 0 else "assistant",
            content=text, ts=_now(), seq=i + 1,
        ))

    msgs = session._history_buffer.build_history()
    contents = [m["content"] for m in msgs]

    for text in covered:
        assert text not in contents, (
            f"{text!r} has seq <= covers_through_seq=2 — must never be "
            f"resent raw, even though this conversation easily fits the "
            f"token budget (t_max=1_000_000); got {contents!r}"
        )
    for text in uncovered:
        assert text in contents, f"{text!r} is uncovered and must still be present"
    bridge_msgs = [c for c in contents if isinstance(c, str) and c.startswith("[summary")]
    assert bridge_msgs, (
        "the summary must still be represented in the projection (never an "
        "elide-only decoration) even on the fits-the-budget branch"
    )


def test_watermark_zero_never_filters_or_bridges(tmp_path, monkeypatch):
    """Tier 2: #4954(2) — a summary with ``covers_through_seq=0`` (nothing
    covered yet — the ``TokenMultiplierLearner``-style cold-start shape,
    or simply no compaction having run) changes NOTHING: no turn is
    filtered, and no bridge is inserted. This is the explicit boundary
    #4954(2)'s ``watermark > 0`` gate draws, not an oversight — an
    uncovering summary has nothing to represent."""
    session = _make_session(tmp_path, t_max=1_000_000, monkeypatch=monkeypatch)
    session.history.append(ChatMessage(
        role="summary", content="a summary that covers nothing yet", ts=_now(),
        meta={"structured": {"topic_arc": "test"}, "covers_through_seq": 0},
    ))
    texts = ["one", "two", "three"]
    for i, text in enumerate(texts):
        session.history.append(ChatMessage(
            role="user" if i % 2 == 0 else "assistant",
            content=text, ts=_now(), seq=i + 1,
        ))

    msgs = session._history_buffer.build_history()
    contents = [m["content"] for m in msgs]
    for text in texts:
        assert text in contents
    assert not any(isinstance(c, str) and c.startswith("[summary") for c in contents), (
        "covers_through_seq=0 must not insert a bridge — nothing is covered"
    )


def test_watermark_follows_whichever_history_the_producer_returns(tmp_path, monkeypatch):
    """Tier 2: #4954(2) — the watermark reflects whichever ``history`` list
    ``self._history_fn()`` returns THIS call, not a value cached from a
    prior call. In production ``_history_fn`` is
    ``Session._active_branch_history``, re-derived fresh every call (the
    same branch/rewind-aware view every OTHER read in this method already
    depends on) — this test simulates a rewind/branch-switch by swapping
    the underlying history list between two calls on the SAME buffer
    instance, and asserts the watermark (and therefore the filtered
    projection) changes to match."""
    from reyn.config.chat import CompactionConfig
    from reyn.runtime.services.router_history_buffer import RouterHistoryBuffer

    def _history_with_watermark(covers: int) -> list:
        h: list = [ChatMessage(
            role="summary", content="s", ts=_now(),
            meta={"covers_through_seq": covers},
        )]
        for i in range(3):
            h.append(ChatMessage(role="user", content=f"t{i + 1}", ts=_now(), seq=i + 1))
        return h

    state = {"history": _history_with_watermark(covers=0)}
    buf = RouterHistoryBuffer(
        history_fn=lambda: state["history"],
        compaction=CompactionConfig(),
        compaction_controller=None,
        model_fn=lambda: "openai/gpt-4o",
        events=None,
        media_store=None,
        router_host=None,
        universal_wrappers_enabled=False,
        non_interactive=False,
    )

    contents_before = [m["content"] for m in buf.build_history()]
    assert "t1" in contents_before, "watermark=0: nothing filtered yet"

    # Simulate a rewind/branch-switch landing on a state where seq 1 IS covered.
    state["history"] = _history_with_watermark(covers=1)
    contents_after = [m["content"] for m in buf.build_history()]
    assert "t1" not in contents_after, (
        "after the producer starts returning a history whose latest summary "
        "covers seq 1, the SAME buffer instance must reflect the new "
        "watermark on its very next call, not a value cached from before"
    )
    assert "t2" in contents_after and "t3" in contents_after


def test_empty_history_returns_empty_messages(tmp_path, monkeypatch):
    """Tier 2: empty history → empty result."""
    session = _make_session(tmp_path, monkeypatch=monkeypatch)
    msgs = session._history_buffer.build_history()
    assert msgs == [], f"expected empty result for empty history, got {msgs!r}"


def test_single_turn_returns_single_message(tmp_path, monkeypatch):
    """Tier 2: single turn → exactly one message, no duplication."""
    session = _make_session(tmp_path, monkeypatch=monkeypatch)
    _push(session, "user", "hello")
    msgs = session._history_buffer.build_history()
    assert msgs == [{"role": "user", "content": "hello"}]


# ── Elide branch (total > effective_trigger) ─────────────────────────────────

# Each "XXXXXXXXXXX...X" text is 320 chars → 80 tokens (chars4).
# 30 turns × 80 tokens = 2400 tokens. With t_max=2800, effective_trigger is
# always < t_max by construction, so 2400 > effective_trigger regardless of SP
# size — default-independent (hot_list_n changes don't affect this bound).

_LONG_TEXT = "X" * 320  # 80 tokens via chars4; use with t_max=2800


def test_history_exceeds_trigger_elides_middle(tmp_path, monkeypatch):
    """Tier 2: when total tokens > effective_trigger, the middle turns are
    elided and head + tail are returned without duplication.

    Uses T_max=2800 with 30 turns of 80-token text (total=2400 tokens).
    2400 > T_max=2800 so the elide branch fires regardless of SP size —
    default-independent: hot_list_n and other SP-affecting defaults don't
    change whether elide fires.
    """
    session = _make_session(tmp_path, t_max=2800, monkeypatch=monkeypatch)
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


def test_elide_inserts_summary_bridge_when_summary_present(tmp_path, monkeypatch):
    """Tier 2: when a summary exists and elide fires, a bridge message is
    inserted between head and tail.

    Uses 30 turns to guarantee elide fires regardless of SP size (see
    test_history_exceeds_trigger_elides_middle for the default-independent
    size rationale).

    #4954(2): the summary's ``covers_through_seq`` must be REALISTIC
    (> 0, and below the pushed turns' own seqs) — a real, #4951-A-derived
    summary always covers turns that actually existed at compaction time
    (seq >= 1); ``covers_through_seq=0`` (this test's value before #4954)
    means "nothing covered yet", under which the bridge is correctly
    ABSENT by design (an uncovering summary has nothing to represent).
    Explicit ``seq=`` (not ``_push``, which leaves every turn at the
    ChatMessage default ``seq=0``) is required here so the watermark
    filter has real seqs to compare against.
    """
    session = _make_session(tmp_path, t_max=2800, monkeypatch=monkeypatch)
    # Inject a summary covering the first 5 (of 35) turns.
    session.history.append(ChatMessage(
        role="summary",
        content="summary of earlier",
        ts=_now(),
        meta={"structured": {"topic_arc": "test"}, "covers_through_seq": 5},
    ))
    # 35 turns × 80 tokens, 5 covered by the summary above (excluded from
    # the projection by #4954(2)) leaves 30 uncovered turns — the same
    # elide-triggering size test_history_exceeds_trigger_elides_middle
    # documents as reliably > T_max=2800 regardless of SP size.
    texts = [f"turn-{i}:" + _LONG_TEXT for i in range(35)]
    for i, text in enumerate(texts):
        session.history.append(ChatMessage(
            role="user" if i % 2 == 0 else "assistant",
            content=text, ts=_now(), seq=i + 1,
        ))

    msgs = session._history_buffer.build_history()
    bridge_msgs = [m for m in msgs if isinstance(m.get("content"), str)
                   and m["content"].startswith("[summary")]
    assert bridge_msgs, (
        "expected a summary bridge message when summary exists and elide fires"
    )
    contents = [m["content"] for m in msgs]
    covered_texts = set(texts[:5])
    assert not covered_texts & set(contents), (
        "the 5 turns covered by the summary's covers_through_seq must never "
        "be resent raw — that duplication is exactly what #4954(2) closes"
    )
