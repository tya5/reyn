"""Tier 2: OS invariant — compute_truncate_floor reads in-memory state only.

PR-N7 (FP-0008): the floor calculation was rewritten to read exclusively
from in-memory session state (= ``ChatSession.iter_applied_seqs`` via the
session's journal snapshot + skill_registry + plan_registry public
methods). The pre-N7 implementation walked snapshot files on disk inside
the async ``truncate_wal_if_eligible`` caller — sync I/O in an async
event loop, which was the root cause of the 13-hour hang observed in the
PR-N5 13236 single-instance pilot.

Tests assert via the public surface (``compute_truncate_floor``,
``truncate_wal_if_eligible``) — and the observable file system — that:

1. **File I/O occurrence is zero** during ``compute_truncate_floor``.
   Verified by deleting the ``.reyn/agents/<name>/state/`` directory
   before computing the floor; with the disk-read path this would
   silently skip the agent (or raise) — with the in-memory path the
   floor still reflects the registered session's watermarks because no
   file is read.

2. **Large N completes quickly**. With 100 registered shim sessions
   the call returns in under a second (we don't pin a tighter time
   budget to avoid flake; the prior disk-path would scan O(N) files
   even on a fast disk and could easily exceed this with real I/O
   contention).

3. **Correctness preserved** — the returned floor equals
   ``min(all yielded applied_seqs) + 1`` across every registered shim.

4. **Dormant sessions skipped** — a shim whose
   ``iter_applied_seqs`` returns ``[]`` does not pin the floor.

5. **Empty registry returns 0** — no live session, no watermark, no
   truncation.

No mocks. Real ``AgentRegistry`` with shim sessions duck-typed onto its
``_agents`` dict; shims expose ``iter_applied_seqs(*, now_ts,
long_await_threshold)`` which is the entire public-surface contract that
``compute_truncate_floor`` depends on.
"""
from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.core.events.state_log import StateLog

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in truncation tests")


def _make_registry(tmp_path: Path, *, with_state_log: bool = True) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl") if with_state_log else None
    return AgentRegistry(
        project_root=tmp_path,
        session_factory=_no_factory,
        state_log=state_log,
    )


class _ShimSession:
    """Minimal duck-typed ChatSession exposing only ``iter_applied_seqs``.

    The public-surface contract that compute_truncate_floor depends on
    is exactly one method. Tests construct shims with the desired
    watermarks; no ChatSession boot path needed.
    """

    def __init__(self, seqs: list[int]) -> None:
        self._seqs = list(seqs)
        self.iter_calls = 0

    def iter_applied_seqs(self, *, now_ts: float, long_await_threshold: float) -> list[int]:
        self.iter_calls += 1
        return list(self._seqs)


def _register_shim(registry: AgentRegistry, name: str, seqs: list[int]) -> _ShimSession:
    """Register a shim session into the registry's in-memory map.

    Also creates the on-disk profile so ``list_names`` returns the name
    (some unrelated paths rely on that), but never seeds a snapshot
    file — the floor calc must succeed without it.
    """
    AgentProfile.new(name, role="").save(registry._dir / name)
    shim = _ShimSession(seqs)
    registry._sessions[name] = {"main": shim}
    return shim


# ---------------------------------------------------------------------------
# Invariant 1 — File I/O occurrence is zero during compute_truncate_floor
# ---------------------------------------------------------------------------


def test_compute_truncate_floor_reads_no_disk_state(tmp_path):
    """Tier 2: floor is computed from in-memory shims even when on-disk
    snapshot directories are absent / deleted.
    """
    registry = _make_registry(tmp_path)
    _register_shim(registry, "alpha", [10])
    _register_shim(registry, "beta", [5])

    # Eradicate any on-disk state that the pre-N7 disk-read path would
    # have walked. compute_truncate_floor must not depend on these files.
    for name in ("alpha", "beta"):
        state_dir = registry._dir / name / "state"
        if state_dir.exists():
            shutil.rmtree(state_dir)

    floor = registry.compute_truncate_floor()
    assert floor == 6, "floor must equal min(in-memory seqs) + 1 = min(10,5)+1 = 6"


