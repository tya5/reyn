"""Tier 2/3: OS invariant — discard chain peer notification (R-D14).

Background: long-timeout multi-agent delegation (e.g.
``chain_timeout_seconds=1800`` for a 30-min research task) leaves the
upstream waiter stuck for the full timeout if the downstream peer's
skill_run is discarded by the user mid-flight. R-D14 plumbs an
immediate notification so the waiter resolves within milliseconds.

Layers exercised here:
  - ``ChatSession._on_chain_peer_discarded`` handler: pops the chain,
    emits ``chain_peer_discarded`` audit event, sends synthesised
    upstream agent_response (Tier 2)
  - ``AgentRegistry.notify_chain_discarded``: scans every other
    session, finds the matching chain, fires the handler (Tier 2)
  - ``/skill discard`` slash integration: looks up the run's
    chain_id from ``running_skills_chain`` and calls the registry
    notify (Tier 3)

Reference: PR-discard-chain-notify (R-D14) D14.3–D14.5 in the active plan.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.chat.session import ChatSession
from reyn.core.events.state_log import StateLog

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_registry_with_two_agents(
    tmp_path: Path,
) -> tuple[AgentRegistry, ChatSession, ChatSession, StateLog]:
    """Build a registry holding two real ChatSessions named A and B.

    Both sessions share the same StateLog (single-process invariant).
    The registry's ``_agents`` dict is populated by calling
    ``get_or_load`` for each name. No background tasks are started —
    the test drives the inboxes synchronously.
    """
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    def factory(profile: AgentProfile) -> ChatSession:
        # Each session gets its own snapshot path under the agent's
        # state dir so they don't collide.
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return ChatSession(
            agent_name=profile.name,
            state_log=state_log,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    registry = AgentRegistry(
        project_root=tmp_path,
        session_factory=factory,
        state_log=state_log,
    )
    # Register both agents on disk so get_or_load can find them.
    for name in ("A", "B"):
        agent_dir = tmp_path / ".reyn" / "agents" / name
        agent_dir.mkdir(parents=True, exist_ok=True)
        AgentProfile.new(name, role="").save(agent_dir)

    sess_a = registry.get_or_load("A")
    sess_b = registry.get_or_load("B")
    # Wire the back-reference so ChatSession can call into registry.
    sess_a._registry = registry
    sess_b._registry = registry
    return registry, sess_a, sess_b, state_log


def _drain_outbox(session: ChatSession) -> list:
    out = []
    while not session.outbox.empty():
        out.append(session.outbox.get_nowait())
    return out


# ---------------------------------------------------------------------------
# D14.4: _on_chain_peer_discarded handler
# ---------------------------------------------------------------------------


def test_handler_resolves_chain_and_emits_audit_event(tmp_path: Path):
    """Tier 2: handler pops the chain, emits chain_peer_discarded, sends upstream response."""
    registry, sess_a, _sess_b, state_log = _make_registry_with_two_agents(tmp_path)

    async def go():
        # A registers a chain waiting on B
        await sess_a.chains.register(
            chain_id="X-001",
            from_user=True,
            depth=1,
            original_text="please research X",
            sender="user",
            waiting_on={"B"},
            origin_agent="user",
            origin_depth=0,
        )
        assert sess_a.chains.find_chain("X-001") is not None
        # Trigger the handler directly
        await sess_a._on_chain_peer_discarded(
            chain_id="X-001",
            peer="B",
            reason="user_discarded_skill_run",
        )

    asyncio.run(go())
    # Chain is gone
    assert sess_a.chains.find_chain("X-001") is None
    # WAL has chain_resolve for X-001
    events = list(state_log.iter_from(0))
    resolve_events = [
        e for e in events
        if e["kind"] == "chain_resolve" and e.get("chain_id") == "X-001"
    ]
    (only_resolve,) = resolve_events
    assert only_resolve["chain_id"] == "X-001"


def test_handler_is_idempotent_when_chain_already_resolved(tmp_path: Path):
    """Tier 2: handler returns silently if the chain has already been resolved."""
    _registry, sess_a, _sess_b, _log = _make_registry_with_two_agents(tmp_path)

    async def go():
        # No chain registered → handler should no-op
        await sess_a._on_chain_peer_discarded(
            chain_id="never_registered", peer="B", reason="x",
        )

    asyncio.run(go())  # No exception is the assertion


# ---------------------------------------------------------------------------
# D14.3: AgentRegistry.notify_chain_discarded
# ---------------------------------------------------------------------------


def test_notify_finds_waiter_and_returns_true(tmp_path: Path):
    """Tier 2: notify_chain_discarded scans agents and fires the matching handler."""
    registry, sess_a, _sess_b, _log = _make_registry_with_two_agents(tmp_path)

    async def go():
        await sess_a.chains.register(
            chain_id="X-002",
            from_user=True,
            depth=1,
            original_text="task",
            sender="user",
            waiting_on={"B"},
            origin_agent="user",
            origin_depth=0,
        )
        notified = await registry.notify_chain_discarded(
            chain_id="X-002", by_agent_name="B",
        )
        return notified

    notified = asyncio.run(go())
    assert notified is True
    # A's chain was force-resolved
    assert sess_a.chains.find_chain("X-002") is None


def test_notify_returns_false_when_no_waiter_tracks_chain(tmp_path: Path):
    """Tier 2: an unknown chain returns False; no side effects."""
    registry, _sess_a, _sess_b, _log = _make_registry_with_two_agents(tmp_path)

    async def go():
        return await registry.notify_chain_discarded(
            chain_id="ghost-chain", by_agent_name="B",
        )

    assert asyncio.run(go()) is False


def test_notify_excludes_self_from_scan(tmp_path: Path):
    """Tier 2: the notifying agent's own chains are NOT touched.

    Defensive: B might (rarely) register a chain X-003 of its own at
    the same time it discards a skill_run that processes X-003 from A.
    notify_chain_discarded must skip B's own ChainManager.
    """
    registry, _sess_a, sess_b, _log = _make_registry_with_two_agents(tmp_path)

    async def go():
        # B registers a chain (irrelevant to the discard)
        await sess_b.chains.register(
            chain_id="X-self",
            from_user=False,
            depth=1,
            original_text="t",
            sender="caller",
            waiting_on={"C"},
            origin_agent="A",
            origin_depth=1,
        )
        # B notifies for the same chain_id
        notified = await registry.notify_chain_discarded(
            chain_id="X-self", by_agent_name="B",
        )
        return notified

    notified = asyncio.run(go())
    # Not notified — only B tracks it, and we exclude B
    assert notified is False
    # B's own chain is intact
    assert sess_b.chains.find_chain("X-self") is not None


# ---------------------------------------------------------------------------
# D14.5: /skill discard integration
# ---------------------------------------------------------------------------


def test_slash_discard_notifies_upstream_chain_waiter(tmp_path: Path, monkeypatch):
    """Tier 3: /skill discard on B with a chain-tagged run resolves A's chain immediately."""
    monkeypatch.chdir(tmp_path)
    registry, sess_a, sess_b, state_log = _make_registry_with_two_agents(tmp_path)
    sess_b.is_attached = True  # required for slash dispatch

    async def go():
        # A is waiting on B for chain X-D14
        await sess_a.chains.register(
            chain_id="X-D14",
            from_user=True,
            depth=1,
            original_text="long task",
            sender="user",
            waiting_on={"B"},
            origin_agent="user",
            origin_depth=0,
        )
        # B has a skill_run processing chain X-D14
        b_reg = sess_b.get_skill_registry()
        assert b_reg is not None
        await b_reg.start(
            run_id="run_b_d14",
            skill_name="research",
            skill_input={"type": "input", "data": {}},
        )
        # Stash the chain_id that the spawn path would normally set
        sess_b.running_skills_chain["run_b_d14"] = "X-D14"

        # User discards B's skill_run (with --force; the bare form is
        # confirmation-only and covered in test_skill_slash_command.py).
        consumed = await sess_b._maybe_handle_slash(
            "/skill discard run_b_d14 --force",
        )
        assert consumed is True
        # Allow the registry to fire the handler on A
        for _ in range(3):
            await asyncio.sleep(0)

    asyncio.run(go())

    # Verifications:
    # 1. A's chain is force-resolved (no longer tracked)
    assert sess_a.chains.find_chain("X-D14") is None
    # 2. WAL has chain_resolve for X-D14
    events = list(state_log.iter_from(0))
    resolves = [
        e for e in events
        if e["kind"] == "chain_resolve" and e.get("chain_id") == "X-D14"
    ]
    (only_resolve,) = resolves
    assert only_resolve["chain_id"] == "X-D14"
    # 3. WAL also has skill_discarded for B's run
    discarded = [
        e for e in events
        if e["kind"] == "skill_discarded"
        and e.get("run_id") == "run_b_d14"
    ]
    (only_discarded,) = discarded
    assert only_discarded["run_id"] == "run_b_d14"
    # 4. B's running_skills_chain is cleaned up
    assert "run_b_d14" not in sess_b.running_skills_chain


