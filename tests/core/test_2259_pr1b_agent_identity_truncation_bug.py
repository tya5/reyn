"""Tier 2: #2259 PR-1b — agent identity/lineage TRUNCATION bug (RED on main, GREEN under fix).

The SECOND truncation data-loss bug of the #2259 class (sibling of PR-1's config bug), and a
SECURITY bug: rewind rebuilds `_agent_create_seq` + `_spawn_lineage` from the `agent_created`
WAL events (`_agent_lifecycle` is a WAL scan; registry "the WAL is the trusted source"). The
WAL is truncated below floor = min(agent applied_seq), and `agent_created` is NOT exempt — so a
long-lived agent whose `agent_created` fell below the floor loses its lineage edge on rewind.
A dropped lineage edge → `resolved_profile_for` skips the ⊆-parent conjunct → the child runs
**UN-CAPPED** = capability escalation-on-rewind.

The fix mirrors PR-1 (config-as-snapshot): identity/lineage is recorded as a per-agent
truncation-surviving GENERATION (full-state, seq-keyed, prune-KEEPS-BASE), and rewind
reconstructs identity/lineage from the generation — not from the truncatable WAL event.

These tests assert the CORRECT (capped) as-of-rewind cap. RED on current main (the edge is lost
→ un-capped); GREEN once identity/lineage survives truncation as a generation.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.snapshot_generations import rewind
from reyn.core.events.state_log import StateLog
from reyn.runtime.registry import AgentRegistry
from reyn.security.permissions.effective import ContextualPermission, tool_contextually_denied


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


def _registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
    )


def _bind_parent_narrowing(tmp_path: Path, *, parent: str, profile: str, body: str) -> None:
    """Bind ``parent`` to a narrowing capability ``profile`` via a topology (mirrors the
    #2103 B-core cap test's `_bind`), so ``resolved_profile_for(parent)`` imposes a real
    deny that a child ⊆ parent must inherit. The parent's AGENT dir is created by
    ``create_agent`` (not here), so the #2161 parent-existence check sees it present."""
    td = tmp_path / ".reyn" / "topologies"
    td.mkdir(parents=True, exist_ok=True)
    (td / f"{parent}.yaml").write_text(
        f"name: {parent}\nkind: network\nmembers: [{parent}, peer]\n"
        f"profiles:\n  {parent}: {profile}\n",
        encoding="utf-8",
    )
    pd = tmp_path / ".reyn" / "capability_profiles"
    pd.mkdir(parents=True, exist_ok=True)
    (pd / f"{profile}.yaml").write_text(body, encoding="utf-8")


@pytest.mark.asyncio
async def test_child_parent_cap_survives_wal_truncation_of_agent_created(tmp_path):
    """Tier 2: a child spawned ⊆ a narrowing parent must STAY capped after a rewind, even when
    the agents' `agent_created` events were truncated below the floor. RED on main: the lineage
    edge is rebuilt from the truncated WAL → gone → `resolved_profile_for(child)` loses the
    parent-conjunct = UN-capped (security escalation-on-rewind). GREEN: identity/lineage
    survives truncation as a per-agent generation → the ⊆-parent cap is reconstructed."""
    _bind_parent_narrowing(
        tmp_path, parent="parent_a", profile="prole",
        body="name: prole\ntool_deny: [exec]\n",
    )
    reg = _registry(tmp_path)
    log = reg.state_log

    # parent P (narrowed by its topology binding) + child C spawned ⊆ P — via the REAL
    # create_agent seam, so the production identity-capture path is exercised.
    await reg.create_agent("parent_a")
    await reg.create_agent("child_a", parent="parent_a")

    # SANITY (pre-truncation): the child is capped at ⊆ parent (denies P's exec).
    pre, _ = reg.resolved_profile_for("child_a")
    assert isinstance(pre, ContextualPermission)
    assert tool_contextually_denied(pre, "exec"), "pre-rewind child must be ⊆ parent"

    # the agents advance far past their create seqs (filler → the truncation floor climbs).
    for i in range(120):
        await log.append("inbox_put", n=i)

    # GC truncates the WAL below floor 100 → the agents' agent_created@{1,2} are GONE.
    await log.truncate_below(100)
    await log.flush()
    stats = log.last_truncate_stats
    assert stats["dropped"] >= 2, "the early agent_created events should have been truncated"
    # The SAME-boundary generation GC runs too — it must KEEP the identity base below floor
    # (prune-KEEPS-BASE); a drop-all-below GC here would re-introduce the bug.
    await reg._prune_generations_below(100)
    idstore = reg._agent_identity_generation_store()
    assert idstore.latest_at_or_below("parent_a", log.current_seq) is not None, (
        "the parent's identity generation must survive the floor-100 GC (prune-KEEPS-BASE)"
    )

    # forward-checkout / rewind reconstruction → rebuild identity + lineage as-of-cut.
    await reg._materialize_rewind(
        reconstruct_seq=log.current_seq, workspace_at_or_below=log.current_seq,
    )

    # SECURITY dimension (the load-bearing one): the child is STILL capped at ⊆ parent.
    after, _ = reg.resolved_profile_for("child_a")
    assert isinstance(after, ContextualPermission) and tool_contextually_denied(
        after, "exec"
    ), (
        "ESCALATION-ON-REWIND: child runs UN-capped after rewind — the dropped lineage edge "
        "made resolved_profile_for skip the ⊆-parent conjunct (the security bug). The "
        "parent-cap must survive via the truncation-surviving identity generation."
    )
    # data-loss dimension: the lineage edge itself survives the rewind.
    assert reg.is_spawn_descendant("child_a", "parent_a"), (
        "lineage lost on rewind: child's ⊆-parent edge was rebuilt from the truncated "
        "agent_created WAL event (RED on main; GREEN once identity is a generation)"
    )


@pytest.mark.asyncio
async def test_identity_generation_survives_but_post_cut_child_is_undone(tmp_path):
    """Tier 2: the boundary control — a child created AFTER the rewind cut is undone (its cap
    question is moot), guarding against the rebuild blindly resurrecting every generation
    regardless of the cut. Identity generations are as-of-cut (latest seq ≤ cut), like config."""
    _bind_parent_narrowing(
        tmp_path, parent="parent_a", profile="prole",
        body="name: prole\ntool_deny: [exec]\n",
    )
    reg = _registry(tmp_path)
    log = reg.state_log

    await reg.create_agent("parent_a")
    await log.flush()  # #2259 PR-2b: create_agent is async — drain so current_seq + gen are durable
    cut = log.current_seq  # rewind target = just after P exists, before C is spawned
    await reg.create_agent("child_a", parent="parent_a")
    await log.flush()

    # rewind to BEFORE C was spawned → C did not exist as-of-cut → undone (dropped).
    # Production invariant: _materialize_rewind always has an active rewind record.
    # Add one so is_active_seq correctly marks child_a's post-cut create-seq as abandoned.
    R = await rewind(log, target_n=cut)
    await reg._materialize_rewind(reconstruct_seq=R, workspace_at_or_below=cut)

    assert not (tmp_path / ".reyn" / "agents" / "child_a").is_dir(), (
        "a child created AFTER the cut must be undone on rewind (not resurrected from its "
        "post-cut identity generation)"
    )
    assert not reg.is_spawn_descendant("child_a", "parent_a"), "post-cut child has no as-of-cut lineage edge"


def test_identity_store_prune_keeps_base(tmp_path):
    """Tier 1: AgentIdentityGenerationStore.prune_below KEEPS the single highest generation
    below the floor per agent (the truncation-surviving base) and drops the rest — the crux
    carried over from ConfigGenerationStore. Multi-gen per agent is real (purge + name-reuse
    re-records identity at the reuse seq). Drop-all-below would re-introduce the escalation bug."""
    from reyn.core.events.agent_identity_generations import AgentIdentityGenerationStore

    store = AgentIdentityGenerationStore(tmp_path / "gens")
    for seq in (1, 5, 9):  # three identity generations for one re-created name
        store.record("agt", create_seq=seq, spawn_parent=None, spawn_parent_seq=None, seq=seq)

    dropped = store.prune_below(7)  # floor 7 → gens {1,5} are below; keep the base (5), drop 1
    assert dropped == 1
    # The pre-base history (seq 1) is pruned, but the BASE (5, highest < floor) + the
    # at/above-floor gen (9) survive — so identity stays reconstructable at any cut ≥ the base.
    assert store.latest_at_or_below("agt", 4) is None, "below-base history is pruned"
    assert store.latest_at_or_below("agt", 6)[0] == 5, "the base (highest < floor) survives"
    assert store.latest_at_or_below("agt", 10)[0] == 9, "the at/above-floor gen survives"
