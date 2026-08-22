"""Tier 2: #5094/#5096, architect ruling (issuecomment-5379623427) —
``SessionBoundTransport.request_attach``/``request_session_switch``
EXPLICITLY answer ``False``, not inherited from ``ClientTransport``'s own
convenience default.

This transport is structurally the WRONG place to answer "attach a
different agent" — it is send-side only, bound to ONE already-attached
session (its own class docstring). "Which agent is attached" is a
REGISTRY-level question this class deliberately has no reference to. The
REAL fix for the owner-reported "attach coder-smith failed" over
``--connect`` (lead-coder's diagnosis, #5094 issuecomment-5379598384) is
NOT teaching this class to reach for a registry — it is #5096's own ②:
the CLIENT-side dispatch layer now recognizes ``/attach``/``/session
switch`` and calls ``ClientTransport.request_attach``/
``request_session_switch`` directly (the dedicated typed op
``AgUiTransport`` already implements correctly), so a remote ``/attach``
never reaches server-side slash dispatch — and this method — at all. See
``test_5096_slash_dispatch_routes_attach_directly.py`` for that witness.

This file's own witness is narrower and structural: EXPLICITLY writing
``False`` (rather than silently inheriting it) means a future edit that
tries to make this method "helpfully" do something is a deliberate,
reviewable change to a documented decision, not a silent default that
happened to already be there.

Real ``AgentRegistry`` + real ``Session`` (via ``make_session``) + real
``SessionBoundTransport`` throughout — no mocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.interfaces.transport.session_bound import SessionBoundTransport
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _registry(tmp_path: Path) -> AgentRegistry:
    def factory(profile: AgentProfile) -> Session:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        s = make_session(
            agent_name=profile.name,
            agent_role=profile.role,
            snapshot_path=agent_dir / "state" / "snapshot.json",
            registry=reg,
        )
        s.load_history()
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    reg.create("alpha")
    reg.create("bravo")
    return reg


@pytest.mark.asyncio
async def test_request_attach_answers_false_even_for_a_real_target_agent(
    tmp_path: Path,
) -> None:
    """Tier 2: NOT "the target doesn't exist" -- a genuinely existing,
    attachable second agent is in the registry, and this method still
    answers False, because it cannot correctly answer True either (it has
    no registry to have performed the attach through)."""
    reg = _registry(tmp_path)
    session = await reg.attach("alpha")
    assert reg.exists("bravo")  # positive control: the target is real
    transport = SessionBoundTransport(session, display_sink=lambda _msg: None)

    result = await transport.request_attach("bravo")

    assert result is False
    assert reg.attached_name == "alpha", (
        "request_attach must not mutate the registry -- it structurally "
        "cannot answer this question, so it must not silently half-answer "
        "it either"
    )


@pytest.mark.asyncio
async def test_request_session_switch_answers_false(tmp_path: Path) -> None:
    """Tier 2: same reasoning, the sibling method."""
    reg = _registry(tmp_path)
    session = await reg.attach("alpha")
    transport = SessionBoundTransport(session, display_sink=lambda _msg: None)

    result = await transport.request_session_switch("some-session-id")

    assert result is False
