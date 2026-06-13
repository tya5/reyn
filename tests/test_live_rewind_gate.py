"""Tier 2: OS invariant — Phase-1 end-to-end live-rewind gate (ADR-0038, #1533).

Real `AgentRegistry` + `ChatSession` + `StateLog` + real git (no mocks). The unit
tests cover each piece (await_quiescent, rewind_to, WorkspaceVersionStore, the
cut_generation capture wiring); THIS gate proves the **composition** end-to-end —
that a live session's turn-boundary auto-capture and a subsequent global rewind
revert **both substrates** (runtime AgentSnapshot + workspace files) to as-of-N.

Critically, the gate drives the **real production boundary** ``_run_router_loop``
via a Fake (no-LLM) loop_driver, so the **genuine** ``cut_generation`` call-site
fires — calling ``cut_generation`` directly would give the gate the very wiring
blind-spot it exists to catch (if ``_run_router_loop`` ever stops calling it, a
direct-call gate stays green). The ONLY simulated part is the op's file-write.
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
        # simulated op effect: the turn writes a workspace file.
        (self._ws / _WORKSPACE_FILE).write_text(self._content[user_text], encoding="utf-8")
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
async def test_live_rewind_reverts_both_substrates_to_as_of_n(tmp_path):
    """Tier 2: a live session's auto-capture → global rewind reverts BOTH substrates.

    Drives the real ``_run_router_loop`` twice (turn A writes file v1, turn B writes
    v2), so the genuine ``cut_generation`` auto-captures runtime + workspace at each
    boundary. ``rewind_to`` the turn-A checkpoint must revert:
      - workspace: ``code.py`` on disk back to v1
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

    session = ChatSession(agent_name="alpha", state_log=state_log, snapshot_path=snap_path)
    session.register_intervention_listener("test")
    # production wiring: the registry injects its single shared stores.
    session.attach_workspace_store(reg.workspace_store)
    session.attach_anchor_store(reg.anchor_store)
    reg._agents["alpha"] = session
    # swap the no-LLM driver in; the rest of _run_router_loop (incl cut_generation) is real.
    session._loop_driver = _FakeTurnDriver(
        session, tmp_path, {"turn A": "v1", "turn B": "v2"},
    )

    # turn A → file v1 + runtime [A]; genuine cut_generation auto-captures @ seqA.
    await session._run_router_loop("turn A", "c1")
    seq_a = session.current_snapshot.applied_seq
    # turn B → file v2 + runtime [A, B]; auto-captures @ seqB.
    await session._run_router_loop("turn B", "c1")

    # pre-rewind sanity: both substrates advanced to B.
    assert (tmp_path / _WORKSPACE_FILE).read_text(encoding="utf-8") == "v2"
    assert _turn_markers(session.current_snapshot) == ["turn A", "turn B"]
    # the auto-capture actually fired (else the gate would be vacuous).
    assert seq_a in reg.workspace_store.seqs()

    # ── global rewind to the turn-A checkpoint ──
    await reg.rewind_to(seq_a)

    # workspace substrate: real file reverted to v1.
    assert (tmp_path / _WORKSPACE_FILE).read_text(encoding="utf-8") == "v1"
    # runtime substrate, DISK location: persisted snapshot is as-of-A.
    assert _turn_markers(AgentSnapshot.load("alpha", snap_path)) == ["turn A"]
    # runtime substrate, LIVE IN-MEMORY location: the loaded session is as-of-A
    # (reset_for_rewind + restore_state's journal.install — distinct from the save).
    assert _turn_markers(session.current_snapshot) == ["turn A"]
