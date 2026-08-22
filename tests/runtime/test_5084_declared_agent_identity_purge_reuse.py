"""Tier 2: #5084 — a DECLARED agent (a hand-written ``profile.yaml``, never
routed through ``create_agent``) used as a spawn ``parent`` must still be
identity-tracked, so a LATER purge + same-name re-declare of it is caught —
closing 2 independent escalation paths measured live, no rewind needed
(tui-coder): ``resolved_profile_for``'s ⊆-parent cap, and
``is_spawn_descendant``'s C1 forge-guard.

Root cause: ``_agent_create_seq`` (the pre-#5084 frozen-identity source) is
populated ONLY by ``create_agent`` — a declared parent never gets an entry,
so the frozen edge always read a bare ``None`` ("no staleness signal, honour
the link" — the correct answer for a parent's first-ever appearance, but the
SAME answer after a purge + re-declare produced a genuinely DIFFERENT
identity). Measured: both consumers stayed on the OLD (narrower) parent's
answer after the parent was purged and re-declared with a wider/absent
topology binding.

Fix (architect ruling — see ``AgentRegistry.agent_directory_identity``'s own
docstring for the full reasoning): freeze/compare the parent AGENT
DIRECTORY's own ``(ino, st_birthtime)``, re-stat'd fresh at comparison
time. A content-only edit (e.g. a live role edit, ``/agent edit role``)
never changes the directory's own identity — only ``rmtree`` + recreate
does — so a routine, legitimate edit does not count as a new identity (see
``test_2103_B_agent_spawn_lineage_cap_1953.py::test_b_narrow_parent_after_
spawn_recaps_live``, witness ⑤', which stays green through this fix). A
directory's own inode CAN be reused after ``rmtree``, so
``AgentRegistry.remove(purge=True)`` ACTIVELY invalidates any spawn-lineage
edge naming the purged agent as parent (stamping an impossible sentinel,
``INVALIDATED_SPAWN_PARENT_IDENTITY``) as the backstop for that.

Real ``AgentRegistry`` + on-disk topology/profile YAML (no mocks).
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.security.permissions.effective import ContextualPermission, tool_contextually_denied


def _registry(tmp_path: Path) -> AgentRegistry:
    return AgentRegistry(project_root=tmp_path, session_factory=lambda profile: None)


def _declare_narrowed_parent(tmp_path: Path, *, name: str, profile: str, deny: str) -> None:
    """A DECLARED agent (profile.yaml written directly — never ``create_agent``'d)
    bound via topology to a narrowing capability profile, mirroring
    ``test_2103_B_agent_spawn_lineage_cap_1953.py``'s own ``_bind`` helper but
    for the "never went through create_agent" scenario #5084 is about."""
    td = tmp_path / ".reyn" / "topologies"
    td.mkdir(parents=True, exist_ok=True)
    (td / f"{name}.yaml").write_text(
        f"name: {name}\nkind: network\nmembers: [{name}, peer]\n"
        f"profiles:\n  {name}: {profile}\n",
        encoding="utf-8",
    )
    pd = tmp_path / ".reyn" / "capability_profiles"
    pd.mkdir(parents=True, exist_ok=True)
    (pd / f"{profile}.yaml").write_text(f"name: {profile}\ntool_deny: [{deny}]\n", encoding="utf-8")
    AgentProfile.new(name).save(tmp_path / ".reyn" / "agents" / name)


def _declare_unrestricted(tmp_path: Path, *, name: str) -> None:
    """A DECLARED agent with no topology binding at all — present but
    unrestricted, the "wider replacement" half of the escalation repro."""
    # Remove any prior binding for this name so the re-declare is genuinely
    # unrestricted, not a stale binding from a previous identity.
    topo = tmp_path / ".reyn" / "topologies" / f"{name}.yaml"
    if topo.is_file():
        topo.unlink()
    AgentProfile.new(name).save(tmp_path / ".reyn" / "agents" / name)


@pytest.mark.asyncio
async def test_purge_then_redeclare_via_api_rejects_stale_child_cap(tmp_path: Path) -> None:
    """Tier 2: #5084 witness ⑥ — the API path. ``remove(purge=True)`` then a
    same-name re-declare must be caught: the child's ⊆-parent cap fails
    CLOSED (never silently adopts the new parent's wider/absent
    restriction) and the C1 forge-guard rejects the child as a descendant
    of the (different) new parent.

    This is the MEASURED escalation repro (tui-coder, live, no rewind
    needed) run to its conclusion: pre-#5084 both assertions below failed."""
    _declare_narrowed_parent(tmp_path, name="parent_a", profile="prole", deny="exec")
    reg = _registry(tmp_path)
    await reg.create_agent("child_a", parent="parent_a")

    pre, _ = reg.resolved_profile_for("child_a")
    assert isinstance(pre, ContextualPermission)
    assert tool_contextually_denied(pre, "exec"), "sanity: child starts out capped"
    assert reg.is_spawn_descendant("child_a", "parent_a")

    reg.remove("parent_a", purge=True)
    # #5084 witness: the ACTIVE-invalidation mechanism itself, via the
    # PUBLIC read `frozen_spawn_parent_identity` — checked BEFORE the
    # re-declare below, so this specifically pins "remove(purge=True)
    # invalidates immediately" rather than "the re-declare happened to get
    # a different stat" (the latter is what the end-to-end assertions
    # further down would also pass under, even without this mechanism).
    assert reg.frozen_spawn_parent_identity("child_a") == (
        AgentRegistry.INVALIDATED_SPAWN_PARENT_IDENTITY
    ), (
        "remove(purge=True) must actively invalidate the edge immediately, "
        "not rely solely on a later stat happening to differ"
    )
    _declare_unrestricted(tmp_path, name="parent_a")  # same name, different identity

    after, _ = reg.resolved_profile_for("child_a")
    # FAIL CLOSED: must NOT silently inherit the new (unrestricted) parent's
    # profile — the restrictive floor is composed instead, so exec is STILL
    # denied (via the floor, not via the old parent's now-gone binding).
    assert isinstance(after, ContextualPermission)
    assert tool_contextually_denied(after, "exec"), (
        "ESCALATION: child adopted the NEW (unrestricted) same-named parent's "
        "profile instead of failing closed — the purge+reuse went undetected"
    )
    assert not reg.is_spawn_descendant("child_a", "parent_a"), (
        "FORGE-GUARD BYPASS: child still reads as a descendant of a parent that "
        "purged+reused the same name — a different identity, not a real ancestor"
    )


@pytest.mark.asyncio
async def test_manual_rmtree_then_redeclare_rejects_stale_child_cap(tmp_path: Path) -> None:
    """Tier 2: #5084 witness ⑦ — the FILESYSTEM path (bypassing reyn's own
    ``remove()`` entirely — an operator/config apply deleting
    ``.reyn/agents/<parent>/`` directly). Caught by the comparison-time
    directory stat alone (no active-invalidation backstop reaches this path,
    since reyn's own API was never called).

    ⚠️ Weaker than ⑥ on a platform where ``os.stat`` does not expose
    ``st_birthtime`` (most Linux filesystems via plain ``stat()``) — the
    comparison then relies on ``ino`` alone, and an inode number CAN in
    principle be reused by the OS for the new directory. Not asserted here
    as a guaranteed-forever property on every platform; disclosed rather
    than silently assumed (architect co-vet, issuecomment-5380635009)."""
    _declare_narrowed_parent(tmp_path, name="parent_b", profile="prole_b", deny="exec")
    reg = _registry(tmp_path)
    await reg.create_agent("child_b", parent="parent_b")

    pre, _ = reg.resolved_profile_for("child_b")
    assert tool_contextually_denied(pre, "exec")

    shutil.rmtree(tmp_path / ".reyn" / "agents" / "parent_b")  # bypasses remove()
    _declare_unrestricted(tmp_path, name="parent_b")

    after, _ = reg.resolved_profile_for("child_b")
    assert isinstance(after, ContextualPermission)
    assert tool_contextually_denied(after, "exec"), (
        "ESCALATION via a manual rmtree+redeclare (outside reyn's own API) — "
        "the comparison-time directory stat did not catch the identity change"
    )
    assert not reg.is_spawn_descendant("child_b", "parent_b")


@pytest.mark.asyncio
async def test_strip_falsifier_reverting_to_the_profile_file_stat_breaks_live_recap(
    tmp_path: Path,
) -> None:
    """Tier 2: #5084 witness ⑤ (negative control) — pins the SPECIFIC prior
    design mistake (file-stat instead of directory-stat) so it cannot
    silently regress back in.

    ``AgentProfile.save()`` currently writes ``profile.yaml`` IN PLACE
    (``path.write_text(...)``, no temp+rename) — so today, a plain role
    edit does not even change the FILE's own ``(ino, st_birthtime)``
    either, and a role-edit-only repro can't distinguish "stat the file"
    from "stat the directory" (confirmed by hand: reverting
    ``agent_directory_identity`` to the file path and re-running JUST a role-edit
    scenario leaves it green — not a real discriminator for THAT specific
    repro, which is why this test does not use one). The REAL reason to
    prefer the directory is forward-looking robustness: if ``save()`` is
    ever made atomic (a temp-file + ``os.replace``, the pattern this
    codebase uses everywhere ELSE for a durable write), THAT would mint a
    NEW inode for
    ``profile.yaml`` on every save while leaving the agent DIRECTORY's own
    identity untouched — reproduced directly here rather than waiting for
    ``save()`` to actually change (the file-stat design's exact failure
    mode, simulated without needing ``save()`` itself to be atomic yet)."""
    agent_dir = tmp_path / ".reyn" / "agents" / "parent_c"
    AgentProfile.new("parent_c").save(agent_dir)
    reg = _registry(tmp_path)

    dir_identity_before = reg.agent_directory_identity("parent_c")
    assert dir_identity_before is not None
    profile_path = agent_dir / "profile.yaml"
    file_ino_before = profile_path.stat().st_ino

    # Simulate an ATOMIC replace of profile.yaml ALONE (temp file + os.replace
    # — the pattern the rest of this codebase already uses for durable
    # writes, e.g. AgentIdentityGenerationStore.record) — mints a NEW inode
    # for the file while the agent DIRECTORY itself is never touched.
    tmp_profile = profile_path.with_suffix(".yaml.tmp")
    tmp_profile.write_text(profile_path.read_text(encoding="utf-8"), encoding="utf-8")
    tmp_profile.replace(profile_path)

    file_ino_after = profile_path.stat().st_ino
    assert file_ino_after != file_ino_before, (
        "sanity: the simulated atomic replace must actually mint a new file inode "
        "for this test to demonstrate anything"
    )
    dir_identity_after = reg.agent_directory_identity("parent_c")
    assert dir_identity_after == dir_identity_before, (
        "the agent DIRECTORY's own identity must survive an atomic replace of "
        "just profile.yaml — this is exactly what a file-stat design would get "
        "wrong the moment AgentProfile.save() becomes atomic (it isn't yet, "
        "which is why this test simulates the replace directly rather than "
        "going through save() itself)"
    )
