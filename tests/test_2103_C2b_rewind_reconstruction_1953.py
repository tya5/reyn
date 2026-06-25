"""Tier 2: #2103 C2b — the REWIND-across-reuse half (destroy-side mirror of #2168).

The live registry behaviour (identity-keyed `_spawn_lineage`, probes A/B) is covered in
test_2103_C2b_identity_keyed_lineage. This file is the rewind-path mirror (tui's lane,
per that file's docstring): a forward-checkout / rewind reconstruction must rebuild the
identity-keyed lineage from the WAL `agent_created` records so the same staleness logic
holds AFTER reconstruction as it does live — i.e. a name-reused parent must NOT resurrect
an orphan's subtree membership (the forge-guard) on a rewind.

`_materialize_rewind` rebuilds `_agent_create_seq` (name→create_seq, as-of-cut identities)
and `_spawn_lineage` (child→(parent, FROZEN parent_seq)) from the SAME ag_created scan, so
live + rewind agree by construction. The orphan's edge keeps the parent identity AT-SPAWN;
after a purge + name-reuse the reconstructed current identity differs (and the purged name
is excluded from the rebuild) → the edge reads STALE → is_spawn_descendant rejects = no
resurrection. This is the destroy-side mirror of the #2158 session_vanished
reconstruction-symmetry.

Scope: this file falsifies the forge-guard (is_spawn_descendant) rewind path. The CAP
escalation is covered live by probe B (test_2103_C2b_identity_keyed_lineage); on rewind it
is additionally backstopped by the as-of-cut purge-exclusion (a reconstructed purged parent
is not a resolvable referent), so a dedicated rewind-cap test is multiply-backstopped /
not cleanly falsifiable and is intentionally omitted.

Real AgentRegistry + StateLog + on-disk agents (no mocks); _materialize_rewind driven
directly for precise as-of-cut control (the path rewind_to + crash-recovery share).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry


def _registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return AgentRegistry(
        project_root=tmp_path, session_factory=lambda p: None, state_log=state_log
    )


def _seed(tmp_path: Path, name: str, *, profile: str | None = None) -> None:
    AgentProfile.new(name).save(tmp_path / ".reyn" / "agents" / name)


@pytest.mark.asyncio
async def test_rewind_across_name_reuse_does_not_resurrect_orphan(tmp_path):
    """Tier 2: a forward-checkout that reconstructs lineage from the WAL must NOT resurrect
    the orphan under a purged+reused parent name. child was spawned under parent@identity-1;
    parent is purged + the name reused (identity-2). After _materialize_rewind rebuilds the
    lineage, child's edge (frozen at identity-1) reads STALE vs the reused name → it is NOT
    in the reused parent's subtree. RED if the rebuild keyed on name instead of identity."""
    _seed(tmp_path, "parent")
    _seed(tmp_path, "child")
    reg = _registry(tmp_path)
    log = reg.state_log
    # parent@identity-1, child spawned ⊆ parent (edge frozen at parent's create_seq s1).
    s1 = await log.append("agent_created", entity_kind="agent", name="parent", sid="",
                          parent=None, parent_seq=None, profile={"name": "parent", "role": ""})
    await log.append("agent_created", entity_kind="agent", name="child", sid="",
                     parent="parent", parent_seq=s1, profile={"name": "child", "role": ""})
    # parent purged, then the NAME reused → a new identity (s_reuse).
    await log.append("agent_purged", entity_kind="agent", name="parent", sid="")
    await log.append("agent_created", entity_kind="agent", name="parent", sid="",
                     parent=None, parent_seq=None, profile={"name": "parent", "role": ""})

    # forward-checkout past everything → rebuild lineage from the WAL as-of-cut.
    await reg._materialize_rewind(reconstruct_seq=log.current_seq,
                                  workspace_at_or_below=log.current_seq)

    # no resurrection: child's frozen edge does not match the reused parent identity.
    assert not reg.is_spawn_descendant("child", "parent"), (
        "resurrection: orphan child reconstructed INTO the reused parent's subtree "
        "(rewind rebuild must key the edge on frozen identity, not name)"
    )


@pytest.mark.asyncio
async def test_rewind_without_reuse_preserves_subtree_membership(tmp_path):
    """Tier 2: the boundary control — WITHOUT name-reuse, a rewind that reconstructs the
    lineage preserves the child's subtree membership (the edge's frozen identity still
    matches the reconstructed parent identity). Guards against the rebuild over-rejecting
    every reconstructed edge (which would make the resurrection test pass tautologically)."""
    _seed(tmp_path, "parent")
    _seed(tmp_path, "child")
    reg = _registry(tmp_path)
    log = reg.state_log
    s1 = await log.append("agent_created", entity_kind="agent", name="parent", sid="",
                          parent=None, parent_seq=None, profile={"name": "parent", "role": ""})
    await log.append("agent_created", entity_kind="agent", name="child", sid="",
                     parent="parent", parent_seq=s1, profile={"name": "child", "role": ""})

    await reg._materialize_rewind(reconstruct_seq=log.current_seq,
                                  workspace_at_or_below=log.current_seq)

    # preserved: no reuse → frozen edge identity matches reconstructed parent → in subtree.
    assert reg.is_spawn_descendant("child", "parent"), (
        "over-rejection: a reconstructed edge with NO name-reuse lost its subtree membership "
        "(the rebuild must preserve a still-valid frozen identity)"
    )