# ---------------------------------------------------------------------------
# Invariant 2 — Large N completes quickly (in-memory iteration, not disk)
# ---------------------------------------------------------------------------


def test_compute_truncate_floor_large_n_completes_quickly(tmp_path):
    """Tier 2: 100 registered shim sessions → floor returns in under 1s.

    We don't pin a tighter budget to avoid CI flake on noisy runners.
    The point is "no per-agent disk read" not a precise wall-clock.
    """
    N = 100
    registry = _make_registry(tmp_path)
    for i in range(1, N + 1):
        _register_shim(registry, f"agent_{i:03d}", [i])

    t0 = time.monotonic()
    floor = registry.compute_truncate_floor()
    elapsed = time.monotonic() - t0

    assert floor == 2, "floor must equal min(1..100) + 1 = 2"
    assert elapsed < 1.0, (
        f"compute_truncate_floor with N=100 took {elapsed:.3f}s; "
        "in-memory iteration should complete in tens of ms"
    )


# ---------------------------------------------------------------------------
# Invariant 3 — Correctness preserved: returned floor = min(all yielded) + 1
# ---------------------------------------------------------------------------


def test_compute_truncate_floor_min_of_all_yielded_seqs(tmp_path):
    """Tier 2: each shim's yielded seqs combine; floor is min(all) + 1."""
    registry = _make_registry(tmp_path)
    _register_shim(registry, "alpha", [8, 3])     # session.applied_seq + 1 skill phase
    _register_shim(registry, "beta", [5])         # session.applied_seq alone
    _register_shim(registry, "gamma", [12, 7, 9]) # session + 2 plan steps

    floor = registry.compute_truncate_floor()
    assert floor == 4, "floor must equal min(3, 5, 7) + 1 = 4 (min watermark = 3)"


# ---------------------------------------------------------------------------
# Invariant 4 — Dormant session (no yielded seqs) does not pin the floor
# ---------------------------------------------------------------------------


def test_dormant_session_excluded_from_floor(tmp_path):
    """Tier 2: a shim whose iter_applied_seqs returns [] is excluded.

    Matches the pre-N7 behaviour where an agent with applied_seq == 0
    was skipped from the floor calc.
    """
    registry = _make_registry(tmp_path)
    _register_shim(registry, "alpha", [10])
    _register_shim(registry, "dormant", [])  # no watermarks

    floor = registry.compute_truncate_floor()
    assert floor == 11, "dormant session does not pin floor; alpha's 10 + 1 = 11"


# ---------------------------------------------------------------------------
# Invariant 5 — Empty registry returns 0
# ---------------------------------------------------------------------------


def test_empty_registry_returns_zero(tmp_path):
    """Tier 2: registry with no live sessions returns 0 (= no truncation)."""
    registry = _make_registry(tmp_path)
    assert registry.compute_truncate_floor() == 0


# ---------------------------------------------------------------------------
# Invariant 6 — Truncate eligibility chain delivers the in-memory floor
# ---------------------------------------------------------------------------


def test_truncate_wal_if_eligible_uses_in_memory_floor(tmp_path):
    """Tier 2: truncate_wal_if_eligible drops entries below the in-memory floor.

    End-to-end: register shims → append WAL entries → fire the async
    eligibility path → observe the dropped/kept partition. No on-disk
    snapshot files involved.
    """
    registry = _make_registry(tmp_path)
    _register_shim(registry, "alpha", [8])  # session applied_seq = 8 → floor = 9

    async def go():
        for i in range(1, 12):  # seqs 1..11
            await registry.state_log.append("inbox_put", target=f"a{i}", payload={})
        return await registry.truncate_wal_if_eligible()

    stats = asyncio.run(go())
    assert stats is not None, "truncate must fire when floor > 1"
    # floor = 9 → keep seqs 9..11 (3 entries), drop 1..8 (8 entries)
    assert stats["dropped"] == 8
    assert stats["kept"] == 3
