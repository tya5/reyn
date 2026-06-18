"""reyn.runtime.session_buses — Session-backed intervention bus adapters.

The intervention-routing adapters that bind to a ``Session``:

- ``AgentRequestBus`` — a ``RequestBus`` adapter that forwards an
  intervention to the Session's ``handle_intervention`` so the Agent owns the
  routing decision.
- ``ChatInterventionBus`` — a ``UserChannel`` implementation that routes a
  prompt through the Session's outbox/inbox to the attached listener, stamping
  origin-channel provenance for cross-channel routing.

Both hold a ``Session`` and delegate to it; the ``Session`` type is referenced
only under ``TYPE_CHECKING`` so importing this module never imports
``session`` (one-directional ``session`` -> ``session_buses`` at runtime).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
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

    One instance per skill spawn — captures `run_id` and a default `skill_name`
    so the chat session can drop pending interventions when the spawn is
    cancelled. Interventions emitted by ops carry their own `skill_name` from
    `OpContext`; this bus only fills in `run_id` (which the OS layer doesn't
    have, since chat tracks runs separately from `SkillRuntime.run_id`).

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
        skill_name: str | None,
        *,
        channel_id: str | None = None,
    ) -> None:
        self._session = session
        self._run_id = run_id
        self._skill_name = skill_name
        # issue #268 Phase 2 continuation: optional channel_id stamping.
        # Production wiring (= Session._build_intervention_bus_for_skill)
        # passes the session's canonical channel_id (e.g. "tui") so
        # skill-emitted ivs carry provenance for cross-channel routing.
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
        A2A-spawned skills (= ``_build_agent`` constructs one per
        skill spawn regardless of caller), and A2AInterventionBus needs
        a clean slot to stamp ``a2a:<run_id>`` downstream. Respects
        pre-existing stamping (= upstream-set origin wins for
        multi-hop delegation provenance).
        """
        # issue #268 Phase 2 continuation: stamp origin channel for
        # cross-channel routing (only when configured AND no chain
        # override is going to claim this iv first). Use the bus's
        # captured ``_run_id`` for the override lookup since
        # ``iv.run_id`` isn't filled in until below.
        if self._channel_id is not None and iv.origin_channel_id is None:
            override_active = False
            run_id_for_lookup = iv.run_id or self._run_id
            if (
                run_id_for_lookup is not None
                and self._session._intervention_overrides
            ):
                chain_id = self._session.running_skills_chain.get(
                    run_id_for_lookup,
                )
                if chain_id is not None:
                    override_active = (
                        chain_id in self._session._intervention_overrides
                    )
            if not override_active:
                iv.origin_channel_id = self._channel_id
        if iv.run_id is None:
            iv.run_id = self._run_id
        if not iv.skill_name:
            iv.skill_name = self._skill_name
        # PR-intervention-link L6: short-circuit if a previous (crashed-then-
        # restored) run's intervention was already answered post-restart.
        # The L5 watcher buffered the answer keyed by run_id; the resuming
        # skill's first ask_user picks it up here without dispatching a
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
