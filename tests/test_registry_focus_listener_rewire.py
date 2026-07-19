"""Tier 2: focus-following front-end listeners re-wire on agent switch.

The REPL binds session-level listeners (the working-indicator chat-event callback
and the ask_user intervention listener) that must follow the ATTACHED session. If
they stay bound to the initially-attached session, a `/attach <other>` leaves the
spinner dead and interventions auto-refusing on the new agent. The registry
re-wires them on every attach. The intervention listener's has_active_listener()
is the public, observable proxy for the shared wire/unwire mechanism (the
chat-event callback rides the same path).

Real AgentRegistry + real Sessions (no mocks).
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import DEFAULT_CHAT_CHANNEL_ID, Session
from tests._support.agent_session import make_session


def _registry(tmp_path) -> AgentRegistry:
    def factory(profile: AgentProfile) -> Session:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return make_session(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=BudgetTracker(CostConfig()),
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    reg.create("alpha")
    reg.create("beta")
    return reg


@pytest.mark.asyncio
async def test_focus_listeners_follow_agent_switch(tmp_path) -> None:
    """Tier 2: binding the intervention listener follows the focused session
    across a switch — it lands on the newly-attached agent and leaves the old."""
    reg = _registry(tmp_path)
    try:
        # Mirror chat.py → run_repl: attach the initial agent, then bind.
        alpha = await reg.attach("alpha")
        reg.bind_focus_listeners(
            on_chat_event=lambda *a, **k: None,
            intervention_channel=DEFAULT_CHAT_CHANNEL_ID,
        )
        # Bind wired the currently-focused session.
        assert alpha.interventions.has_active_listener()

        # /attach beta — the listener must follow the focus.
        beta = await reg.attach("beta")
        assert beta.interventions.has_active_listener(), "listener followed to beta"
        assert not alpha.interventions.has_active_listener(), "and left alpha"

        # Teardown unwires the LIVE (beta) session, not the original.
        reg.unbind_focus_listeners()
        assert not beta.interventions.has_active_listener()
    finally:
        await asyncio.wait_for(reg.shutdown(), timeout=5.0)
