"""Tier 2: OS invariant — Phase-1 end-to-end live-rewind gate (ADR-0038, #1533).

Real `AgentRegistry` + `Session` + `StateLog` (no mocks). The unit tests cover
each piece (await_quiescent, rewind_to, the cut_generation wiring); THIS gate
proves the **composition** end-to-end — that a live session's turn-boundary
auto-capture and a subsequent global rewind revert the runtime substrate
(AgentSnapshot, WAL+snapshot based, git-independent) to as-of-N.

Critically, the gate drives the **real production boundary** ``_run_router_loop``
via a Fake (no-LLM) loop_driver, so the **genuine** ``cut_generation`` call-site
fires — calling ``cut_generation`` directly would give the gate the very wiring
blind-spot it exists to catch (if ``_run_router_loop`` ever stops calling it, a
direct-call gate stays green).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


class _FakeTurnDriver:
    """No-LLM loop_driver: each turn appends a runtime inbox entry. Substitutes
    for ``RouterLoopDriver`` so the real ``_run_router_loop`` runs (and its genuine
    ``cut_generation`` fires) without an LLM.
    """

    def __init__(self, session: Session, workspace_root: Path, content_by_turn: dict[str, str]):
        self._session = session
        self._ws = workspace_root
        self._content = content_by_turn

    async def run_turn(self, user_text: str, chain_id: str) -> None:
        # a real runtime mutation so the snapshot differs turn-to-turn.
        await self._session._journal.append_inbox(
            kind="user_message", payload={"turn": user_text},
        )

    # cancel protocol the rewind's cancel_inflight drives (no live turn here → no-op).
    def request_cancel(self) -> None:
        return None

    def is_cancel_requested(self) -> bool:
        return False


def _turn_markers(snap: AgentSnapshot) -> list[str]:
    return [m["payload"]["turn"] for m in snap.inbox]


@pytest.mark.asyncio
async def test_live_rewind_reverts_runtime_to_as_of_n(tmp_path):
    """Tier 2: a live session's auto-capture → global rewind reverts the runtime substrate.

    Drives the real ``_run_router_loop`` twice (turn A, turn B), so the genuine
    ``cut_generation`` auto-captures the runtime snapshot at each boundary.
    ``rewind_to`` the turn-A checkpoint must revert:
      - runtime (disk): the persisted snapshot is as-of-A
      - runtime (live in-memory): the loaded session reflects as-of-A
    The runtime disk + live assertions are SEPARATE wirings (the on-disk save vs
    ``reset_for_rewind`` + ``restore_state``'s ``journal.install``); a disk-only
    assert would miss a stale live session, which would corrupt the next turn.
    """
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = AgentRegistry(
        project_root=tmp_path,
        session_factory=lambda _p: (_ for _ in ()).throw(AssertionError("no factory")),
        state_log=state_log,
    )
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")
    snap_path = tmp_path / ".reyn" / "agents" / "alpha" / "state" / "snapshot.json"

    session = make_session(agent_name="alpha", state_log=state_log, snapshot_path=snap_path)
    session.register_intervention_listener("test")
    # production wiring: the registry injects its single shared anchor store.
    session.attach_anchor_store(reg.anchor_store)
    reg._sessions["alpha"] = {"main": session}
    # swap the no-LLM driver in; the rest of _run_router_loop (incl cut_generation) is real.
    session._loop_driver = _FakeTurnDriver(
        session, tmp_path, {"turn A": "v1", "turn B": "v2"},
    )

    # turn A → runtime [A]; genuine cut_generation auto-captures @ seqA.
    await session._run_router_loop("turn A", "c1")
    await session._journal.flush()  # #2259 PR-2b: drain async WAL+snapshot+gen (applied_seq durable)
    seq_a = session.current_snapshot.applied_seq
    # turn B → runtime [A, B]; auto-captures @ seqB.
    await session._run_router_loop("turn B", "c1")

    # pre-rewind sanity: the runtime substrate advanced to B.
    assert _turn_markers(session.current_snapshot) == ["turn A", "turn B"]

    # ── global rewind to the turn-A checkpoint ──
    await session._journal.flush()  # #2259 PR-2b: WAL+gens durable before the rewind reads them
    await reg.rewind_to(seq_a)

    # runtime substrate, DISK location: persisted snapshot is as-of-A.
    assert _turn_markers(AgentSnapshot.load("alpha", snap_path)) == ["turn A"]
    # runtime substrate, LIVE IN-MEMORY location: the loaded session is as-of-A
    # (reset_for_rewind + restore_state's journal.install — distinct from the save).
    assert _turn_markers(session.current_snapshot) == ["turn A"]
