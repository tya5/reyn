"""Tier 2: heartbeat-timeout surface-loss detection (ADR-0039 P3 liveness).

A half-open connection (the TCP socket looks alive but the peer is gone) must not
hide a dead surface, or fail-close could never fire. The SurfaceManager's liveness
signal is a per-surface heartbeat; ``sweep_dead`` detaches any surface whose last
heartbeat is older than the liveness timeout. This pins that a surface which stops
heart-beating IS detected as lost, while a surface kept fresh is NOT swept.

Real SurfaceManager instance, deterministic injected clock — no async sleeps.
"""
from __future__ import annotations

from reyn.interfaces.transport.agui.surface import SurfaceManager


def _mgr() -> SurfaceManager:
    return SurfaceManager(authorized=lambda uid: bool(uid), liveness_timeout=45.0)


def test_stale_surface_is_swept_as_dead() -> None:
    """Tier 2: a surface past the liveness timeout is detected + detached."""
    m = _mgr()
    m.attach("c1", "operator", now=0.0)
    m.heartbeat("c1", now=10.0)

    # Not yet stale at 10 + timeout.
    assert m.sweep_dead(now=50.0) == []
    assert m.has_surfaces()

    # Past the liveness timeout since the last heartbeat (10) → swept.
    dead = m.sweep_dead(now=10.0 + 45.0 + 1.0)
    assert dead == ["c1"]
    assert not m.has_surfaces()
    assert m.surface_count() == 0


def test_fresh_surface_survives_sweep() -> None:
    """Tier 2: a surface kept heart-beating is never swept (no false loss)."""
    m = _mgr()
    m.attach("c1", "operator", now=0.0)
    # Keep it fresh right up to the sweep instant.
    m.heartbeat("c1", now=100.0)
    assert m.sweep_dead(now=101.0) == []
    assert m.is_active_driver("c1")


def test_heartbeat_loss_arms_grace_window() -> None:
    """Tier 2: sweeping the LAST surface leaves it empty → the grace window arms
    (the liveness signal feeds the fail-close decision)."""
    m = _mgr()
    m.attach("c1", "operator", now=0.0)
    m.sweep_dead(now=1000.0)  # c1 long stale → swept
    assert not m.has_surfaces()
    # Grace has not yet elapsed relative to the empty instant, but it IS armed.
    assert not m.should_fail_close(now=1000.0)
    assert m.should_fail_close(now=1000.0 + m.grace_seconds)
