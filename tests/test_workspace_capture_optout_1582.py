"""Tier 2: OS invariant — workspace-capture opt-out = runtime-only rewind (#1582).

`time_travel.workspace_capture: false` selects runtime-only rewind: the registry
attaches NO workspace store, so `cut_generation` skips the per-boundary shadow-git
capture (the largest constant cost) while the runtime substrate (AgentSnapshot
generations + WAL) stays intact + consistent-cut-coherent. The existing
None-guards (attach / capture / restore) make this coherent by construction —
these tests pin that the gate flips the behavior and runtime rewind still works.

Non-default round-trip (feedback_roundtrip_test_nondefault_value): the opt-out
(`False`, the NON-default) is exercised end-to-end — gate off → no workspace gen
+ rewind reverts runtime but NOT the workspace; default (`True`) → workspace gen
present. Real AgentRegistry + ChatSession + StateLog; a no-LLM `_FakeTurnDriver`
drives the real `_run_router_loop` so genuine `cut_generation` fires. No mocks.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.chat.session import ChatSession
from reyn.config import TimeTravelConfig, _build_time_travel_config
from reyn.events.snapshot_generations import is_active_seq
from reyn.events.state_log import StateLog

_WS_FILE = "code.py"


class _FakeTurnDriver:
    """No-LLM driver: writes the turn's workspace file + appends a runtime inbox
    marker so the real `_run_router_loop` runs and its genuine `cut_generation`
    fires. Only the file write is simulated."""

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


def _make_registry(tmp_path: Path, *, workspace_capture: bool) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    def _factory(profile: AgentProfile) -> ChatSession:
        snap = tmp_path / ".reyn" / "agents" / profile.name / "state" / "snapshot.json"
        return ChatSession(agent_name=profile.name, state_log=state_log, snapshot_path=snap)

    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
        workspace_capture=workspace_capture,
    )
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")
    return reg


# ── config layer (round-trip, non-default) ────────────────────────────────


def test_config_parse_non_default_and_default() -> None:
    """Tier 2: time_travel.workspace_capture parses the NON-default (False) and
    defaults to True when absent; a non-bool is a loud config error."""
    assert _build_time_travel_config({"workspace_capture": False}).workspace_capture is False
    assert _build_time_travel_config(None).workspace_capture is True
    assert _build_time_travel_config({}).workspace_capture is True
    assert TimeTravelConfig().workspace_capture is True
    with pytest.raises(ValueError):
        _build_time_travel_config({"workspace_capture": "no"})


# ── registry gate ──────────────────────────────────────────────────────────


def test_gate_off_no_workspace_store(tmp_path) -> None:
    """Tier 2: workspace_capture=False → the registry attaches NO workspace store
    (the property gate returns None) → cut_generation has nothing to capture."""
    reg = _make_registry(tmp_path, workspace_capture=False)
    assert reg.workspace_store is None


@pytest.mark.skipif(shutil.which("git") is None, reason="git required for capture")
def test_gate_on_builds_workspace_store(tmp_path) -> None:
    """Tier 2: default (True) → the workspace store is built (contrast control)."""
    reg = _make_registry(tmp_path, workspace_capture=True)
    assert reg.workspace_store is not None


# ── end-to-end: runtime-only rewind coherence (the non-default path) ───────


@pytest.mark.asyncio
async def test_optout_rewind_reverts_runtime_not_workspace(tmp_path) -> None:
    """Tier 2: with capture OFF, a turn's cut_generation records the runtime
    generation but NO workspace generation; a rewind reverts the runtime
    substrate (inbox markers) while the workspace file is left as-is — coherent
    runtime-only rewind. This is the consistent-cut-without-workspace invariant."""
    reg = _make_registry(tmp_path, workspace_capture=False)
    session = await reg.attach("alpha")
    session.register_intervention_listener("test")
    session._loop_driver = _FakeTurnDriver(session, tmp_path, {"A": "vA", "B": "vB"})

    await session._run_router_loop("A", "c1")
    seq_a = session.current_snapshot.applied_seq
    await session._run_router_loop("B", "c1")

    # No workspace substrate captured (store is None).
    assert reg.workspace_store is None
    assert (tmp_path / _WS_FILE).read_text(encoding="utf-8") == "vB"

    # Runtime checkout still works (consistent-cut on the runtime substrate alone).
    await reg.checkout(seq_a)
    markers = [m["payload"]["turn"] for m in session.current_snapshot.inbox]
    assert markers == ["A"]                                  # runtime reverted
    assert is_active_seq(reg.state_log, seq_a)               # cut landed on the runtime substrate
    # Workspace NOT reverted (no workspace store to restore) — runtime-only.
    assert (tmp_path / _WS_FILE).read_text(encoding="utf-8") == "vB"


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("git") is None, reason="git required for capture")
async def test_default_captures_workspace_generation(tmp_path) -> None:
    """Tier 2: default (capture ON) — contrast control: a turn's cut_generation
    DOES capture a workspace generation at the boundary seq (the cost the opt-out
    removes), and a rewind reverts the workspace too."""
    reg = _make_registry(tmp_path, workspace_capture=True)
    session = await reg.attach("alpha")
    session.register_intervention_listener("test")
    session._loop_driver = _FakeTurnDriver(session, tmp_path, {"A": "vA", "B": "vB"})

    await session._run_router_loop("A", "c1")
    seq_a = session.current_snapshot.applied_seq
    await session._run_router_loop("B", "c1")

    assert reg.workspace_store is not None
    assert seq_a in await reg.workspace_store.seqs()         # workspace gen captured
    await reg.checkout(seq_a)
    assert (tmp_path / _WS_FILE).read_text(encoding="utf-8") == "vA"  # workspace reverted too
