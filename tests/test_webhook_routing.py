"""Tier 2: FP-0043 S4b-5 — webhook-transport session routing (registry-only helper).

An inbound webhook delivery (deliver_to_agent) routes to a PER-SENDER session keyed
by parsing the ``"<transport>:<external_id>"`` sender — slack/line get their own
logical-transport namespace, generic webhooks get ``webhook:`` — isolated from the
agent's "main" conversation and from other senders. Output (reply-to-source) is the
EXISTING FP-0041 interceptor (plugin sets reply_to=ExternalRef) and is unchanged.
Real AgentRegistry + StateLog (no mocks).

Falsification (feedback_falsify_acceptance_test_before_proof): the not-main test
reds if routing falls back to "main"; the parse tests red if the sender stops
splitting on the first ':'.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.chat.session import Session
from reyn.chat.webhook_routing import parse_webhook_sender, resolve_webhook_session
from reyn.core.events.state_log import StateLog


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


def test_parse_webhook_sender():
    """Tier 2: sender → (transport, external_id); only the FIRST ':' splits."""
    assert parse_webhook_sender("slack:U456") == ("slack", "U456")
    assert parse_webhook_sender("line:user:U999") == ("line", "user:U999")
    assert parse_webhook_sender("webhook:github:42") == ("webhook", "github:42")
    # no transport prefix → generic webhook namespace, whole sender as external_id.
    assert parse_webhook_sender("bare") == ("webhook", "bare")


@pytest.mark.asyncio
async def test_resolve_webhook_routes_per_sender_not_main(tmp_path):
    """Tier 2: a webhook sender resolves to its own logical-transport session, NOT main."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "agent")

    s = resolve_webhook_session(reg, "agent", "slack:U456")
    assert s is reg.get_session("agent", "slack:U456")   # logical-transport namespace
    assert reg.get_session("agent", "main") is not s       # isolated from main


@pytest.mark.asyncio
async def test_resolve_webhook_senders_are_isolated_and_persistent(tmp_path):
    """Tier 2: distinct senders → distinct sessions; the same sender resumes its own."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "agent")

    u1 = resolve_webhook_session(reg, "agent", "slack:U456")
    u2 = resolve_webhook_session(reg, "agent", "slack:U999")
    assert u1 is not u2                                   # per-external-user isolation
    assert resolve_webhook_session(reg, "agent", "slack:U456") is u1  # persistent


@pytest.mark.asyncio
async def test_resolve_webhook_generic_namespace(tmp_path):
    """Tier 2: a generic (non slack/line) webhook gets the webhook: namespace."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "agent")

    g = resolve_webhook_session(reg, "agent", "webhook:github:42")
    assert g is reg.get_session("agent", "webhook:github:42")
    assert reg.get_session("agent", "main") is not g
