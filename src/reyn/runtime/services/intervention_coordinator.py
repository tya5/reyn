"""InterventionCoordinator — intervention dispatch orchestration.

Orchestrates one intervention's dispatch: park the iv as stalled when its
origin channel has no live listener, else route through ``InterventionHandler``.
Holds the ``InterventionRegistry`` + ``InterventionHandler`` it delegates to.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from reyn.user_intervention import InterventionAnswer

if TYPE_CHECKING:
    from reyn.core.events.events import EventLog
    from reyn.user_intervention import RequestBus, UserIntervention

    from .intervention_handler import InterventionHandler
    from .intervention_registry import InterventionRegistry

logger = logging.getLogger(__name__)


class InterventionCoordinator:
    """Owns chain-override state and the per-intervention dispatch orchestration."""

    def __init__(
        self,
        *,
        registry: "InterventionRegistry",
        handler: "InterventionHandler",
        events: "EventLog",
    ) -> None:
        self._registry = registry
        self._handler = handler
        self._events = events

    # ── Dispatch orchestration ──────────────────────────────────────────────

    async def dispatch(self, iv: "UserIntervention") -> InterventionAnswer:
        """Dispatch one intervention.

        issue #268 Phase 2: when the iv carries an ``origin_channel_id`` whose
        listener is gone (origin channel closed mid-call), park it stalled
        instead of delivering to a fall-through listener; other channels
        observe / claim / discard it.
        """
        # Origin-pin stall check: an iv pinned to an absent listener is parked
        # in the stalled queue rather than delivered to a fall-through listener.
        if (
            iv.origin_channel_id is not None
            and not self._registry.has_listener(iv.origin_channel_id)
        ):
            self._events.emit(
                "intervention_routed",
                route="user_channel_stalled",
                iv_kind=iv.kind,
                iv_id=iv.id,
                origin_channel_id=iv.origin_channel_id,
            )
            self._registry.park_stalled(iv)
            return await iv.future
        # Default: route through the regular InterventionHandler.
        return await self._handler.dispatch(iv)
