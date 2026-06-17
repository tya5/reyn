"""Tier 2: OS invariant — AgentRegistry's WAL truncation orchestrator.

The orchestrator gathers ``applied_seq`` from every live session + every
active skill / plan snapshot, computes the universally-absorbed floor,
and rewrites the WAL to drop entries below it. This is the policy layer
on top of ``StateLog.truncate_below`` (the rewrite primitive, tested
separately).

PR-N7 (FP-0008): the floor calculation reads exclusively from in-memory
state (= ``Session.iter_applied_seqs`` via the session's journal +
skill / plan registries), not from disk. Tests therefore register a
duck-typed shim session into ``registry._agents`` and accumulate
watermarks (= the seqs the shim yields) in the shim's seq list. On-disk
profiles are still created so ``list_names`` reflects the right agents
for unrelated paths, but the floor calc itself never reads files.

Tests target the public surface (``truncate_wal_if_eligible``,
``compute_truncate_floor``); observation flows through:
  - the on-disk WAL (re-read after truncation)
  - the returned stats dict

Policy compliance:
- No ``unittest.mock`` usage (Fake > Mock per
  docs/deep-dives/contributing/testing.ja.md)
- No private-state assertion beyond ``_last_truncation_ts`` for throttle
  observation, which has no public surface and is the cleanest probe
  (alternative: time-travel patches, which would be worse)
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.core.events.state_log import StateLog

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _no_factory(_profile):
    """Session factory used by tests that never instantiate a session."""
    raise AssertionError("session factory must not be called in truncation tests")


def _make_registry(tmp_path: Path, *, with_state_log: bool = True) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl") if with_state_log else None
    return AgentRegistry(
        project_root=tmp_path,
        session_factory=_no_factory,
        state_log=state_log,
    )


class _ShimSession:
    """Minimal duck-typed Session exposing only ``iter_applied_seqs``.

    Tests accumulate watermarks via the seed helpers below; the shim
    surfaces them through the same public-surface contract that
    ``AgentRegistry.compute_truncate_floor`` consumes in production
    (= session.iter_applied_seqs returning the union of journal +
    skill + plan watermarks).
    """

    def __init__(self) -> None:
        self._seqs: list[int] = []

    def iter_applied_seqs(self, *, now_ts: float, long_await_threshold: float) -> list[int]:
        return list(self._seqs)


def _get_or_create_shim(registry: AgentRegistry, name: str) -> _ShimSession:
    """Return the shim session for ``name``, creating + registering it
    on first use. Mirrors the on-disk-profile creation pattern so
    ``list_names`` still includes the agent.
    """
    if name not in registry._sessions:
        AgentProfile.new(name, role="").save(registry._dir / name)
        registry._sessions[name] = {"main": _ShimSession()}
    shim = registry._sessions[name]["main"]
    assert isinstance(shim, _ShimSession), (
        f"agent {name!r} is registered with a non-shim object — test fixture bug"
    )
    return shim


def _seed_agent(registry: AgentRegistry, name: str, *, applied_seq: int) -> None:
    """Register the agent's session-level applied_seq watermark.

    PR-N7: pushes the watermark into the shim's seq list. ``applied_seq=0``
    is intentionally a no-op (mirrors the pre-N7 ``dormant skip`` invariant:
    a session whose journal snapshot has applied_seq == 0 has never
    absorbed a WAL event and is excluded from the floor calc).
    """
    shim = _get_or_create_shim(registry, name)
    if applied_seq > 0:
        shim._seqs.append(int(applied_seq))


def _seed_skill(
    registry: AgentRegistry,
    agent_name: str,
    run_id: str,
    *,
    last_phase_applied_seq: int,
) -> None:
    """Register an active-skill watermark for the agent's shim session.

    PR-N7: ``run_id`` is retained in the signature for call-site
    compatibility with the pre-N7 disk-seed helper, but the in-memory
    path doesn't need it — the shim only yields the seq.
    """
    del run_id  # only needed for the pre-N7 disk path
    shim = _get_or_create_shim(registry, agent_name)
    shim._seqs.append(int(last_phase_applied_seq))


def _seed_plan(
    registry: AgentRegistry,
    agent_name: str,
    plan_id: str,
    *,
    last_step_applied_seq: int,
) -> None:
    """Register an active-plan watermark for the agent's shim session.

    PR-N7: ``plan_id`` retained for call-site compatibility; in-memory
    floor calc only needs the seq.
    """
    del plan_id
    shim = _get_or_create_shim(registry, agent_name)
    shim._seqs.append(int(last_step_applied_seq))


def _wal_seqs(state_log: StateLog) -> set[int]:
    return {e["seq"] for e in state_log.iter_from(0) if isinstance(e.get("seq"), int)}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_compute_floor_uses_min_applied_seq_across_agents(tmp_path):
    """Tier 2: floor = min(全 agent applied_seq) + 1 when no active skills exist."""
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=10)
    _seed_agent(registry, "beta", applied_seq=4)
    _seed_agent(registry, "gamma", applied_seq=7)

    floor = registry.compute_truncate_floor()
    assert floor == 5  # min(10, 4, 7) + 1


def test_compute_floor_includes_active_skill_snapshots(tmp_path):
    """Tier 2: an in-flight skill at last_phase_applied_seq=2 forces floor=3 even if all agents are at 10."""
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=10)
    _seed_agent(registry, "beta", applied_seq=10)
    _seed_skill(registry, "alpha", "run_xyz", last_phase_applied_seq=2)

    floor = registry.compute_truncate_floor()
    assert floor == 3  # min(10, 10, 2) + 1


def test_compute_floor_includes_active_plan_snapshots(tmp_path):
    """Tier 2: ADR-0023 §3.1 — plan_snapshot.last_step_applied_seq pins
    the floor, preventing WAL truncation from dropping plan_step_*
    events the resume analyzer needs."""
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=10)
    _seed_plan(registry, "alpha", "p001", last_step_applied_seq=4)

    floor = registry.compute_truncate_floor()
    assert floor == 5  # min(10, 4) + 1


def test_compute_floor_min_across_skill_and_plan(tmp_path):
    """Tier 2: multi-source floor — skill at 6 + plan at 3 + agent at 10
    → floor=4 (= the plan watermark wins). Plans + skills participate
    equally per ADR-0023 §3.1."""
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=10)
    _seed_skill(registry, "alpha", "run_xyz", last_phase_applied_seq=6)
    _seed_plan(registry, "alpha", "p001", last_step_applied_seq=3)

    floor = registry.compute_truncate_floor()
    assert floor == 4  # min(10, 6, 3) + 1


def test_compute_floor_zero_when_no_persistent_agents(tmp_path):
    """Tier 2: dormant agents (snapshot-less) are skipped; with zero persistent agents, floor=0 (no truncation).

    The registry __init__ auto-creates `default` without a snapshot file
    — it's truly dormant (no session ever instantiated this run, no events
    can target it). Excluding it from the floor calc, no agents constrain
    the WAL and floor=0 (treat as 'don't know, don't truncate').
    """
    registry = _make_registry(tmp_path)
    # `default` was auto-created but has no snapshot.json — dormant, skipped.
    floor = registry.compute_truncate_floor()
    assert floor == 0


def test_compute_floor_skips_dormant_agent_with_zero_applied_seq(tmp_path):
    """Tier 2: a snapshot-on-disk with applied_seq=0 (e.g. written by restore_all for an unused agent) is skipped from floor calc.

    Semantically equivalent to "snapshot file missing" — the agent has
    never absorbed a WAL event, so it doesn't constrain truncation. This
    invariant matters because PR21 ``restore_all`` writes baseline
    snapshots for every known agent at restart time.
    """
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=10)
    _seed_agent(registry, "dormant", applied_seq=0)  # restore_all-style baseline

    floor = registry.compute_truncate_floor()
    # Dormant skipped → floor based on alpha alone
    assert floor == 11


def test_truncate_eligible_drops_below_floor_and_returns_stats(tmp_path):
    """Tier 2: truncate_wal_if_eligible drops entries with seq below the computed floor."""
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=8)

    async def go():
        # Append 10 WAL entries
        for i in range(1, 11):
            await registry.state_log.append("inbox_put", target=f"a{i}", payload={})
        return await registry.truncate_wal_if_eligible()

    stats = asyncio.run(go())
    assert stats is not None
    # alpha applied_seq=8 → floor = 9; drop seq 1..8, keep 9, 10
    assert stats["dropped"] == 8
    assert stats["kept"] == 2
    assert _wal_seqs(registry.state_log) == {9, 10}


def test_truncate_throttled_within_window(tmp_path):
    """Tier 2: a second call within `_TRUNCATION_THROTTLE_SECS` returns None without rewriting."""
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=8)

    async def go():
        for i in range(1, 11):
            await registry.state_log.append("inbox_put", target=f"a{i}", payload={})
        first = await registry.truncate_wal_if_eligible()
        # Append more, then immediately re-attempt
        await registry.state_log.append("inbox_put", target="extra", payload={})
        second = await registry.truncate_wal_if_eligible()
        return first, second

    first, second = asyncio.run(go())
    assert first is not None
    assert second is None  # throttled


def test_truncate_no_op_when_no_state_log(tmp_path):
    """Tier 2: registry with state_log=None returns None — defensive for tests / non-chat invocation."""
    registry = _make_registry(tmp_path, with_state_log=False)
    _seed_agent(registry, "alpha", applied_seq=5)

    async def go():
        return await registry.truncate_wal_if_eligible()

    assert asyncio.run(go()) is None


def test_truncate_no_op_when_floor_not_advanced(tmp_path):
    """Tier 2: floor=0 (no live session pinning a watermark) skips the rewrite entirely.

    PR-N7: with no registered shim sessions, ``compute_truncate_floor``
    returns 0, and ``truncate_wal_if_eligible`` returns None (= don't
    truncate, keep WAL intact). Mirrors the conservative fail-closed
    posture the pre-N7 disk-read path took when no watermark was
    available.
    """
    registry = _make_registry(tmp_path)
    # No shim sessions registered → no watermarks → floor = 0.

    async def go():
        await registry.state_log.append("inbox_put", target="a", payload={})
        return await registry.truncate_wal_if_eligible()

    result = asyncio.run(go())
    assert result is None
    # WAL untouched
    assert _wal_seqs(registry.state_log) == {1}


def test_truncate_advances_seqs_across_active_skill_completion(tmp_path):
    """Tier 2: end-to-end progression — active skill phase-advances,
    in-memory watermark updates, next truncation drops the older range.

    PR-N7: the watermark mutation that previously happened via
    ``SkillSnapshot.load + save`` now happens via the shim's seq list
    directly — the production path also mutates SkillRegistry's
    in-memory snapshot when ``advance_phase`` fires, and
    ``iter_applied_phase_seqs`` reads the fresh value.
    """
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=10)
    _seed_skill(registry, "alpha", "run_zzz", last_phase_applied_seq=3)
    shim = registry._sessions["alpha"]["main"]

    async def go():
        for i in range(1, 21):
            await registry.state_log.append("inbox_put", target=f"a{i}", payload={})
        # First truncation: floor = min(10, 3) + 1 = 4 → drop 1..3
        first = await registry.truncate_wal_if_eligible()
        # Skill phase-advances to seq 15 — replace the watermark in the shim
        # (mirrors SkillRegistry.advance_phase mutating its in-memory snapshot).
        shim._seqs = [10, 15]
        # Wait past throttle window
        registry._last_truncation_ts = None
        # Second truncation: floor = min(10, 15) + 1 = 11 → drop 4..10
        second = await registry.truncate_wal_if_eligible()
        return first, second

    first, second = asyncio.run(go())
    assert first["dropped"] == 3
    assert first["kept"] == 17
    assert second["dropped"] == 7
    # Surviving: 11..20
    assert _wal_seqs(registry.state_log) == set(range(11, 21))
