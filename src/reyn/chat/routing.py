"""RoutingLayer — outbox → transport fan-out based on TransportRef.

FP-0013 Component C.

Subscribers register ``{TransportRef type → async handler}`` via
``register()``.  On each ``OutboxMessage``, ``dispatch()`` looks up the
handler for ``type(msg.reply_to)`` and awaits it.

Fallback behaviour (migration safety):
  - ``msg.reply_to is None`` → dispatched to the handler registered for
    ``TuiRef`` (= default surface), if any.  Enables incremental adoption:
    existing code that does not yet stamp ``reply_to`` still reaches the TUI
    renderer unchanged.
  - No handler registered for the ref type → message is silently dropped
    with a debug log.  This is intentional: during migration some outbox
    kinds (e.g. ``__end__``) are consumed directly by the caller's loop and
    should not be re-dispatched.

P7: this module contains no skill-specific strings — handler registration is
driven entirely by the caller (transport adapters).
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from reyn.chat.outbox import OutboxMessage
from reyn.chat.transport import TransportRef, TuiRef

logger = logging.getLogger(__name__)

# Type alias for a handler: async callable that accepts an OutboxMessage.
OutboxHandler = Callable[[OutboxMessage], Awaitable[None]]


class RoutingLayer:
    """Dispatch each OutboxMessage to the handler matching its reply_to type.

    Usage::

        routing = RoutingLayer()
        routing.register(TuiRef, tui_renderer_handler)
        routing.register(A2aRef, a2a_resolve_future)

        # From an outbox consumer loop:
        async for msg in outbox_stream:
            await routing.dispatch(msg)
    """

    def __init__(self) -> None:
        self._handlers: dict[type, OutboxHandler] = {}

    def register(self, ref_type: type, handler: OutboxHandler) -> None:
        """Register ``handler`` as the dispatch target for ``ref_type``.

        ``ref_type`` must be one of the TransportRef variant classes
        (TuiRef, McpRef, A2aRef, AgentRef, SystemRef).  Calling register
        twice with the same type replaces the previous handler.
        """
        self._handlers[ref_type] = handler

    async def dispatch(self, msg: OutboxMessage) -> None:
        """Dispatch ``msg`` to the registered handler for its reply_to type.

        If ``msg.reply_to`` is None the message is routed to the TuiRef
        handler (= migration fallback).  If no handler is registered for
        the resolved type the message is dropped with a debug log.
        """
        ref = msg.reply_to
        # Migration fallback: None → TuiRef
        ref_type: type = TuiRef if ref is None else type(ref)

        handler = self._handlers.get(ref_type)
        if handler is None:
            logger.debug(
                "RoutingLayer.dispatch: no handler for %s (kind=%r), dropping",
                ref_type.__name__,
                msg.kind,
            )
            return
        await handler(msg)

    def registered_types(self) -> frozenset[type]:
        """Return the set of TransportRef types that have registered handlers.

        Exposed for introspection in tests — not part of the primary API.
        """
        return frozenset(self._handlers.keys())


__all__ = ["RoutingLayer", "OutboxHandler"]
