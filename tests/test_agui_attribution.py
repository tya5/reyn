"""Tier 2: a wire grant is attributed to WHO granted + WHICH terminal (P3).

2-on-1 answering makes attribution load-bearing: the ``user_answered_intervention``
audit event must carry the authenticated ``auth_user_id`` AND the connection id, so
an operator's grant is attributable to the identity and the specific terminal it
came from. This pins that the attribution threaded from the endpoint reaches the P6
audit event on the answer path.

Real ``Session`` + real EventLog subscriber — no mocks.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from _async_wait import wait_until  # noqa: E402 — shared #1751 test wait helper

from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from reyn.user_intervention import UserIntervention
from tests._support.agent_session import make_session


def _make_session(tmp_path: Path) -> Session:
    session = make_session(
        agent_name="alpha",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "alpha_snapshot.json",
    )
    session.register_intervention_listener("tui")
    return session


@pytest.mark.asyncio
async def test_answer_stamps_auth_user_id_and_connection(tmp_path, monkeypatch) -> None:
    """Tier 2: an answer delivered with attribution emits
    ``user_answered_intervention`` carrying ``auth_user_id`` + ``auth_connection_id``."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    captured: list = []
    session.subscribe_chat_events(
        lambda ev: captured.append(ev)
        if getattr(ev, "type", None) == "user_answered_intervention"
        else None
    )

    iv = UserIntervention(kind="ask_user", prompt="Approve?", run_id="r1")
    iv.future = asyncio.get_running_loop().create_future()
    task = asyncio.ensure_future(session._dispatch_intervention(iv))
    await wait_until(lambda: bool(session.interventions.list_active()))

    ok = await session.answer_intervention_by_id(
        iv.id,
        "approved",
        attribution={"auth_user_id": "operator", "auth_connection_id": "laptop-abc"},
    )
    assert ok is True
    await asyncio.wait_for(task, timeout=2.0)

    assert captured, "user_answered_intervention was not emitted"
    data = captured[-1].data
    assert data.get("auth_user_id") == "operator"
    assert data.get("auth_connection_id") == "laptop-abc"
    assert data.get("intervention_id") == iv.id


@pytest.mark.asyncio
async def test_local_answer_has_no_wire_attribution(tmp_path, monkeypatch) -> None:
    """Tier 2: a local (in-process) answer with no attribution keeps the event
    shape unchanged — no ``auth_user_id`` key leaks in."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    captured: list = []
    session.subscribe_chat_events(
        lambda ev: captured.append(ev)
        if getattr(ev, "type", None) == "user_answered_intervention"
        else None
    )

    iv = UserIntervention(kind="ask_user", prompt="Approve?", run_id="r1")
    iv.future = asyncio.get_running_loop().create_future()
    task = asyncio.ensure_future(session._dispatch_intervention(iv))
    await wait_until(lambda: bool(session.interventions.list_active()))

    await session.answer_oldest_intervention_text("ok")
    await asyncio.wait_for(task, timeout=2.0)

    assert captured
    assert "auth_user_id" not in captured[-1].data
