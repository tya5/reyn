"""Tier 2: run_skill + skill_resolve op registration stays in op_runtime and
live dispatch reaches the reyn.skill backend (#1794 S4).

S4 consolidates the skill-execution logic into ``reyn.skill`` while keeping the
Control IR op registration in ``op_runtime`` (P3/P4 registry↔backend locality).
These tests pin both halves of that contract:

  - the ``run_skill`` / ``skill_resolve`` kinds are registered via the
    ``op_runtime`` import side-effect (registration did NOT move), and
  - dispatching each op through the public ``execute_op`` registry path
    reaches the ``reyn.skill`` backend (= the thin op handle delegates, the
    logic is wired and not merely defined).

Real EventLog / Workspace / OpContext / registry — no mocks.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.core.events.events import EventLog
from reyn.core.op_runtime import available_kinds, execute_op
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import RunSkillIROp, SkillResolveIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver


def _make_ctx(tmp_path: Path, events: EventLog):
    from reyn.core.op_runtime.context import OpContext

    ws = Workspace(events=events)
    resolver = PermissionResolver(
        config_permissions={},
        project_root=tmp_path,
        interactive=False,
    )
    return OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=resolver,
        skill_name="test_skill",
        run_id="s4_delegation_test",
        current_phase="main",
        sub_state_dir_override=str(tmp_path / "sub_state"),
    )


def test_op_registration_stays_in_op_runtime():
    """Tier 2: run_skill + skill_resolve register via the op_runtime import.

    Importing op_runtime (above) eagerly imports the backend modules, whose
    ``register(kind, handle)`` side-effect populates the registry. If the
    registration had moved out of op_runtime, these kinds would be absent.
    """
    kinds = available_kinds()
    assert "run_skill" in kinds
    assert "skill_resolve" in kinds


def test_skill_resolve_dispatch_reaches_reyn_skill_backend(tmp_path):
    """Tier 2: execute_op(skill_resolve) routes through the registry to the
    reyn.skill.skill_resolve backend and returns its resolution result.

    The resolved=True / source="stdlib" / skill_md_path shape can only be
    produced by reyn.skill.skill_resolve.resolve running, so a correct result
    proves the registry → thin op handle → reyn.skill delegation chain.
    """
    events = EventLog()
    ctx = _make_ctx(tmp_path, events)
    op = SkillResolveIROp(kind="skill_resolve", name="direct_llm")

    result = asyncio.run(execute_op(op, ctx, caller="control_ir"))

    assert result["resolved"] is True
    assert result["source"] == "stdlib"
    assert result["skill_md_path"].endswith("direct_llm/skill.md")
    # The backend also emits its completion event through the duck-typed ctx.
    assert any(e.type == "skill_resolve_completed" for e in events.all())


def test_run_skill_dispatch_reaches_reyn_skill_backend(tmp_path):
    """Tier 2: execute_op(run_skill) routes through the registry into the
    reyn.skill.run_skill backend's reference-resolution logic.

    A non-existent skill name fails inside reyn.skill.run_skill._resolve_skill_ref
    (SkillNotFoundError), which execute_op captures as status="error" carrying
    the offending name. status="error" (not "skipped") proves the registered
    handler ran AND reached the reyn.skill resolution logic; "skipped" would
    mean no handler was registered.
    """
    events = EventLog()
    ctx = _make_ctx(tmp_path, events)
    op = RunSkillIROp(
        kind="run_skill",
        skill="totally_made_up_skill_s4_xyz",
        input={"type": "user_message", "data": {"text": "x"}},
    )

    result = asyncio.run(execute_op(op, ctx, caller="control_ir"))

    assert result["status"] == "error"
    assert "totally_made_up_skill_s4_xyz" in result["error"]
