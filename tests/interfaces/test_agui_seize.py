"""Tier 2: symmetric, auth-gated seize of the active-driver token (ADR-0039 P3 D4).

The active-driver token marks WHICH connection holds interactive authority (Axis-B
UX). Any connection in the Axis-A-authorized set may seize equally — no preferred
connection, no handshake. Security lives in Axis-A: an unauthenticated / unauthorized
connection can neither seize nor (having been deposed) answer. This pins:

- the first attached surface holds the token; a peer can seize it symmetrically;
- a deposed holder is no longer the active driver (the substrate that rejects its
  late answer at delivery-time — the seize↔answer race);
- an unauthorized identity's seize is refused (Axis-A gate);
- an unattached connection cannot seize.

Real SurfaceManager instance, injected clock — no mocks.
"""
from __future__ import annotations

from reyn.interfaces.transport.agui.surface import SurfaceManager


def _mgr():
    # Authorized iff a non-empty user-id (v1 single operator = the authorized set).
    return SurfaceManager(authorized=lambda uid: bool(uid))


def test_first_surface_holds_token_then_peer_seizes_symmetrically() -> None:
    """Tier 2: two operator terminals; either may take authority (D4)."""
    m = _mgr()
    m.attach("laptop", "operator", now=0.0)
    m.attach("desktop", "operator", now=1.0)
    # First attach holds the token.
    assert m.is_active_driver("laptop")
    assert not m.is_active_driver("desktop")

    # Symmetric seize from the equal peer — no handshake.
    assert m.seize("desktop", "operator", now=2.0) is True
    assert m.is_active_driver("desktop")
    # The deposed holder is now a non-holding equal peer.
    assert not m.is_active_driver("laptop")


def test_deposed_holder_is_not_active_driver_after_seize() -> None:
    """Tier 2: the seize↔answer race substrate — a deposed holder fails the
    active-driver check, so its in-flight answer is rejected at delivery-time."""
    m = _mgr()
    m.attach("c1", "operator", now=0.0)
    m.attach("c2", "operator", now=0.0)
    assert m.seize("c2", "operator", now=1.0)
    # c1's later answer would be checked here and rejected (409 not-active-driver).
    assert m.is_active_driver("c1") is False


def test_unauthorized_seize_is_refused() -> None:
    """Tier 2: an unauthorized identity (Axis-A) cannot seize — security gate."""
    m = _mgr()
    m.attach("c1", "operator", now=0.0)
    m.attach("c2", None, now=0.0)  # attached but no authenticated user-id
    assert m.seize("c2", None, now=1.0) is False
    # Authority did not move to the unauthorized connection.
    assert m.is_active_driver("c1")


def test_unattached_connection_cannot_seize() -> None:
    """Tier 2: a connection with no attached surface cannot seize the token."""
    m = _mgr()
    m.attach("c1", "operator", now=0.0)
    assert m.seize("ghost", "operator", now=1.0) is False
    assert m.is_active_driver("c1")