def test_slash_discard_no_chain_does_not_notify(tmp_path: Path, monkeypatch):
    """Tier 3: discarding a non-chain-tagged run (= user-initiated) does NOT call notify."""
    monkeypatch.chdir(tmp_path)
    registry, _sess_a, sess_b, state_log = _make_registry_with_two_agents(tmp_path)
    sess_b.is_attached = True

    notify_calls: list = []
    original_notify = registry.notify_chain_discarded

    async def fake_notify(*, chain_id, by_agent_name, **kwargs):
        notify_calls.append((chain_id, by_agent_name))
        return await original_notify(
            chain_id=chain_id, by_agent_name=by_agent_name, **kwargs,
        )

    registry.notify_chain_discarded = fake_notify  # type: ignore[assignment]

    async def go():
        b_reg = sess_b.get_skill_registry()
        await b_reg.start(
            run_id="run_b_solo", skill_name="standalone",
            skill_input={"type": "input", "data": {}},
        )
        # No chain_id stashed — this is a user-initiated run
        # (running_skills_chain would either be missing or None)
        await sess_b._maybe_handle_slash("/skill discard run_b_solo --force")
        for _ in range(2):
            await asyncio.sleep(0)

    asyncio.run(go())

    # notify_chain_discarded was never called — no chain to notify
    assert notify_calls == []
    # Skill was discarded normally
    events = list(state_log.iter_from(0))
    discarded = [e for e in events if e["kind"] == "skill_discarded"]
    assert any(e.get("run_id") == "run_b_solo" for e in discarded)
