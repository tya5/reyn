"""SpawnTracker — the per-session spawn/vanish lifecycle subsystem (#3133 P3,
extracted from ``Session`` via Extract Class, same Protocol-inject pattern as
``CapabilityVisibility`` at #3129/#3121 step3).

``Session`` historically owned three fields plus the four methods that
read/write them: the trusted ``sid -> task`` record for sessions THIS session
spawned (#2103 S1bc-exec), and the ephemeral auto-vanish scheduling state
(#2103). This module extracts that cohesive field+method cluster into an
INDEPENDENT class that OWNS the state — ``Session`` holds exactly one
reference (``self._spawn_tracker``) and delegates via thin forwarders; it
does not construct a bundle and unpack it back into its own fields (the
#3082 Fowler anti-pattern this extraction is designed to avoid).

Ownership split:

- **Owned here**: ``_spawned_tasks`` (bounded ``sid -> task`` OrderedDict),
  ``_vanish_scheduled`` (bool), ``_vanish_task`` (the detached teardown
  task's strong ref) — mutated ONLY by the four methods below, no other
  ``Session`` code path touches them (verified by grep).
- **Injected dependency (constructor)**: ``registry`` / ``journal`` /
  ``chains`` / ``inbox`` — stable for the session's lifetime, read but never
  reassigned here. ``agent_name`` is likewise stable (``Agent`` is frozen,
  same stability class as ``CapabilityVisibility.agent_name``).
- **Injected dependency (live provider, constructor)**: ``session_id_provider``
  and ``ephemeral_provider`` are zero-arg callables reading
  ``Session._session_id`` / ``Session._ephemeral`` LIVE — both are
  Session-owned state REASSIGNED post-construction by the owning
  ``AgentRegistry`` (``session_id``: spawn-time re-key, ``registry.py``
  ``spawn_session_recorded``; ``ephemeral``: ``registry.py``
  ``spawn_session`` / ``pipeline_executor_driver.py`` set it True AFTER
  ``Session.__init__`` returns), so a snapshot copied once at construction
  would go stale and silently use the WRONG value (the same staleness hazard
  ``CapabilityVisibility`` documents for its ``session_id_provider`` /
  ``available_skills_provider``) — this class reads through a live getter
  rather than owning a second, staleable copy. ★ Ground correction vs the
  #3133 P3 firm comment (which specified plain ``session_id: str`` /
  ``ephemeral: bool``): both fields are mutated by external assignment
  (``session._session_id = ...`` / ``session._ephemeral = True``) after
  construction, so a plain constructor value would freeze the pre-mutation
  value forever — the same pattern #3129 already solved with a provider.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Callable, Protocol

_MAX_SPAWNED_TASKS = 256


class _Registry(Protocol):
    """The one method this class needs from its ``registry`` dep (the
    ``AgentRegistry``): the single ephemeral-teardown seam also used by the
    rewind as-of-cut drop. A Protocol keeps ``SpawnTracker`` decoupled from
    the concrete registry type (no import of a ``Session`` sibling)."""

    async def remove_session(self, agent_name: str, sid: str) -> bool: ...


class _Journal(Protocol):
    """The one method this class needs from its ``journal`` dep (the
    session's recovery journal): attach the shared per-checkpoint anchor
    store so ``cut_generation`` records against the registry's boundary."""

    def set_anchor_store(self, anchor_store: object) -> None: ...


class _Chains(Protocol):
    """The one method this class needs from its ``chains`` dep (the
    ``ChainManager``): whether a delegation chain is still pending an
    ``agent_response`` — the awaited-work guard for auto-vanish."""

    def all_chain_ids(self) -> "list[str]": ...


class _Inbox(Protocol):
    """The one method this class needs from the session's inbox queue:
    whether the drain queue is empty (auto-vanish precondition)."""

    def empty(self) -> bool: ...


class SpawnTracker:
    """Owns the per-session spawn/vanish lifecycle state (#2103 S1bc-exec /
    #2103 auto-vanish) — the trusted spawned-task correlation record + the
    ephemeral session auto-vanish scheduling."""

    def __init__(
        self,
        *,
        registry: "_Registry | None",
        journal: "_Journal",
        chains: "_Chains",
        inbox: "_Inbox",
        agent_name: str,
        session_id_provider: "Callable[[], str]",
        ephemeral_provider: "Callable[[], bool]",
    ) -> None:
        self._registry = registry
        self._journal = journal
        self._chains = chains
        self._inbox = inbox
        self._agent_name = agent_name
        self._session_id_provider = session_id_provider
        self._ephemeral_provider = ephemeral_provider
        # sid -> trusted original-task record for spawned sessions, so a compromised
        # sub-session can't forge task framing (#2103 S1bc-exec)
        self._spawned_tasks: "OrderedDict[str, str]" = OrderedDict()
        # Spawned EPHEMERAL session auto-vanish state (#2103)
        self._vanish_scheduled: bool = False
        self._vanish_task: "asyncio.Task | None" = None

    # ── #2103 S1bc-exec: spawned-task correlation record (bounded) ──────────────

    def record_spawned_task(self, sid: str, task: str) -> None:
        """Record a session-I-spawned's ``sid -> task`` BEFORE submitting it, so when its
        result routes back the header renders ``task=<my OWN request>`` from this TRUSTED
        record (not the spawned session's echo). Bounded: evicted on result arrival;
        ``_MAX_SPAWNED_TASKS`` cap evicts oldest so a never-arriving result can't grow it."""
        self._spawned_tasks[sid] = task
        self._spawned_tasks.move_to_end(sid)
        while len(self._spawned_tasks) > _MAX_SPAWNED_TASKS:
            self._spawned_tasks.popitem(last=False)  # evict oldest in-flight

    def lookup_and_evict_spawned_task(self, sid: "str | None") -> "str | None":
        """The TRUSTED task for a spawned ``sid``, or None (not one I spawned / already
        consumed). Evict-on-read — a result is consumed once; a spoofed/unknown sid → None
        → the caller renders the safe ``kind=agent`` fallback (still fenced)."""
        if not sid:
            return None
        return self._spawned_tasks.pop(sid, None)

    def attach_anchor_store(self, anchor_store: object) -> None:
        """Attach the shared per-checkpoint anchor store (#1547).

        The registry injects its single ``AnchorStore`` so the journal's
        ``cut_generation`` records the rewind-timeline preview text against the
        same boundary seq the registry's ``list_rewind_points`` surfaces.
        """
        self._journal.set_anchor_store(anchor_store)

    def _maybe_schedule_ephemeral_vanish(self) -> None:
        """#2103: an ephemeral spawned session auto-vanishes once its work is done —
        the turn completed and no further trigger is queued (the inbox is drained, so
        the run-loop is about to idle-block). Schedules a DETACHED teardown via the
        registry's ``remove_session`` seam (the SAME teardown the rewind as-of-cut drop
        uses): it cancels this idle run-loop, drops the session, emits
        ``session_vanished``, and purges the dir. Detached (not awaited here) because
        ``remove_session`` cancels THIS run-loop task — running it inline would cancel
        the caller. Idempotent (the ``_vanish_scheduled`` guard). The main session +
        persistent spawns are never ``_ephemeral`` -> unaffected.

        "Work done" = the inbox is drained AND there is no AWAITED work whose resume
        arrives OUTSIDE the now-empty inbox: a pending delegation chain (an
        ``agent_response`` is still coming — ``self._chains``). Without this guard a
        spawned ephemeral session that DELEGATES + awaits a response has a
        transiently-empty inbox mid-await -> it would vanish (dir purged +
        ``session_vanished``) before the response lands = silent + destructive. A
        spawned session CAN reach delegate + await (it has the full ChainManager +
        send_to_agent wiring), so the guard is load-bearing, not theoretical.
        """
        if (not self._ephemeral_provider() or self._vanish_scheduled
                or self._registry is None or not self._inbox.empty()):
            return
        # awaited-work guard (delegate-then-await): the resume arrives outside the
        # now-empty inbox, so emptiness alone is not "done".
        if self._chains.all_chain_ids():
            return
        self._vanish_scheduled = True
        # Keep a strong ref (self._vanish_task) so the task is not GC'd before it runs
        # (it self-cancels this run-loop, so it is otherwise unreferenced).
        self._vanish_task = asyncio.create_task(
            self._registry.remove_session(self._agent_name, self._session_id_provider())
        )
