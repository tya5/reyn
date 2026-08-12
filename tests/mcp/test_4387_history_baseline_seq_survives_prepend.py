"""Tier 2: #4387 architect review (Phase B ②) — MCP's reply-harvest baseline
survives a prepend to ``session.history``.

``send_to_agent_impl`` captures a "baseline" before dispatching a request,
then later asks "what's new since then". It used to capture ``len(session.
history)`` and read ``session.history[baseline:]`` — a POSITION index. #4387
Phase B's on-demand older-entry loading (not yet implemented) will PREPEND
older entries to ``self.history`` for other consumers (TUI scrollback paging,
``_active_branch_history``); a position index silently reads the WRONG slice
once anything can prepend — no exception, just old messages reported as "the
new reply".

Fixed to capture a ``seq`` watermark (``_history_baseline_seq``) and filter by
coordinate (``msg.seq > baseline_seq``) instead — immune to a prepend by
construction, since ``seq`` is a property of each entry, not its position.

Real ``ChatMessage`` instances, real list mutation (a plain ``insert(0, ...)``
stands in for what ``_load_older_entries`` will eventually do — the shape
under test is "prepend changes positions but not seqs", not that primitive's
own not-yet-written implementation).
"""
from __future__ import annotations

from reyn.mcp.server import _history_baseline_seq, _new_agent_history_entries
from reyn.runtime.chat_message import ChatMessage


def _msg(role: str, content: str, seq: int) -> ChatMessage:
    return ChatMessage(role=role, content=content, seq=seq)


class _FakeSession:
    def __init__(self, history: "list[ChatMessage]") -> None:
        self.history = history


def test_baseline_seq_filters_correctly_after_a_prepend() -> None:
    """Tier 2: the exact failure shape architect named. Capture a baseline,
    prepend OLDER entries (as on-demand loading will), append a genuinely
    NEW reply, then harvest — must return only the new reply, never any of
    the prepended older ones."""
    s = _FakeSession([
        _msg("user", "hi", seq=10),
        _msg("assistant", "hello", seq=11),
    ])
    baseline_seq = _history_baseline_seq(s)
    assert baseline_seq == 11

    # Simulate Phase B's on-demand older-entry load: entries OLDER than
    # anything currently held get PREPENDED — positions shift, seqs don't.
    s.history.insert(0, _msg("assistant", "ancient reply", seq=3))
    s.history.insert(0, _msg("user", "ancient question", seq=2))

    # The real new reply, appended normally after the baseline was captured.
    s.history.append(_msg("assistant", "genuinely new reply", seq=12))

    harvested = _new_agent_history_entries(s, baseline_seq)

    assert harvested == ["genuinely new reply"], (
        "must harvest exactly the post-baseline reply — not the prepended "
        "ancient one, which now sits at a list position >= the old "
        "position-based baseline would have used"
    )


def test_baseline_seq_matches_the_position_based_answer_without_a_prepend() -> None:
    """Tier 2: accept-side — with NO prepend (today's only real shape, since
    Phase B ② isn't implemented yet), the seq-based filter agrees with what
    the old position-based slice would have returned. The fix changes HOW
    the answer is computed, not what it is, in the case that already works."""
    s = _FakeSession([
        _msg("user", "hi", seq=1),
        _msg("assistant", "hello", seq=2),
    ])
    baseline_seq = _history_baseline_seq(s)
    old_style_baseline_len = len(s.history)

    s.history.append(_msg("assistant", "reply A", seq=3))
    s.history.append(_msg("user", "more", seq=4))
    s.history.append(_msg("assistant", "reply B", seq=5))

    seq_based = _new_agent_history_entries(s, baseline_seq)
    position_based = [
        m.text for m in s.history[old_style_baseline_len:]
        if m.role in ("assistant", "agent") and m.text
    ]
    assert seq_based == position_based == ["reply A", "reply B"]


def test_baseline_seq_respects_chain_id_filter() -> None:
    """Tier 2: the ``chain_id`` scoping (concurrent callers on the same
    session) still works under the seq-based filter — unrelated axis, must
    not have been dropped by the rewrite."""
    s = _FakeSession([_msg("user", "hi", seq=1)])
    baseline_seq = _history_baseline_seq(s)

    reply_a = _msg("assistant", "for chain A", seq=2)
    reply_a.meta["chain_id"] = "chain-a"
    reply_b = _msg("assistant", "for chain B", seq=3)
    reply_b.meta["chain_id"] = "chain-b"
    s.history.extend([reply_a, reply_b])

    assert _new_agent_history_entries(s, baseline_seq, chain_id="chain-a") == [
        "for chain A"
    ]
