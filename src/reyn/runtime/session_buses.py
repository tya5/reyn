"""reyn.runtime.session_buses — Session-backed intervention + presentation adapters.

The routing adapters that bind to a ``Session``:

- ``AgentRequestBus`` — a ``RequestBus`` adapter that forwards an
  intervention to the Session's ``handle_intervention`` so the Agent owns the
  routing decision.
- ``ChatInterventionBus`` — a ``UserChannel`` implementation that routes a
  prompt through the Session's outbox/inbox to the attached listener, stamping
  origin-channel provenance for cross-channel routing.
- ``OutboxPresentationRenderer`` (FP-0054 PR-B) — a ``PresentationRenderer`` that
  routes a resolved presentation's render model through the SAME outbox the other
  two use, as a new ``"presentation"`` ``OutboxMessage`` kind. Fire-and-forget: it
  never awaits anything (the `present` op's ack is already complete before this
  runs), mirroring the op's own fire-and-continue contract.

All three hold a ``Session`` and delegate to it; the ``Session`` type is referenced
only under ``TYPE_CHECKING`` so importing this module never imports
``session`` (one-directional ``session`` -> ``session_buses`` at runtime).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from reyn.runtime.outbox import OutboxMessage

if TYPE_CHECKING:
    from reyn.core.present.binding import ResolvedPresentation
    from reyn.runtime.session import Session
    from reyn.user_intervention import InterventionAnswer, UserIntervention


class AgentRequestBus:
    """``RequestBus`` adapter that subscribes to a Session (= Agent).

    issue #254 Phase 3: OS-layer callers (= ``handle_limit_exceeded``,
    permission gates, ``ask_user`` op) hold a ``RequestBus``-typed
    reference; this adapter forwards ``request(iv)`` to the Agent's
    ``handle_intervention(iv)`` so the Agent owns the routing decision.

    Phase 3 ships behaviour parity (= ``handle_intervention`` just
    forwards to ``_dispatch_intervention``); Phase 4 will add
    ``self_answer`` / ``parent_delegate`` branches on the Agent side
    without changing this adapter's surface.

    The adapter satisfies the ``RequestBus`` runtime_checkable Protocol
    so OS code typed against ``bus: RequestBus`` (or the legacy
    ``InterventionBus`` alias) accepts it without further wiring.
    """

    def __init__(self, session: "Session") -> None:
        self._session = session

    @property
    def session(self) -> "Session":
        """Read-only accessor for the backing Session. Tests verify
        that adapters from the same session share identity through this
        surface."""
        return self._session

    async def request(self, iv: "UserIntervention") -> "InterventionAnswer":
        """``RequestBus.request`` — delegate to the Agent's intervention handler."""
        return await self._session.handle_intervention(iv)


