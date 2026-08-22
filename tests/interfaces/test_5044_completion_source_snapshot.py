"""Tier 2: #5044 (architect ruling, issuecomment-5378399712/5378442342) —
``ChatReadModel.completion_source()`` returns a ``CompletionSourceSnapshot``
VALUE, never the live ``Session`` itself.

The root #5044/#4995 shares with #5079: "sync, I/O-performing,
Session-mutating does not fit the async-marshal shape". The DIFFERENT
answer here (vs #5079's I/O-off-loop/apply-on-loop split): the actual
problem is ``completion_source()`` handing out a LIVE ``Session``
reference at all, not a threading one — completion needs candidate
strings (+ freshness), the worker updates, the UI only reads.

Real ``AgentRegistry`` + real ``Session`` + real ``RegistryReadModel``
throughout — no mocks.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.data.skills.registry import SkillEntry
from reyn.interfaces.repl.read_model import (
    CompletionSourceSnapshot,
    RegistryReadModel,
)
from reyn.llm.model_resolver import ModelResolver
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_params import CapabilityScope
from reyn.user_intervention import UserIntervention
from tests._support.agent_session import make_session

#: A fixture-declared model class, wired into the session's resolver at
#: construction time (six-questions ②: the test's OWN value, never
#: re-derived by calling ``session.known_model_classes()`` a second time).
_PROBE_MODEL_CLASS = "probe-class"

#: A fixture-declared skill, wired via ``CapabilityScope`` at construction
#: time -- the same reason as above.
_PROBE_SKILL = SkillEntry(
    name="probe-skill", description="a probe skill", path="probe.md",
)


def _registry(tmp_path: Path) -> AgentRegistry:
    def factory(profile: AgentProfile) -> Session:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        s = make_session(
            agent_name=profile.name,
            agent_role=profile.role,
            snapshot_path=agent_dir / "state" / "snapshot.json",
            registry=reg,
            workspace_state_dir=tmp_path / ".reyn",
            resolver=ModelResolver({_PROBE_MODEL_CLASS: "gemini/gemini-2.5-flash-lite"}),
            capability_scope=CapabilityScope(available_skills=[_PROBE_SKILL]),
        )
        s.load_history()
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    reg.create("alpha")
    reg.create("bravo")
    return reg


@pytest.mark.asyncio
async def test_completion_source_returns_a_value_never_the_live_session(
    tmp_path: Path,
) -> None:
    """Tier 2: the return type is ``CompletionSourceSnapshot``, never
    ``Session`` — the #5044 architect ruling's literal claim, checked by
    TYPE, not merely "an assert on some field happened to pass"."""
    reg = _registry(tmp_path)
    await reg.attach("alpha")
    read_model = RegistryReadModel(reg)

    result = read_model.completion_source()

    assert isinstance(result, CompletionSourceSnapshot)
    assert not isinstance(result, Session), (
        "completion_source() handed out the live Session object -- exactly "
        "the #5044 defect this class exists to close"
    )


@pytest.mark.asyncio
async def test_snapshot_fields_reflect_real_attached_session_state(
    tmp_path: Path,
) -> None:
    """Tier 2: each field is populated off the REAL attached session's real
    state.

    Six-questions ②: every expected value here is one THIS TEST constructed
    or captured independently — never the same expression the
    implementation itself evaluates. A future edit that breaks the
    plumbing (e.g. reading the wrong resolver, the wrong intervention
    registry, the wrong workspace root) has a real chance of going red;
    re-calling ``session.known_model_classes()`` etc. would only catch an
    edit to that ONE line, which is not the property under test.
    """
    reg = _registry(tmp_path)
    session = await reg.attach("alpha")
    session.register_intervention_listener("probe-listener")
    iv = UserIntervention(kind="ask_user", prompt="Q?", choices=[], id="probe-iv-id")
    iv.future = asyncio.get_running_loop().create_future()
    dispatch_task = asyncio.create_task(session.interventions.dispatch(iv))
    await asyncio.sleep(0)  # let dispatch() enqueue before we read

    read_model = RegistryReadModel(reg)
    result = read_model.completion_source()

    iv.future.set_result(None)
    await dispatch_task

    assert result is not None
    # AgentRegistry auto-bootstraps a "default" agent (registry.py) --
    # superset, not exact equality, so this test does not pin an
    # unrelated implementation detail.
    assert {"alpha", "bravo"} <= set(result.agent_names)
    assert _PROBE_MODEL_CLASS in result.known_model_classes
    assert result.active_intervention_ids == ("probe-iv-id",)
    assert result.workspace_dir == tmp_path / ".reyn" / "agents" / "alpha", (
        f"got {result.workspace_dir!r} -- Agent.workspace_dir anchors on "
        "workspace_state_dir/agents/<name>, and this factory set "
        "workspace_state_dir to tmp_path/.reyn explicitly (never the "
        "cwd-fallback default, which would make this assertion "
        "order-dependent on the ambient cwd)"
    )
    assert result.available_skills == (_PROBE_SKILL,)


@pytest.mark.asyncio
async def test_completion_source_returns_none_when_nothing_attached(
    tmp_path: Path,
) -> None:
    """Tier 2: an unattached registry (no session yet) answers None, the
    same "nothing to show" convention every other frame-sufficiency read
    already uses -- never a fabricated empty snapshot."""
    reg = _registry(tmp_path)
    read_model = RegistryReadModel(reg)

    assert read_model.completion_source() is None
