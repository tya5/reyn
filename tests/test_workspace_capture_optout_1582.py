"""Tier 2: OS invariant — rewind/checkout is runtime-only (WAL+snapshot, #1582/#2248).

Rewind/checkout/PITR are WAL+snapshot based and git-independent: they revert the
runtime substrate (AgentSnapshot generations + WAL) along a consistent cut. Repo
files are NOT versioned by Reyn (the prior shadow-git workspace layer was removed
in #2248), so a rewind reverts agent / conversation state but leaves the workspace
files as-is.

Real AgentRegistry + Session + StateLog; a no-LLM `_FakeTurnDriver` drives the
real `_run_router_loop` so genuine `cut_generation` fires. No mocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.snapshot_generations import is_active_seq
from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session

_WS_FILE = "code.py"


class _FakeTurnDriver:
    """No-LLM driver: appends a runtime inbox marker per turn so the real
    `_run_router_loop` runs and its genuine `cut_generation` fires."""

    def __init__(self, session: Session, ws_root: Path, content: dict[str, str]) -> None:
        self._session = session
        self._ws = ws_root
        self._content = content

    async def run_turn(self, user_text: str, chain_id: str) -> None:
        await self._session._journal.append_inbox(
            kind="user_message", payload={"turn": user_text},
        )

    def request_cancel(self) -> None:
        return None

    def is_cancel_requested(self) -> bool:
        return False


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    def _factory(profile: AgentProfile) -> Session:
        snap = tmp_path / ".reyn" / "agents" / profile.name / "state" / "snapshot.json"
        return make_session(agent_name=profile.name, state_log=state_log, snapshot_path=snap)

    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
    )
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")
    return reg


@pytest.mark.asyncio
async def test_rewind_reverts_runtime_not_workspace(tmp_path) -> None:
    """Tier 2: a turn's cut_generation records the runtime generation; a checkout
    reverts the runtime substrate (inbox markers) while the workspace file is left
    as-is — coherent runtime-only rewind (the consistent-cut-without-workspace
    invariant; repo files are not Reyn-versioned, #2248)."""
    reg = _make_registry(tmp_path)
    session = await reg.attach("alpha")
    session.register_intervention_listener("test")
    session._loop_driver = _FakeTurnDriver(session, tmp_path, {"A": "vA", "B": "vB"})
    # a workspace file the user/agent owns — Reyn does not version it.
    (tmp_path / _WS_FILE).write_text("vB", encoding="utf-8")

    await session._run_router_loop("A", "c1")
    await session._journal.flush()  # #2259 PR-2b: drain before durable read
    seq_a = session.current_snapshot.applied_seq
    await session._run_router_loop("B", "c1")

    # Runtime checkout works (consistent-cut on the runtime substrate alone).
    await session._journal.flush()  # #2259 PR-2b: drain before durable read
    await reg.checkout(seq_a)
    markers = [m["payload"]["turn"] for m in session.current_snapshot.inbox]
    assert markers == ["A"]                                  # runtime reverted
    assert is_active_seq(reg.state_log, seq_a)               # cut landed on the runtime substrate
    # Workspace NOT reverted — repo files are the user's work product, not rewound.
    assert (tmp_path / _WS_FILE).read_text(encoding="utf-8") == "vB"