class ChatInterventionBus:
    """``UserChannel`` implementation that routes through Session's
    outbox/inbox to the attached TUI listener.

    One instance per sub-run — captures `run_id` and a default `actor`
    so the chat session can drop pending interventions when the run is
    cancelled. Interventions emitted by ops carry their own `actor` from
    `OpContext`; this bus only fills in `run_id` (which the OS layer doesn't
    have, since chat tracks runs separately from the runtime's run_id).

    Phase 2 (issue #254): the canonical method is ``deliver`` (= the
    Agent↔User contract).  ``request`` is retained as an alias so
    callers typed against ``InterventionBus`` / ``RequestBus`` continue
    to work unchanged.  Phase 3 will route OS-level requests through the
    Agent layer, which will then call ``deliver`` on this channel — at
    that point ``request`` becomes unused at top-level (= a candidate
    for Phase 5 removal).
    """

    def __init__(
        self,
        session: "Session",
        run_id: str | None,
        actor: str | None,
        *,
        channel_id: str | None = None,
    ) -> None:
        self._session = session
        self._run_id = run_id
        self._actor = actor
        # issue #268 Phase 2 continuation: optional channel_id stamping.
        # Production wiring (= Session._build_intervention_bus_for_run)
        # passes the session's canonical channel_id (e.g. "tui") so
        # emitted ivs carry provenance for cross-channel routing.
        # Test fixtures that construct ChatInterventionBus directly
        # without passing channel_id see unchanged behaviour (= no
        # stamping → no stall check → existing dispatch path).
        self._channel_id = channel_id

    @property
    def channel_id(self) -> str | None:
        """Configured channel identifier for issue #268 origin-pin
        routing. ``None`` means stamping is disabled for this instance
        (= test-fixture default that doesn't engage the new mechanism).
        """
        return self._channel_id

    async def deliver(self, iv: "UserIntervention") -> "InterventionAnswer":
        """``UserChannel.deliver`` — route the prompt to Session's
        outbox/inbox so the attached TUI surfaces it to the user.

        issue #268 Phase 2 continuation: when this bus was constructed
        with a ``channel_id`` AND no chain-override is active for the
        iv's run, stamp ``iv.origin_channel_id`` so the agent layer
        can attribute the iv to this channel for cross-channel
        observe / discard / claim routing. The override-aware skip
        matters because the SAME ChatInterventionBus instance services
        A2A-spawned sub-runs (= ``_build_agent`` constructs one per
        sub-run regardless of caller), and A2AInterventionBus needs
        a clean slot to stamp ``a2a:<run_id>`` downstream. Respects
        pre-existing stamping (= upstream-set origin wins for
        multi-hop delegation provenance).
        """
        if self._channel_id is not None and iv.origin_channel_id is None:
            iv.origin_channel_id = self._channel_id
        if iv.run_id is None:
            iv.run_id = self._run_id
        if not iv.actor:
            iv.actor = self._actor
        # PR-intervention-link L6: short-circuit if a previous (crashed-then-
        # restored) run's intervention was already answered post-restart.
        # The L5 watcher buffered the answer keyed by run_id; the resuming
        # run's first ask_user picks it up here without dispatching a
        # duplicate prompt.
        if iv.run_id is not None:
            buffered = self._session.consume_buffered_intervention_answer(iv.run_id)
            if buffered is not None:
                return buffered
        return await self._session._dispatch_intervention(iv)

    async def request(self, iv: "UserIntervention") -> "InterventionAnswer":
        """``RequestBus.request`` — Phase 2 backwards-compat alias.

        Delegates to ``deliver``; preserved so existing call sites typed
        against ``InterventionBus`` keep working until the Phase 3 Agent
        migration moves them onto the Agent-mediated path.
        """
        return await self.deliver(iv)

    # Note: _dispatch_intervention on session.py is now a thin wrapper around
    # InterventionRegistry.dispatch (wave 2 of PR-refactor-session-1). Kept
    # method-level call so the bus signature stays stable.


class SpawnBridgeInterventionListener:
    """A spawned/driver session's intervention bridge that routes the child's
    ``ask_user`` / permission interventions to the PARENT session's live-operator
    listener *by construction* (#2708 P3.2a). The intervention analog of
    ``SpawnBridgePresentationConsumer`` (``runtime/presentation_consumer.py``).

    A chat-invoked pipeline runs in a spawned driver-session that gets a fresh
    ``InterventionRegistry(enforce_listener_presence=True)`` with **no** listener
    registered (no ``bind_focus_listeners`` / ``register_intervention_listener`` for
    the driver). So a driver ``ask_user`` would hit the no-listener short-circuit
    (``services/intervention_registry.py`` — ``dispatch`` returns an empty
    ``InterventionAnswer(text="")``) and **silently auto-refuse** — even under
    ``run_pipeline_attached`` where a live operator is synchronously blocked on the
    parent (#2721, the intervention-delivery sibling of the #2707 present gap).

    This bridge, wired ONLY on the attached driver-spawn path
    (``session_api._spawn_pipeline_driver_session``), makes the driver's router
    intervention bus dispatch on the PARENT session instead of the child:
    ``bus()`` returns a ``ChatInterventionBus`` bound to ``parent_session``, so the
    child's ``ask_user`` routes into the parent's ``InterventionRegistry`` — which
    HAS the live operator's listener — announces on the PARENT's outbox (the
    operator sees it exactly as a chat-native ask_user), and the operator's answer
    resolves the SAME ``iv.future`` the driver's op awaits. The delivery is
    byte-identical to a parent-native ask_user (same channel-id stamping), and no
    reverse answer path is needed (the future lives on the shared
    ``UserIntervention``).

    Detached (``start_pipeline_run``) and ephemeral-headless spawns pass no bridge
    and keep the fail-closed default (no live operator) — the detached case is a
    tracked known-RED cell (P3 spawn-routing completeness gate), NOT blessed-correct.
    """

    def __init__(self, parent_session: "Session", parent_channel_id: str) -> None:
        self._parent_session = parent_session
        self._parent_channel_id = parent_channel_id

    @property
    def parent_session(self) -> "Session":
        """Read-only accessor for the bridged-to parent session (tests / audit)."""
        return self._parent_session

    @property
    def parent_channel_id(self) -> str:
        """The parent's intervention channel the bridged iv is stamped with — the
        SAME id the parent operator registered its listener under, so the parent
        coordinator routes the bridged iv to the live listener (not the stalled
        queue)."""
        return self._parent_channel_id

    def bus(
        self, *, run_id: "str | None" = None, actor: "str | None" = None,
    ) -> "ChatInterventionBus":
        """Build the driver's router intervention bus bound to the PARENT session.

        The child ignores its OWN session (the analog of
        ``SpawnBridgePresentationConsumer.sink`` ignoring the child): the returned
        bus delivers through ``parent_session._dispatch_intervention`` and stamps
        ``parent_channel_id`` so the parent's live operator listener resolves it —
        identical to a parent-native chat ask_user."""
        return ChatInterventionBus(
            self._parent_session,
            run_id=run_id,
            actor=actor,
            channel_id=self._parent_channel_id,
        )


