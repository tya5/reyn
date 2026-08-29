"""Tier 2: Session._build_history_for_router token-budget slicing correctness.

#1128 step 3 (Fork B) introduced a token-budget elide (head+tail, dropping
the middle) coinciding with effective_trigger. #5367 (owner ruling) retired
that elide branch entirely — see ``RouterHistoryBuffer.build_history``'s
own docstring for why — so the router view now always returns ALL
(watermark-filtered) turns raw, whatever the total token estimate.

Tests pin:
- Small/any-size conversation: all (watermark-filtered) turns returned, no
  duplication (the Q4 attractor root cause was exactly this duplication).
- Empty history: empty result.
- Summary bridge inserted whenever a summary exists (watermark > 0) — never
  gated on elide, which no longer exists.
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

# ── Window-utilization: all turns returned raw ────────────────────────────────


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
    watermark filter runs unconditionally, before anything else, so a
    covered turn is excluded here regardless of conversation size — #5367
    later retired elide itself, which only makes this filter's
    unconditional positioning matter MORE (it is now the only thing
    standing between a covered turn and being resent raw).
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


def test_seq_zero_sentinel_turn_is_never_excluded_by_a_watermark(tmp_path, monkeypatch):
    """Tier 2: #4954(2) TESTS-READ finding (lead-coder) — ``seq == 0`` is
    the #3704 "no coordinate assigned" sentinel (pre-#3704 legacy history),
    NOT "the oldest turn". A bare ``m.seq > watermark`` would treat it as
    older than everything and drop it the instant ANY watermark exists —
    and such a turn was never a compaction candidate either
    (``compaction_controller.py``'s own filter is ``t.seq > prev_cover``,
    always false at seq=0), so it was never summarised: dropping it here
    would be silent, PERMANENT content loss with no other place that ever
    stops sending it. The fix (``m.seq == 0 or m.seq > watermark``) must
    keep such a turn in the projection regardless of the watermark's
    value — matching the exact predicate ``Session``'s own #4468
    security-latch scan already uses (session.py, same
    ``_compaction_watermark()`` value)."""
    session = _make_session(tmp_path, t_max=1_000_000, monkeypatch=monkeypatch)
    session.history.append(ChatMessage(
        role="summary", content="summary of the first exchange", ts=_now(),
        meta={"structured": {"topic_arc": "test"}, "covers_through_seq": 5},
    ))
    # A legacy, pre-#3704 turn — seq defaults to 0 (never explicitly set).
    session.history.append(ChatMessage(
        role="user", content="legacy-turn-no-coordinate", ts=_now(),
    ))
    for i, text in enumerate(["covered-1", "covered-2"]):
        session.history.append(ChatMessage(
            role="user" if i % 2 == 0 else "assistant",
            content=text, ts=_now(), seq=i + 1,
        ))
    session.history.append(ChatMessage(
        role="assistant", content="uncovered-3", ts=_now(), seq=10,
    ))

    msgs = session._history_buffer.build_history()
    contents = [m["content"] for m in msgs]

    assert "legacy-turn-no-coordinate" in contents, (
        f"a seq==0 (no-coordinate-assigned) turn must never be excluded by "
        f"any watermark value — it was never a compaction candidate, so "
        f"excluding it here is silent permanent loss; got {contents!r}"
    )
    assert "covered-1" not in contents and "covered-2" not in contents, (
        "real covered turns (seq 1, 2 <= watermark 5) must still be excluded"
    )
    assert "uncovered-3" in contents


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


# ── Summary bridge ────────────────────────────────────────────────────────────

# Each "XXXXXXXXXXX...X" text is 320 chars → 80 tokens (chars4). Sized large
# only because the fixture below predates #5367 (when it needed to exceed
# effective_trigger to trigger elide) — kept as-is, size no longer matters.
_LONG_TEXT = "X" * 320


def test_elide_inserts_summary_bridge_when_summary_present(tmp_path, monkeypatch):
    """Tier 2: when a summary exists (watermark > 0), a bridge message is
    inserted — unconditionally, not gated on any size/budget decision.

    #5367 (owner ruling): this test used to also require a LARGE history
    to "guarantee elide fires" — that whole size-triggered branch is
    retired (see ``RouterHistoryBuffer.build_history``'s own docstring);
    the bridge's OWN condition (#4954(2): watermark > 0) is untouched by
    that removal, so this test still passes and needed only its stale
    "when elide fires" framing corrected, not deletion. 35 turns is kept
    here (not shrunk to a minimal size) only because the fixture already
    existed at this size and there is no reason to churn it.

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
    # the projection by #4954(2)) leaves 30 uncovered turns — size is not
    # load-bearing post-#5367 (nothing depends on exceeding a trigger any
    # more), kept only because the fixture predates that removal.
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
        "expected a summary bridge message when a summary exists (watermark > 0)"
    )
    contents = [m["content"] for m in msgs]
    covered_texts = set(texts[:5])
    assert not covered_texts & set(contents), (
        "the 5 turns covered by the summary's covers_through_seq must never "
        "be resent raw — that duplication is exactly what #4954(2) closes"
    )
