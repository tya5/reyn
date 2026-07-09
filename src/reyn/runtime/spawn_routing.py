"""reyn.runtime.spawn_routing — the typed spawn-time user-reaching routing decision (#2708 P3-item3).

A spawned session inherits two user-reaching capabilities from its spawn site: where its
``present`` renders, and where its ``ask_user`` reaches an operator. When a spawn site
declares NEITHER (the pre-P3-item3 ``None``/``None`` default), the child silently self-binds
— its ``present`` writes to its own undrained outbox (the #2710 detached / #2706 agent-step
orphan class) and its ``ask_user`` origin-pin-parks forever on a ``"tui"`` channel no listener
serves (the confirmed #2710 detached HANG) or silently empty-auto-refuses. The failure was
*incidental*, not chosen.

This module makes the decision **explicit, typed, and reviewed by construction**. Each of the
three spawn seams (``AgentRegistry.spawn_session`` / ``.spawn_session_recorded`` /
``session_api.spawn_ephemeral_session``) takes ``presentation_consumer`` + ``intervention_bridge``
as REQUIRED, no-default kwargs (pinned by ``inspect.signature`` — the #1402
completeness-by-construction mechanism generalized to the spawn axis), so a spawn site cannot
omit the decision. A ``SpawnRouting`` value names ONE of three decisions and resolves to the
concrete pair a site forwards:

- :class:`BridgeToParent` — the child bridges its user-reaching capabilities to its spawning
  PARENT: ``present`` renders to the parent's sink and ``ask_user`` reaches the parent's live
  operator listener (the P3.1 / P3.2a ``SpawnBridge*`` seams). Used by the attached pipeline
  driver spawn AND the LLM ``session_spawn`` tool (a delegated sub-agent's ask_user must reach
  the operator, "delegated-work-can-ask") — since the parent is a live session with its own
  declared routing, this is hang-safe by construction (parent attached → operator; a parent
  that cannot serve degrades to that parent's own reviewed fail-mode, never an unbounded park).
- :class:`AuditOnlyNoSurface` — no attachable surface (detached async pipeline, headless
  ephemeral agent-step): ``present`` is audit-only (the durable ``presented`` P6 event fires;
  the visible draw is a documented no-op — not an orphan) and ``ask_user`` returns a typed,
  reason'd refusal (never silent-empty, never a park/hang). The reviewed *deliberate* fail-mode.
- :class:`ReviewedNA` — a spawn where self-binding to the factory default is genuinely correct
  (a real user-attachable conversation session, or a crash-recovery re-wake). Its ``site`` MUST
  be a member of the reviewed :data:`_REVIEWED_SELF_BOUND_SPAWN_SITES` frozenset — constructing
  one for any other site raises, so a NEW spawn site cannot silently join the self-bound set
  (the #2708 NA-ratchet generalized to the spawn axis).

Enforcement is three-layered, mirroring the landed P1 present-sink gate:
  1. required no-default kwargs (``inspect.signature`` pin) — a site cannot omit the decision;
  2. the :class:`ReviewedNA` frozenset ratchet — a new self-bound site cannot silently join;
  3. an AST guard (``tests/test_spawn_routing_gate_2708.py``) — every ``src/reyn`` call to a
     spawn seam passes both routing kwargs explicitly, so a new gap is a PR-time CI failure.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.runtime.session import Session

# The reviewed set of spawn sites where self-binding to the session factory's own default
# (``presentation_consumer=None`` / ``intervention_bridge=None``) is genuinely correct — either
# a real user-attachable conversation session (the user can drain its outbox / answer its
# ask_user by attaching), or a crash-recovery re-wake with no attached caller. Membership is a
# REVIEWED ratchet (the #2708 NA-ratchet model): a new spawn site cannot self-bind without being
# added here in review. Keyed ``"<src-relpath>::<enclosing-function>"`` so the id is stable
# under line moves and unambiguous across same-named functions. Each member states WHY it is NA:
#   - resolve_session:      a transport-native inbound session (web:<thread> / a2a:<peer>); the
#                           transport drains its OWN outbox — no parent to bridge to.
#   - restore_all:          crash-recovery re-creates a spawned session to re-adopt its snapshot;
#                           a headless re-wake with no attached surface (present → its own audit log).
#   - _rewake_pipeline_runs: pipeline driver crash-recovery re-wake; the originally-attached caller
#                           is gone, the result routes via the inbox reply address.
#   - session_cmd:          ``/session new`` opens a real attachable conversation session under the
#                           agent — a HUMAN starts it and ``/session switch``es to focus + drain it.
# NB: the LLM ``session_spawn`` tool (``router_host_adapter.spawn_session``) is deliberately NOT
# here — it is LLM-initiated + backgrounded, so no operator is guaranteed to know the child sid to
# attach + drain, and a self-bound child would hit the very origin-pin ask_user hang this gate
# closes. It routes ``BridgeToParent`` instead (delegated-work-can-ask), NOT self-bound.
_REVIEWED_SELF_BOUND_SPAWN_SITES = frozenset({
    "runtime/registry.py::resolve_session",
    "runtime/registry.py::restore_all",
    "runtime/registry.py::_rewake_pipeline_runs",
    "interfaces/slash/session.py::session_cmd",
})


class SpawnRouting:
    """Base of the typed spawn-time routing decision. A subclass resolves to the
    ``(presentation_consumer, intervention_bridge)`` pair a spawn site forwards to the spawn seam
    via the required ``presentation_consumer=`` / ``intervention_bridge=`` kwargs."""

    @property
    def presentation_consumer(self) -> "object | None":
        """The present-sink consumer the spawned session is constructed with (or ``None`` to
        inherit the session factory's own default — the self-bound case)."""
        raise NotImplementedError

    @property
    def intervention_bridge(self) -> "object | None":
        """The ask_user/permission bridge the spawned session is constructed with (or ``None``
        to keep the self-bound, listener-enforced default)."""
        raise NotImplementedError


class BridgeToParent(SpawnRouting):
    """An attached PARENT surface exists — bridge the child's user-reaching capabilities to it
    (P3.1 present + P3.2a intervention). Used by the attached pipeline driver spawn: a ``present``
    step reaches the parent's sink and an ``ask_user`` step reaches the parent's live operator
    listener, both by construction."""

    def __init__(self, parent_session: "Session") -> None:
        self._parent_session = parent_session

    @property
    def presentation_consumer(self) -> "object":
        from reyn.runtime.presentation_consumer import SpawnBridgePresentationConsumer

        return SpawnBridgePresentationConsumer(
            parent_consumer=self._parent_session.presentation_consumer,
            parent_session=self._parent_session,
        )

    @property
    def intervention_bridge(self) -> "object":
        from reyn.runtime.session import DEFAULT_CHAT_CHANNEL_ID
        from reyn.runtime.session_buses import SpawnBridgeInterventionListener

        # The parent chat surface registers its operator listener under DEFAULT_CHAT_CHANNEL_ID
        # ("tui") — the same id a parent-native ask_user stamps — so the bridged iv routes to the
        # live listener, not the stalled queue.
        return SpawnBridgeInterventionListener(
            parent_session=self._parent_session,
            parent_channel_id=DEFAULT_CHAT_CHANNEL_ID,
        )


class AuditOnlyNoSurface(SpawnRouting):
    """No attachable surface (detached async pipeline / headless ephemeral agent-step): the
    DELIBERATE, reviewed fail-mode. ``present`` is audit-only (the durable ``presented`` P6 event
    fires; the visible draw is a documented no-op — not an orphan outbox message) and ``ask_user``
    returns a typed, reason'd refusal (``InterventionAnswer.refused`` → the op returns
    ``status="refused"``) — never a silent empty answer, never a park/hang."""

    @property
    def presentation_consumer(self) -> "object":
        from reyn.runtime.presentation_consumer import AuditOnlyPresentationConsumer

        return AuditOnlyPresentationConsumer()

    @property
    def intervention_bridge(self) -> "object":
        from reyn.runtime.session_buses import AuditOnlyInterventionBridge

        return AuditOnlyInterventionBridge()


class ReviewedNA(SpawnRouting):
    """Self-binding to the session factory's own default is genuinely correct here — a real
    user-attachable conversation session, or a crash-recovery re-wake. ``site`` MUST be a member
    of :data:`_REVIEWED_SELF_BOUND_SPAWN_SITES`; constructing one for any other site raises, so a
    NEW spawn site cannot silently self-bind (the #2708 NA-ratchet, spawn axis)."""

    def __init__(self, site: str) -> None:
        if site not in _REVIEWED_SELF_BOUND_SPAWN_SITES:
            raise ValueError(
                f"ReviewedNA refused for spawn site {site!r}: not a reviewed self-bound site. A "
                f"spawn that reaches the user must declare BridgeToParent / SelfDeliveringWithDrain "
                f"/ AuditOnlyNoSurface; only {sorted(_REVIEWED_SELF_BOUND_SPAWN_SITES)} may self-bind "
                f"to the factory default (add here ONLY via review — see the frozenset rationale)."
            )
        self._site = site

    @property
    def site(self) -> str:
        """The reviewed self-bound spawn-site id this decision was constructed for."""
        return self._site

    @property
    def presentation_consumer(self) -> None:
        return None

    @property
    def intervention_bridge(self) -> None:
        return None


__all__ = [
    "AuditOnlyNoSurface",
    "BridgeToParent",
    "ReviewedNA",
    "SpawnRouting",
    "_REVIEWED_SELF_BOUND_SPAWN_SITES",
]
