"""Tier 2: #5080 — the agent-layer ``base_dir`` WRITE side, complementary to
#4200's own READ side (``test_4200_session_base_dir_resolution.py``).

#4200 taught ``Session._workspace_base_dir`` to read an agent-layer
override — a read path nothing wrote to (architect's own measurement,
#5080 issue thread). #5080 gives ``registry.create``/``create_agent`` (the
ONE creation seam every surface — CLI / web / slash / the ``spawn_agent``
LLM tool — routes through) an optional ``base_dir`` parameter that writes
it, validated ⊆ the project workspace (#4206's own axis ①, restrict-only,
applied to a "file zone").

Store: THIS agent's own ``profile.yaml`` (``.reyn/agents/<name>/``) — NOT
``.reyn/capability_profiles/<X>.yaml`` (architect BLOCK, 2nd round,
#5081): that directory's ``<X>`` is keyed by PROFILE name, a free string a
topology's ``profiles: {member: profile_name}`` binding writes with no
uniqueness constraint against agent names (``profiles: {alice: alice}`` is
a real, unconstrained possibility -- no uniqueness constraint rules it out) -- writing base_dir there would silently
collide with an unrelated narrowing template bound to a same-named
profile. ``profile.yaml`` is keyed by AGENT identity, so the collision is
structurally impossible, not merely mitigated.

Bound: reject a request outside the project workspace, never clamp it in,
name the boundary — the SAME shape ``spawn_session``'s own ``base_dir``
argument already uses for the session layer, just bounded by the project
workspace here rather than a spawner's own effective base_dir. Protected
at USE (``Session._workspace_base_dir``'s own read), not only at WRITE
(``registry.create``) — architect BLOCK, 1st round: ``.reyn`` is the
agent's default write zone, so the store is directly agent-writable
through the ordinary file-write op, bypassing ``create`` entirely.

Real ``AgentRegistry``/``Session`` construction throughout — no mocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.runtime.registry import AgentRegistry
from tests._support.agent_session import make_session


def _no_factory(profile):
    raise RuntimeError("session factory not used in these write-side tests")


def _profile_path(project_root: Path, name: str) -> Path:
    return project_root / ".reyn" / "agents" / name / "profile.yaml"


def _resolved_op_context_base_dir(session) -> "Path | None":
    """The public-surface read (mirrors ``test_4200_session_base_dir_
    resolution.py``'s own helper, same rationale): build a REAL chat-router
    OpContext through the same object every op handler is actually handed
    and read the resolved ``Workspace.base_dir`` off it, rather than
    asserting on ``Session._workspace_base_dir`` directly (a private
    attribute — CLAUDE.md's testing policy forbids that)."""
    return session._router_op_context_source.build().workspace.base_dir


# ── witness ① — a base_dir-equipped agent's session sees it ────────────────


@pytest.mark.asyncio
async def test_agent_created_with_base_dir_resolves_it_via_the_session(
    tmp_path: Path,
) -> None:
    """Tier 2: ``registry.create_agent(name, base_dir=X)`` writes this
    agent's own ``profile.yaml``'s ``base_dir:`` key, and a REAL session
    for that agent resolves it through its real op-context (the
    public-surface value an op handler actually observes) — reading the
    actual value, not just "no exception was raised" (architect's own
    explicit condition on this witness)."""
    project_root = tmp_path / "project"
    target_dir = project_root / "workers" / "alpha"

    reg = AgentRegistry(project_root=project_root, session_factory=_no_factory)
    await reg.create_agent("alpha", base_dir=str(target_dir))

    profile_path = _profile_path(project_root, "alpha")
    assert profile_path.is_file() and "base_dir:" in profile_path.read_text(
        encoding="utf-8",
    ), "no base_dir: key was written to alpha's own profile.yaml"

    session = make_session(
        agent_name="alpha", workspace_state_dir=project_root / ".reyn",
        snapshot_path=(
            project_root / ".reyn" / "agents" / "alpha" / "state" / "snapshot.json"
        ),
    )
    resolved = _resolved_op_context_base_dir(session)
    assert resolved == target_dir.resolve(), (
        f"session resolved base_dir={resolved!r}, "
        f"expected the agent-created value {target_dir.resolve()!r}"
    )


# ── witness ② — no base_dir given -> existing behavior is undisturbed ──────


@pytest.mark.asyncio
async def test_agent_created_without_base_dir_writes_no_override_key(
    tmp_path: Path,
) -> None:
    """Tier 2: omitting ``base_dir`` (every pre-#5080 caller, and any new
    caller with nothing to impose) writes NO ``base_dir:`` key into
    profile.yaml at all — byte-identical to the pre-#5080 ``create``/
    ``create_agent`` behavior (regression guard: an existing agent,
    created before this feature existed, has no such key either, and
    must keep resolving through whatever layer it already fell through
    to)."""
    project_root = tmp_path / "project"
    reg = AgentRegistry(project_root=project_root, session_factory=_no_factory)
    await reg.create_agent("alpha")

    profile_path = _profile_path(project_root, "alpha")
    assert profile_path.is_file()
    assert "base_dir" not in profile_path.read_text(encoding="utf-8"), (
        "an agent created with no base_dir must not gain a base_dir: key "
        "in its own profile.yaml"
    )


# ── witness ③ — session-layer override still wins over the new agent default ─


@pytest.mark.asyncio
async def test_session_layer_override_still_wins_over_the_new_agent_default(
    tmp_path: Path,
) -> None:
    """Tier 2: #4200's own priority order (session > agent > Agent-object
    default) is unaffected by #5080 giving the agent layer a real writer
    — a session-layer override (this session's own config.yaml, #4200's
    pre-existing mechanism) still takes precedence over the agent-layer
    default #5080 just made writable."""
    project_root = tmp_path / "project"
    agent_default_dir = project_root / "agent-default"
    # #5081 (3rd round): must resolve INSIDE project_root -- the session
    # layer is now bounded too.
    session_override_dir = project_root / "session-override"
    session_override_dir.mkdir(parents=True)

    reg = AgentRegistry(project_root=project_root, session_factory=_no_factory)
    await reg.create_agent("alpha", base_dir=str(agent_default_dir))

    session = make_session(
        agent_name="alpha", workspace_state_dir=project_root / ".reyn",
        snapshot_path=(
            project_root / ".reyn" / "agents" / "alpha" / "state" / "snapshot.json"
        ),
    )
    session_cfg = Path(session._snapshot_path).parent / "config.yaml"
    session_cfg.parent.mkdir(parents=True, exist_ok=True)
    session_cfg.write_text(
        f"name: s\nbase_dir: {session_override_dir}\n", encoding="utf-8",
    )

    resolved = _resolved_op_context_base_dir(session)
    assert resolved == session_override_dir, (
        f"session resolved base_dir={resolved!r}, "
        f"expected the SESSION-layer override {session_override_dir!r} "
        f"to win over the agent-layer default {agent_default_dir!r}"
    )


# ── witness ④ — a base_dir outside the workspace is rejected ───────────────


@pytest.mark.asyncio
async def test_base_dir_outside_the_workspace_is_rejected(tmp_path: Path) -> None:
    """Tier 2: the upper bound itself — a requested ``base_dir`` outside
    the project workspace is REJECTED (a raised ``ValueError`` naming the
    boundary), never silently clamped into it. Without this, the
    restrict-only claim in every docstring/tool-description this PR wrote
    would be a claim with nothing enforcing it.

    Strip-falsifier: commenting out the bound-check block in
    ``registry.create`` (leaving the write unconditional) turns this
    green (accepted) instead of red — the escape hatch #5080's own
    accept criterion exists to close."""
    project_root = tmp_path / "project"
    outside_dir = tmp_path / "outside-the-workspace"

    reg = AgentRegistry(project_root=project_root, session_factory=_no_factory)
    with pytest.raises(ValueError, match="outside the project workspace"):
        await reg.create_agent("alpha", base_dir=str(outside_dir))

    # Rejected atomically: no partial state survives a rejected create.
    assert not reg.exists("alpha")
    assert not _profile_path(project_root, "alpha").exists()


# ── witness ⑤ — protect-at-USE, not only at-write (architect BLOCK, 1st) ───


@pytest.mark.asyncio
async def test_a_directly_tampered_profile_pointing_outside_is_not_used(
    tmp_path: Path,
) -> None:
    """Tier 2: architect's BLOCK on PR #5081 (1st round) — ``registry.
    create``'s own bound check only gates the ONE seam. ``.reyn`` is the
    agent's default WRITE zone (``permissions.py``'s own
    ``_DEFAULT_WRITE_ZONES``), so ``profile.yaml`` is directly
    agent-writable through the ordinary file-write op, bypassing
    ``create`` entirely — reyn's own vocabulary: "Protect-at-use
    migration ... writing the config alone grants nothing usable." This
    test hand-writes the file exactly the way an agent's own file-write
    op would (bypassing ``registry.create`` completely) and proves the
    out-of-bounds value is NOT used — the session falls through to the
    next layer instead.

    Strip-falsifier: removing the bound-check block in ``Session.
    _workspace_base_dir``'s agent-layer branch (returning
    ``agent_override`` unconditionally, as PR #5081's own first version
    did) turns this red — the tampered path would be used."""
    project_root = tmp_path / "project"
    outside_dir = tmp_path / "outside-the-workspace"
    fallback_dir = tmp_path / "agent-object-fallback"

    profile_path = _profile_path(project_root, "alpha")
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(
        f"name: alpha\nrole: ''\ncreated_at: ''\nbase_dir: {outside_dir}\n",
        encoding="utf-8",
    )

    session = make_session(
        agent_name="alpha", workspace_state_dir=project_root / ".reyn",
        workspace_base_dir=fallback_dir,
        snapshot_path=(
            project_root / ".reyn" / "agents" / "alpha" / "state" / "snapshot.json"
        ),
    )
    resolved = _resolved_op_context_base_dir(session)
    assert resolved != outside_dir.resolve(), (
        f"a directly-tampered profile.yaml pointing outside the workspace "
        f"({outside_dir!r}) was USED — protect-at-write alone is not "
        f"protect-at-use"
    )
    assert resolved == fallback_dir, (
        f"expected fall-through to the Agent object's own base_dir "
        f"{fallback_dir!r}, got {resolved!r}"
    )


# ── witness ⑦ — protect-at-USE for the SESSION layer too (architect BLOCK, 3rd) ─


@pytest.mark.asyncio
async def test_a_directly_tampered_session_config_pointing_outside_is_not_used(
    tmp_path: Path,
) -> None:
    """Tier 2: architect's BLOCK on PR #5081 (3rd round) — the 1st round's
    protect-at-use fix only bounded the AGENT-layer read. The SESSION-
    layer read (``<session_state_dir>/config.yaml``, which resolves to
    ``.reyn/agents/<name>/state/config.yaml`` — still inside ``.reyn``,
    the agent's own default write zone) is read FIRST and was left
    unbounded, so an attacker never needs to touch the agent layer at
    all: writing directly to the session-layer file reaches an unbounded
    value before the 1st round's own defense is ever consulted.
    Architect's own generalization: "the discriminator isn't the layer,
    it's whether the value is inside .reyn and gets used."

    This test hand-writes the SESSION-layer config.yaml exactly the way
    an agent's own file-write op would (bypassing ``spawn_session``'s
    own LLM-tool-level check, ``router_host_adapter.py``, completely) and
    proves the out-of-bounds value is NOT used.

    Strip-falsifier: reverting the session-layer branch to unconditional
    (as PR #5081's 2nd-round version did) turns this red — the tampered
    path would be used, without ever reaching the agent-layer defense."""
    project_root = tmp_path / "project"
    outside_dir = tmp_path / "outside-the-workspace"
    fallback_dir = project_root / "agent-object-fallback"

    session = make_session(
        agent_name="alpha", workspace_state_dir=project_root / ".reyn",
        workspace_base_dir=fallback_dir,
        snapshot_path=(
            project_root / ".reyn" / "agents" / "alpha" / "state" / "snapshot.json"
        ),
    )
    session_cfg = Path(session._snapshot_path).parent / "config.yaml"
    session_cfg.parent.mkdir(parents=True, exist_ok=True)
    session_cfg.write_text(f"name: s\nbase_dir: {outside_dir}\n", encoding="utf-8")

    resolved = _resolved_op_context_base_dir(session)
    assert resolved != outside_dir.resolve(), (
        f"a directly-tampered session config.yaml pointing outside the "
        f"workspace ({outside_dir!r}) was USED — the session-layer read "
        f"was left unbounded, reachable before the agent-layer defense"
    )
    assert resolved == fallback_dir, (
        f"expected fall-through to the Agent object's own base_dir "
        f"{fallback_dir!r}, got {resolved!r}"
    )


# ── witness ⑥ — the store move doesn't disturb topology narrowing (2nd BLOCK) ─


@pytest.mark.asyncio
async def test_base_dir_creation_does_not_disturb_a_same_named_topology_profile(
    tmp_path: Path,
) -> None:
    """Tier 2: architect's BLOCK on PR #5081 (2nd round) — before this
    fix, base_dir lived in ``.reyn/capability_profiles/<X>.yaml``, keyed
    by PROFILE name. A topology's ``profiles: {member: profile_name}``
    binding is a free string with NO uniqueness constraint against agent
    names (``topology.py``'s own ``profile_for``: ``self.profiles.get(
    member)``; only ``_validate_agent_name``/``_validate_topology_name``
    validate names, never profile-name uniqueness) — ``profiles: {alpha:
    alpha}`` (an agent bound to a same-named narrowing template) is
    a real, unconstrained possibility -- nothing in topology.py rules
    it out. Creating agent "alpha" with a base_dir
    would have silently overwritten profile "alpha"'s own narrowing
    template. This test binds a topology member to a capability_profile
    NAMED THE SAME as the agent being created, creates that agent WITH
    base_dir, and proves the topology's own narrowing survives untouched
    — because base_dir no longer lives in that directory at all.

    Strip-falsifier: reverting the store to ``capability_profiles/
    <name>.yaml`` (this BLOCK's own root cause) turns this red — the
    narrowing template would be clobbered by the base_dir write."""
    import yaml

    project_root = tmp_path / "project"
    reg = AgentRegistry(project_root=project_root, session_factory=_no_factory)

    # A capability_profile named "alpha" (#1827 S3's own subject: a
    # reusable narrowing template, PROFILE-name-keyed) -- pre-existing,
    # unrelated to the agent this test is about to create.
    narrowing_profile_path = (
        project_root / ".reyn" / "capability_profiles" / "alpha.yaml"
    )
    narrowing_profile_path.parent.mkdir(parents=True, exist_ok=True)
    narrowing_profile_path.write_text(
        yaml.safe_dump({"name": "alpha", "tool_deny": ["exec"]}), encoding="utf-8",
    )

    target_dir = project_root / "workers" / "alpha"
    await reg.create_agent("alpha", base_dir=str(target_dir))

    # The pre-existing narrowing template must be untouched -- same
    # content, not silently overwritten by the base_dir write.
    on_disk = yaml.safe_load(narrowing_profile_path.read_text(encoding="utf-8"))
    assert on_disk == {"name": "alpha", "tool_deny": ["exec"]}, (
        f"the pre-existing capability_profile 'alpha' (a topology "
        f"narrowing template) was disturbed by creating an agent also "
        f"named 'alpha' with a base_dir -- got {on_disk!r}"
    )

    # And the agent's own base_dir still resolves correctly, from its
    # OWN profile.yaml, not the capability_profile.
    session = make_session(
        agent_name="alpha", workspace_state_dir=project_root / ".reyn",
        snapshot_path=(
            project_root / ".reyn" / "agents" / "alpha" / "state" / "snapshot.json"
        ),
    )
    assert _resolved_op_context_base_dir(session) == target_dir.resolve()
