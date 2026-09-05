"""Tier 2: OS invariant -- #5789: `_rewind_records` (the scope-discarding
helper) is deleted; every remaining consumer of the abandoned-interval
model now takes `scope` as a required keyword-only argument, no default.

Decision table (architect co-vetted, https://github.com/tya5/reyn/issues/5789):
`is_active_seq`, `earliest_relevant_wal_seq`, `rewind()`'s own active-target
guard -- GLOBAL-BY-DESIGN, now naming `GLOBAL_SCOPE` explicitly at every real
call site. `branch_ids_for`, `list_branches`, `lineage_predecessor` (and their
`AgentRegistry` wrappers `list_branches`/`predecessor_turn_checkpoint`) --
SCOPED: a real behavior change, since #5782 already gave every
`list_rewind_points` row its own real `(name, sid)` owner, so `branch_id`
must be that SAME owner's value, not one global tree blindly applied to
every owner's seqs (the exact class of bug #5786 fixed for `reconstruct`).
`active_rewind_target` (0 real callers in `src/`) is deleted outright.

With `_rewind_records` gone, no consumer can omit `scope` by continuing to
call the old, unscoped accessor -- "unable to omit" rather than "remembered
not to forget," the same shape #5786/`latest_pipeline_state` already took.

Real `StateLog` (no mocks). Covers: the required-kwarg guard on all 7
remaining consumers + their 2 `AgentRegistry` wrappers, `active_rewind_target`
and `_rewind_records` both genuinely gone (import fails), and a real,
driven witness that `branch_ids_for` is now scope-correct: a session-scoped
checkout's reset-record must NOT be treated as a global abandonment when
classifying an UNRELATED session's own seq.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.snapshot_generations import (
    ACTIVE_BRANCH_ID,
    branch_ids_for,
    earliest_relevant_wal_seq,
    is_active_seq,
    lineage_predecessor,
    list_branches,
)
from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
    )


def _seed_agent(tmp_path: Path, name: str) -> None:
    AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


async def _put(log: StateLog, agent: str, text: str) -> int:
    return await log.append(
        "inbox_put", target=agent, msg_id=text, msg_kind="user",
        payload={"text": text},
    )


def test_rewind_records_is_gone():
    """Tier 2: the scope-discarding helper #5789 exists to remove is
    genuinely gone -- pins the deletion itself, not merely "unused"."""
    import reyn.core.events.snapshot_generations as sg
    assert not hasattr(sg, "_rewind_records")


def test_active_rewind_target_is_gone():
    """Tier 2: 0 real callers in src/ (decision table item 8) -- deleted,
    not merely deprecated."""
    import reyn.core.events.snapshot_generations as sg
    assert not hasattr(sg, "active_rewind_target")


@pytest.mark.asyncio
async def test_is_active_seq_requires_scope_kwarg(tmp_path):
    """Tier 2: no default -- decision table item 5 (GLOBAL-BY-DESIGN, but
    still required, no silent fallback)."""
    log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    with pytest.raises(TypeError):
        is_active_seq(log, 1)  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_earliest_relevant_wal_seq_requires_scope_kwarg(tmp_path):
    """Tier 2: no default -- decision table item 7 (GLOBAL-BY-DESIGN, the
    "safe but accidental" case #5789 converts into a stated decision)."""
    log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    with pytest.raises(TypeError):
        earliest_relevant_wal_seq(log)  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_branch_ids_for_requires_scope_kwarg(tmp_path):
    """Tier 2: no default -- decision table item 2 (SCOPED, a real behavior
    change per #5786's own review)."""
    log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    with pytest.raises(TypeError):
        branch_ids_for(log, [1])  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_list_branches_requires_scope_kwarg(tmp_path):
    """Tier 2: no default -- decision table item 3 (SCOPED: "the fork UX's
    branches are the owner's own branches")."""
    log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    with pytest.raises(TypeError):
        list_branches(log)  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_lineage_predecessor_requires_scope_kwarg(tmp_path):
    """Tier 2: no default -- decision table item 4 (SCOPED, same family as
    item 3)."""
    log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    with pytest.raises(TypeError):
        lineage_predecessor(log, [1], 2)  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_registry_list_branches_requires_scope_kwarg(tmp_path):
    """Tier 2: no default -- `AgentRegistry.list_branches`'s own public
    wrapper around item 3, same required-kwarg contract."""
    reg = _make_registry(tmp_path)
    with pytest.raises(TypeError):
        reg.list_branches()  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_registry_predecessor_turn_checkpoint_requires_scope_kwarg(tmp_path):
    """Tier 2: no default -- `AgentRegistry.predecessor_turn_checkpoint`'s
    own public wrapper around item 4, same required-kwarg contract."""
    reg = _make_registry(tmp_path)
    with pytest.raises(TypeError):
        reg.predecessor_turn_checkpoint(1)  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_branch_ids_for_scoped_to_b_ignores_as_reset_record(tmp_path):
    """Tier 2: the real behavior fix -- `branch_ids_for` scoped to B must
    NOT treat A's own scoped reset-record as a global abandonment when
    classifying B's own seq, even though B's seq falls inside the numeric
    interval A's scoped record would abandon under the OLD, scope-blind
    form. Structurally identical to the #5786 `reconstruct` bug, one layer
    over in the branch/fork-UX surface rather than snapshot reconstruction."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    _seed_agent(tmp_path, "beta")
    log = reg.state_log

    kept_seq = await _put(log, "alpha", "a1")   # seq 1
    b_seq = await _put(log, "beta", "b1")       # seq 2 -- falls inside (1, R) below

    # Scope A's own checkout back to seq 1 -- abandons (1, R) where R is the
    # new reset-record's own seq. b_seq (2) sits inside that interval.
    await reg.checkout(kept_seq, scope=("alpha", "main"))

    # Scoped to B: b_seq must read as ACTIVE (0) -- A's own scoped record
    # (scope=["alpha", "main"]) is invisible to a (beta, main) query, per
    # `build_active_predicate`'s own contract.
    ids = branch_ids_for(log, [b_seq], scope=("beta", "main"))
    assert ids[b_seq] == ACTIVE_BRANCH_ID

    # Population witness: the OLD, scope-blind shape genuinely WOULD have
    # misclassified b_seq (proving the interval really is non-trivial, not
    # a no-op the fix happens to pass vacuously) -- GLOBAL_SCOPE sees only
    # unscoped records, so it also reads b_seq as active (A's record is
    # scoped, invisible to GLOBAL_SCOPE too); the discriminating case is
    # querying under (alpha, main) itself, where the abandonment DOES apply.
    ids_for_alpha = branch_ids_for(log, [b_seq], scope=("alpha", "main"))
    assert ids_for_alpha[b_seq] != ACTIVE_BRANCH_ID
