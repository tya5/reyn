"""Tier 2: FP-0008 #1132 — the events audit log honors workspace_state_dir.

The events log lives under `Agent.state_dir` (`Path(state_dir)/events/...`). That
was hardcoded to the cwd-relative `.reyn`, so a run that passed an explicit
host-side `--state-dir` (e.g. an in-container run, where base_dir is the container
repo and state is kept host-side) had its Workspace artifacts/offload under
`--state-dir` but its events split off under `.reyn` — not co-located.

Now `Agent.state_dir` honors `workspace_state_dir` when provided (events
co-locate with the rest of the run's state), and still defaults to `.reyn` for
the common no-state-dir case (unchanged host behavior).

Real Agent, no mocks. Docstring opens "Tier 2:".
"""
from __future__ import annotations

from pathlib import Path

from reyn.agent import Agent


def test_state_dir_honors_workspace_state_dir(tmp_path: Path) -> None:
    """Tier 2: an explicit workspace_state_dir becomes Agent.state_dir (events root)."""
    host_state = tmp_path / "host_state"
    agent = Agent(model="standard", workspace_state_dir=host_state)
    assert agent.state_dir == str(host_state)
    # The events directory the run will use derives from state_dir, so it
    # co-locates under the host state dir (alongside artifacts / control_ir_offload).
    events_root = Path(agent.state_dir) / "events"
    assert events_root.is_relative_to(host_state), (
        f"events must live under the explicit state dir; got {events_root}"
    )


def test_state_dir_defaults_to_reyn_when_unset(tmp_path: Path) -> None:
    """Tier 2: with no workspace_state_dir, Agent.state_dir defaults to .reyn (back-compat)."""
    agent = Agent(model="standard")
    assert agent.state_dir == ".reyn"


def test_workspace_and_events_state_dir_co_locate(tmp_path: Path) -> None:
    """Tier 2: the events root and the Workspace state dir share the same root.

    Closes the #1132 split: both derive from the one workspace_state_dir, so a
    consumer resolving an event's raw_output_ref against the offload root (which
    lives under the Workspace state dir) finds it co-located with the events.
    """
    host_state = tmp_path / "s"
    agent = Agent(model="standard", workspace_state_dir=host_state)
    # Agent.state_dir (events) and the workspace_state_dir threaded to Workspace
    # are the same host dir — events/, artifacts/, control_ir_offload/ all under it.
    assert Path(agent.state_dir) == host_state