# The reason a detached/headless spawn's ``ask_user`` is refused — carried on the typed
# ``InterventionAnswer.reason`` so the pipeline/agent-step sees a DELIBERATE outcome, never a
# fabricated empty answer nor a park/hang.
NO_SURFACE_REFUSAL_REASON = (
    "no interactive surface attached to this detached/headless run — ask_user cannot reach an "
    "operator; the run proceeds with a deliberate refusal rather than hanging or fabricating an "
    "empty answer"
)


class _AuditOnlyInterventionChannel:
    """The ``UserChannel`` / ``RequestBus`` a detached/headless spawn dispatches ``ask_user`` on:
    it DELIBERATELY refuses every intervention with a reason, resolving IMMEDIATELY — it never
    enqueues, never announces, never awaits a future. So there is no origin-pin park/hang (the
    confirmed #2710 detached fail-mode: the self-bound ``ChatInterventionBus`` stamps
    ``origin_channel_id='tui'``, and ``InterventionCoordinator.dispatch`` parks it stalled +
    ``await iv.future`` forever) and no silent empty-string auto-refuse."""

    async def deliver(self, iv: "UserIntervention") -> "InterventionAnswer":
        from reyn.user_intervention import InterventionAnswer

        return InterventionAnswer(refused=True, reason=NO_SURFACE_REFUSAL_REASON)

    async def request(self, iv: "UserIntervention") -> "InterventionAnswer":
        return await self.deliver(iv)


class AuditOnlyInterventionBridge:
    """The intervention analog of ``AuditOnlyPresentationConsumer`` (``runtime/
    presentation_consumer.py``): the DELIBERATE ``ask_user`` routing for a spawn with NO
    attachable operator surface — a detached pipeline driver (``start_pipeline_run``) or a
    headless ephemeral agent-step worker (``run_agent_step``).

    Where ``SpawnBridgeInterventionListener`` routes a child ``ask_user`` to a live PARENT
    operator, this bridge has no parent to route to, so its ``bus()`` returns a channel that
    resolves every ``ask_user`` with a typed, reason'd REFUSAL
    (``InterventionAnswer(refused=True, reason=...)``). That is the reviewed replacement for the
    two incidental fail-modes an unrouted detached spawn hit before: the origin-pin park/hang
    (confirmed the live #2710 fail-mode) and, on other constructions, the silent empty-string
    auto-refuse (``InterventionRegistry.dispatch``'s ``enforce_listener_presence`` short-circuit).
    Chosen EXPLICITLY at the spawn site via ``runtime/spawn_routing.AuditOnlyNoSurface``."""

    def bus(
        self, *, run_id: "str | None" = None, actor: "str | None" = None,
    ) -> "_AuditOnlyInterventionChannel":
        """Build the spawn's router intervention channel — a refuse-with-reason sink. The
        ``run_id`` / ``actor`` are accepted for signature-parity with
        ``SpawnBridgeInterventionListener.bus`` but unused (a refusal carries no provenance)."""
        return _AuditOnlyInterventionChannel()


class OutboxPresentationRenderer:
    """``PresentationRenderer`` (``core/present/renderer.py``) that routes a resolved
    presentation's render model onto the Session's outbox as a ``"presentation"``
    ``OutboxMessage`` — the SAME queue every other display kind (agent/status/error/
    trace/intervention) already flows through.

    Deliberately thin: this class does NOT convert ``nodes`` to Rich renderables — that
    conversion is a UI-layer concern (``interfaces/repl/renderer.py``'s
    ``format_inline_message``), consistent with how every other outbox kind carries raw
    data in ``meta`` and lets the UI layer decide how to draw it. ``op_runtime`` (which
    constructs the ``ResolvedPresentation`` this class receives) never imports Rich or
    prompt_toolkit; this adapter is the seam where that boundary is respected.
    """

    surface_name = "inline-cui"

    def __init__(self, session: "Session") -> None:
        self._session = session

    def render(self, resolved: "ResolvedPresentation") -> None:
        self._session.outbox.put_nowait(
            OutboxMessage(kind="presentation", text="", meta={"nodes": resolved.nodes})
        )
