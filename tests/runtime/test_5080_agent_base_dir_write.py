"""Tier 2: #5080 — the agent-layer ``base_dir`` WRITE side, complementary to
#4200's own READ side (``test_4200_session_base_dir_resolution.py``).

#4200 taught ``Session._workspace_base_dir`` to read an agent-layer
override from ``.reyn/capability_profiles/<name>.yaml`` — a read path
nothing wrote to (architect's own measurement, #5080 issue thread: read
present, no writer). #5080 gives ``registry.create``/``create_agent`` (the
ONE creation seam every surface — CLI / web / slash / the ``spawn_agent``
LLM tool — routes through) an optional ``base_dir`` parameter that writes
that key, validated ⊆ the project workspace (#4206's own axis ①,
restrict-only, applied to a "file zone" — owner ruling relayed by
architect/lead-coder: reject a request outside the floor, never clamp it
in, name the boundary — the SAME shape ``spawn_session``'s own
``base_dir`` argument already uses for the session layer, just bounded by
the project workspace here rather than a spawner's own effective
base_dir).

Real ``AgentRegistry``/``Session`` construction throughout — no mocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.runtime.registry import AgentRegistry
from tests._support.agent_session import make_session


def _no_factory(profile):
    raise RuntimeError("session factory not used in these write-side tests")


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
    """Tier 2: ``registry.create_agent(name, base_dir=X)`` writes
    ``.reyn/capability_profiles/<name>.yaml``'s ``base_dir:`` key, and a
    REAL session for that agent resolves it through its real op-context
    (the public-surface value an op handler actually observes) — reading
    the actual value, not just "no exception was raised" (architect's own
    explicit condition on this witness)."""
    project_root = tmp_path / "project"
    target_dir = project_root / "workers" / "alpha"

    reg = AgentRegistry(project_root=project_root, session_factory=_no_factory)
    await reg.create_agent("alpha", base_dir=str(target_dir))

    cap_profile_path = project_root / ".reyn" / "capability_profiles" / "alpha.yaml"
    assert cap_profile_path.is_file(), "no capability_profiles/<name>.yaml was written"

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
async def test_agent_created_without_base_dir_writes_no_override_file(
    tmp_path: Path,
) -> None:
    """Tier 2: omitting ``base_dir`` (every pre-#5080 caller, and any new
    caller with nothing to impose) writes NO
    ``capability_profiles/<name>.yaml`` at all — byte-identical to the
    pre-#5080 ``create``/``create_agent`` behavior (regression guard: an
    existing agent, created before this feature existed, has no such
    file either, and must keep resolving through whatever layer it
    already fell through to)."""
    project_root = tmp_path / "project"
    reg = AgentRegistry(project_root=project_root, session_factory=_no_factory)
    await reg.create_agent("alpha")

    cap_profile_path = project_root / ".reyn" / "capability_profiles" / "alpha.yaml"
    assert not cap_profile_path.exists(), (
        "an agent created with no base_dir must not gain a "
        "capability_profiles/<name>.yaml file"
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
    session_override_dir = tmp_path / "session-override"
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

    # Rejected atomically: no partial state (no profile, no capability
    # override) survives a rejected create.
    assert not reg.exists("alpha")
    cap_profile_path = project_root / ".reyn" / "capability_profiles" / "alpha.yaml"
    assert not cap_profile_path.exists()
