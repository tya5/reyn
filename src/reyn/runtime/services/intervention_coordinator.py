"""InterventionCoordinator — chain-override ownership + intervention dispatch.

Owns the per-chain intervention overrides (the ``RequestBus`` registered for
ask_user prompts emitted by skills spawned under a chain_id) and orchestrates
one intervention's dispatch: notify any registered chain-override observer
(side-effects only), park the iv as stalled when its origin channel has no live
listener, else route through ``InterventionHandler``. Holds the
``InterventionRegistry`` + ``InterventionHandler`` it delegates to; reads the
session's run→chain map through an injected accessor. The override state lives
here (not on Session), and ``is_override_active`` is the single predicate the
dispatch path and ``ChatInterventionBus`` both consult.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Callable

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
        running_skills_chain_fn: "Callable[[], dict]",
    ) -> None:
        self._registry = registry
        self._handler = handler
        self._events = events
        # run_id → chain_id map lives on Session; read it live each call.
        self._running_skills_chain_fn = running_skills_chain_fn
        # chain_id → RequestBus. The one piece of intervention state that does
        # not live in the registry/handler.
        self._overrides: dict[str, "RequestBus"] = {}

    # ── Override registration (chain_id → RequestBus) ───────────────────────

    def register_override(self, chain_id: str, bus: "RequestBus") -> None:
        """Register a ``RequestBus`` for ask_user prompts emitted by skills
        spawned under *chain_id*. Caller must pair with ``unregister_override``
        in a try/finally."""
        self._overrides[chain_id] = bus

    def unregister_override(self, chain_id: str) -> None:
        """Remove an override. Idempotent."""
        self._overrides.pop(chain_id, None)

    def has_override(self, chain_id: str) -> bool:
        """Return True iff *chain_id* currently has a registered override."""
        return chain_id in self._overrides

    def get_override(self, chain_id: str) -> "RequestBus | None":
        """Return the override bus for *chain_id* or None if absent."""
        return self._overrides.get(chain_id)

    def override_count(self) -> int:
        """Return the number of currently-registered overrides."""
        return len(self._overrides)

    # ── Override resolution ─────────────────────────────────────────────────

    def _override_for_run(self, run_id: "str | None") -> "RequestBus | None":
        """Resolve the override bus active for *run_id* (run → chain → bus)."""
        if run_id is None or not self._overrides:
            return None
        chain_id = self._running_skills_chain_fn().get(run_id)
        if chain_id is None:
            return None
        return self._overrides.get(chain_id)

    def is_override_active(self, run_id: "str | None") -> bool:
        """True iff a chain-override is registered for *run_id*'s chain.

        The single predicate consulted by both the dispatch path and
        ``ChatInterventionBus.deliver`` (origin-channel stamping skip).
        """
        return self._override_for_run(run_id) is not None

    # ── Dispatch orchestration ──────────────────────────────────────────────

    async def dispatch(self, iv: "UserIntervention") -> InterventionAnswer:
        """Dispatch one intervention.

        issue #292 (α): a registered chain override runs as a **side-effect
        observer** (notify A2A peer surfaces / write history / post webhook)
        BEFORE the regular dispatch path, instead of replacing it — the iv
        always flows through ``InterventionHandler.dispatch`` so it lands in
        the registry's active set + WAL + persistent answer buffer.

        issue #268 Phase 2: when the iv carries an ``origin_channel_id`` whose
        listener is gone (origin channel closed mid-call), park it stalled
        instead of delivering to a fall-through listener; other channels
        observe / claim / discard it.
        """
        # Override observer: notify before dispatch so the peer learns
        # input-required before the awaiter (handler.dispatch) blocks the iv
        # future. Notification must NOT block dispatch — a failed webhook /
        # SSE append / status mirror is best-effort.
        override = self._override_for_run(iv.run_id)
        if override is not None:
            try:
                await override.on_dispatch(iv)
            except Exception:  # noqa: BLE001 — side effects are best-effort
                logger.exception(
                    "intervention override on_dispatch raised "
                    "(iv_id=%s)", iv.id,
                )
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
            try:
                return await iv.future
            except asyncio.CancelledError:
                return InterventionAnswer(text="")
        # Default: route through the regular InterventionHandler.
        return await self._handler.dispatch(iv)
