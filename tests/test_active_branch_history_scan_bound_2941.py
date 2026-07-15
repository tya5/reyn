"""Tier 2: #2941 — the LLM-facing active-branch filter must not re-scan the WAL
once PER HISTORY MESSAGE (the owner-reported ``reyn chat`` freeze, growing with
session length).

Root cause: ``Session._active_branch_history`` called ``is_active_seq(state_log,
seq)`` once per history message; ``is_active_seq`` re-derives
``_abandoned_intervals(_rewind_records(state_log))`` from scratch each time, and
``_rewind_records`` does a FULL ``state_log.iter_from(1)`` scan (json-decoding
every WAL line). So a turn with N history messages and an M-entry WAL did O(N x M)
json.loads work EVERY turn. The fix (``build_active_predicate``) hoists the
seq-independent abandoned-interval derivation OUT of the per-message loop: one
``iter_from`` scan per ``build_history()`` call, reused for every message
(O(N + M) instead of O(N x M)).

Real seam: real ``Session`` + real ``StateLog`` + the real ``checkout`` reset-record
primitive, with a ``StateLog`` subclass counting ``iter_from`` calls (a public
method override — not a private-state peek) to assert the SCAN COUNT bound
directly, rather than inferring it from wall-clock timing (flaky).

This test must FLIP RED if the per-message ``is_active_seq`` scan is restored:
locally reverting ``_active_branch_history`` to call ``is_active_seq`` per
message (instead of ``build_active_predicate`` once) makes
``iter_from_calls == n`` (one scan per message) instead of ``<= 2`` — confirmed
during development (see PR body for the strip-falsification record).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.snapshot_generations import checkout
from reyn.core.events.state_log import StateLog
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.session import Session


class _CountingStateLog(StateLog):
    """A real ``StateLog`` that counts ``iter_from`` calls — a public-method
    override, not a private-state assertion, so the scan-count bound is
    verified through the same seam production code calls."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.iter_from_calls = 0

    def iter_from(self, min_seq: int):
        self.iter_from_calls += 1
        return super().iter_from(min_seq)


def _session(tmp_path: Path, state_log: StateLog) -> Session:
    s = Session(
        agent_name="alice", state_log=state_log,
        snapshot_path=tmp_path / "alice_snapshot.json",
    )
    s.register_intervention_listener("test")
    return s


@pytest.mark.asyncio
async def test_active_branch_history_scans_wal_o1_per_build_not_per_message(tmp_path, monkeypatch):
    """Tier 2: N history messages + a rewind cost O(1) iter_from() calls per
    build_history(), NOT O(N). Pre-hoist this was N calls (one full WAL scan per
    message) — the actual freeze mechanism, worsening with both message count and
    WAL length as the session grows."""
    monkeypatch.chdir(tmp_path)
    state_log = _CountingStateLog(tmp_path / "state.wal")
    s = _session(tmp_path, state_log)

    n = 40
    anchors = []
    for i in range(n):
        await state_log.append("step_completed")  # WAL activity between turns (real turn shape)
        s._append_history(ChatMessage(role="user", content=f"turn {i}"))
        anchors.append(s.history[-1].meta["wal_seq"])
    # A rewind exists so the abandoned-interval predicate is non-trivial (not the
    # degenerate empty-list fast path).
    await checkout(state_log, target_seq=anchors[5])

    state_log.iter_from_calls = 0  # measure only the build_history() call under test
    wire = s._history_buffer.build_history()

    assert len(wire) > 0, "sanity: history is non-empty and visible"
    assert state_log.iter_from_calls <= 2, (
        f"expected O(1) WAL scans per build_history() (got {state_log.iter_from_calls} "
        f"for n={n} history messages) — a per-message re-scan (the #2941 freeze bug) "
        "would grow with N, not stay bounded"
    )


@pytest.mark.asyncio
async def test_active_branch_history_scan_count_independent_of_message_count(tmp_path, monkeypatch):
    """Tier 2: the scan count for a build_history() call does NOT grow when the
    message count grows (the O(N) vs O(1) discriminator) — a smaller history and
    a larger history cost the SAME number of iter_from() calls."""
    monkeypatch.chdir(tmp_path)
    state_log = _CountingStateLog(tmp_path / "state.wal")
    s = _session(tmp_path, state_log)

    async def _turn(i: int) -> int:
        await state_log.append("step_completed")
        s._append_history(ChatMessage(role="user", content=f"turn {i}"))
        return s.history[-1].meta["wal_seq"]

    for i in range(5):
        await _turn(i)
    await checkout(state_log, target_seq=(await _turn(5)))

    state_log.iter_from_calls = 0
    s._history_buffer.build_history()
    small_count = state_log.iter_from_calls

    for i in range(6, 60):
        await _turn(i)

    state_log.iter_from_calls = 0
    s._history_buffer.build_history()
    large_count = state_log.iter_from_calls

    assert small_count == large_count, (
        f"scan count must be independent of message count (small history: "
        f"{small_count} calls, {60 - 6 + 6}x-larger history: {large_count} calls) — "
        "a per-message scan would make large_count >> small_count"
    )
