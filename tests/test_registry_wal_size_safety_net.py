"""Tier 2: OS invariant — AgentRegistry's WAL size safety net (R-D4).

Background: ``truncate_wal_if_eligible`` is normally called at semantic
boundaries (``skill_phase_advanced`` / ``skill_completed``). Long-idle
skills (one phase running for hours, only LLM activity) or multi-agent
sessions where phase-completion events are rare can let the WAL grow
unboundedly between triggers.

The safety net adds a size-driven trigger: when the WAL file grows
past a threshold (default 1 MB), call ``truncate_wal_if_eligible`` and
bypass the 5-second throttle so even bursty turns can drain a bloated
WAL on the spot.

Verified end-to-end via:
  - real WAL file size on disk (we append synthetic events to inflate it)
  - stats dict returned from the truncate primitive
  - throttle observation (re-call within window must NOT bypass without
    the size flag)

Reference: PR-runtime-wal-size-safety (R-D4) in the active plan.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry

# ---------------------------------------------------------------------------
# Helpers (mirrors test_registry_wal_truncate.py post-PR-N7)
# ---------------------------------------------------------------------------


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in truncation tests")


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return AgentRegistry(
        project_root=tmp_path,
        session_factory=_no_factory,
        state_log=state_log,
    )


class _ShimSession:
    """Minimal duck-typed Session exposing only ``iter_applied_seqs``.

    PR-N7 (FP-0008): the WAL truncation floor reads exclusively from
    in-memory session state. Tests register a shim into ``_agents`` and
    seed the watermarks via ``_seed_agent`` / ``_seed_skill``.
    """

    def __init__(self) -> None:
        self._seqs: list[int] = []

    def iter_applied_seqs(self, *, now_ts: float, long_await_threshold: float) -> list[int]:
        return list(self._seqs)


def _get_or_create_shim(registry: AgentRegistry, name: str) -> _ShimSession:
    if name not in registry._sessions:
        AgentProfile.new(name, role="").save(registry._dir / name)
        registry._sessions[name] = {"main": _ShimSession()}
    shim = registry._sessions[name]["main"]
    assert isinstance(shim, _ShimSession)
    return shim


def _seed_agent(registry: AgentRegistry, name: str, *, applied_seq: int) -> None:
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

    PR-N7: ``run_id`` retained for call-site compatibility; in-memory
    floor calc only needs the seq.
    """
    del run_id
    shim = _get_or_create_shim(registry, agent_name)
    shim._seqs.append(int(last_phase_applied_seq))


async def _inflate_wal(log: StateLog, *, target_bytes: int) -> None:
    """Append events until the WAL file size exceeds ``target_bytes``.

    Each append is a separate ``inbox_put`` (the cheapest event kind)
    so we don't accidentally re-trigger truncation logic.
    """
    seq = 0
    while True:
        await log.append("inbox_put", agent="alpha",
                         message={"text": "x" * 200})
        seq += 1
        if log.path.stat().st_size > target_bytes:
            return


# ---------------------------------------------------------------------------
# Bypass-throttle behavior
# ---------------------------------------------------------------------------


def test_truncate_wal_bypasses_throttle_when_flag_set(tmp_path: Path):
    """Tier 2: bypass_throttle=True ignores the 5s throttle gate.

    Pre-stamp the throttle to "just now" so the second call would
    normally be skipped; verify bypass=True still proceeds.
    """
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=100)
    _seed_agent(registry, "beta", applied_seq=120)

    async def go():
        # First call: succeeds, stamps throttle
        first = await registry.truncate_wal_if_eligible()
        assert first is not None
        ts1 = registry.last_truncation_ts
        assert ts1 is not None

        # Second call WITHOUT bypass: throttled (skipped)
        second = await registry.truncate_wal_if_eligible()
        assert second is None
        assert registry.last_truncation_ts == ts1  # unchanged

        # Third call WITH bypass: proceeds despite throttle window
        third = await registry.truncate_wal_if_eligible(bypass_throttle=True)
        assert third is not None
        assert registry.last_truncation_ts is not None
        assert registry.last_truncation_ts >= ts1

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Size safety net method
# ---------------------------------------------------------------------------


