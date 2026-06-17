"""Tier 2: FP-0008 #1115 Stage 0 Part B — cross-workspace artifact handle read.

Context: the `eval` skill runs a target skill (via `run_skill`), collects its
phase artifacts, and hands each artifact's path to the `judge_phase` sub-skill,
which `file.read`s it from a *different* workspace to evaluate it. The path is
deliberately a reference (not inlined data) to keep large artifact JSON out of
the prompt.

#1115 Stage 0 changed `store_artifact` to return a state_dir-relative handle
(``artifacts/...``) instead of a base_dir-relative path (``.reyn/artifacts/...``).
A consumer in a different workspace that ``file.read``s the bare handle resolves
it against ITS base_dir → the file is not found (the handle lacks the prefix to
reach state_dir). Part B fixes this by having ``Agent.phase_artifacts`` resolve
each handle to an absolute path (via the producing run's own workspace) before
it crosses the boundary — so the judge can read it regardless of its base_dir.

This file pins, at the Workspace level (no LLM, no mocks):
  (a) the bare state_dir handle is NOT readable across the workspace boundary
      (= the Stage 0 break Part B fixes);
  (b) the OS-resolved absolute path IS readable across the boundary and
      round-trips the stored content (= the Part B mechanism that
      ``Agent.phase_artifacts`` now applies).
"""
from __future__ import annotations

import json
from pathlib import Path

from reyn.data.workspace.workspace import Workspace
from reyn.events.events import EventLog


def test_bare_handle_not_readable_across_workspace_boundary(tmp_path: Path) -> None:
    """Tier 2: (a) the bare state_dir handle does not resolve in another workspace.

    This is the latent break #1115 Stage 0 introduced for the eval→judge path:
    a raw ``artifacts/...`` handle file.read from a consumer workspace points at
    ``base_dir/artifacts/...``, not the state_dir where the artifact lives.
    """
    base = tmp_path / "repo"
    base.mkdir()

    producer = Workspace(events=EventLog(), base_dir=base)
    handle = producer.store_artifact(
        "some_phase", {"type": "demo", "data": {"v": 1}}, skill_name="target_skill"
    )

    # A judge-like consumer in the same base_dir reads the BARE handle.
    consumer = Workspace(events=EventLog(), base_dir=base)
    _content, found = consumer.read_file(handle)
    assert not found, (
        "Bare state_dir handle must NOT resolve across the boundary "
        "(it would point at base_dir/artifacts, not state_dir/artifacts) — "
        "this is exactly the break Part B fixes by resolving to an absolute path"
    )


def test_resolved_absolute_path_is_readable_across_workspace_boundary(
    tmp_path: Path,
) -> None:
    """Tier 2: (b) the OS-resolved absolute path reads + round-trips cross-boundary.

    Mirrors ``Agent.phase_artifacts`` (resolve the handle via the producing
    run's workspace) → eval hands the absolute path → judge file.reads it.
    """
    base = tmp_path / "repo"
    base.mkdir()

    artifact = {"type": "demo", "data": {"v": 1, "note": "judge-me"}}
    producer = Workspace(events=EventLog(), base_dir=base)
    handle = producer.store_artifact(
        "some_phase", artifact, skill_name="target_skill"
    )

    # = what Agent.phase_artifacts now produces: resolve via the producing
    #   run's own workspace (sidesteps any sub-state-dir nesting).
    abs_path = str(producer.resolve_artifact_handle(handle))
    assert Path(abs_path).is_absolute()

    # A judge-like consumer in the same base_dir reads the ABSOLUTE path.
    consumer = Workspace(events=EventLog(), base_dir=base)
    content, found = consumer.read_file(abs_path)
    assert found, (
        "Resolved absolute artifact path must be readable across the "
        "eval→judge workspace boundary"
    )
    assert json.loads(content) == artifact
