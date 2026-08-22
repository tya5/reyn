"""Tier 2: #4200 (read side) — a session's EFFECTIVE ``base_dir`` resolves a
session-layer override, then an agent-layer default, before falling back to
the shared ``Agent``'s own ``workspace_base_dir`` — the SAME "layer in front
of the shared Agent identity" shape #2103-S1a capability narrowing already
uses (``AgentRegistry.per_session_narrowing`` / ``resolved_profile_for``).

Companion to #4215①'s own staleness fix (``hot_reloader``/``session_id_fn``):
this module's last test is the SAME hazard class, discovered while
implementing this feature (not on architect's own "measured" list) —
``RouterOpContextSource``'s ``workspace_base_dir`` field was captured
EAGERLY at ``Session.__init__``, before a spawned child's real per-session
override (persisted to ``config.yaml`` by the registry's post-construction
spawn-time fixup) exists. A frozen capture would silently give a spawned
child the PARENT's base_dir forever.

Real objects throughout — real ``Session``/``Agent``/``AgentRegistry``
construction, real filesystem ``tmp_path`` roots, real YAML writes at the
exact paths production code reads from. No mocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.spawn_routing import AuditOnlyNoSurface
from tests._support.agent_session import make_session
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML


def _write_config(path: Path, base_dir: "Path | str") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"name: s\nbase_dir: {base_dir}\n", encoding="utf-8")


# ── Gate 1: session-layer override wins ──────────────────────────────────────


def _resolved_op_context_base_dir(session: Session) -> "Path | None":
    """The public-surface read: build a REAL chat-router OpContext through
    the same object every op handler is actually handed
    (``RouterOpContextSource.build()`` — ``Session._make_router_op_context``
    and ``RouterHostAdapter.make_router_op_context`` are both one-line
    delegations to it, per that class's own docstring), and read the
    resolved ``Workspace.base_dir`` off it — the value an op handler
    actually observes, not an internal bookkeeping field."""
    return session._router_op_context_source.build().workspace.base_dir


def test_session_layer_override_wins_over_the_agent_default(tmp_path: Path) -> None:
    """Tier 2: a base_dir written into THIS session's own
    <session_state_dir>/config.yaml is what the session's real op-context
    resolves to — the SAME sibling-of-snapshot_path file
    _read_per_session_hooks already reads (#2285), now carrying a base_dir
    key too."""
    project_root = tmp_path / "project"
    override_dir = tmp_path / "session-override"
    override_dir.mkdir(parents=True)
    session = make_session(
        agent_name="alpha", workspace_state_dir=project_root / ".reyn",
        snapshot_path=project_root / ".reyn" / "agents" / "alpha" / "state" / "snapshot.json",
    )
    _write_config(Path(session._snapshot_path).parent / "config.yaml", override_dir)

    assert _resolved_op_context_base_dir(session) == override_dir


# ── Gate 2: agent-layer default, when no session override exists ────────────


def test_agent_layer_default_wins_over_the_agent_object_when_no_session_override(
    tmp_path: Path,
) -> None:
    """Tier 2: absent a session-layer override, a base_dir in the agent's own
    profile.yaml (.reyn/agents/<name>/ — #5081: NOT capability_profiles/,
    which is keyed by PROFILE name, a namespace agent names don't own) is
    the next layer — still ahead of the shared Agent object's own
    workspace_base_dir."""
    project_root = tmp_path / "project"
    # #5081: must resolve INSIDE project_root -- Session._workspace_base_dir
    # now bounds the agent-layer read to a subset of the project workspace
    # (protect-at-use).
    agent_default_dir = project_root / "agent-default"
    agent_default_dir.mkdir(parents=True)
    session = make_session(
        agent_name="alpha", workspace_state_dir=project_root / ".reyn",
        workspace_base_dir=tmp_path / "agent-object-value",  # the Agent's own value
        snapshot_path=project_root / ".reyn" / "agents" / "alpha" / "state" / "snapshot.json",
    )
    _write_config(
        project_root / ".reyn" / "agents" / "alpha" / "profile.yaml", agent_default_dir,
    )

    assert _resolved_op_context_base_dir(session) == agent_default_dir


# ── Gate 3: regression — neither override present, byte-identical to pre-#4200 ──


def test_falls_back_to_the_agent_objects_own_value_when_no_override_exists(
    tmp_path: Path,
) -> None:
    """Tier 2: regression guard — a caller that sets neither override keeps
    the EXACT prior default (the shared Agent's own workspace_base_dir), so
    #4200's read-side change is inert for every session that opts into
    nothing."""
    project_root = tmp_path / "project"
    agent_value = tmp_path / "agent-object-value"
    session = make_session(
        agent_name="alpha", workspace_state_dir=project_root / ".reyn",
        workspace_base_dir=agent_value,
        snapshot_path=project_root / ".reyn" / "agents" / "alpha" / "state" / "snapshot.json",
    )

    assert _resolved_op_context_base_dir(session) == agent_value


# ── Gate 4: a malformed override file fails open toward the next layer ──────


def test_a_malformed_session_config_falls_through_to_the_agent_layer(tmp_path: Path) -> None:
    """Tier 2: a malformed session-layer config.yaml must not crash session
    construction — it is skipped (surfaced via a log warning, per
    AgentRegistry.per_session_narrowing's own fail-open handling of the
    SAME sibling file), and resolution falls through to the next layer.
    Skipping only WIDENS toward the next fallback, never past the
    effective floor (restrict-only posture, same as narrowing)."""
    project_root = tmp_path / "project"
    # #5081: must resolve INSIDE project_root -- Session._workspace_base_dir
    # now bounds the agent-layer read (subset of) the project workspace (protect-at-use).
    agent_default_dir = project_root / "agent-default"
    agent_default_dir.mkdir(parents=True)
    session = make_session(
        agent_name="alpha", workspace_state_dir=project_root / ".reyn",
        snapshot_path=project_root / ".reyn" / "agents" / "alpha" / "state" / "snapshot.json",
    )
    session_cfg = Path(session._snapshot_path).parent / "config.yaml"
    session_cfg.parent.mkdir(parents=True, exist_ok=True)
    session_cfg.write_text("base_dir: [unterminated\n", encoding="utf-8")
    _write_config(
        project_root / ".reyn" / "agents" / "alpha" / "profile.yaml", agent_default_dir,
    )

    assert _resolved_op_context_base_dir(session) == agent_default_dir


# ── Gate 5: the staleness witness — a spawned child sees ITS OWN override ────


@pytest.mark.asyncio
async def test_a_spawned_childs_op_context_resolves_the_childs_own_override_not_the_parents(
    tmp_path: Path,
) -> None:
    """Tier 2: the actual owner concern for the plumbing fix — a spawned
    child session's real chat-router OpContext (built through
    RouterOpContextSource, the SAME object RouterHostAdapter/Session both
    delegate to) resolves the CHILD's own base_dir override, not the
    PARENT's, even though the override is written into the child's
    config.yaml AFTER Session.__init__ already ran (the registry's
    post-construction spawn-time fixup, same timing #4215①'s
    session_id_fn/hot_reloader guard against).

    Falsify: capture ``workspace_base_dir_fn`` eagerly instead of as a
    live callable (``lambda v=session._workspace_base_dir: v`` — freezes
    at definition time, the exact pre-fix shape) and this goes RED — the
    child's op-context would resolve the PARENT's base_dir instead."""
    project_root = tmp_path / "project"
    (project_root / "reyn.yaml").parent.mkdir(parents=True, exist_ok=True)
    (project_root / "reyn.yaml").write_text(MINIMAL_REYN_YAML, encoding="utf-8")
    state_log = StateLog(project_root / ".reyn" / "wal.jsonl")
    parent_base_dir = tmp_path / "parent-base-dir"
    parent_base_dir.mkdir(parents=True)
    child_override_dir = tmp_path / "child-base-dir-override"
    child_override_dir.mkdir(parents=True)

    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        return make_session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            workspace_base_dir=parent_base_dir,
            workspace_state_dir=project_root / ".reyn",
        )

    reg = AgentRegistry(project_root=project_root, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    reg.create("worker")

    routing = AuditOnlyNoSurface()
    sid = await reg.spawn_session_recorded(
        "worker", mode="persistent",
        presentation_consumer=routing.presentation_consumer,
        intervention_bridge=routing.intervention_bridge,
    )
    child = reg.get_session("worker", sid)
    assert child is not None

    # Write the override into the CHILD's own config.yaml — after Session()
    # already ran (spawn_session_recorded's post-construction snapshot-path
    # fixup already happened by this point too, matching real timing).
    _write_config(Path(child._snapshot_path).parent / "config.yaml", child_override_dir)

    resolved = child._router_op_context_source.build()

    assert resolved.workspace.base_dir == child_override_dir, (
        f"child's op-context resolved base_dir={resolved.workspace.base_dir!r}, "
        f"expected its OWN override {child_override_dir!r} (parent was "
        f"{parent_base_dir!r}) — an eager capture would silently freeze the "
        f"parent's value onto every spawned child"
    )
