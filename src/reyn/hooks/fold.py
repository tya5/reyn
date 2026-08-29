"""reyn.hooks.fold — the shared "fold launches, not events" batching
primitive (#5516).

Before this module, every in-process hook-event accumulation point
(``_BoundedEventBridge`` in ``ingress.py`` for ``mcp_resource_updated``/
``file_changed``, and ``ComposedEventConsumer`` in ``composed_consumer.py``
for ``composed:*``) drained its queue ONE EVENT AT A TIME: each item that
arrived launched its OWN hook (its OWN child process, for ``exec``/
``exec_capture``; its OWN inbox push, for ``template_push``). A burst of N
events produced N launches — measured directly against a real session's
event log (#5516 issue): 98 ``hook_shell_executed`` launches for 97
``mcp_resource_updated`` notifications in one session.

Owner ruling (#5516, verbatim): "1 メッセージずつ hook 起動する意味ないでしょ"
(there's no point launching a hook once per message). The fix owner
specified is NOT collapsing N events into 1 event (that would lose N-1
events' data — concretely, for ``cron_fired``, N-1 real cron jobs' work
would vanish). It is collapsing N LAUNCHES into 1 launch that carries all N
events' data as an array.

This module owns exactly the DRAINING/BATCHING half of that — "how many
queued items can I fold into the batch I am about to dispatch, without
losing any and without waiting for more" — never the dispatch itself
(that stays each caller's own concern: ``_BoundedEventBridge`` calls its
injected ``hook_trigger``, ``ComposedEventConsumer`` calls
``HookDispatcher.dispatch_bus_event_batch``). One implementation, not two
independently-drifting copies (CLAUDE.md: a duplicated rule drifts) —
both accumulation points share the exact same 3 acceptance conditions
below, so they share this one function.

## The 3 acceptance conditions (#5516's own canonical spec, not a style
choice — a queue whose bound direction ever flips silently breaks a
design that assumed one side)

**① Direction-independent.** Must not depend on which end of the queue
overflow drops (``_BoundedEventBridge`` today: drop-newest;
``HookBus``'s subscription queue today: drop-oldest — see each
``deliver``/``publish`` site's own docstring). This module never reads
or assumes an overflow direction — it only ever reads ``qsize()`` and
drains via ``get_nowait()``, both direction-agnostic.

**② Count BEFORE any matcher/filter.** The count this module returns is
"how many items were sitting in the queue at drain time" — a MATCHER-
BLIND count (a caller filters afterwards, e.g. per-hook-entry matcher
evaluation). Reading it after filtering would under-count (a caller
would then drain past its own boundary chasing a moving target).

**③ Read ``qsize()`` strictly BEFORE dispatching the batch, in the SAME
call.** Reading it after dispatch (or in a separate call) would let
items that arrive DURING dispatch inflate the count and be silently
swept into the "already batched" accounting — losing them. Reading it
before, exactly once, per batch, is what proves the no-loss guarantee
below.

## No-loss guarantee (owner's own proof, #5516 issue §3b/§3c)

An item arriving WHILE a batch is being assembled/dispatched is not
lost: ``asyncio.Queue`` is FIFO, so it queues strictly AFTER the N
items already counted for the current batch — it is picked up whole on
this loop's NEXT iteration, never silently absorbed into the batch that
already launched.

```
batch 1: get() -> e_1, qsize() = N (read HERE, before dispatch)  -> skip = N
         queue: [e_2 ... e_{N+1}]
mid-dispatch: e_x arrives -> queue tail (position N+2)
after:   e_2..e_{N+1} drained into batch 1 (N items, exactly)
batch 2: get() -> e_x -- not lost, not double-counted
```

This guarantee is exactly why condition ③ is an ACCEPTANCE CONDITION,
not a preference: reading ``qsize()`` at any other point breaks the
proof above.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Protocol, TypeVar

T = TypeVar("T")
T_co = TypeVar("T_co", covariant=True)


class _FoldableQueue(Protocol[T_co]):
    """The minimal surface :func:`drain_folded` needs — satisfied by a
    real ``asyncio.Queue`` (used by ``_BoundedEventBridge``, one queue per
    adapter) AND by ``reyn.hooks.bus.HookBusSubscription`` (used by
    ``ComposedEventConsumer`` — a WRAPPER around its own private queue,
    exposing exactly this surface publicly, #5516). A ``Protocol`` here
    rather than a literal ``asyncio.Queue`` type hint means neither caller
    has to reach into the other's private state to satisfy this
    function's signature. Covariant: every member here only ever
    PRODUCES a ``T_co`` (``get``/``get_nowait``), never accepts one as a
    parameter — the standard shape for a read-only Protocol."""

    async def get(self) -> T_co: ...
    def get_nowait(self) -> T_co: ...
    def qsize(self) -> int: ...


async def drain_folded(
    queue: "_FoldableQueue[T]",  # T unbound at the call boundary, so use the invariant T here
    dispatch_batch: "Callable[[list[T]], Awaitable[Any]]",
) -> None:
    """Run forever (the caller wraps this in ``asyncio.create_task`` and
    cancels it — this coroutine never returns on its own, matching
    ``_BoundedEventBridge._drain``'s and ``ComposedEventConsumer._run``'s
    own pre-#5516 shape exactly, so ``aclose``/``stop``'s cancellation
    handling needs no change).

    Each iteration: blocks on the FIRST item via ``queue.get()`` — launch
    happens immediately on the first item, never after a time window (owner
    ruling #5516 §1b: this is WHY folding adds no latency, unlike a
    window-based batcher). Then reads ``qsize()`` — condition ③, see this
    module's own docstring — as N, and drains exactly N MORE items via
    ``get_nowait()`` (bounded: never chases an item that arrives after this
    read). Calls ``dispatch_batch`` exactly ONCE with the full ``1+N``-item
    batch, in queue order (oldest-first, i.e. arrival order).

    Per-item failure isolation is ``dispatch_batch``'s own job, entirely
    — this loop has NO ``try``/``except`` of its own around the
    ``await dispatch_batch(batch)`` call below; a ``dispatch_batch`` that
    raises WOULD propagate out and kill this ``while True`` loop with a
    single raise. Safety comes from each of the three real callers
    wrapping their OWN ``dispatch_batch`` closure body in a
    ``try``/``except`` INSIDE the callback itself (``ingress.py:180`` /
    ``composed_consumer.py:110`` / ``external_fire.py:185``, verified
    directly against each — lead-coder TESTS-READ finding, #5516), same
    per-hook-isolation posture as before #5516. This function's own
    contract is therefore: call ``dispatch_batch`` exactly once per
    batch and trust it not to raise — it does not add a resilience layer
    of its own on top."""
    while True:
        first = await queue.get()
        n_more = queue.qsize()  # condition ③: read BEFORE any drain/dispatch below
        batch = [first]
        for _ in range(n_more):
            batch.append(queue.get_nowait())
        await dispatch_batch(batch)


def observe_drain_task_death(
    task: "asyncio.Task[None]", *, emit_event: "Callable[..., Any] | None", label: str,
) -> None:
    """#5521 (architect ruling, on this issue's own thread) — wire via
    ``task.add_done_callback(functools.partial(observe_drain_task_death,
    emit_event=..., label=...))`` right where a caller creates the task
    that runs :func:`drain_folded` forever (``asyncio.create_task``/
    ``asyncio.ensure_future``).

    This does NOT swallow anything and does NOT add a try/except anywhere
    — :func:`drain_folded`'s own contract above ("this loop has NO
    try/except of its own … a dispatch_batch that raises WOULD propagate
    out and kill this while True loop with a single raise") is completely
    unchanged. A ``done_callback`` fires strictly AFTER the task has
    already ended, one way or another — it OBSERVES a death that already
    happened, it cannot prevent or convert one. ``task.exception()`` on an
    already-done, non-cancelled task returns the exception (never raises
    it) — that is what makes this an observation, not a second isolation
    layer of the #5516-arc-#5527 kind (this module deliberately does not
    grow its own ``except Exception`` here).

    ``task.cancelled()`` is checked FIRST and returns early — a cancelled
    task is the NORMAL shutdown shape every one of this repo's 3 real
    callers uses (``stop()``/``aclose()``: ``task.cancel()`` then await
    with ``CancelledError`` suppressed), not a death. Calling
    ``task.exception()`` on a cancelled task raises ``CancelledError``
    itself, so this order is load-bearing, not incidental.

    Real incident this closes (#5516 arc, #5521): the 3 current callers
    each wrap their own ``dispatch_batch`` callback body in its own
    ``try``/``except`` (see :func:`drain_folded`'s own docstring) — but
    that convention is enforced NOWHERE. A future 4th caller that drops
    that inner try/except would have its own callback's raise propagate
    all the way up through this loop and kill the drain task PERMANENTLY
    (the hook point it served never fires again) with — before this —
    zero record anywhere an operator or ``doctor`` could read. #5356's
    own ruling on the SAME "silent, persistent" shape
    (``hooks_layer_rejected``) applies here directly: a log line alone is
    invisible with the shipped config, and this failure is exactly the
    kind #5356 was written for — permanent, not one-shot (contrast the
    #5527 test-double-signature drift this issue was originally bundled
    with: THAT failure is caught immediately, on the very next dispatch
    attempt, by the callback's own surviving try/except, and is already
    a WARNING-level log line at the shipped default — see architect's own
    finding on this issue's thread for why the two get the OPPOSITE
    visibility treatment).

    ``emit_event`` is ``None``-tolerant (a caller/test double that hasn't
    wired an audit-emit sink degrades to a silent no-op here, same
    posture ``FsWatcher``'s own ``self._emit_event`` already has) —
    OBSERVATION is best-effort by design; it must never itself become a
    reason the drain task's real death is masked or delayed."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None or emit_event is None:
        return
    emit_event(
        "hook_drain_task_died", label=label,
        reason=f"{type(exc).__name__}: {exc}",
    )


__all__ = ["drain_folded"]
