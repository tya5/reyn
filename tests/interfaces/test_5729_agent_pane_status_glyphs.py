"""Tier 2: #5729 — the agent pane's per-session rows carry turn_active/
iv_waiting as 2 INDEPENDENT glyph slots, sourced from a REAL
``AgentRegistry.all_sessions_status()`` riding the real ``_snapshot()``
producer — never collapsed into one status indicator (architect ruling:
"turn dispatched AND waiting on an answer" is the one combination that
matters most to an operator, and a single glyph could not carry it).

Real ``AgentRegistry``/``Session`` throughout, mirroring
``test_3338_tui_status_chrome_liveness.py``'s own ``_real_snapshot``
producer (a local copy, not a cross-file import — this session's own
established convention, #5588)."""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.interfaces.inline.textual_chat.chrome import pane_commands, pane_payload
from reyn.interfaces.repl.status import _snapshot
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session

AGENT = "chrome-5729-agent"


async def _real_snapshot(tmp_path: Path) -> "tuple[dict, Session, AgentRegistry]":
    state_log = StateLog(tmp_path / "state.wal")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        return make_session(
            agent_name=profile.name,
            state_log=state_log,
            snapshot_path=tmp_path / f"{profile.name}_snapshot.json",
            registry=holder.get("reg"),
        )

    registry = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = registry
    AgentProfile.new(AGENT, role="").save(tmp_path / ".reyn" / "agents" / AGENT)
    session = await registry.attach(AGENT)
    snap = _snapshot(registry)
    assert snap is not None, "the real producer returned no snapshot"
    return snap, session, registry


@pytest.mark.asyncio
async def test_all_sessions_status_rides_the_real_snapshot(tmp_path) -> None:
    """Tier 2: the real ``_snapshot()`` producer carries ``all_sessions_status``
    — an idle attached session shows both bools False."""
    snap, _session, _registry = await _real_snapshot(tmp_path)
    rows = snap["all_sessions_status"]
    assert rows == [{"agent": AGENT, "sid": "main", "turn_active": False, "iv_waiting": False}]


@pytest.mark.asyncio
async def test_agent_pane_shows_no_glyphs_while_idle(tmp_path) -> None:
    """Tier 2: deny side — an idle session's row carries neither glyph."""
    snap, _session, _registry = await _real_snapshot(tmp_path)
    rows = pane_payload("agent", snapshot=snap)
    session_row = next(r for r in rows if "main" in r)
    assert "●" not in session_row
    assert "?" not in session_row


@pytest.mark.asyncio
async def test_agent_pane_shows_both_glyphs_together_never_collapsed(tmp_path) -> None:
    """Tier 2: the architect's central ruling, at the rendered-row level —
    when a session is BOTH turn_active and iv_waiting, the row carries BOTH
    glyphs, not one collapsed indicator. Driven by directly recomputing the
    pane payload against a hand-built snapshot dict carrying real
    ``all_sessions_status``-shaped rows (the pure-function boundary
    ``_agent_pane_entries`` actually renders from) — the session-driving
    half of this claim (can both bools genuinely be True at once) is
    covered end-to-end in
    tests/runtime/test_5729_status_registry_wiring.py; this test is the
    presentation half."""
    snap, _session, registry = await _real_snapshot(tmp_path)
    tree = snap["session_tree"]
    sid = tree[0]["sessions"][0]["sid"]
    snap = {**snap, "all_sessions_status": [
        {"agent": AGENT, "sid": sid, "turn_active": True, "iv_waiting": True},
    ]}
    rows = pane_payload("agent", snapshot=snap)
    session_row = next(r for r in rows if sid in r)
    assert "●" in session_row, f"turn_active glyph missing: {session_row!r}"
    assert "?" in session_row, f"iv_waiting glyph missing: {session_row!r}"

    cmds = pane_commands("agent", snap)
    assert len(cmds) == len(rows), "agent rows and their commands drifted apart"


@pytest.mark.asyncio
async def test_agent_pane_status_is_process_scoped_never_fabricated_for_a_sibling(
    tmp_path,
) -> None:
    """Tier 2: deny side — a session absent from ``all_sessions_status``
    (e.g. a sibling process's session, #5729's own process-scope limit)
    renders blank glyphs, never a fabricated "not running" mark. Simulated
    here by simply omitting the row (this process genuinely cannot see a
    sibling process's session — there is no live one to construct)."""
    snap, _session, _registry = await _real_snapshot(tmp_path)
    snap = {**snap, "all_sessions_status": []}
    rows = pane_payload("agent", snapshot=snap)
    session_row = next(r for r in rows if "main" in r)
    assert "●" not in session_row
    assert "?" not in session_row
