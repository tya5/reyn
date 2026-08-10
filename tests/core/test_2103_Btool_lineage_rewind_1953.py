"""Tier 2: #2103 B-tool — the spawn lineage survives a rewind round-trip (the security linchpin).

B-core caps a spawned agent at ⊆ parent via the OS-set spawn lineage. B-tool carries the
lineage on the ``agent_created`` WAL event and REBUILDS it as-of-cut during
rewind-reconstruction. The hazard this guards: if a re-materialised child lost its
lineage, resolved_profile_for would skip the parent-conjunct → the child resolves
UN-capped → escalation-on-rewind. So the round-trip MUST preserve the cap.

Real AgentRegistry + StateLog + on-disk agents (no mocks); _materialize_rewind driven
directly for precise as-of-cut control (the path rewind_to + crash-recovery share).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.snapshot_generations import checkout, rewind
from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.security.permissions.effective import tool_contextually_denied


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return AgentRegistry(project_root=tmp_path, session_factory=lambda p: None, state_log=state_log)


def _seed(tmp_path: Path, name: str) -> None:
    AgentProfile.new(name).save(tmp_path / ".reyn" / "agents" / name)


def _bind(tmp_path: Path, *, member: str, profile: str, body: str) -> None:
    td = tmp_path / ".reyn" / "topologies"
    td.mkdir(parents=True, exist_ok=True)
    (td / f"{member}.yaml").write_text(
        f"name: {member}\nkind: network\nmembers: [{member}, peer]\nprofiles:\n  {member}: {profile}\n",
        encoding="utf-8",
    )
    pd = tmp_path / ".reyn" / "capability_profiles"
    pd.mkdir(parents=True, exist_ok=True)
    (pd / f"{profile}.yaml").write_text(body, encoding="utf-8")


def _agent_dir(tmp_path: Path, name: str) -> Path:
    return tmp_path / ".reyn" / "agents" / name


@pytest.mark.asyncio
async def test_lineage_survives_rewind_round_trip_child_stays_capped(tmp_path):
    """Tier 2: the LINCHPIN — spawn child C under parent P → drop C (rewind before its
    create) → forward-checkout past the create → C is re-materialised AND its lineage is
    rebuilt as-of-cut, so C is STILL ⊆ P (the parent's deny still applies). RED if the
    as-of-cut lineage rebuild is absent (the re-materialised C would resolve un-capped =
    escalation-on-rewind)."""
    _bind(tmp_path, member="P", profile="prole", body="name: prole\ntool_deny: [exec]\n")
    _seed(tmp_path, "P")
    _seed(tmp_path, "C")
    reg = _make_registry(tmp_path)
    log = reg.state_log
    # C is spawned under P: its agent_created carries the parent lineage (the WAL carry).
    cseq = await log.append("agent_created", entity_kind="agent", name="C", sid="",
                            parent="P", profile={"name": "C", "role": ""})
    # Rewind to BEFORE C's create: C's seq lands in the abandoned interval (cseq-1, R1).
    R1 = await rewind(log, target_n=cseq - 1)   # makes is_active_seq(cseq)=False

    # drop C (simulate a prior-cut drop), then rewind to BEFORE C's create → stays gone.
    reg._drop_agent("C")
    await reg._materialize_rewind(reconstruct_seq=R1, workspace_at_or_below=cseq - 1)
    assert not _agent_dir(tmp_path, "C").exists()  # C didn't exist as-of-cut

    # forward-checkout PAST C's create: Phase-2 checkout since cseq is now abandoned.
    # checkout subsumes R1 (R1 falls in (cseq, R2)), leaving C's seq active again.
    R2 = await checkout(log, target_seq=cseq)    # new active target; R1 subsumed
    await reg._materialize_rewind(reconstruct_seq=R2, workspace_at_or_below=cseq)
    assert _agent_dir(tmp_path, "C").is_dir()  # re-materialised
    contextual, _ = reg.resolved_profile_for("C")
    assert contextual is not None
    assert tool_contextually_denied(contextual, "exec")  # STILL capped at parent


@pytest.mark.asyncio
async def test_rewound_out_child_is_dropped_parent_survives(tmp_path):
    """Tier 2: a child created AFTER the cut is dropped by the rewind (gone from the
    public list_names); the as-of-cut lineage rebuild excludes it by construction (a
    full rebuild over present-as-of-cut agents → no stale edge). The parent P, present
    as-of-cut, survives and remains resolvable."""
    _bind(tmp_path, member="P", profile="prole", body="name: prole\ntool_deny: [exec]\n")
    _seed(tmp_path, "P")
    _seed(tmp_path, "C")
    reg = _make_registry(tmp_path)
    log = reg.state_log
    cseq = await log.append("agent_created", entity_kind="agent", name="C", sid="",
                            parent="P", profile={"name": "C", "role": ""})
    # Rewind to BEFORE C's create: puts C's seq in the abandoned interval (cseq-1, R).
    R = await rewind(log, target_n=cseq - 1)     # makes is_active_seq(cseq)=False

    # rewind to BEFORE C's create → C dropped (the rebuild excludes the absent child).
    await reg._materialize_rewind(reconstruct_seq=R, workspace_at_or_below=cseq - 1)
    names = reg.list_names()
    assert "C" not in names      # the post-cut child is gone (no stale lineage to resolve)
    assert "P" in names          # the parent (present as-of-cut) survives
