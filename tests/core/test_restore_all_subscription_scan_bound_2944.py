"""Tier 2: #2944 — ``AgentRegistry.restore_all``'s task-subscription replay must not
re-scan the WAL once PER WAL ENTRY (the O(WAL²) cold-start sibling of #2941/#2938).

Root cause: ``restore_all`` called ``self._task_subscriptions.replay(..., is_active=
lambda s: is_active_seq(self._state_log, s))``. ``SubscriptionRegistry.replay`` calls
``is_active(seq)`` once PER WAL ENTRY it iterates; each call re-derives
``_abandoned_intervals(_rewind_records(state_log))`` from scratch, and
``_rewind_records`` does a FULL ``state_log.iter_from(1)`` scan (json-decoding every
WAL line). So an M-entry WAL did O(M²) json.loads work on EVERY cold start (before
the input box appears) — measured 250->0.130s, 500->0.503s, 1000->1.991s,
2000->8.057s (quadratic growth). The fix (``build_active_predicate``, added by #2938
for the sibling turn-loop bug) hoists the seq-independent abandoned-interval
derivation OUT of the per-entry loop: one ``iter_from`` scan for the whole replay,
reused for every entry (O(M) instead of O(M^2)).

Real seam: real ``AgentRegistry`` + real ``StateLog`` + the real ``checkout`` reset-
record primitive, with a ``StateLog`` subclass counting ``iter_from`` calls (a
public-method override, not a private-state peek) to assert the SCAN COUNT bound
directly — mirrors ``tests/test_active_branch_history_scan_bound_2941.py``.

This test must FLIP RED if the per-entry ``is_active_seq`` scan is restored: locally
reverting the ``restore_all`` hoist to
``is_active=lambda s: is_active_seq(self._state_log, s)`` makes the WAL-scan count
grow with WAL size instead of staying bounded (confirmed during development — see the
PR body for the strip-falsification record).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.snapshot_generations import checkout
from reyn.core.events.state_log import StateLog
from reyn.runtime.registry import AgentRegistry


class _CountingStateLog(StateLog):
    """A real ``StateLog`` that counts ``iter_from`` calls — a public-method
    override, not a private-state assertion, so the scan-count bound is verified
    through the same seam production code calls."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.iter_from_calls = 0

    def iter_from(self, min_seq: int):
        self.iter_from_calls += 1
        return super().iter_from(min_seq)


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


def _make_registry(tmp_path: Path, state_log: StateLog) -> AgentRegistry:
    return AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
    )


async def _seed_wal(state_log: StateLog, n: int) -> "list[int]":
    seqs = []
    for i in range(n):
        seqs.append(await state_log.append("step_completed", i=i))
    return seqs


@pytest.mark.asyncio
async def test_restore_all_scan_count_independent_of_wal_size(tmp_path, monkeypatch):
    """Tier 2: restore_all's WAL-scan count does NOT grow when the WAL grows — the
    O(1) vs O(M) discriminator. A smaller WAL and a much larger WAL (both with a
    rewind, so the abandoned-interval predicate is non-trivial) cost the SAME number
    of ``iter_from`` calls."""
    monkeypatch.chdir(tmp_path)

    small_log = _CountingStateLog(tmp_path / "small" / ".reyn" / "wal.jsonl")
    seqs = await _seed_wal(small_log, 30)
    await checkout(small_log, target_seq=seqs[5])
    small_reg = _make_registry(tmp_path / "small", small_log)
    small_log.iter_from_calls = 0
    await small_reg.restore_all()
    small_count = small_log.iter_from_calls

    large_log = _CountingStateLog(tmp_path / "large" / ".reyn" / "wal.jsonl")
    seqs = await _seed_wal(large_log, 600)
    await checkout(large_log, target_seq=seqs[5])
    large_reg = _make_registry(tmp_path / "large", large_log)
    large_log.iter_from_calls = 0
    await large_reg.restore_all()
    large_count = large_log.iter_from_calls

    assert small_count > 0, "sanity: restore_all does scan the WAL at all"
    assert small_count == large_count, (
        f"restore_all's iter_from() call count must be independent of WAL size "
        f"(30-entry WAL: {small_count} calls, 600-entry WAL: {large_count} calls) — "
        "a per-entry is_active_seq re-scan (the #2944 O(WAL^2) cold-start bug) would "
        "make large_count grow proportionally with entry count, not stay equal"
    )


@pytest.mark.asyncio
async def test_restore_all_scan_count_bounded_not_proportional_to_wal_entries(
    tmp_path, monkeypatch,
):
    """Tier 2: the actual bug — restore_all's WAL-scan count stays BOUNDED (a small
    constant), not proportional to the number of WAL entries replayed."""
    monkeypatch.chdir(tmp_path)
    state_log = _CountingStateLog(tmp_path / ".reyn" / "wal.jsonl")
    n = 200
    seqs = await _seed_wal(state_log, n)
    await checkout(state_log, target_seq=seqs[10])  # non-trivial abandoned interval
    reg = _make_registry(tmp_path, state_log)

    state_log.iter_from_calls = 0
    await reg.restore_all()

    assert state_log.iter_from_calls < 20, (
        f"expected a small, WAL-size-independent number of iter_from() calls "
        f"(got {state_log.iter_from_calls} for a {n}-entry WAL) — a per-entry "
        "is_active_seq scan (the #2944 bug) would scale with n, not stay bounded"
    )
