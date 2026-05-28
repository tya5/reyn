"""Tier 2: shell op subprocess anchors cwd to workspace.base_dir.

FP-0008 PR-I functional invariant. The structural fix in this PR
(Workspace.base_dir explicit + os.chdir removal in benchmark workspace)
addresses the concurrent-coroutine race on process-wide CWD. BUT the
shell op handler in src/reyn/op_runtime/shell.py invokes
asyncio.create_subprocess_shell without an explicit cwd= arg --
meaning the subprocess inherits process-global CWD, NOT the
workspace.base_dir the structural fix established.

Without this functional invariant, the chdir-removal would create a
NEW v3-style cascade: shell ops run in the launcher's CWD, not the
benchmark workspace, so `git checkout` fails with "Not a git
repository" on the unaltered process CWD.

This file pins:
  1. The shell op subprocess executes with cwd=workspace.base_dir.
  2. Concurrent shell ops in distinct workspaces see distinct CWDs
     (the actual race-elimination behavioral check).

The tests use real subprocess invocation (no mocks) and real
Workspace instances with explicit base_dir.

Tier rule discipline: every test docstring opens with Tier 2; no
mocks; no private-state assertions; no format-pinning.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.events.events import EventLog
from reyn.op_runtime.context import OpContext
from reyn.op_runtime.shell import handle as shell_handle
from reyn.permissions.permissions import PermissionDecl
from reyn.schemas.models import ShellIROp
from reyn.workspace.workspace import Workspace


def _build_ctx(workspace_dir: Path) -> OpContext:
    """Build a minimal OpContext anchored to an explicit workspace dir."""
    events = EventLog()
    workspace = Workspace(events=events, base_dir=workspace_dir)
    return OpContext(
        workspace=workspace,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        skill_name="test_skill",
        skill=None,
        model="standard",
        resolver=None,
        subscribers=[],
        output_language=None,
        max_phase_visits=25,
        sub_state_dir_override=None,
        state_dir_strategy="control_ir",
        shell_allowed=True,
        mcp_servers={},
        mcp_clients={},
        intervention_bus=None,
        current_phase="",
        caller="direct",
        parent_skill_run_id=None,
    )


def test_shell_op_subprocess_runs_in_workspace_base_dir(tmp_path: Path) -> None:
    """Tier 2: shell op `pwd` returns workspace.base_dir, not the process CWD.

    Anchor: build a Workspace with an explicit base_dir under tmp_path
    (= distinct from Path.cwd() at test time). Run a `pwd` shell op.
    The op's stdout must match the workspace base_dir, NOT the test
    runner's CWD.
    """
    workspace_dir = tmp_path / "anchor_a"
    workspace_dir.mkdir()
    ctx = _build_ctx(workspace_dir)
    op = ShellIROp(kind="shell", cmd="pwd", timeout=5)

    result = asyncio.run(shell_handle(op, ctx, caller="control_ir"))

    assert result["status"] == "ok", (
        f"shell op did not complete cleanly: {result}"
    )
    actual_cwd = result["stdout"].strip()
    # Resolve both sides to handle symlinks (= macOS /tmp -> /private/tmp).
    assert Path(actual_cwd).resolve() == workspace_dir.resolve(), (
        f"shell op subprocess ran in {actual_cwd!r}, expected "
        f"{str(workspace_dir.resolve())!r} (= workspace.base_dir). "
        f"The PR-I structural fix (Workspace.base_dir explicit + "
        f"chdir removal) requires the shell op handler to pass "
        f"cwd= to the subprocess; otherwise the subprocess inherits "
        f"process-global CWD and the race the structural fix "
        f"eliminated is reintroduced at the shell layer."
    )


def test_concurrent_shell_ops_isolated_by_workspace(tmp_path: Path) -> None:
    """Tier 2: 4 concurrent shell ops each see THEIR workspace.base_dir.

    The actual race-elimination behavioral check: under
    --concurrency 4, run 4 shell `pwd` ops in 4 distinct workspaces
    concurrently via asyncio.gather. Each op's resolved stdout must
    match ITS workspace.base_dir, never another coroutine's.

    Pre-fix (= shell.py without cwd=), all 4 ops would inherit
    process-global CWD => all 4 would report the SAME path => race
    contaminates results. Post-fix, each coroutine's subprocess uses
    its own ctx.workspace.base_dir => 4 distinct paths observed.
    """
    workspace_dirs = [tmp_path / f"anchor_{i}" for i in range(4)]
    for d in workspace_dirs:
        d.mkdir()

    async def _run_one(workspace_dir: Path) -> str:
        ctx = _build_ctx(workspace_dir)
        op = ShellIROp(kind="shell", cmd="pwd", timeout=5)
        result = await shell_handle(op, ctx, caller="control_ir")
        return result["stdout"].strip()

    async def _run_all() -> list[str]:
        return await asyncio.gather(*(_run_one(d) for d in workspace_dirs))

    actual_cwds = asyncio.run(_run_all())

    # Each subprocess saw its own workspace base_dir
    for actual, expected in zip(actual_cwds, workspace_dirs):
        assert Path(actual).resolve() == expected.resolve(), (
            f"Concurrent shell op race: subprocess for workspace "
            f"{str(expected)!r} reported CWD {actual!r}. "
            f"This is the v3-style cascade reintroduced at the shell "
            f"layer. The shell.py handler must pass cwd= to the "
            f"subprocess."
        )

    # And the four reported CWDs are pairwise distinct
    resolved_cwds = {Path(c).resolve() for c in actual_cwds}
    assert len(resolved_cwds) == len(workspace_dirs), (
        f"Concurrent shell ops reported overlapping CWDs (= race): "
        f"{actual_cwds!r}. Expected 4 distinct paths."
    )
