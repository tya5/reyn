"""Tier 2: FP-0043 S4b-3a — cron-transport session routing (registry-only helper).

A fired message-based cron job re-keys from the agent's shared "main" session to a
``cron:<job_name>`` Session — persistent per job (the stable job name resumes the
same Session across fires) and isolated from both "main" and other jobs. Real
AgentRegistry + StateLog (no mocks).

Falsification (feedback_falsify_acceptance_test_before_proof): the not-main /
persistence assertions red if resolve_cron_session stops namespacing by job (both
checked in the companion comments).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.chat.session import Session
from reyn.core.events.state_log import StateLog
from reyn.runtime.cron.routing import cron_session_id, resolve_cron_session


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    def _factory(profile: AgentProfile) -> Session:
        s = Session(agent_name=profile.name, state_log=state_log)
        s.register_intervention_listener("test")
        return s

    return AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
    )


def _seed(tmp_path: Path, name: str) -> None:
    AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


def test_cron_session_id():
    """Tier 2: the routing-key for a cron job is cron:<job_name>."""
    assert cron_session_id("morning_news") == "cron:morning_news"


@pytest.mark.asyncio
async def test_resolve_cron_session_maps_job_to_its_own_session(tmp_path):
    """Tier 2: a job resolves to its own cron:<job> session, NOT the agent's main."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "news_agent")

    s = resolve_cron_session(reg, "news_agent", "morning_news")
    assert s is reg.get_session("news_agent", "cron:morning_news")
    # the re-key: cron delivery is NOT the agent's shared "main" session.
    assert reg.get_session("news_agent", "main") is not s


@pytest.mark.asyncio
async def test_resolve_cron_session_is_persistent_per_job(tmp_path):
    """Tier 2: the same job resumes the SAME session across fires (history accrues)."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "news_agent")

    first = resolve_cron_session(reg, "news_agent", "morning_news")
    second = resolve_cron_session(reg, "news_agent", "morning_news")  # next fire
    assert second is first                            # persistent, not fresh-per-fire


@pytest.mark.asyncio
async def test_resolve_cron_session_jobs_are_isolated(tmp_path):
    """Tier 2: distinct jobs get distinct sessions (no cross-job bleed)."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "news_agent")

    a = resolve_cron_session(reg, "news_agent", "morning_news")
    b = resolve_cron_session(reg, "news_agent", "nightly_digest")
    assert a is not b
    assert reg.get_session("news_agent", "cron:morning_news") is a
    assert reg.get_session("news_agent", "cron:nightly_digest") is b
