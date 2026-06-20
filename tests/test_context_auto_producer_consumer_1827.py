"""Tier 2: context-auto producer↔consumer link (#1827 S4b follow-up).

Closes the grep-only coverage gap (lead review note): the PRODUCER
(``InterventionHandler.deliver_answer_to(external_source=True)`` stamping the
``external_source`` marker on the session history) and the CONSUMER
(``Session._effective_contextual_for_turn`` reading that marker → narrowing) are
linked in ONE test, so a future rename of the marker key on either side breaks
this test (they can't drift apart).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from _async_wait import wait_until  # noqa: E402 — shared #1751 test wait helper

from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from reyn.security.permissions.effective import tool_contextually_denied
from reyn.user_intervention import UserIntervention


def _session(tmp_path: Path) -> Session:
    s = Session(
        agent_name="alpha",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
    )
    s.register_intervention_listener("test")
    return s


@pytest.mark.asyncio
async def test_external_answer_narrows_next_turn_end_to_end(tmp_path):
    """Tier 2: an external peer answer delivered through the real intervention
    path narrows the next turn's contextual — producer stamp → consumer compose.

    Before the answer the agent is un-narrowed (no static profile); after the
    EXTERNAL answer lands in history, ``_effective_contextual_for_turn`` denies the
    dangerous surfaces (context-auto), proving the marker producer and consumer
    agree end-to-end.
    """
    s = _session(tmp_path)

    # pre-condition: un-narrowed (byte-identical) — the consumer sees no taint.
    pre = s._effective_contextual_for_turn()
    assert pre is None

    # PRODUCER: deliver an external peer answer through the real handler path.
    iv = UserIntervention(kind="ask_user", prompt="name?", run_id="r1")
    iv.future = asyncio.get_running_loop().create_future()
    task = asyncio.ensure_future(s._dispatch_intervention(iv))
    await wait_until(lambda: bool(s._interventions.list_active()))
    delivered = await s._deliver_answer_to(iv, "Mallory", external_source=True)
    await asyncio.gather(task, return_exceptions=True)
    assert delivered is True

    # the marker landed on the session history (producer)
    assert any((m.meta or {}).get("external_source") for m in s.history)

    # CONSUMER: the next turn's contextual now narrows (context-auto).
    eff = s._effective_contextual_for_turn()
    assert eff is not None
    assert tool_contextually_denied(eff, "exec__sandboxed_exec")
    assert tool_contextually_denied(eff, "memory_operation__remember_shared")
    assert not tool_contextually_denied(eff, "recall")  # read stays allowed


@pytest.mark.asyncio
async def test_local_answer_does_not_narrow(tmp_path):
    """Tier 2: a LOCAL answer (non-external) leaves the next turn un-narrowed
    (falsify gate — only the external-source marker triggers context-auto)."""
    s = _session(tmp_path)
    iv = UserIntervention(kind="ask_user", prompt="?", run_id="r1")
    iv.future = asyncio.get_running_loop().create_future()
    task = asyncio.ensure_future(s._dispatch_intervention(iv))
    await wait_until(lambda: bool(s._interventions.list_active()))
    await s._deliver_answer_to(iv, "local user input", external_source=False)
    await asyncio.gather(task, return_exceptions=True)

    assert not any((m.meta or {}).get("external_source") for m in s.history)
    eff = s._effective_contextual_for_turn()
    assert eff is None
