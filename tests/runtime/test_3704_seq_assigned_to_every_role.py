"""Tier 2: #3704 (owner-ratified, 2026-08-08) — every persisted history entry
gets a monotonic seq at append time, regardless of role.

``Session._append_history``'s seq-assignment used to gate on
``msg.role in ("user", "agent")`` — ``"agent"`` was never a real
``ChatMessage`` role (the Literal has always been "user"/"assistant"/"tool"/
"system"/"summary"), so the condition only ever matched "user". assistant/
tool entries persisted with ``seq == 0`` forever, and
``CompactionController._select_candidates``'s ``t.seq > prev_cover`` filter
reads seq==0 as "already covered" — so assistant/tool turns were silently
and permanently excluded from every compaction candidate set.

Three witnesses:
(a) assign side — every role gets a nonzero seq at persist time.
(b) consume side — the ONE witness that shows this issue is actually fixed:
    assistant/tool turns now appear in the compaction candidate set
    (``t.seq > prev_cover`` passes for them). An assign-side-only witness is
    not sufficient (architect's own framing) — seq being assigned proves
    nothing about whether the consumer that was silently dropping these
    turns actually picks them up now.
(c) migration compat — old-format history (assistant/tool stuck at seq==0)
    does not corrupt ``_next_seq`` bootstrap on load.
"""
from __future__ import annotations

import json

from reyn.runtime.chat_message import ChatMessage, Disclosure
from tests._support.session import make_session as _make_session

# ── (a) assign side: every role gets a monotonic seq ────────────────────────


def test_every_role_gets_a_nonzero_seq_at_persist_time(tmp_path, monkeypatch):
    """Tier 2: #3704 arm (a) — user/assistant/tool/system entries ALL get a
    monotonic seq when appended, not just "user".

    Falsification (performed for real): reverting the role gate (restoring
    ``if msg.role in ("user", "agent") and msg.seq == 0:``) makes this test
    RED — assistant/tool/system entries stay at seq==0 while user does not."""
    session = _make_session(tmp_path, monkeypatch=monkeypatch)

    session._append_history(ChatMessage(role="user", content="hello", ts="t1"))
    session._append_history(ChatMessage(role="assistant", content="hi", ts="t2"))
    session._append_history(ChatMessage(role="tool", content="result", ts="t3"))
    session._append_history(
        ChatMessage(role="system", content="note", ts="t4", disclosure=Disclosure.INTERNAL),
    )

    seqs = [m.seq for m in session.history]
    assert all(s > 0 for s in seqs), f"expected every entry to get a nonzero seq, got {seqs!r}"
    # Monotonic: strictly increasing in append order.
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


# ── (b) consume side: the actual fix — assistant/tool reach the compaction candidate set ──


def test_assistant_and_tool_turns_are_not_excluded_from_compaction_candidates(
    tmp_path, monkeypatch,
):
    """Tier 2: #3704 arm (b) — the ONE witness that shows this is actually
    fixed, not just that seq gets assigned. Drives the REAL
    ``CompactionController._select_candidates`` (the exact consumer the
    issue names) with a mix of roles and confirms assistant/tool turns —
    previously stuck at seq==0, always read as "already covered" by
    ``t.seq > prev_cover`` — now pass the filter and appear in the returned
    candidate set.

    Falsification (performed for real): reverting the role gate makes
    assistant/tool stay at seq==0, and this test goes RED — the candidate
    set excludes them (``t.seq > prev_cover`` is False for seq==0 whenever
    prev_cover >= 0)."""
    # A small t_max keeps head_budget/tail_budget small, so with enough
    # turns the middle ones genuinely fall OUTSIDE both the head and tail
    # slices — the actual "candidate" zone _select_candidates filters by
    # seq. With the large default t_max, 3 tiny turns fit entirely inside
    # head+tail and nothing is ever a "middle" candidate regardless of seq.
    session = _make_session(tmp_path, monkeypatch=monkeypatch, t_max=20_000)

    filler = "middle turn padding text " * 20
    for i in range(40):
        role = "user" if i % 3 == 0 else ("assistant" if i % 3 == 1 else "tool")
        session._append_history(
            ChatMessage(role=role, content=f"{filler} #{i}", ts=f"t{i}")
        )

    turns = list(session.history)
    controller = session._compaction_controller
    candidates = controller._select_candidates(turns, prev_cover=0)

    candidate_roles = {t.role for t in candidates}
    assert "assistant" in candidate_roles, (
        f"assistant turns must reach the compaction candidate set; got roles {candidate_roles!r}"
    )
    assert "tool" in candidate_roles, (
        f"tool turns must reach the compaction candidate set; got roles {candidate_roles!r}"
    )


# ── (c) migration: old-format history (assistant/tool stuck at 0) loads safely ──


def test_old_format_history_with_zero_seq_assistant_turns_bootstraps_correctly(
    tmp_path, monkeypatch,
):
    """Tier 2: #3704 arm (c) — a history.jsonl written by the OLD (buggy)
    code, where assistant/tool entries are stuck at seq==0 forever (no
    backfill on read), does not corrupt ``_next_seq`` bootstrap: the max
    is taken over nonzero seqs only, so the zeros are correctly ignored
    rather than treated as "seq 0 is the latest"."""
    session = _make_session(tmp_path, monkeypatch=monkeypatch)
    history_path = session.history_path
    history_path.parent.mkdir(parents=True, exist_ok=True)

    # Simulates OLD-format entries: user gets real seqs, assistant/tool are
    # frozen at 0 (exactly what the pre-fix code persisted).
    old_entries = [
        {"role": "user", "content": "q1", "ts": "t1", "seq": 1, "meta": {}},
        {"role": "assistant", "content": "a1", "ts": "t2", "seq": 0, "meta": {}},
        {"role": "user", "content": "q2", "ts": "t3", "seq": 2, "meta": {}},
        {"role": "tool", "content": "r1", "ts": "t4", "seq": 0, "meta": {}},
    ]
    with history_path.open("w", encoding="utf-8") as f:
        for e in old_entries:
            f.write(json.dumps(e) + "\n")

    session.history = []
    session.load_history()

    assert [m.seq for m in session.history] == [1, 0, 2, 0]

    # Public-behavior proof that _next_seq bootstrapped past the real max
    # (2), not confused by the zeros: a NEW entry appended after load gets
    # the next real seq (3), not 1 (which a naive max-including-zeros, or a
    # bootstrap that got stuck on the last zero, could produce).
    session._append_history(ChatMessage(role="assistant", content="a2", ts="t5"))
    assert session.history[-1].seq == 3
