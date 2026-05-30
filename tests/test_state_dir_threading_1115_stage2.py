"""Tier 2: FP-0008 #1115 Stage 2 — workspace state_dir threading.

For an in-container run, the workspace base_dir is the container repo (e.g.
``/testbed``) while the state_dir (artifacts + events) must stay on the HOST so
they survive container teardown (the Stage 0 decouple). This needs base_dir and
state_dir to be threadable independently from the run construction.

Pins (public surface only — no mocks):
  (a) OSRuntime threads workspace_state_dir → Workspace.state_dir, decoupled
      from base_dir;
  (b) default (no workspace_state_dir) keeps the legacy base_dir/.reyn = host
      behavior unchanged.
"""
from __future__ import annotations

from pathlib import Path

from reyn.kernel.runtime import OSRuntime
from reyn.schemas.models import Phase, Skill, SkillGraph


def _one_phase_skill() -> Skill:
    p = Phase(
        name="draft", instructions="d",
        input_schema={"type": "object", "properties": {}}, allowed_ops=[],
    )
    return Skill(
        name="state_dir_test", entry_phase="draft", phases={"draft": p},
        graph=SkillGraph(transitions={}, can_finish_phases=["draft"]),
        final_output_schema={"type": "object", "properties": {}}, final_output_name="result",
    )


def test_state_dir_threads_decoupled_from_base_dir(tmp_path: Path) -> None:
    """Tier 2: (a) workspace_state_dir is honored independently of base_dir."""
    base = tmp_path / "container_testbed"
    base.mkdir()
    host_state = tmp_path / "host_state"

    rt = OSRuntime(
        _one_phase_skill(), model="stub/model", run_id="r1",
        workspace_base_dir=base, workspace_state_dir=host_state,
    )

    assert rt.workspace.base_dir == base.resolve()
    assert rt.workspace.state_dir == host_state.resolve()
    assert not rt.workspace.state_dir.is_relative_to(rt.workspace.base_dir)
    # artifacts live under the host state_dir, not the (container) base_dir.
    assert (host_state / "artifacts").is_dir()


def test_default_state_dir_is_base_dir_reyn(tmp_path: Path) -> None:
    """Tier 2: (b) without workspace_state_dir, state_dir = base_dir/.reyn (host)."""
    base = tmp_path / "repo"
    base.mkdir()
    rt = OSRuntime(
        _one_phase_skill(), model="stub/model", run_id="r2",
        workspace_base_dir=base,
    )
    assert rt.workspace.state_dir == (base / ".reyn").resolve()
