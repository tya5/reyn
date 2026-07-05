"""Tier 2: #2585 PR2 — ephemeral pipeline-spawned sessions force non_interactive=True.

``AgentRegistry.spawn_session_recorded(mode="ephemeral")`` is the primitive behind
``run_agent_step`` (pipeline ``agent`` steps): the spawned worker receives exactly ONE
prompt via ``MessageBus.request`` and returns — structurally headless, no interactive
user on the other end. But the spawned session is constructed via the SAME shared
``session_factory`` the registry was built with (captured from whichever frontend
launched the pipeline — A2A/MCP/chainlit/dogfood all deliberately set
``non_interactive=False``, "interactive byte-identical"). Without an override, an
ephemeral worker could inherit ``non_interactive=False`` and land on the SP's "ask ONE
clarifying question" branch with no one to answer it.

This test builds a registry whose factory mirrors that interactive-frontend default
(``non_interactive=False``) and asserts:
  1. an ephemeral spawn is forced to ``non_interactive=True`` regardless of the
     factory's default (the fix, in ``AgentRegistry.spawn_session_recorded``'s
     ``mode == "ephemeral"`` branch);
  2. a persistent spawn is NOT touched — it keeps whatever the factory set — because a
     persistent spawn (e.g. agent-to-agent multi-session) may eventually have a real
     user on the other end, so forcing it there would be wrong.

Real AgentRegistry + real Session (no mocks); mirrors the ``holder`` deferred-registry-
ref factory pattern used by ``test_2103_A_ephemeral_auto_vanish_1953.py`` /
``test_pipeline_r5_run_agent_step.py``. Assertions read the PUBLIC ``session.
non_interactive`` property (added alongside this fix), not the "private"
``_non_interactive`` attribute directly.

FALSIFY: drop the ``ephemeral_session._non_interactive = True`` line in
``spawn_session_recorded`` → the ephemeral assertion goes RED (stays False, inherited
from the factory).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session


def _make_registry(tmp_path: Path) -> AgentRegistry:
    """A registry whose factory mirrors an interactive frontend (chainlit/A2A/MCP/
    dogfood): every constructed session gets ``non_interactive=False`` — the shared
    default per ``scoped_session_factory.py``'s "interactive byte-identical" comment."""
    state_log = StateLog(tmp_path / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        s = Session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=False,
        )
        s.register_intervention_listener("test")
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    AgentProfile.new("alice", role="").save(tmp_path / ".reyn" / "agents" / "alice")
    return reg


@pytest.mark.asyncio
async def test_ephemeral_spawn_forces_non_interactive_true(tmp_path):
    """Tier 2: #2585 PR2 — an ephemeral spawn is forced to non_interactive=True even
    though the factory's default (mirroring an interactive frontend) is False. RED if
    the override in spawn_session_recorded's ephemeral branch is dropped."""
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")  # the live main session (factory default: False)

    eph_sid = await reg.spawn_session_recorded("alice", mode="ephemeral")
    eph = reg._peek_session("alice", eph_sid)

    assert eph.non_interactive is True


@pytest.mark.asyncio
async def test_persistent_spawn_keeps_factory_default(tmp_path):
    """Tier 2: #2585 PR2 — the fix is scoped to ephemeral spawns only: a persistent
    spawn (e.g. agent-to-agent multi-session) keeps whatever the factory set
    (False here), proving the override does NOT leak into spawn_session() itself.
    RED if a future change moves the override into the shared spawn_session path."""
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")

    per_sid = await reg.spawn_session_recorded("alice", mode="persistent")
    per = reg._peek_session("alice", per_sid)

    assert per.non_interactive is False
