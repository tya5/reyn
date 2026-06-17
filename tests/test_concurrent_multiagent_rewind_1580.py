"""Tier 2: OS invariant — concurrent multi-agent rewind coherence (#1580, owner-question).

Owner asked: parallel agent contexts have their turn boundaries at DIFFERENT global
seqs — is a rewind still coherent? The mechanism is designed (one global single-seq
WAL → a single reset-record is a global consistent-cut across every agent;
``await_quiescent`` barrier, #1533) but every prior rewind test is single-agent.
This gate exercises the multi-agent path end-to-end.

The load-bearing assertion: rewinding to **one** agent's boundary seq correctly cuts
**every** agent's runtime AND the single shared workspace to the consistent
global-seq state — i.e. the cut is global, not per-agent. A per-agent (non-global)
rewind would leave the other agent's post-cut turns live and the workspace
inconsistent → this test would fail.

Real AgentRegistry + 2 real ChatSessions (shared state_log + workspace) + git; a
no-LLM ``_FakeTurnDriver`` drives the real ``_run_router_loop`` so genuine
``cut_generation`` fires (only the file write is simulated). No mocks.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.chat.session import ChatSession
from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.state_log import StateLog

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git required for the workspace substrate",
)

_WS_FILE = "code.py"


class _FakeTurnDriver:
    """No-LLM driver: writes the SHARED workspace file (keyed by user_text) + a
    runtime inbox marker, so the real ``_run_router_loop`` runs and its genuine
    ``cut_generation`` fires. Only the write is simulated."""

    def __init__(self, session: ChatSession, ws_root: Path, content: dict[str, str]) -> None:
        self._session = session
        self._ws = ws_root
        self._content = content

    async def run_turn(self, user_text: str, chain_id: str) -> None:
        (self._ws / _WS_FILE).write_text(self._content[user_text], encoding="utf-8")
        await self._session._journal.append_inbox(
            kind="user_message", payload={"turn": user_text},
        )

    def request_cancel(self) -> None:
        return None

    def is_cancel_requested(self) -> bool:
        return False


def _markers(snap: AgentSnapshot) -> list[str]:
    return [m["payload"]["turn"] for m in snap.inbox]


def _two_agent_registry(tmp_path: Path):
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    def _factory(profile: AgentProfile) -> ChatSession:
        snap = tmp_path / ".reyn" / "agents" / profile.name / "state" / "snapshot.json"
        return ChatSession(agent_name=profile.name, state_log=state_log, snapshot_path=snap)

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    for name in ("alpha", "beta"):
        AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)
    alpha = reg.get_or_load("alpha")   # web/TUI seam → attaches shared workspace + anchor stores
    beta = reg.get_or_load("beta")
    for s, content in (
        (alpha, {"a1": "A1", "a2": "A2", "a3": "A3"}),
        (beta, {"b1": "B1", "b2": "B2", "b3": "B3"}),
    ):
        s.register_intervention_listener("test")
        s._loop_driver = _FakeTurnDriver(s, tmp_path, content)
    return reg, alpha, beta


def _snap_path(tmp_path: Path, name: str) -> Path:
    return tmp_path / ".reyn" / "agents" / name / "state" / "snapshot.json"


@pytest.mark.asyncio
async def test_concurrent_2agent_rewind_is_global_consistent_cut(tmp_path):
    """Tier 2: rewinding to ONE agent's boundary cuts EVERY agent + the workspace.

    Interleaved global seqs: a1 < b1 < a2 < b2. Rewind to **beta's** first boundary
    (seq_b1) — a seq that is NOT alpha's boundary. The global consistent-cut must:
      - workspace → the as-of-seq_b1 state (last write ≤ seq_b1 = beta's B1)
      - alpha runtime → [a1] (alpha's a2 at seq_a2 > seq_b1 is abandoned — the
        cross-agent cut: alpha is cut even though seq_b1 is beta's boundary)
      - beta runtime → [b1] (beta's b2 abandoned)
    """
    reg, alpha, beta = _two_agent_registry(tmp_path)

    await alpha._run_router_loop("a1", "c1")
    seq_a1 = alpha.current_snapshot.applied_seq
    await beta._run_router_loop("b1", "c1")
    seq_b1 = beta.current_snapshot.applied_seq
    await alpha._run_router_loop("a2", "c1")
    seq_a2 = alpha.current_snapshot.applied_seq
    await beta._run_router_loop("b2", "c1")

    # interleaved, distinct global seqs (the owner's "different boundary seqs").
    assert seq_a1 < seq_b1 < seq_a2
    assert (tmp_path / _WS_FILE).read_text(encoding="utf-8") == "B2"          # pre-rewind head
    assert _markers(alpha.current_snapshot) == ["a1", "a2"]
    assert _markers(beta.current_snapshot) == ["b1", "b2"]

    # ── global rewind to BETA's first boundary ──
    await reg.rewind_to(seq_b1)

    # workspace: the consistent global-seq state as-of seq_b1 (beta's B1).
    assert (tmp_path / _WS_FILE).read_text(encoding="utf-8") == "B1"
    # alpha cut too (a2 > seq_b1 abandoned) — even though seq_b1 is BETA's boundary.
    assert _markers(alpha.current_snapshot) == ["a1"]
    assert _markers(AgentSnapshot.load("alpha", _snap_path(tmp_path, "alpha"))) == ["a1"]
    # beta cut (b2 abandoned).
    assert _markers(beta.current_snapshot) == ["b1"]
    assert _markers(AgentSnapshot.load("beta", _snap_path(tmp_path, "beta"))) == ["b1"]


@pytest.mark.asyncio
async def test_both_agents_resume_after_concurrent_rewind(tmp_path):
    """Tier 2: after a global rewind both agents (quiesced by the barrier) resume.

    Each agent ran a turn (so each was driven + quiesced through the rewind's
    cancel_inflight + await_quiescent barrier); after the rewind both run a NEW
    turn through the real loop — proving the quiesced sessions are live-usable, and
    the new turns extend the post-rewind active branch coherently.
    """
    reg, alpha, beta = _two_agent_registry(tmp_path)

    await alpha._run_router_loop("a1", "c1")
    seq_a1 = alpha.current_snapshot.applied_seq
    await beta._run_router_loop("b1", "c1")          # > seq_a1, abandoned by the rewind

    await reg.rewind_to(seq_a1)                        # cut to alpha's first boundary
    assert _markers(alpha.current_snapshot) == ["a1"]
    assert _markers(beta.current_snapshot) == []      # beta's b1 abandoned

    # both resume through the real loop on the post-rewind active branch.
    await alpha._run_router_loop("a3", "c1")
    await beta._run_router_loop("b3", "c1")
    assert _markers(alpha.current_snapshot) == ["a1", "a3"]
    assert _markers(beta.current_snapshot) == ["b3"]
    assert (tmp_path / _WS_FILE).read_text(encoding="utf-8") == "B3"   # last post-rewind write


# ── single-agent internal concurrency (owner follow-up): await_quiescent coverage ──


@pytest.mark.asyncio
async def test_single_agent_inflight_skill_plan_intervention_drained_by_rewind(tmp_path):
    """Tier 2: rewind drains in-flight skill + plan + intervention tasks — no
    straggler WAL append crosses the reset-record (await_quiescent coverage, #1533).

    Owner follow-up: within ONE agent, chat/plan/skill run concurrently. This
    exercises ``await_quiescent``'s append-capable coverage set LIVE end-to-end
    (not per-source): an in-flight skill (``running_skills``), plan
    (``running_plans``), and fire-and-forget intervention task
    (``_inflight_wal_tasks``) are all parked *before* their would-be WAL append
    when the rewind fires. The barrier (``cancel_inflight`` + ``await_quiescent``)
    must drain every one so none appends past the reset-record. If any escaped,
    its append would land after R (the straggler bug #1533 guards) → this fails.
    """
    reg, alpha, _beta = _two_agent_registry(tmp_path)
    state_log = reg.state_log

    await alpha._run_router_loop("a1", "c1")
    seq_a1 = alpha.current_snapshot.applied_seq

    # Inject in-flight, append-capable tasks parked BEFORE their append (in-flight
    # at the moment of rewind). Each would append a distinct straggler marker if it
    # were ever released — proving "no straggler crosses R" is a real assertion.
    release = asyncio.Event()

    async def _parked_then_append(marker: str) -> None:
        await release.wait()
        await state_log.append(
            "inbox_put", target="alpha", msg_id=marker, msg_kind="user", payload={},
        )

    skill_t = asyncio.create_task(_parked_then_append("skill-straggler"))
    plan_t = asyncio.create_task(_parked_then_append("plan-straggler"))
    iv_t = asyncio.create_task(_parked_then_append("iv-straggler"))
    alpha.running_skills["s1"] = skill_t          # in-flight skill
    alpha.running_plans["p1"] = plan_t            # in-flight plan
    alpha._track_wal_task(iv_t)                    # fire-and-forget intervention task
    for _ in range(5):                            # let each reach `await release.wait()`
        await asyncio.sleep(0)

    # ── global rewind: the cancel_inflight + await_quiescent barrier must drain all ──
    res = await reg.rewind_to(seq_a1)
    reset_seq = res["reset_seq"]

    # every in-flight append-capable task settled (cancelled + joined → done).
    assert skill_t.done() and plan_t.done() and iv_t.done()
    # the reset-record is the head — no straggler append crossed it.
    assert state_log.current_seq == reset_seq

    # releasing the (now-cancelled) tasks cannot resurrect a straggler append.
    release.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert state_log.current_seq == reset_seq
    appended = {e.get("msg_id") for e in state_log.iter_from(1)}
    assert appended.isdisjoint({"skill-straggler", "plan-straggler", "iv-straggler"})
