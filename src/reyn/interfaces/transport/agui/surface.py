"""Attached-surface tracking for the AG-UI server — the fail-close substrate (P3).

ADR-0039 D4/D5(b): the single-writer server hosts a session; N thin clients
*attach* (do not own). This module owns the server-side bookkeeping the arc's
load-bearing safety invariant needs, with a **clock-injected, synchronous core**
so the grace-window / liveness policy is exercised by a fake clock (no async
sleeps, no flake):

- :class:`SurfaceManager` — per-agent tracker of attached operator surfaces (=
  authenticated connections), the single **active-driver token** (Axis-B UX: one
  connection holds interactive authority at a time; symmetric seize within the
  Axis-A-authorized set), and the two liveness signals that decide **fail-close**:
  a per-surface heartbeat (a half-open TCP connection cannot hide a dead surface)
  and a **grace window T** (a brief blip + reconnect within T keeps a pending
  intervention alive; only ZERO surfaces for the whole of T trips a DENY).

The **trigger** policy here is deliberately narrow (R3): the grace window applies
ONLY to a *had-then-lost* surface. A server that never had a surface never arms
the timer (``_last_empty_at`` stays ``None``), so the dispatch-time detached-spawn
DENY (#2773) is untouched — "unified fail-close" unifies the DENY *terminal*
(shape + audit), not the trigger.

The manager decides *when* to fail-close; *which* pending interventions to DENY
is a per-intervention scope decision on the session side (R2 — an A2A-origin-pin
intervention with a live listener survives an operator-surface loss).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

# Defaults (seconds). Grace T is the reconnect window; liveness timeout is how
# long a surface may go without a heartbeat before it is swept as dead. Both are
# constructor-overridable so an operator/test can tune or shrink them.
DEFAULT_GRACE_SECONDS = 20.0
DEFAULT_LIVENESS_TIMEOUT = 45.0


@dataclass
class Surface:
    """One attached AG-UI operator surface (an authenticated connection)."""

    connection_id: str
    user_id: str | None
    last_seen: float


class SurfaceManager:
    """Per-agent attached-surface tracker: active-driver token, liveness, grace.

    All time-dependent methods take an explicit ``now`` (monotonic seconds) so
    the policy is deterministic under a fake clock. ``authorized`` is the
    Axis-A membership predicate (``user_id -> bool``) seize is gated on — the
    security control lives in Axis-A; seize symmetry is a UX statement within
    the already-authorized set.
    """

    def __init__(
        self,
        *,
        authorized: "Callable[[str | None], bool]",
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
        liveness_timeout: float = DEFAULT_LIVENESS_TIMEOUT,
    ) -> None:
        self._authorized = authorized
        self._grace_seconds = grace_seconds
        self._liveness_timeout = liveness_timeout
        self._surfaces: dict[str, Surface] = {}
        self._active_driver: str | None = None
        # Set to the ``now`` at which the surface set became empty AFTER having
        # held at least one surface (had-then-lost). ``None`` = either surfaces
        # are present OR none has ever attached (never-had — no grace, R3).
        self._last_empty_at: float | None = None

    # ── attach / detach / liveness ───────────────────────────────────────────

    def attach(self, connection_id: str, user_id: str | None, now: float) -> None:
        """Record a newly-attached surface. The first attachment takes the
        active-driver token; a reattach within the grace window disarms it."""
        self._surfaces[connection_id] = Surface(connection_id, user_id, now)
        self._last_empty_at = None
        if self._active_driver is None:
            self._active_driver = connection_id

    def detach(self, connection_id: str, now: float) -> None:
        """Remove a surface. If it held the token, authority moves to an
        arbitrary remaining surface (else nobody). Arms the grace window when
        this leaves the set empty (had-then-lost)."""
        existed = self._surfaces.pop(connection_id, None) is not None
        if self._active_driver == connection_id:
            self._active_driver = next(iter(self._surfaces), None)
        if existed and not self._surfaces:
            self._last_empty_at = now

    def heartbeat(self, connection_id: str, now: float) -> None:
        """Refresh a surface's liveness timestamp (SSE keepalive / ping)."""
        s = self._surfaces.get(connection_id)
        if s is not None:
            s.last_seen = now

    def sweep_dead(self, now: float) -> list[str]:
        """Detach every surface whose last heartbeat is older than the liveness
        timeout (half-open detection). Returns the swept connection ids."""
        dead = [
            cid
            for cid, s in self._surfaces.items()
            if now - s.last_seen > self._liveness_timeout
        ]
        for cid in dead:
            self.detach(cid, now)
        return dead

    # ── reads ────────────────────────────────────────────────────────────────

    @property
    def grace_seconds(self) -> float:
        return self._grace_seconds

    @property
    def liveness_timeout(self) -> float:
        return self._liveness_timeout

    def surface_count(self) -> int:
        return len(self._surfaces)

    def has_surfaces(self) -> bool:
        return bool(self._surfaces)

    def active_driver(self) -> str | None:
        return self._active_driver

    def is_active_driver(self, connection_id: str) -> bool:
        return self._active_driver == connection_id

    # ── active-driver token: symmetric, auth-gated seize (D4) ────────────────

    def seize(self, connection_id: str, user_id: str | None, now: float) -> bool:
        """Symmetric seize of the active-driver token (Axis-B), gated on Axis-A.

        Any *authorized* attached surface may seize equally — no preferred
        connection, no handshake (D4). Refused for an unknown/unattached
        connection or an unauthorized identity. The deposed holder simply
        becomes a non-holding equal peer; its later answer is rejected at
        delivery-time by the active-driver check (seize↔answer race)."""
        if connection_id not in self._surfaces:
            return False
        if not self._authorized(user_id):
            return False
        self.heartbeat(connection_id, now)
        self._active_driver = connection_id
        return True

    # ── fail-close decision (grace window) ───────────────────────────────────

    def should_fail_close(self, now: float) -> bool:
        """True iff the surface set has been empty for the whole grace window
        after a had-then-lost transition. False while any surface remains, and
        False for a never-had-surface server (R3 — dispatch-time DENY is #2773's
        own immediate path, not this timer)."""
        return (
            not self._surfaces
            and self._last_empty_at is not None
            and now - self._last_empty_at >= self._grace_seconds
        )


