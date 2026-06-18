"""Tier 2: #1726 FP-0043 Stage 3 — Registry holds N Sessions per Agent.

The structural multi-session enabler: identity (Agent, S2) is shared per name;
conversation Sessions are keyed by an opaque session-id (default "main" → N=1
byte-identical). spawn_session opens an additional Session under the SAME Agent
object. Inbound routing to non-default sessions is Stage 4 — S3 just makes the
structure hold N.

Real AgentRegistry + real Session (no mocks).
"""
from __future__ import annotations

import pytest

from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import _DEFAULT_SID, AgentRegistry
from reyn.runtime.session import Session


def _registry(tmp_path):
    def factory(profile: AgentProfile):
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return Session(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=BudgetTracker(CostConfig()),
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    return reg


def test_default_session_lookup_unchanged(tmp_path) -> None:
    """Tier 2: #1726 — get_or_load(name) yields the default "main" session, and
    get_session(name) / get_session(name, "main") return that SAME instance
    (the prior single-session lookup, unchanged at N=1)."""
    reg = _registry(tmp_path)
    s = reg.get_or_load("default")
    assert reg.get_session("default") is s
    assert reg.get_session("default", _DEFAULT_SID) is s
    assert reg.loaded_names() == ["default"]


def test_spawn_session_creates_distinct_session_sharing_agent(tmp_path) -> None:
    """Tier 2: #1726 — spawn_session opens an ADDITIONAL Session under the agent:
    a distinct CONVERSATION instance under the SAME identity. Observably: the
    spawned session is a different object with its own inbox, but reports the
    identical identity (agent_name/role), and the registry still lists ONE agent.
    (Impl shares the same Agent object via the S2 ``agent=`` seam — verified by
    construction in spawn_session; the frozen+private Agent isn't an observable
    surface, so the test pins the public identity-equivalence contract.)"""
    reg = _registry(tmp_path)
    main = reg.get_or_load("default")
    sid = reg.spawn_session("default")
    spawned = reg.get_session("default", sid)

    assert sid != _DEFAULT_SID
    assert spawned is not None and spawned is not main, "a distinct conversation Session"
    # Same identity (public surface), different conversation.
    assert spawned.agent_name == main.agent_name == "default"
    assert spawned.agent_role == main.agent_role, "same identity (role) as the agent"
    assert spawned.inbox is not main.inbox, "conversation (inbox) is per-session"
    # Still ONE agent in the registry (N sessions under one identity).
    assert reg.loaded_names() == ["default"]


def test_default_session_unaffected_by_spawn(tmp_path) -> None:
    """Tier 2: #1726 — spawning a second session does not disturb the default
    one (get_or_load still returns the original "main" instance)."""
    reg = _registry(tmp_path)
    main = reg.get_or_load("default")
    reg.spawn_session("default")
    assert reg.get_or_load("default") is main
    assert reg.get_session("default") is main


@pytest.mark.asyncio
async def test_attach_session_focuses_existing_and_rejects_unknown(tmp_path) -> None:
    """Tier 2: #1726 Stage 4a — attach_session focuses an EXISTING session of the
    agent (public attached_name/attached_sid/attached_session reflect it) and
    raises KeyError for an unknown sid (the graceful-error substrate the /session
    handler + forwarder rely on). No build — the session must already exist."""
    reg = _registry(tmp_path)
    reg.get_or_load("default")
    sid = reg.spawn_session("default")
    try:
        focused = await reg.attach_session("default", sid)
        assert reg.attached_name == "default"
        assert reg.attached_sid == sid
        assert reg.attached_session() is focused
        assert focused is reg.get_session("default", sid)

        with pytest.raises(KeyError):
            await reg.attach_session("default", "no-such-sid")
    finally:
        for task in reg.running_tasks():
            task.cancel()
