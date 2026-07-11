"""Tier 2: AG-UI thin-client heartbeat retune (10s->25s, piggyback, 60s timeout).

Owner-raised concern: the remote thin client's dedicated heartbeat POST every
10s was aggressive server load relative to prior art (Socket.IO 25s, Phoenix
30s, SignalR 15s+2x timeout). This pins the three retuned invariants:

- **Piggyback** (``remote_client._heartbeat_due``): a real client->server POST
  within the interval window suppresses the next dedicated heartbeat ping.
- **Fast-path** (``endpoint.agui_submit``): a heartbeat on an already-attached
  connection never touches ``registry.exists()`` (a filesystem stat) — pure
  in-memory liveness refresh.
- **LOAD-BEARING INVARIANT (architect's gate)**: interval < timeout <
  timeout+grace, so the half-open backstop + grace window still cover
  detection and the unified fail-close DENY still fires within grace once a
  half-open client goes silent. Two demonstration tests show WHY the ordering
  matters: a misconfigured timeout with zero margin over the interval
  false-positives a live client; the retuned production numbers absorb
  realistic jitter.

Real ``SurfaceManager`` / ``Session`` instances, deterministic injected clocks
(sync methods) or a fake monotonic clock (the endpoint test) — no mocks.
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from _async_wait import wait_until  # noqa: E402 — shared #1751 test wait helper
from fastapi import FastAPI
from fastapi.testclient import TestClient

from reyn.core.events.state_log import StateLog
from reyn.interfaces.repl.remote_client import _HEARTBEAT_INTERVAL, _heartbeat_due
from reyn.interfaces.transport.agui import endpoint as endpoint_mod
from reyn.interfaces.transport.agui.endpoint import router
from reyn.interfaces.transport.agui.surface import (
    DEFAULT_GRACE_SECONDS,
    DEFAULT_LIVENESS_TIMEOUT,
    SurfaceManager,
    surface_registry,
)
from reyn.interfaces.web.auth import AuthContext
from reyn.runtime.session import Session
from reyn.runtime.session_buses import NO_SURFACE_REFUSAL_REASON
from reyn.user_intervention import UserIntervention

# ── (d) the load-bearing ordering invariant, on the actual shipped constants ──


def test_client_interval_below_server_timeout_below_timeout_plus_grace() -> None:
    """Tier 2: interval < timeout < timeout+grace (the architect's gate). A live,
    idle client at the client's own cadence is never false-swept, and the
    half-open backstop + grace window together cover detection."""
    assert _HEARTBEAT_INTERVAL < DEFAULT_LIVENESS_TIMEOUT
    assert DEFAULT_LIVENESS_TIMEOUT < DEFAULT_LIVENESS_TIMEOUT + DEFAULT_GRACE_SECONDS


# ── (a) piggyback: a real send within the window suppresses the heartbeat ────


def test_piggyback_suppresses_heartbeat_after_a_real_send_within_window() -> None:
    """Tier 2: a real client->server POST (any type) within the interval window
    makes the next dedicated heartbeat tick a no-op; a full idle interval with NO
    traffic still fires it."""
    last_send = 100.0
    # A real POST (e.g. a user turn) just landed — well within the window.
    assert _heartbeat_due(last_send, now=last_send + 1.0, interval=_HEARTBEAT_INTERVAL) is False
    assert (
        _heartbeat_due(last_send, now=last_send + _HEARTBEAT_INTERVAL / 2, interval=_HEARTBEAT_INTERVAL)
        is False
    )
    # A full interval of silence since the last send — the ping is due.
    assert _heartbeat_due(last_send, now=last_send + _HEARTBEAT_INTERVAL, interval=_HEARTBEAT_INTERVAL) is True


# ── (b) heartbeat fast-path: no registry.exists() filesystem stat ────────────


class _CountingRegistry:
    """A minimal real registry Fake (endpoint's touch-points) that counts calls
    to ``exists()`` — the ``Path.is_file()`` stat the heartbeat fast-path must
    now bypass for an already-attached connection."""

    def __init__(self) -> None:
        self.exists_calls = 0

    def exists(self, name: str) -> bool:
        self.exists_calls += 1
        return True

    async def attach(self, name: str):
        raise AssertionError("a heartbeat POST must never attach a session")


def _app_with_token(token: str) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.auth = AuthContext(token=token, require_token=True)
    return app


def test_heartbeat_on_attached_connection_skips_registry_exists_stat(monkeypatch) -> None:
    """Tier 2: a heartbeat POST on an already-attached connection is a pure
    in-memory op — zero ``registry.exists()`` filesystem stats — and DOES refresh
    the surface's liveness (proven by contrast against an un-heartbeated peer)."""
    registry = _CountingRegistry()
    monkeypatch.setattr(endpoint_mod, "get_registry", lambda: registry)

    agent_name = f"hb-fastpath-{uuid.uuid4().hex}"
    manager = surface_registry().for_agent(agent_name, authorized=lambda uid: bool(uid))
    manager.attach("conn-1", "operator", now=0.0)
    manager.attach("conn-2", "operator", now=0.0)  # a peer that never heartbeats

    client = TestClient(_app_with_token("s3cret"))
    resp = client.post(
        f"/agui/chat/{agent_name}?token=s3cret&connection_id=conn-1",
        json={"type": "heartbeat"},
    )
    assert resp.status_code == 200
    assert registry.exists_calls == 0  # the filesystem stat was skipped entirely

    # Contrast proof the heartbeat DID refresh conn-1's liveness: sweeping well
    # past the default timeout detaches the un-heartbeated peer (conn-2) but
    # spares conn-1 (its last_seen was just bumped by the POST above).
    dead = manager.sweep_dead(now=DEFAULT_LIVENESS_TIMEOUT + 1.0)
    assert dead == ["conn-2"]  # the un-heartbeated peer is gone
    assert manager.has_surfaces()
    assert manager.is_active_driver("conn-1")  # conn-1 (refreshed) survives


# ── (c) the retuned numbers: live client survives; half-open client is ───────
# ── swept + typed-DENY'd within grace (strip: bad numbers break this) ────────


def test_retuned_defaults_survive_a_client_heartbeating_on_cadence() -> None:
    """Tier 2: a client heartbeating at the retuned client interval never trips
    the PRODUCTION ``DEFAULT_LIVENESS_TIMEOUT`` (no false-positive sweep)."""
    m = SurfaceManager(authorized=lambda uid: bool(uid))  # production defaults
    m.attach("c1", "operator", now=0.0)
    t = 0.0
    for _ in range(6):
        t += _HEARTBEAT_INTERVAL
        m.heartbeat("c1", now=t)
        assert m.sweep_dead(now=t) == []
    assert m.has_surfaces()


def test_production_timeout_absorbs_realistic_heartbeat_jitter() -> None:
    """Tier 2: the retuned 60s timeout comfortably absorbs a late/jittery
    heartbeat on the 25s cadence (2.4x margin) — no false sweep."""
    m = SurfaceManager(authorized=lambda uid: bool(uid))  # production defaults
    m.attach("c1", "operator", now=0.0)
    late = _HEARTBEAT_INTERVAL + 5.0  # 5s of jitter/delay on top of one cadence
    assert m.sweep_dead(now=late) == []


def test_bad_retune_with_zero_margin_false_positives_a_live_client() -> None:
    """Tier 2: strip-falsify demonstrating WHY timeout must exceed interval — a
    misconfigured timeout equal to the interval affords zero jitter margin, so
    even a trivially-late (on-cadence, real-world-jittery) heartbeat is already
    swept as dead. This is the failure mode the >= ordering test above guards
    against; the retuned production numbers (60s vs 25s) do NOT exhibit it
    (see the jitter-absorption test above)."""
    bad_timeout = _HEARTBEAT_INTERVAL  # misconfigured: timeout == interval, no margin
    m = SurfaceManager(authorized=lambda uid: bool(uid), liveness_timeout=bad_timeout)
    m.attach("c1", "operator", now=0.0)
    # The exact on-cadence instant is still safe (strict `>` in sweep_dead)...
    assert m.sweep_dead(now=_HEARTBEAT_INTERVAL) == []
    # ...but ANY jitter beyond that (a heartbeat 10ms late, or a poll tick
    # landing a hair after) already trips a false sweep of a LIVE client.
    dead = m.sweep_dead(now=_HEARTBEAT_INTERVAL + 0.01)
    assert dead == ["c1"]  # false positive: c1 never stopped heart-beating


def _make_session(tmp_path: Path) -> Session:
    return Session(
        agent_name="alpha",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "alpha_snapshot.json",
    )


def _dispatch(session, *, origin: str) -> tuple[UserIntervention, asyncio.Task]:
    iv = UserIntervention(kind="ask_user", prompt="Approve?", run_id="r1")
    iv.origin_channel_id = origin
    iv.future = asyncio.get_running_loop().create_future()
    task = asyncio.ensure_future(session._dispatch_intervention(iv))
    return iv, task


@pytest.mark.asyncio
async def test_half_open_client_swept_then_typed_denied_within_grace(
    tmp_path, monkeypatch
) -> None:
    """Tier 2: THE strip-falsify gate — with the RETUNED production numbers, a
    half-open client (stops heart-beating, socket never FINs) is detected via
    ``sweep_dead`` only once ``DEFAULT_LIVENESS_TIMEOUT`` elapses, and the
    pending intervention is typed-DENY'd only after the grace window on top of
    that — never before, never parked forever. This is the invariant the
    architect's acceptance gate names: the retune must not loosen ADR-0039's
    'last-surface-gone -> typed DENY' behavior."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.register_intervention_listener("tui")
    iv, task = _dispatch(session, origin="tui")
    await wait_until(lambda: bool(session.interventions.list_active()))

    m = SurfaceManager(authorized=lambda uid: bool(uid))  # production defaults
    m.attach("c1", "operator", now=0.0)
    m.heartbeat("c1", now=0.0)

    # Half-open: no more heartbeats arrive. Not yet stale just under the timeout.
    assert m.sweep_dead(now=DEFAULT_LIVENESS_TIMEOUT - 1.0) == []
    assert m.has_surfaces()

    # Past the liveness timeout -> swept (half-open detected); grace arms here.
    swept_at = DEFAULT_LIVENESS_TIMEOUT + 1.0
    dead = m.sweep_dead(now=swept_at)
    assert dead == ["c1"]
    assert not m.has_surfaces()

    # Grace window not yet elapsed relative to the sweep instant -> not yet DENY.
    assert m.should_fail_close(now=swept_at + DEFAULT_GRACE_SECONDS - 1.0) is False
    # Grace elapsed -> fail-close fires.
    assert m.should_fail_close(now=swept_at + DEFAULT_GRACE_SECONDS) is True

    # Drive the actual typed-DENY resolution (mirrors the endpoint driver, which
    # unregisters the listener + calls fail_close_interventions once armed).
    session.unregister_intervention_listener("tui")
    denied = await session.fail_close_interventions(NO_SURFACE_REFUSAL_REASON)
    assert iv.id in denied

    answer = await asyncio.wait_for(task, timeout=2.0)  # RED if it parks instead
    assert answer.refused is True
    assert answer.reason == NO_SURFACE_REFUSAL_REASON
