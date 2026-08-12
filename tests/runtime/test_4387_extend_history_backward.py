"""Tier 2: #4387 Phase B ② (remaining consumers) — ``Session.extend_history_backward``,
the PUBLIC paging primitive external callers (the TUI's scrollback paging /
search, via ``RegistryReadModel.load_older_conversation_history``) use to page
further back than what Phase B ① (#4400) bounded ``load_history()``'s startup
read to.

This is a thin public wrapper over the already-tested private
``_load_older_entries`` (see ``test_4387_active_branch_history_extend_on_demand.py``
for the WAL-rewind-driven internal caller, ``_active_branch_history``) — what's
new here is the "give me one more page" derivation (``before_seq`` from
``self.history[0].seq``) and the public/external-consumer surface itself, not
the underlying disk-read mechanism.

Real ``Session`` + real durable ``history.jsonl`` (via ``_append_history``,
same seam every other #4387 test in this arc uses) — no fakes.
"""
from __future__ import annotations

from pathlib import Path

from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _session(tmp_path: Path) -> Session:
    return make_session(agent_name="alice", snapshot_path=tmp_path / "snapshot.json")


def _append_turns(s: Session, n: int, *, start: int = 1) -> None:
    for i in range(start, start + n):
        s._append_history(ChatMessage(role="user", content=f"turn {i}"))


def test_extend_backward_pages_in_content_older_than_the_bounded_prefix(tmp_path, monkeypatch):
    """Tier 2: 10 real, durable turns; ``self.history`` truncated to the
    newest 3 (simulating Phase B ①'s bounded startup load) — one call must
    prepend exactly the 7 older turns, in the correct oldest-first order."""
    monkeypatch.chdir(tmp_path)
    s = _session(tmp_path)
    _append_turns(s, 10)
    s.history = s.history[-3:]
    assert [m.content for m in s.history] == ["turn 8", "turn 9", "turn 10"]

    extended = s.extend_history_backward(min_lines=200)

    assert extended == 7, f"expected all 7 older turns prepended in one call, got {extended}"
    assert [m.content for m in s.history] == [f"turn {i}" for i in range(1, 11)], (
        "extend_history_backward must PREPEND (not replace) — the already-loaded "
        "newest 3 turns must still be present, in order, after the older 7"
    )


def test_extend_backward_returns_zero_at_the_true_start_of_history(tmp_path, monkeypatch):
    """Tier 2: the caller's ONLY "nothing more exists" signal — calling again
    once every entry is already loaded must return 0, not re-derive/duplicate
    anything already in ``self.history``."""
    monkeypatch.chdir(tmp_path)
    s = _session(tmp_path)
    _append_turns(s, 5)
    s.history = s.history[-2:]

    first = s.extend_history_backward(min_lines=200)
    assert first == 3
    second = s.extend_history_backward(min_lines=200)

    assert second == 0, "already at the file's start — must signal exhaustion, not repeat"
    assert [m.content for m in s.history] == [f"turn {i}" for i in range(1, 6)], (
        "a second (BOF) call must not duplicate or reorder what's already loaded"
    )


def test_extend_backward_on_an_unbounded_session_is_a_correct_no_op(tmp_path, monkeypatch):
    """Tier 2: accept-side — a session whose ``self.history`` already holds
    everything on disk (never bounded) must return 0 too, the same signal as
    true-BOF, since there is genuinely nothing older to page in."""
    monkeypatch.chdir(tmp_path)
    s = _session(tmp_path)
    _append_turns(s, 4)  # self.history is NOT truncated — already complete

    extended = s.extend_history_backward(min_lines=200)

    assert extended == 0
    assert [m.content for m in s.history] == [f"turn {i}" for i in range(1, 5)]