def test_maybe_truncate_for_size_skips_when_wal_small(tmp_path: Path):
    """Tier 2: ``maybe_truncate_for_size`` is a no-op when WAL < threshold.

    Avoids wasted rewrites on small WALs (rewrite cost is real even
    when nothing drops).
    """
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=10)

    async def go():
        # WAL is essentially empty — well below threshold
        result = await registry.maybe_truncate_for_size(threshold_bytes=1_000_000)
        assert result is None

    asyncio.run(go())


def test_maybe_truncate_for_size_fires_when_wal_large(tmp_path: Path):
    """Tier 2: WAL > threshold → truncate fires even without phase events.

    Inflates the WAL with synthetic events, then triggers the size
    safety net. The truncate stats dict is returned (= it ran).
    """
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=10_000)

    async def go():
        # Inflate WAL above the small threshold (50 KB to keep test fast)
        await _inflate_wal(registry._state_log, target_bytes=51_200)
        result = await registry.maybe_truncate_for_size(threshold_bytes=50_000)
        assert result is not None, (
            "WAL size > threshold must trigger truncate"
        )
        # The stats dict should have a 'kept' key (== StateLog.truncate_below contract)
        assert isinstance(result, dict)

    asyncio.run(go())


def test_maybe_truncate_for_size_bypasses_throttle(tmp_path: Path):
    """Tier 2: size-triggered call ignores the throttle.

    Throttle is meaningless for size-triggered: WAL is bloated NOW,
    waiting another 5s changes nothing.
    """
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=10_000)

    async def go():
        # Burn the throttle window
        await registry.truncate_wal_if_eligible()
        ts1 = registry.last_truncation_ts
        # Inflate
        await _inflate_wal(registry._state_log, target_bytes=51_200)
        # Size-triggered call should proceed even within throttle window
        result = await registry.maybe_truncate_for_size(threshold_bytes=50_000)
        assert result is not None
        assert registry.last_truncation_ts is not None
        assert registry.last_truncation_ts >= ts1

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Active-skill-event protection (regression check)
# ---------------------------------------------------------------------------


def test_size_safety_net_protects_active_skill_events(tmp_path: Path):
    """Tier 2: size-triggered truncate respects the active skill floor.

    Given an active skill at last_phase_applied_seq=50 and a WAL inflated
    above the threshold, the truncation must drop events below seq 51
    while keeping events >= 51 (= active skill events protected).
    """
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=10_000)
    # Active skill pulls the floor down to last_phase_applied_seq + 1.
    # Pick a small value so our inflated WAL straddles it.
    _seed_skill(registry, "alpha", "run_active", last_phase_applied_seq=50)

    async def go():
        await _inflate_wal(registry._state_log, target_bytes=51_200)
        # Size-triggered truncate
        result = await registry.maybe_truncate_for_size(threshold_bytes=50_000)
        assert result is not None, "expected size-triggered truncate to run"
        # Floor = min(10_000, 50) + 1 = 51. Events with seq < 51 dropped,
        # seq >= 51 kept (active skill protection).
        events = list(registry._state_log.iter_from(0))
        kept_seqs = [e["seq"] for e in events]
        assert kept_seqs, "expected some events to be kept"
        assert min(kept_seqs) >= 51, (
            f"active skill floor (51) violated; min kept seq = {min(kept_seqs)}"
        )
        # And events below the floor really were dropped (= we inflated way more
        # than 50 entries, so dropping below 51 must have removed dozens).
        assert result["dropped"] > 0, (
            f"expected drops below floor; stats={result}"
        )

    asyncio.run(go())
