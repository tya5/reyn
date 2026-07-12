"""reyn.hooks.composed_consumer — the composed->Sync consumer bridge
(Hook-Event Redesign Phase 5 part 1, proposal
``docs/deep-dives/proposals/0059-hook-event-redesign.md`` §9 item 3 / #2881).

``Composer`` (Phase 4b, ``reyn.hooks.composer``) publishes a composed
``HookEvent`` (``kind="composed:<name>"``) ONLY to the ``HookBus`` — that
module's invariant #5 keeps the Composer itself structurally decoupled from
``HookDispatcher`` (it never re-enters Sync dispatch). Phase 5 part 1 makes
``composed:<name>`` a subscribable Sync ``on:`` target (``reyn.hooks.loader``
now accepts the ``composed:`` prefix), so SOMETHING must bridge: observe a
composed event on the Bus, and run any Sync-registered ``HookDef`` whose
``on:`` equals that composed kind. This module is that bridge.

This is the ratified "``#5 structural-non-reentry`` -> ``§224
valve-metered-allow``" transition (proposal §9 item 3 / §8.4 item 3): a
wake=true push a consumer hook makes here traverses ``HookDispatcher``'s
existing E-path (``_push_resolved`` -> the injected ``put_inbox`` callable ->
the Session's ``HOOK_INBOX_KIND``/``kind="hook"`` inbox message) — the SAME
path every other hook-driven wake takes, so the Session's existing
``max_hook_driven_turns`` loop-valve counts a composed->wake turn with ZERO
new bounding logic. A self-stimulating composed->wake->(builtin
dispatch)->composed loop therefore force-closes at the cap exactly like any
other hook-driven chain (see the Tier-2 loop-valve pin test for this phase).

Deliberately NOT part of ``Composer``/``ComposerRegistry``
(``reyn.hooks.composer``) — that module's invariant #5 must stay true (a
Composer only ever calls ``HookBus.publish``); this is a SEPARATE Bus
subscriber, symmetric in shape to a Composer's own ``run()`` loop, that
reacts to ``composed:*`` kinds specifically. ``HookDispatcher.
dispatch_bus_event`` (the method this module calls) does NOT re-publish to
the bus — a composed event already arrived via the bus, so re-broadcasting
it here would be a duplicate delivery to any sibling Composer/subscriber
correlating on the same kind.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from reyn.hooks.bus import HookBus
from reyn.hooks.composer import COMPOSED_KIND_PREFIX
from reyn.hooks.dispatcher import HookDispatcher

_log = logging.getLogger(__name__)


class ComposedEventConsumer:
    """Bridges Bus-observed ``composed:*`` events into Sync ``HookDispatcher``
    execution.

    Construct once per Session with that session's ``HookBus`` + the SAME
    session's ``HookDispatcher``; call :meth:`start` once (idempotent — a
    second call is a no-op while the background task is alive) and
    :meth:`stop` at session teardown for a clean cancellation, mirroring
    ``reyn.hooks.composer.ComposerRegistry``'s start/stop shape."""

    def __init__(self, *, bus: HookBus, dispatcher: HookDispatcher) -> None:
        self._bus = bus
        self._dispatcher = dispatcher
        self._task: "asyncio.Task | None" = None

    def start(self) -> None:
        """Start the background subscription task. Idempotent: a second call
        while already running is a no-op (no duplicate subscription)."""
        if self._task is not None:
            return
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        """Cancel the background task and await its clean exit. Idempotent —
        safe to call even if :meth:`start` was never called."""
        if self._task is None:
            return
        task, self._task = self._task, None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    @property
    def is_running(self) -> bool:
        """Public (non-private-state) surface for tests/observability."""
        return self._task is not None and not self._task.done()

    async def _run(self) -> None:
        async with self._bus.subscribe() as sub:
            while True:
                event = await sub.get()
                if not event.kind.startswith(COMPOSED_KIND_PREFIX):
                    continue  # not a composed event — nothing for this bridge to do
                try:
                    await self._dispatcher.dispatch_bus_event(event)
                except Exception as exc:  # noqa: BLE001 — a bridge fault must never kill the task
                    _log.warning(
                        "ComposedEventConsumer: dispatch_bus_event(%r) raised: %s: %s",
                        event.kind, type(exc).__name__, exc,
                    )


__all__ = ["ComposedEventConsumer"]
