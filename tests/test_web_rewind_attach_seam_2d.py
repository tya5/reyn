"""Tier 2: OS invariant — 2d web checkout restores the runtime substrate via the
registry attach-seam (ADR-0038 2d-2, #1533).

The 2d merge-critical gating. The web session is acquired via
``registry.attach`` → ``get_or_load`` (registry.py), which **auto-attaches** the
shared anchor store. This gate proves that auto-attach makes a web-path-acquired
session's ``checkout`` revert the runtime substrate AND record the rewind-timeline
anchor — i.e. the web session is NOT a #1556-class runtime-only/no-anchor
acquisition.

Rewind/checkout are WAL+snapshot based (git-independent, #2248): they restore
agent / conversation state, NOT repo files. Real AgentRegistry + Session +
StateLog, no mocks; a no-LLM ``_FakeTurnDriver`` drives the real
``_run_router_loop`` so genuine ``cut_generation`` fires.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session


class _FakeTurnDriver:
    """No-LLM driver: appends a runtime inbox marker per turn, so the real
    ``_run_router_loop`` runs and its genuine ``cut_generation`` fires."""

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


@pytest.mark.asyncio
async def test_web_path_session_checkout_restores_runtime(tmp_path) -> None:
    """Tier 2: a session acquired via get_or_load (the web attach-seam) reverts the
    RUNTIME substrate on checkout (WAL+snapshot based, git-independent)."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    def _factory(profile: AgentProfile) -> Session:
        snap = tmp_path / ".reyn" / "agents" / profile.name / "state" / "snapshot.json"
        return Session(agent_name=profile.name, state_log=state_log, snapshot_path=snap)

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")

    # WEB acquisition path: get_or_load auto-attaches the anchor store.
    session = reg.get_or_load("alpha")
    session.register_intervention_listener("test")
    session._loop_driver = _FakeTurnDriver(session, tmp_path, {"A": "v1", "B": "v2"})

    await session._run_router_loop("A", "c1")
    await session._journal.flush()  # #2259 PR-2b: drain before durable read
    seq_a = session.current_snapshot.applied_seq
    await session._run_router_loop("B", "c1")

    # Web checkout (the unified primitive) to seq A → the runtime substrate reverts.
    await session._journal.flush()  # #2259 PR-2b: drain before durable read
    await reg.checkout(seq_a)
    markers = [m["payload"]["turn"] for m in session.current_snapshot.inbox]
    assert markers == ["A"]                                            # runtime reverted


@pytest.mark.asyncio
async def test_web_path_session_records_anchor_for_picker(tmp_path) -> None:
    """Tier 2: the get_or_load seam auto-attaches the anchor store too, so a web
    turn's cut_generation records the rewind-timeline anchor (the picker's
    preview + the edit pre-fill source). Empty anchor store = #1556-class bug."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    def _factory(profile: AgentProfile) -> Session:
        snap = tmp_path / ".reyn" / "agents" / profile.name / "state" / "snapshot.json"
        return Session(agent_name=profile.name, state_log=state_log, snapshot_path=snap)

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")
    session = reg.get_or_load("alpha")
    session.register_intervention_listener("test")
    session._loop_driver = _FakeTurnDriver(session, tmp_path, {"A": "v1"})

    await session._run_router_loop("A", "c1")
    await session._journal.flush()  # #2259 PR-2b: drain before durable read
    seq_a = session.current_snapshot.applied_seq

    # The anchor store (auto-attached) recorded this boundary — both the
    # picker's truncated preview (get) AND the edit pre-fill's full source
    # (get_full) come from the turn's user_text via cut_generation, which now
    # persists both. Asserting both non-empty proves the attach end-to-end
    # (a #1556-class runtime-only acquisition would leave both empty).
    assert reg.anchor_store.get(seq_a) != ""
    assert reg.anchor_store.get_full(seq_a) != ""
