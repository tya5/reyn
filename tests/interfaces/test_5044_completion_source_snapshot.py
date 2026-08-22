"""Tier 2: #5044 (architect ruling, issuecomment-5378399712/5378442342) —
``ChatReadModel.completion_session()`` returns a ``CompletionSourceSnapshot``
VALUE, never the live ``Session`` itself.

The root #5044/#4995 shares with #5079: "sync, I/O-performing,
Session-mutating does not fit the async-marshal shape". The DIFFERENT
answer here (vs #5079's I/O-off-loop/apply-on-loop split): the actual
problem is ``completion_session()`` handing out a LIVE ``Session``
reference at all, not a threading one — completion needs candidate
strings (+ freshness), the worker updates, the UI only reads.

Real ``AgentRegistry`` + real ``Session`` + real ``RegistryReadModel``
throughout — no mocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.interfaces.repl.read_model import (
    CompletionSourceSnapshot,
    RegistryReadModel,
)
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
async def test_completion_session_returns_a_value_never_the_live_session(
    tmp_path: Path,
) -> None:
    """Tier 2: the return type is ``CompletionSourceSnapshot``, never
    ``Session`` — the #5044 architect ruling's literal claim, checked by
    TYPE, not merely "an assert on some field happened to pass"."""
    reg = _registry(tmp_path)
    await reg.attach("alpha")
    read_model = RegistryReadModel(reg)

    result = read_model.completion_session()

    assert isinstance(result, CompletionSourceSnapshot)
    assert not isinstance(result, Session), (
        "completion_session() handed out the live Session object -- exactly "
        "the #5044 defect this class exists to close"
    )


@pytest.mark.asyncio
async def test_snapshot_fields_reflect_real_attached_session_state(
    tmp_path: Path,
) -> None:
    """Tier 2: each field is populated off the REAL attached session's real
    state, not a fabricated placeholder — checked against the session's own
    accessors directly, not re-derived by this test."""
    reg = _registry(tmp_path)
    session = await reg.attach("alpha")
    read_model = RegistryReadModel(reg)

    result = read_model.completion_session()

    assert result is not None
    # AgentRegistry auto-bootstraps a "default" agent (registry.py) --
    # superset, not exact equality, so this test does not pin an
    # unrelated implementation detail.
    assert {"alpha", "bravo"} <= set(result.agent_names)
    assert result.known_model_classes == tuple(session.known_model_classes())
    assert result.active_intervention_ids == tuple(
        iv.id for iv in session.interventions.list_active()
    )
    assert result.workspace_dir == session.workspace_dir
    assert result.available_skills == tuple(session.available_skills())


@pytest.mark.asyncio
async def test_completion_session_returns_none_when_nothing_attached(
    tmp_path: Path,
) -> None:
    """Tier 2: an unattached registry (no session yet) answers None, the
    same "nothing to show" convention every other frame-sufficiency read
    already uses -- never a fabricated empty snapshot."""
    reg = _registry(tmp_path)
    read_model = RegistryReadModel(reg)

    assert read_model.completion_session() is None
