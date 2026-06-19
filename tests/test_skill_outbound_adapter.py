"""Tier 2: the skill-outbound record + Session adapter (#1794 S3).

Pins the runtime-boundary seam that lets SkillRunner live in reyn.skill without
a reyn.runtime dependency: a skill emits the transport-neutral
SkillOutboundMessage, and Session._skill_outbox_adapter converts it to the
runtime OutboxMessage (reply_to=None) before enqueuing. Includes the falsify —
bypassing the adapter (enqueuing the neutral record directly) fails, proving the
adapter is load-bearing, not decorative.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from reyn.skill.skill_outbound import SkillOutboundMessage


def _make_session(tmp_path: Path) -> Session:
    return Session(
        agent_name="t",
        state_log=StateLog(tmp_path / "t.wal"),
        snapshot_path=tmp_path / "snap.json",
    )


def test_skill_outbound_message_is_neutral():
    """Tier 2: the record carries kind/text/meta only (no transport reply_to)."""
    m = SkillOutboundMessage(kind="status", text="hi", meta={"a": 1})
    assert (m.kind, m.text, m.meta) == ("status", "hi", {"a": 1})
    assert not hasattr(m, "reply_to")


@pytest.mark.asyncio
async def test_adapter_round_trips_to_outbox_message(tmp_path):
    """Tier 2: the adapter converts the neutral record → OutboxMessage with
    kind/text/meta preserved and reply_to=None (behavior-identical to the prior
    direct OutboxMessage constructs)."""
    session = _make_session(tmp_path)
    await session._skill_outbox_adapter(
        SkillOutboundMessage(kind="error", text="boom", meta={"chain_id": "c1"}),
    )
    msg = session.outbox.get_nowait()
    assert (msg.kind, msg.text, msg.meta) == ("error", "boom", {"chain_id": "c1"})
    assert msg.reply_to is None


@pytest.mark.asyncio
async def test_adapter_is_load_bearing_falsify(tmp_path):
    """Tier 2: FALSIFY — bypassing the adapter (enqueuing the neutral record
    straight through _put_outbox) fails, because the record lacks the transport
    reply_to the runtime outbox path reads. Proves the adapter is required."""
    session = _make_session(tmp_path)
    with pytest.raises(AttributeError):
        await session._put_outbox(
            SkillOutboundMessage(kind="status", text="x"),  # type: ignore[arg-type]
        )


def test_make_skill_subscribers_builds_forwarder(tmp_path):
    """Tier 2: the subscriber factory builds a ChatEventForwarder for the skill
    (the construction SkillRunner DI's so it stays reyn.runtime-free)."""
    from reyn.runtime.forwarder import ChatEventForwarder

    session = _make_session(tmp_path)
    subs = session._make_skill_subscribers("demo_skill", run_id="r1")
    forwarder = subs[0]
    assert isinstance(forwarder, ChatEventForwarder)
    assert forwarder.skill_name == "demo_skill"
    assert forwarder.run_id == "r1"
