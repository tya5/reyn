"""Tier 2: OS invariant — Phase-2 end-to-end live-fork gate (ADR-0038 D8, #1533).

The Phase-2 (A)-equivalent of `test_live_rewind_gate.py`. Real `AgentRegistry` +
`Session` + `StateLog` (no mocks). 2a-2's checkout test proves correctness with
manually-built captures; THIS gate proves the **production-wiring composition**: a
genuine `_run_router_loop` turn's `cut_generation` auto-capture × Phase-2
`checkout` (branch-switch to an abandoned seq) × a **post-rewind continue turn
driven through the real loop** × checkout-back. The runtime substrate (inbox,
WAL+snapshot based, git-independent) must follow the fork lineage at each step.

The genuine-new coverage over the Phase-1 gate: the Phase-1 gate stops at undo —
it never drives a turn *after* a rewind. Fork UX is "fork, then keep working", so
driving turn C through the real loop on the post-undo branch (and then reviving
the abandoned branch + checking back out) is the composition that neither 2a-2
(manual captures) nor the Phase-1 gate (undo-only) exercises.

As in the Phase-1 gate, a `_FakeTurnDriver` (no-LLM) is swapped in so the real
`_run_router_loop` runs and its genuine `cut_generation` fires.
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
        await self._session._journal.append_inbox(
            kind="user_message", payload={"turn": user_text},
        )

    def request_cancel(self) -> None:
        return None

    def is_cancel_requested(self) -> bool:
        return False


def _turn_markers(snap: AgentSnapshot) -> list[str]:
    return [m["payload"]["turn"] for m in snap.inbox]


@pytest.mark.asyncio
async def test_live_fork_checkout_back_follows_lineage_runtime(tmp_path):
    """Tier 2: real-turn captures × checkout branch-switch × post-fork continue.

    turn A → turn B → rewind_to(A) → turn C (REAL turn on the post-undo active
    branch) → checkout(seqB) revives the abandoned B lineage → checkout(seqC)
    switches back. Each checkout asserts the runtime substrate follows the target
    lineage: on-disk snapshot AND live in-memory session — the two distinct wirings
    the Phase-1 gate separates.
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
    session.attach_anchor_store(reg.anchor_store)
    reg._sessions["alpha"] = {"main": session}
    session._loop_driver = _FakeTurnDriver(
        session, tmp_path, {"turn A": "v1", "turn B": "v2", "turn C": "v3"},
    )

    # turn A → runtime [A]; genuine cut_generation @ seqA.
    await session._run_router_loop("turn A", "c1")
    await session._journal.flush()  # #2259 PR-2b: drain before durable read
    seq_a = session.current_snapshot.applied_seq
    # turn B → runtime [A, B]; auto-captures @ seqB.
    await session._run_router_loop("turn B", "c1")
    await session._journal.flush()  # #2259 PR-2b: drain before durable read
    seq_b = session.current_snapshot.applied_seq

    # pre-fork sanity: the runtime substrate at B.
    assert _turn_markers(session.current_snapshot) == ["turn A", "turn B"]

    # ── undo to A: B's branch becomes abandoned (a dead branch) ──
    await reg.rewind_to(seq_a)
    assert _turn_markers(session.current_snapshot) == ["turn A"]

    # ── turn C: a REAL turn through the production loop on the post-undo branch ──
    # (genuine new coverage — the session must be live-usable after a rewind).
    await session._run_router_loop("turn C", "c1")
    await session._journal.flush()  # #2259 PR-2b: drain before durable read
    seq_c = session.current_snapshot.applied_seq
    assert _turn_markers(session.current_snapshot) == ["turn A", "turn C"]

    # ── checkout(seqB): branch-switch — revive the abandoned B lineage ──
    await reg.checkout(seq_b)
    assert _turn_markers(AgentSnapshot.load("alpha", snap_path)) == ["turn A", "turn B"]  # disk
    assert _turn_markers(session.current_snapshot) == ["turn A", "turn B"]    # live in-memory

    # ── checkout(seqC): switch back to the C lineage ──
    await reg.checkout(seq_c)
    assert _turn_markers(AgentSnapshot.load("alpha", snap_path)) == ["turn A", "turn C"]  # disk
    assert _turn_markers(session.current_snapshot) == ["turn A", "turn C"]    # live in-memory
