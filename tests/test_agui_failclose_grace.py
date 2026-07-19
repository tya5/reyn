"""Tier 2: unified fail-close with a grace window (ADR-0039 P3 — THE gate test).

The load-bearing safety invariant of the arc (D5b). A pending intervention whose
last answerable operator surface is lost must resolve to a typed DENY — never park
unbounded — but ONLY after a grace window T with zero surfaces, so a brief
disconnect+reconnect within T keeps it pending. Two amendment corrections are
pinned here:

- **R2 (per-intervention scope).** The DENY is scoped to interventions whose
  answerable surface set is empty, NOT a blanket "all clients gone" sweep: an
  intervention still answerable by a live listener (an A2A origin-pin peer)
  survives an operator-surface loss.
- **grace decision.** ``SurfaceManager.should_fail_close`` is False within T and
  after a reconnect (disarmed), True only once T elapses with zero surfaces.

Strip-falsify: remove the ``future.set_result(...refused...)`` in
``InterventionRegistry.deny_unanswerable_active`` → the pending future is never
resolved → it PARKS → ``test_grace_exceeded_denies_pending_intervention`` times out
(RED). That is the proof the DENY path — not a park — is what this test guards.

Real ``Session`` + real ``SurfaceManager`` + injected clock — no mocks.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from _async_wait import wait_until  # noqa: E402 — shared #1751 test wait helper

from reyn.core.events.state_log import StateLog
from reyn.interfaces.transport.agui.surface import SurfaceManager
from reyn.runtime.session import Session
from reyn.runtime.session_buses import NO_SURFACE_REFUSAL_REASON
from reyn.user_intervention import UserIntervention
from tests._support.agent_session import make_session


def _make_session(tmp_path: Path) -> Session:
    session = make_session(
        agent_name="alpha",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "alpha_snapshot.json",
    )
    return session


def _dispatch(session, *, origin: str) -> tuple[UserIntervention, asyncio.Task]:
    iv = UserIntervention(kind="ask_user", prompt="Approve?", run_id="r1")
    iv.origin_channel_id = origin
    iv.future = asyncio.get_running_loop().create_future()
    task = asyncio.ensure_future(session._dispatch_intervention(iv))
    return iv, task


def _mgr() -> SurfaceManager:
    return SurfaceManager(authorized=lambda uid: bool(uid), grace_seconds=20.0)


def test_pending_survives_disconnect_within_grace() -> None:
    """Tier 2: a disconnect + reconnect within T does NOT trip fail-close."""
    m = _mgr()
    m.attach("c1", "operator", now=0.0)
    m.detach("c1", now=5.0)  # had-then-lost → grace armed at t=5
    # Within the window: not yet fail-close-able.
    assert m.should_fail_close(now=5.0 + 10.0) is False
    # Reconnect within T disarms the window entirely.
    m.attach("c1", "operator", now=5.0 + 12.0)
    assert m.should_fail_close(now=5.0 + 100.0) is False


def test_grace_arms_only_on_had_then_lost_not_never_had() -> None:
    """Tier 2: a server that never had a surface never arms the grace window (R3).

    The dispatch-time detached DENY (#2773) is untouched by the grace window T."""
    m = _mgr()
    assert m.should_fail_close(now=10_000.0) is False  # never attached


@pytest.mark.asyncio
async def test_grace_exceeded_denies_pending_intervention(tmp_path, monkeypatch) -> None:
    """Tier 2: once T elapses with zero surfaces, the pending intervention is
    typed-DENY'd (THE gate).

    Refused (not parked) — the run resumes with a fail-closed answer."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.register_intervention_listener("tui")  # operator surface present
    iv, task = _dispatch(session, origin="tui")
    await wait_until(lambda: bool(session.interventions.list_active()))

    m = _mgr()
    m.attach("c1", "operator", now=0.0)
    m.detach("c1", now=1.0)  # last operator surface lost
    assert m.should_fail_close(now=1.0 + m.grace_seconds) is True

    # Last surface gone → operator listener unregistered, then fail-close fires.
    session.unregister_intervention_listener("tui")
    denied = await session.fail_close_interventions(NO_SURFACE_REFUSAL_REASON)
    assert iv.id in denied

    answer = await asyncio.wait_for(task, timeout=2.0)  # RED if it parks
    assert answer.refused is True
    assert answer.reason == NO_SURFACE_REFUSAL_REASON


@pytest.mark.asyncio
async def test_failclose_scope_skips_a2a_pinned_intervention(tmp_path, monkeypatch) -> None:
    """Tier 2: fail-close DENYs the operator-answerable intervention but skips one
    still answerable by a live A2A origin-pin listener (R2)."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.register_intervention_listener("tui")
    session.register_intervention_listener("a2a:r9")  # A2A peer still answering

    op_iv, op_task = _dispatch(session, origin="tui")
    a2a_iv, a2a_task = _dispatch(session, origin="a2a:r9")
    await wait_until(lambda: len(session.interventions.list_active()) == 2)

    # Operator surface lost (its listener unregistered); the A2A listener remains.
    session.unregister_intervention_listener("tui")
    denied = await session.fail_close_interventions(NO_SURFACE_REFUSAL_REASON)

    assert op_iv.id in denied
    assert a2a_iv.id not in denied  # per-intervention scope: A2A peer can still answer
    assert not a2a_iv.future.done()

    # Resolve the survivor so its dispatch task completes cleanly.
    op_answer = await asyncio.wait_for(op_task, timeout=2.0)
    assert op_answer.refused is True
    await session.answer_intervention_by_id(a2a_iv.id, "peer-answered")
    await asyncio.wait_for(a2a_task, timeout=2.0)