class SurfaceRegistry:
    """Process-wide ``agent_name -> SurfaceManager`` map (single-writer server).

    Mirrors the module-global holder pattern the web layer already uses
    (``web/deps`` / ``run_registry``). One manager per agent so fail-close and
    the active-driver token are scoped to the session an operator drives.
    """

    def __init__(
        self,
        *,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
        liveness_timeout: float = DEFAULT_LIVENESS_TIMEOUT,
    ) -> None:
        self._by_agent: dict[str, SurfaceManager] = {}
        self._grace_seconds = grace_seconds
        self._liveness_timeout = liveness_timeout

    def for_agent(
        self, agent_name: str, *, authorized: "Callable[[str | None], bool]"
    ) -> SurfaceManager:
        mgr = self._by_agent.get(agent_name)
        if mgr is None:
            mgr = SurfaceManager(
                authorized=authorized,
                grace_seconds=self._grace_seconds,
                liveness_timeout=self._liveness_timeout,
            )
            self._by_agent[agent_name] = mgr
        return mgr

    def get(self, agent_name: str) -> "SurfaceManager | None":
        return self._by_agent.get(agent_name)


# Module-global registry the endpoint reads (one per server process).
_REGISTRY = SurfaceRegistry()


def surface_registry() -> SurfaceRegistry:
    """The process-wide surface registry."""
    return _REGISTRY


def monotonic() -> float:
    """Indirection over ``time.monotonic`` so production reads a real clock while
    the pure ``SurfaceManager`` methods take an injected ``now`` in tests."""
    return time.monotonic()


__all__ = [
    "DEFAULT_GRACE_SECONDS",
    "DEFAULT_LIVENESS_TIMEOUT",
    "Surface",
    "SurfaceManager",
    "SurfaceRegistry",
    "surface_registry",
    "monotonic",
]
