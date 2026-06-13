"""Tier 2: OS invariant — 2d web checkout restores BOTH substrates via the
registry attach-seam (ADR-0038 2d-2, #1533).

The 2d merge-critical gating. The web/Chainlit session is acquired via
``registry.attach`` → ``get_or_load`` (registry.py), which **auto-attaches** the
shared workspace shadow-git + anchor stores. This gate proves that auto-attach
makes a web-path-acquired session's ``checkout`` restore the **workspace**
(not just runtime) — i.e. the web session is NOT runtime-only.

Distinct from ``test_live_fork_gate`` (which builds ``ChatSession`` directly and
attaches the stores MANUALLY): here NO manual ``attach_*`` call is made — the
``get_or_load`` seam does it. If the seam didn't auto-attach (the #1556-class
web-construction bug), the workspace would NOT revert on checkout and this test
would fail. Real AgentRegistry + ChatSession + StateLog + git, no mocks; a
no-LLM ``_FakeTurnDriver`` drives the real ``_run_router_loop`` so genuine
``cut_generation`` fires (only the file write is simulated).
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.chat.session import ChatSession
from reyn.events.state_log import StateLog

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git required for the workspace substrate",
)

_WS_FILE = "code.py"


class _FakeTurnDriver:
    """No-LLM driver: writes the turn's workspace file + appends a runtime inbox
    marker, so the real ``_run_router_loop`` runs and its genuine
    ``cut_generation`` fires. Only the file write is simulated."""

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


@pytest.mark.asyncio
async def test_web_path_session_checkout_restores_workspace(tmp_path) -> None:
    """Tier 2: a session acquired via get_or_load (the web attach-seam) restores
    the WORKSPACE on checkout — proving the seam auto-attached the workspace
    store (no manual attach). The #1556-class runtime-only bug fails this."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    def _factory(profile: AgentProfile) -> ChatSession:
        snap = tmp_path / ".reyn" / "agents" / profile.name / "state" / "snapshot.json"
        return ChatSession(agent_name=profile.name, state_log=state_log, snapshot_path=snap)

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")

    # WEB acquisition path: get_or_load auto-attaches workspace + anchor stores.
    # NO manual attach_workspace_store / attach_anchor_store here — that is the
    # whole point (the seam wires them).
    session = reg.get_or_load("alpha")
    session.register_intervention_listener("test")
    session._loop_driver = _FakeTurnDriver(session, tmp_path, {"A": "v1", "B": "v2"})

    await session._run_router_loop("A", "c1")
    seq_a = session.current_snapshot.applied_seq
    await session._run_router_loop("B", "c1")
    assert (tmp_path / _WS_FILE).read_text(encoding="utf-8") == "v2"   # pre-checkout

    # Web checkout (the unified primitive) to seq A → BOTH substrates revert.
    await reg.checkout(seq_a)
    assert (tmp_path / _WS_FILE).read_text(encoding="utf-8") == "v1"   # WORKSPACE reverted
    markers = [m["payload"]["turn"] for m in session.current_snapshot.inbox]
    assert markers == ["A"]                                            # runtime reverted


@pytest.mark.asyncio
async def test_web_path_session_records_anchor_for_picker(tmp_path) -> None:
    """Tier 2: the get_or_load seam auto-attaches the anchor store too, so a web
    turn's cut_generation records the rewind-timeline anchor (the picker's
    preview + the edit pre-fill source). Empty anchor store = #1556-class bug."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    def _factory(profile: AgentProfile) -> ChatSession:
        snap = tmp_path / ".reyn" / "agents" / profile.name / "state" / "snapshot.json"
        return ChatSession(agent_name=profile.name, state_log=state_log, snapshot_path=snap)

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")
    session = reg.get_or_load("alpha")
    session.register_intervention_listener("test")
    session._loop_driver = _FakeTurnDriver(session, tmp_path, {"A": "v1"})

    await session._run_router_loop("A", "c1")
    seq_a = session.current_snapshot.applied_seq

    # The anchor store (auto-attached) recorded this boundary — both the
    # picker's truncated preview (get) AND the edit pre-fill's full source
    # (get_full) come from the turn's user_text via cut_generation, which now
    # persists both. Asserting both non-empty proves the attach end-to-end
    # (a #1556-class runtime-only acquisition would leave both empty).
    assert reg.anchor_store.get(seq_a) != ""
    assert reg.anchor_store.get_full(seq_a) != ""
