"""Tier 2: HITL answer round-trip resolves the intervention BY ID and resumes (P3).

The load-bearing correctness of the wire HITL flow (ADR-0039 P3, R1): a
``TOOL_CALL_RESULT`` correlates to its intervention by the ``toolCallId`` (= the
intervention id), so ``Session.answer_intervention_by_id`` resolves the EXACT
intervention the operator was shown and the awaiting run resumes with that answer.
An unknown / already-resolved id is a typed reject with NO head-of-queue fallback —
so a grant can never land on a different prompt than the one displayed (the
answer-oldest race the amendment closes).

Real ``Session`` + real ``InterventionRegistry`` (the same funnel the endpoint
drives); no mocks.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from _async_wait import wait_until  # noqa: E402 — shared #1751 test wait helper

from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from reyn.user_intervention import InterventionChoice, UserIntervention


def _make_session(tmp_path: Path) -> Session:
    session = Session(
        agent_name="alpha",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "alpha_snapshot.json",
    )
    session.register_intervention_listener("tui")
    return session


def _iv(**kw) -> UserIntervention:
    iv = UserIntervention(kind="ask_user", prompt="Approve deploy?", **kw)
    iv.future = asyncio.get_running_loop().create_future()
    return iv


@pytest.mark.asyncio
async def test_answer_by_id_resolves_and_resumes(tmp_path, monkeypatch) -> None:
    """Tier 2: answer BY ID resolves that intervention's future → the run resumes
    with the delivered answer text."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    iv = _iv(run_id="r1")
    task = asyncio.ensure_future(session._dispatch_intervention(iv))
    await wait_until(lambda: bool(session.interventions.list_active()))

    ok = await session.answer_intervention_by_id(iv.id, "yes, ship it")
    assert ok is True

    answer = await asyncio.wait_for(task, timeout=2.0)
    assert answer.text == "yes, ship it"  # the run resumed with this answer


@pytest.mark.asyncio
async def test_unknown_id_is_typed_reject_no_head_fallback(tmp_path, monkeypatch) -> None:
    """Tier 2: an answer for an unknown id is rejected and does NOT fall back to
    the head — the pending intervention stays pending (no misdelivered grant)."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    iv = _iv(run_id="r1")
    task = asyncio.ensure_future(session._dispatch_intervention(iv))
    await wait_until(lambda: bool(session.interventions.list_active()))

    rejected = await session.answer_intervention_by_id("not-a-real-id", "sneaky")
    assert rejected is False
    # The real pending intervention was NOT resolved by the misaddressed answer.
    assert not iv.future.done()
    assert session.interventions.head() is iv

    # Clean up: answer it properly so the dispatch task completes.
    await session.answer_intervention_by_id(iv.id, "ok")
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_answer_by_id_validates_choice_server_side(tmp_path, monkeypatch) -> None:
    """Tier 2: a choice answer is validated against the SERVER's registry entry
    (R6) — a bogus choice id is rejected, a valid one resolves by id."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    choices = [
        InterventionChoice(id="yes", label="[Y]es", hotkey="y"),
        InterventionChoice(id="no", label="[N]o", hotkey="n"),
    ]
    iv = _iv(run_id="r2", choices=choices)
    task = asyncio.ensure_future(session._dispatch_intervention(iv))
    await wait_until(lambda: bool(session.interventions.list_active()))

    # Bogus choice id → server-side validation rejects; iv stays pending.
    assert await session.answer_intervention_by_id(iv.id, "", choice_id_override="maybe") is False
    assert not iv.future.done()

    # Valid choice id → resolves by id.
    assert await session.answer_intervention_by_id(iv.id, "", choice_id_override="no") is True
    answer = await asyncio.wait_for(task, timeout=2.0)
    assert answer.choice_id == "no"
