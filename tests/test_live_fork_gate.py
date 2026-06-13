"""Tier 2: OS invariant — Phase-2 end-to-end live-fork gate (ADR-0038 D8, #1533).

The Phase-2 (A)-equivalent of `test_live_rewind_gate.py`. Real `AgentRegistry` +
`ChatSession` + `StateLog` + real git (no mocks). 2a-2's two-substrate test proves
checkout *correctness* with manually-built captures; THIS gate proves the
**production-wiring composition**: a genuine `_run_router_loop` turn's
`cut_generation` auto-capture × Phase-2 `checkout` (branch-switch to an abandoned
seq) × a **post-rewind continue turn driven through the real loop** ×
checkout-back. Both substrates (runtime inbox + workspace shadow-git) must follow
the fork lineage at each step.

The genuine-new coverage over the Phase-1 gate: the Phase-1 gate stops at undo —
it never drives a turn *after* a rewind. Fork UX is "fork, then keep working", so
driving turn C through the real loop on the post-undo branch (and then reviving
the abandoned branch + checking back out) is the composition that neither 2a-2
(manual captures) nor the Phase-1 gate (undo-only) exercises.

As in the Phase-1 gate, a `_FakeTurnDriver` (no-LLM) is swapped in so the real
`_run_router_loop` runs and its genuine `cut_generation` fires; the ONLY simulated
part is the op's file-write.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.chat.session import ChatSession
from reyn.events.agent_snapshot import AgentSnapshot
from reyn.events.state_log import StateLog

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git required for the workspace substrate",
)

_WORKSPACE_FILE = "code.py"


class _FakeTurnDriver:
    """No-LLM loop_driver: each turn writes the workspace file for that turn +
    appends a runtime inbox entry. Substitutes for ``RouterLoopDriver`` so the
    real ``_run_router_loop`` runs (and its genuine ``cut_generation`` fires)
    without an LLM. The only simulated effect is the file write (the op effect).
    """

    def __init__(self, session: ChatSession, workspace_root: Path, content_by_turn: dict[str, str]):
        self._session = session
        self._ws = workspace_root
        self._content = content_by_turn

    async def run_turn(self, user_text: str, chain_id: str) -> None:
        (self._ws / _WORKSPACE_FILE).write_text(self._content[user_text], encoding="utf-8")
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
async def test_live_fork_checkout_back_follows_lineage_both_substrates(tmp_path):
    """Tier 2: real-turn captures × checkout branch-switch × post-fork continue.

    turn A (v1) → turn B (v2) → rewind_to(A) → turn C (v3, REAL turn on the
    post-undo active branch) → checkout(seqB) revives the abandoned B lineage →
    checkout(seqC) switches back. Each checkout asserts BOTH substrates follow the
    target lineage: workspace file on disk + runtime (on-disk snapshot AND live
    in-memory session — the two distinct wirings the Phase-1 gate separates).
    """
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = AgentRegistry(
        project_root=tmp_path,
        session_factory=lambda _p: (_ for _ in ()).throw(AssertionError("no factory")),
        state_log=state_log,
    )
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")
    snap_path = tmp_path / ".reyn" / "agents" / "alpha" / "state" / "snapshot.json"

    session = ChatSession(agent_name="alpha", state_log=state_log, snapshot_path=snap_path)
    session.register_intervention_listener("test")
    session.attach_workspace_store(reg.workspace_store)
    session.attach_anchor_store(reg.anchor_store)
    reg._agents["alpha"] = session
    session._loop_driver = _FakeTurnDriver(
        session, tmp_path, {"turn A": "v1", "turn B": "v2", "turn C": "v3"},
    )

    # turn A → file v1 + runtime [A]; genuine cut_generation @ seqA.
    await session._run_router_loop("turn A", "c1")
    seq_a = session.current_snapshot.applied_seq
    # turn B → file v2 + runtime [A, B]; auto-captures @ seqB.
    await session._run_router_loop("turn B", "c1")
    seq_b = session.current_snapshot.applied_seq

    # pre-fork sanity: both substrates at B, both checkpoints captured.
    assert (tmp_path / _WORKSPACE_FILE).read_text(encoding="utf-8") == "v2"
    assert _turn_markers(session.current_snapshot) == ["turn A", "turn B"]
    ws_seqs = await reg.workspace_store.seqs()
    assert {seq_a, seq_b} <= set(ws_seqs)

    # ── undo to A: B's branch becomes abandoned (a dead branch) ──
    await reg.rewind_to(seq_a)
    assert _turn_markers(session.current_snapshot) == ["turn A"]

    # ── turn C: a REAL turn through the production loop on the post-undo branch ──
    # (genuine new coverage — the session must be live-usable after a rewind).
    await session._run_router_loop("turn C", "c1")
    seq_c = session.current_snapshot.applied_seq
    assert (tmp_path / _WORKSPACE_FILE).read_text(encoding="utf-8") == "v3"
    assert _turn_markers(session.current_snapshot) == ["turn A", "turn C"]
    assert seq_b not in _active_workspace_seqs(reg, await reg.workspace_store.seqs())

    # ── checkout(seqB): branch-switch — revive the abandoned B lineage ──
    await reg.checkout(seq_b)
    assert (tmp_path / _WORKSPACE_FILE).read_text(encoding="utf-8") == "v2"   # workspace follows B
    assert _turn_markers(AgentSnapshot.load("alpha", snap_path)) == ["turn A", "turn B"]  # disk
    assert _turn_markers(session.current_snapshot) == ["turn A", "turn B"]    # live in-memory

    # ── checkout(seqC): switch back to the C lineage ──
    await reg.checkout(seq_c)
    assert (tmp_path / _WORKSPACE_FILE).read_text(encoding="utf-8") == "v3"   # workspace follows C
    assert _turn_markers(AgentSnapshot.load("alpha", snap_path)) == ["turn A", "turn C"]  # disk
    assert _turn_markers(session.current_snapshot) == ["turn A", "turn C"]    # live in-memory


def _active_workspace_seqs(reg: AgentRegistry, ws_seqs) -> set[int]:
    """The workspace generation seqs currently on the active branch."""
    from reyn.events.snapshot_generations import is_active_seq
    return {s for s in ws_seqs if is_active_seq(reg.state_log, s)}
