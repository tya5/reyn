"""Tier 2: #2348 — sessions of the same agent get ISOLATED history + chat events.

Spawned sessions share the agent's identity object, so ``history_path`` (name-only) and
``events_dir`` (name-only) collided across every session of one agent — conversation A's messages
bled into B's ``load_history()``, and both wrote one ``events/agents/<name>/chat`` tree.
``spawn_session``'s fixup now re-keys both per (name, sid), parallel to the already-per-session
snapshot/WAL. "main" (``get_or_load``, not ``spawn_session``) keeps the legacy name-only paths
byte-identical — single-session agents unchanged, no migration.

Durability scoping (owner constraint), primary-verified:
- history.jsonl is an INDEPENDENT durable transcript (``load_history`` reads it directly; it is not
  WAL-reconstructed and is outside the snapshot/rewind field-map) → per-session isolation aligns it
  with the already-per-session WAL; each session's transcript restores from its OWN file.
- rewind does NOT touch the transcript at all (``reset_for_rewind`` clears runtime state, never
  ``self.history``) — a documented, in-scope-adjacent finding surfaced here.

Real seam: real ``AgentRegistry`` + real ``spawn_session`` (no mocks), mirroring the production
factory. ``monkeypatch.chdir`` because Session derives ``.reyn/...`` paths relative to cwd.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        s = Session(agent_name=profile.name, state_log=state_log, registry=holder.get("reg"))
        s.register_intervention_listener("test")
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    AgentProfile.new("alice", role="").save(tmp_path / ".reyn" / "agents" / "alice")
    return reg


@pytest.mark.asyncio
async def test_spawned_sessions_have_isolated_history(tmp_path, monkeypatch):
    """Tier 2: two sessions of one agent get distinct history_path; A's appended message does NOT
    appear in B's load_history(). RED today (shared name-only path), GREEN after the per-sid re-key."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")
    a = reg.get_session("alice", reg.spawn_session("alice", presentation_consumer=None, intervention_bridge=None))
    b = reg.get_session("alice", reg.spawn_session("alice", presentation_consumer=None, intervention_bridge=None))

    assert a.history_path != b.history_path, "spawned sessions must have isolated history paths"
    a._append_history(ChatMessage(role="user", content="secret from A"))
    b.load_history()
    assert all("secret from A" not in str(m.content) for m in b.history), \
        "session B must not see session A's conversation"


@pytest.mark.asyncio
async def test_spawned_events_isolated_and_forwarder_survives_rewire(tmp_path, monkeypatch):
    """Tier 2: #2348 — the chat events of a spawned session land in its OWN per-session events dir
    (not the shared/main tree), AND the set_events_dir subscriber swap preserves the other
    subscribers (the ChatLifecycleForwarder still bridges to the outbox — a naive rebuild would
    drop it and events would stop reaching the outbox/TUI)."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    main = reg.get_or_load("alice")
    a = reg.get_session("alice", reg.spawn_session("alice", presentation_consumer=None, intervention_bridge=None))

    assert a.events_dir != main.events_dir, "spawned session must have an isolated events dir"

    a._chat_events.emit("budget_warn", dimension="daily_tokens")

    # subscriber completeness: the forwarder survived the swap → the outbox got the marker.
    msg = a.outbox.get_nowait()
    assert "budget warn" in msg.text, "ChatLifecycleForwarder must survive set_events_dir (no drop)"

    # EventStore.write() is fire-and-forget off-loop (#2780) — flush before reading
    # the file from outside the EventLog/EventStore's own machinery.
    await a._event_store.flush()

    # the event reached A's NEW per-session store, and did NOT leak into main's shared tree.
    a_blob = "".join(p.read_text() for p in a.events_dir.rglob("*.jsonl"))
    assert "budget_warn" in a_blob, "event must be written to A's per-session events store"
    main_blob = "".join(p.read_text() for p in main.events_dir.rglob("*.jsonl")) \
        if main.events_dir.exists() else ""
    assert "budget_warn" not in main_blob, "A's event must not leak into main's shared events dir"


@pytest.mark.asyncio
async def test_crash_recovery_restores_each_session_own_history(tmp_path, monkeypatch):
    """Tier 2: #2348 durability — after a simulated crash (drop in-memory, reload from disk), each
    session's history restores from its OWN per-session file with no cross-session bleed. history is
    the durable source (file-based, not WAL-reconstructed); the WAL is already per-session."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")
    a = reg.get_session("alice", reg.spawn_session("alice", presentation_consumer=None, intervention_bridge=None))
    b = reg.get_session("alice", reg.spawn_session("alice", presentation_consumer=None, intervention_bridge=None))
    a._append_history(ChatMessage(role="user", content="A-only"))
    b._append_history(ChatMessage(role="user", content="B-only"))

    # simulate crash-recovery: clear in-memory, reload each from its own durable file.
    a.history.clear()
    b.history.clear()
    a.load_history()
    b.load_history()

    assert any("A-only" in str(m.content) for m in a.history)
    assert all("B-only" not in str(m.content) for m in a.history), "no bleed from B into A"
    assert any("B-only" in str(m.content) for m in b.history)
    assert all("A-only" not in str(m.content) for m in b.history), "no bleed from A into B"


@pytest.mark.asyncio
async def test_rewind_reset_does_not_touch_other_sessions_transcript(tmp_path, monkeypatch):
    """Tier 2: #2348 durability — a rewind reset on session A leaves session B's transcript file
    physically untouched (disjoint per-session paths). Also surfaces a finding: rewind does NOT
    currently rewind the transcript — A's own history survives its own reset_for_rewind. Owner
    intends conversations to rewind (append-only branch-tree time-travel, under separate
    investigation); this per-session isolation is compatible with a future branch-view and does not
    pre-empt it — the assertion below pins TODAY's behaviour, not a desired end-state."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")
    a = reg.get_session("alice", reg.spawn_session("alice", presentation_consumer=None, intervention_bridge=None))
    b = reg.get_session("alice", reg.spawn_session("alice", presentation_consumer=None, intervention_bridge=None))
    a._append_history(ChatMessage(role="user", content="A-msg"))
    b._append_history(ChatMessage(role="user", content="B-msg"))
    b_before = b.history_path.read_text()

    await a.reset_for_rewind()  # the real in-memory rewind reset for session A

    assert b.history_path.read_text() == b_before, "B's transcript file must be untouched by A's rewind"
    b.history.clear()
    b.load_history()
    assert any("B-msg" in str(m.content) for m in b.history)
    # documented finding: the transcript is outside rewind scope — A's own file persists.
    assert a.history_path.read_text(), "A's transcript persists across its own rewind reset (finding)"


def test_main_session_keeps_legacy_name_only_paths(tmp_path, monkeypatch):
    """Tier 2: #2348 regression — the "main" session (via get_or_load, never spawn_session) keeps
    the legacy name-only history_path + events_dir unchanged. Single-session agents unchanged,
    no migration."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    main = reg.get_or_load("alice")
    assert main.history_path == Path(".reyn") / "agents" / "alice" / "history.jsonl"
    assert main.events_dir == Path(".reyn") / "events" / "agents" / "alice" / "chat"
