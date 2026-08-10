"""Tier 2: Proposal 0067 P5 (#3978) — Session._deliver_cross_session_message.

The delivery substrate ``send_to_session`` will drive: a generalization of
``Session._cross_session_hook_put`` (#2072) that targets an explicit
``target_agent`` instead of always ``self.agent_name`` — because
``AgentRegistry.get_session``/``resolve_session`` already take an agent name,
the hook-push method just never needed to pass a different one.

Pins (real AgentRegistry + real Session, no mocks — mirrors
``test_resolve_session_routing.py``'s construction pattern):

  1. Cross-AGENT delivery: a message reaches a DIFFERENT agent's session,
     not just another session of the caller's own agent.
  2. A target naming no LIVE session (never loaded) returns False — no
     crash, no silent auto-spawn (delivery-only, per ADR-0040 D5).
  3. wake=True starts the target's run-loop (observable via
     ``AgentRegistry.running_tasks()`` growing by one).
  4. wake=False does NOT start it — the message sits queued until the
     target's own next real turn.

Falsify-verified: reverting the helper to always call
``reg.get_session(self.agent_name, ...)`` (the pre-generalization shape)
makes test 1 go RED — the message lands on the caller's OWN agent instead
of the named target.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.turn_origin import TurnOrigin
from tests._support.agent_session import make_session


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        return make_session(
            agent_name=profile.name, state_log=state_log, registry=holder.get("reg"),
        )

    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
    )
    holder["reg"] = reg
    return reg


def _seed(tmp_path: Path, name: str) -> None:
    AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


@pytest.mark.asyncio
async def test_delivers_to_a_different_agents_session(tmp_path):
    """Tier 2: the message reaches BETA's session when ALPHA is the caller —
    not ALPHA's own "main" session. RED if the helper hardcodes the caller's
    own agent_name (the pre-generalization ``_cross_session_hook_put`` shape).
    """
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")
    _seed(tmp_path, "beta")
    caller = reg.get_or_load("alpha")
    target = reg.get_or_load("beta")

    delivered = await caller._deliver_cross_session_message(
        target_agent="beta", target_session_id="main",
        kind=TurnOrigin.EXTERNAL_MESSAGE,
        payload={"text": "hi from alpha"}, wake=False,
    )

    assert delivered is True
    assert target.inbox.qsize() == 1, "the message must land on beta's inbox"
    assert caller.inbox.qsize() == 0, "the message must NOT land on alpha's own inbox"
    kind, payload = target.inbox.get_nowait()
    assert kind == TurnOrigin.EXTERNAL_MESSAGE
    assert payload["text"] == "hi from alpha"


@pytest.mark.asyncio
async def test_absent_target_session_returns_false(tmp_path):
    """Tier 2: a target naming no LIVE session (never loaded) returns False
    — delivery-only, no silent auto-spawn (ADR-0040 D5: send_to_session
    pairs with an already-running peer, it is not a spawn primitive).
    """
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")
    caller = reg.get_or_load("alpha")

    delivered = await caller._deliver_cross_session_message(
        target_agent="alpha", target_session_id="never-spawned-sid",
        kind=TurnOrigin.EXTERNAL_MESSAGE,
        payload={"text": "hello?"}, wake=False,
    )

    assert delivered is False


@pytest.mark.asyncio
async def test_wake_true_starts_the_target_run_loop(tmp_path):
    """Tier 2: wake=True boots the target session's run-loop — observable as
    AgentRegistry.running_tasks() growing by one for a target that was
    loaded but not yet driving its own loop.
    """
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")
    _seed(tmp_path, "beta")
    caller = reg.get_or_load("alpha")
    reg.get_or_load("beta")  # loaded, but ensure_session_running never called yet

    before = len(reg.running_tasks())
    await caller._deliver_cross_session_message(
        target_agent="beta", target_session_id="main",
        kind=TurnOrigin.EXTERNAL_MESSAGE,
        payload={"text": "wake up"}, wake=True,
    )
    after = len(reg.running_tasks())

    assert after == before + 1, (
        "wake=True must start exactly one new run-loop task for the target"
    )


@pytest.mark.asyncio
async def test_wake_false_does_not_start_the_target_run_loop(tmp_path):
    """Tier 2: wake=False leaves the target's run-loop untouched — the
    behavioral counterpart to the wake=True test above.
    """
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")
    _seed(tmp_path, "beta")
    caller = reg.get_or_load("alpha")
    reg.get_or_load("beta")

    before = len(reg.running_tasks())
    await caller._deliver_cross_session_message(
        target_agent="beta", target_session_id="main",
        kind=TurnOrigin.EXTERNAL_MESSAGE,
        payload={"text": "quiet note"}, wake=False,
    )
    after = len(reg.running_tasks())

    assert after == before, "wake=False must NOT start a run-loop task"
