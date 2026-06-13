"""Tier 2: OS invariant — the 2d web edit path inherits 2c fork semantics
(ADR-0038 2d-3, #1533).

The 2d-3 merge-critical gating, lead-required. The web edit glue is thin; what
must be proven is that the chainlit-free ``resolve_edit_target`` +
``handle_rewind_edit_submit`` — driven through a session acquired via the web
``get_or_load`` seam — produce the SAME fork semantics as TUI 2c
``_submit_edited_fork`` (not merely share function names). Three invariants:

- **edit = new fork**: the edited message runs as a new active branch.
- **original = inactive**: the original turn lands on a now-abandoned branch.
- **lineage cross-fork-point**: editing a forked branch's FIRST turn resolves the
  fork target to the PARENT's fork-point turn (not None, not a same-branch miss).

Plus the genesis guard (first turn → reject, decision B). Real AgentRegistry +
ChatSession + StateLog + git; a no-LLM ``_FakeTurnDriver`` drives the real
``_run_router_loop`` so genuine ``cut_generation`` / fork records fire. No mocks.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from reyn.chainlit_app.rewind_actions import (
    handle_rewind_edit_submit,
    resolve_edit_target,
)
from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.chat.session import ChatSession
from reyn.events.snapshot_generations import is_active_seq
from reyn.events.state_log import StateLog

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git required for the workspace substrate",
)

_WS_FILE = "code.py"


class _FakeTurnDriver:
    """No-LLM driver: writes the turn's workspace file (keyed by user_text) +
    appends a runtime inbox marker, so the real ``_run_router_loop`` runs and its
    genuine ``cut_generation`` / fork records fire. Only the write is simulated."""

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


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    def _factory(profile: AgentProfile) -> ChatSession:
        snap = tmp_path / ".reyn" / "agents" / profile.name / "state" / "snapshot.json"
        return ChatSession(agent_name=profile.name, state_log=state_log, snapshot_path=snap)

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")
    return reg


@pytest.mark.asyncio
async def test_web_edit_makes_new_fork_original_inactive(tmp_path) -> None:
    """Tier 2: editing turn B (web path) re-runs from B's predecessor (A) as a new
    fork — the edited turn is active, the original B is on an abandoned branch,
    and the workspace reflects the edited content."""
    reg = _make_registry(tmp_path)
    session = await reg.attach("alpha")           # web path (attach → get_or_load auto-attach)
    session.register_intervention_listener("test")
    session._loop_driver = _FakeTurnDriver(
        session, tmp_path, {"A": "vA", "B": "vB", "B-edited": "vB2"},
    )

    await session._run_router_loop("A", "c1")
    seq_a = session.current_snapshot.applied_seq
    await session._run_router_loop("B", "c1")
    seq_b = session.current_snapshot.applied_seq
    assert (tmp_path / _WS_FILE).read_text(encoding="utf-8") == "vB"

    # Resolve the edit target for B: predecessor TURN = A.
    info = resolve_edit_target(reg, seq_b)
    assert info["can_edit"] is True
    assert info["fork_target"] == seq_a

    # Submit the edited message = checkout(A) → enqueue "B-edited" on the
    # reset-in-place attached session. checkout already reverted the workspace +
    # abandoned B before the new turn runs.
    result = await handle_rewind_edit_submit(reg, info["fork_target"], "B-edited")
    assert result == ""                                            # success drains via outbox
    assert (tmp_path / _WS_FILE).read_text(encoding="utf-8") == "vA"   # checked out to A
    assert not is_active_seq(reg.state_log, seq_b)                 # original B abandoned

    # Drain the inbox through the real consumer (the production web loop does
    # this) → the edited turn re-runs from A = a new active fork. checkout
    # re-queued the reset snapshot's inbox markers, so drain rather than a single
    # step (the markers dispatch as no-ops; the "user" submission runs the turn).
    while session.inbox.qsize() > 0:
        await session.run_one_iteration()
    assert (tmp_path / _WS_FILE).read_text(encoding="utf-8") == "vB2"   # edited turn ran
    assert is_active_seq(reg.state_log, session.current_snapshot.applied_seq)  # new fork active
    assert not is_active_seq(reg.state_log, seq_b)                 # original B still abandoned


@pytest.mark.asyncio
async def test_web_edit_cross_fork_point_resolves_parent_turn(tmp_path) -> None:
    """Tier 2: editing a FORKED branch's first turn resolves the fork target to
    the PARENT's fork-point turn (lineage cross-fork-point) — not None, not a
    same-branch miss. Mirrors the 2c predecessor cross-fork case on the web path."""
    reg = _make_registry(tmp_path)
    session = reg.get_or_load("alpha")
    session.register_intervention_listener("test")
    session._loop_driver = _FakeTurnDriver(
        session, tmp_path, {"A": "vA", "B": "vB", "C": "vC"},
    )

    await session._run_router_loop("A", "c1")
    seq_a = session.current_snapshot.applied_seq
    await session._run_router_loop("B", "c1")

    # Fork: rewind to A, then run C → C is the FIRST turn of a new branch off A.
    await reg.checkout(seq_a)
    await session._run_router_loop("C", "c1")
    seq_c = session.current_snapshot.applied_seq

    info = resolve_edit_target(reg, seq_c)
    assert info["can_edit"] is True
    # C is its branch's first turn, but the lineage predecessor crosses the
    # fork point back to the parent's turn A — NOT None, NOT same-branch.
    assert info["fork_target"] == seq_a


@pytest.mark.asyncio
async def test_web_edit_first_turn_rejected(tmp_path) -> None:
    """Tier 2: the first turn has no earlier turn to fork from → resolve rejects
    (decision B reject-on-click), the glue never reaches checkout/submit."""
    reg = _make_registry(tmp_path)
    session = reg.get_or_load("alpha")
    session.register_intervention_listener("test")
    session._loop_driver = _FakeTurnDriver(session, tmp_path, {"A": "vA"})

    await session._run_router_loop("A", "c1")
    seq_a = session.current_snapshot.applied_seq

    info = resolve_edit_target(reg, seq_a)
    assert info["can_edit"] is False
    assert info["fork_target"] is None
    assert "first turn" in info["reason"]
