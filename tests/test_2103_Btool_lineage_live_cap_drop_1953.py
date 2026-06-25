"""Tier 2: #2103 B-tool — the spawn-lineage cap must survive LIVE parent removal.

B-core caps a spawned agent at ⊆ parent via the OS-set spawn lineage; B-tool makes that
lineage rewind-durable. The rewind round-trip is covered (test_2103_Btool_lineage_rewind).
This file covers the DESTROY-side mirror on the LIVE (non-rewind) path: removing the
PARENT must NOT silently un-cap a still-live CHILD.

The hazard (tui-coder live-verify, #2160 pre-merge block): ``_spawn_lineage`` is set at
spawn and rebuilt on rewind, but has no LIVE prune — a live ``archive_agent(parent,
purge=True)`` leaves the child's lineage edge in place pointing at an now-ABSENT parent.
``resolved_profile_for`` walks that edge and calls ``resolved_profile_for(parent)``; a
PURGED parent (profile dir gone) resolves to ``None`` → the parent-conjunct is silently
DROPPED → the child resolves un-capped = escalation-via-parent-purge (Decision A defeated).

The boundary is purge-specific: a plain ARCHIVE (purge=False) keeps the parent profile
resolvable, so the child stays capped — the second test is the passing control that pins
the gap to the absent-parent (purge) case the fix must fail-closed.

RED on current #2160 HEAD (purge test: the child un-caps). GREEN after the consumer-side
fail-closed fix: a lineage edge whose parent is ABSENT must DENY (fail-closed),
distinguished from a present-but-unrestricted parent by an existence check.

Real AgentRegistry + StateLog + on-disk agents (no mocks); the production spawn seam
(``create_agent(child, parent=…)``) and the live delete seam (``archive_agent``) drive it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

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


def _child_denied(reg: AgentRegistry, tool: str) -> bool:
    """The observable security property: is ``child`` denied ``tool``? A ``None``
    resolution (no context at all) is NOT denied = the escalation outcome — so this is
    representation-agnostic across the pre-fix (un-capped) and post-fix (fail-closed deny)
    states."""
    ctx, _ = reg.resolved_profile_for("child")
    return ctx is not None and tool_contextually_denied(ctx, tool)


@pytest.mark.asyncio
async def test_live_purge_parent_child_stays_capped(tmp_path):
    """Tier 2: a child spawned ⊆ a parent that denies ``sandboxed_exec`` must STAY denied
    after the parent is LIVE-purged. RED on current HEAD: the un-pruned lineage edge points
    at an absent (purged) parent → the parent-conjunct is dropped → the child resolves
    un-capped (escalation-via-parent-purge). GREEN once the absent-parent edge fails closed."""
    _bind(tmp_path, member="parent", profile="prole", body="name: prole\ntool_deny: [sandboxed_exec]\n")
    _seed(tmp_path, "parent")
    reg = _make_registry(tmp_path)
    # production spawn seam: child created under parent (OS-set lineage, ⊆-parent cap).
    await reg.create_agent("child", parent="parent")
    assert _child_denied(reg, "sandboxed_exec"), "child must be capped ⊆ parent pre-purge"

    await reg.archive_agent("parent", purge=True)  # LIVE purge (NOT a rewind)

    assert _child_denied(reg, "sandboxed_exec"), (
        "escalation-via-parent-purge: child un-capped after parent live-purge "
        "(lineage edge present but purged parent absent → parent-conjunct silently dropped)"
    )


@pytest.mark.asyncio
async def test_live_archive_parent_keeps_child_capped_control(tmp_path):
    """Tier 2: the purge-specific boundary control — a plain ARCHIVE (purge=False) keeps the
    parent profile resolvable, so the child STAYS denied. Passing on current HEAD: this pins
    the gap to the absent-parent (purge) case, so the fail-closed fix targets that and does
    not over-broadly deny on a still-resolvable archived parent."""
    _bind(tmp_path, member="parent", profile="prole", body="name: prole\ntool_deny: [sandboxed_exec]\n")
    _seed(tmp_path, "parent")
    reg = _make_registry(tmp_path)
    await reg.create_agent("child", parent="parent")
    assert _child_denied(reg, "sandboxed_exec"), "child must be capped ⊆ parent pre-archive"

    await reg.archive_agent("parent", purge=False)  # LIVE archive (parent profile persists)

    assert _child_denied(reg, "sandboxed_exec"), (
        "child must stay capped after parent live-archive (parent still resolvable)"
    )
