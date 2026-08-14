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

- **Owned here**: ``_spawned_tasks`` (bounded, two-level
  ``dict[str, OrderedDict[str, str]]`` — ``agent_name -> (sid -> task)``,
  #4740: was a flat ``sid -> task`` OrderedDict until #4740 keyed it by
  agent too, since sid alone is not process-wide-unique), ``_spawn_order``
  (#4740: the companion ``OrderedDict[tuple[str, str], None]`` tracking
  GLOBAL insertion order across every agent, for ``_MAX_SPAWNED_TASKS``
  eviction — the nested dict above has no order of its own once split by
  agent), ``_vanish_scheduled`` (bool), ``_vanish_task`` (the detached
  teardown task's strong ref) — mutated ONLY by ``__init__`` +
  ``record_spawned_task`` + ``lookup_and_evict_spawned_task`` +
  ``_maybe_schedule_ephemeral_vanish`` (the four methods below), no other
  ``Session`` code path touches them (re-verified by grep at #4740 review,
  2026-08-14: grepped ``_spawned_tasks``, ``_spawn_order``,
  ``_vanish_scheduled``, and ``_vanish_task`` across ``src/`` and
  ``tests/`` separately — every hit outside this file is either a
  read-only test bridge (``._vanish_task`` awaited in 3 vanish tests) or a
  comment, never an assignment).
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
from typing import TYPE_CHECKING, Callable, Protocol

if TYPE_CHECKING:
    from reyn.runtime.tracked_tasks import TrackedTaskSet

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
        task_tracker: "TrackedTaskSet",
    ) -> None:
        self._registry = registry
        self._journal = journal
        self._chains = chains
        self._inbox = inbox
        self._agent_name = agent_name
        self._session_id_provider = session_id_provider
        self._ephemeral_provider = ephemeral_provider
        # #4759: the owning Session's single background-task funnel
        # (tracked_tasks.py) -- the vanish task below is real cleanup work
        # (it closes this session's own held MCP connections via
        # remove_session -> aclose_mcp_connections) that was previously
        # detached and invisible to AgentRegistry.shutdown()'s drain, the
        # #4759 root cause. REQUIRED (no None-tolerant fallback): the one
        # production construction site (session.py) always supplies one, and
        # an optional fallback here would silently recreate #4759's own
        # defect shape at a FUTURE construction site that forgets to pass
        # one -- exactly the class of hole this PR exists to close, not
        # reintroduce in its own new code.
        self._task_tracker = task_tracker
        # #4740: (agent_name, sid) -> trusted original-task record for spawned
        # sessions, so a compromised sub-session can't forge task framing
        # (#2103 S1bc-exec). Two-level dict, SAME shape as registry.py's own
        # `self._sessions.setdefault(name, {})[sid] = session` — sid alone is
        # NOT process-wide-unique (registry.py's own `_session_state_dir(self,
        # name, sid)` docstring: on-disk state dir is keyed by (name, sid)), so
        # this spawner recording two DIFFERENT agents' sessions that happen to
        # share a sid (both "main", the common default) would previously let
        # the second record silently overwrite the first, and later let an
        # UNRELATED agent's result consume and render the wrong trusted task=
        # in the result header — the exact forgery this record exists to
        # prevent, now reachable from an ordinary same-sid collision rather
        # than a compromised sub-session (#4735's own agent-collision defect
        # class, one level down: this is #4735's OWN "census remainder half"
        # that got folded into #4735 and lost its open face when #4735 closed
        # — lead-coder review, #4740).
        self._spawned_tasks: "dict[str, OrderedDict[str, str]]" = {}
        # #4740: GLOBAL (across every agent) insertion-order tracker for the
        # `_MAX_SPAWNED_TASKS` bound — the nested dict above has no single
        # order of its own once split by agent, so eviction needs a
        # companion structure to keep the SAME "oldest in-flight, across
        # every spawned agent" bound the prior flat OrderedDict enforced
        # (not narrowed to a PER-agent cap, which would let N agents each
        # hold their own 256-entry tail — an unintended widening of the
        # bound while fixing the collision).
        self._spawn_order: "OrderedDict[tuple[str, str], None]" = OrderedDict()
        # Spawned EPHEMERAL session auto-vanish state (#2103)
        self._vanish_scheduled: bool = False
        self._vanish_task: "asyncio.Task | None" = None

    # ── #2103 S1bc-exec: spawned-task correlation record (bounded) ──────────────

    def record_spawned_task(self, agent_name: str, sid: str, task: str) -> None:
        """Record a session-I-spawned's ``(agent_name, sid) -> task`` BEFORE submitting
        it, so when its result routes back the header renders ``task=<my OWN request>``
        from this TRUSTED record (not the spawned session's echo). Bounded: evicted on
        result arrival; ``_MAX_SPAWNED_TASKS`` cap evicts oldest (globally, across every
        spawned agent) so a never-arriving result can't grow it.

        #4740: keyed by ``(agent_name, sid)``, not ``sid`` alone — see this
        class's own constructor comment for why a bare sid collides across
        agents."""
        key = (agent_name, sid)
        self._spawned_tasks.setdefault(agent_name, OrderedDict())[sid] = task
        self._spawned_tasks[agent_name].move_to_end(sid)
        self._spawn_order[key] = None
        self._spawn_order.move_to_end(key)
        while len(self._spawn_order) > _MAX_SPAWNED_TASKS:
            oldest_agent, oldest_sid = next(iter(self._spawn_order))
            self._spawn_order.popitem(last=False)  # evict oldest in-flight
            inner = self._spawned_tasks.get(oldest_agent)
            if inner is not None:
                inner.pop(oldest_sid, None)
                if not inner:
                    del self._spawned_tasks[oldest_agent]

    def lookup_and_evict_spawned_task(
        self, agent_name: "str | None", sid: "str | None",
    ) -> "str | None":
        """The TRUSTED task for a spawned ``(agent_name, sid)``, or None (not one I
        spawned / already consumed). Evict-on-read — a result is consumed once; a
        spoofed/unknown (agent_name, sid) → None → the caller renders the safe
        from=-only fallback (still fenced, still kind=prompt — proposal 0067 P4
        (#3978): the sid/from distinction rides the header's OTHER field, not
        `kind`, since architect's 2026-08-10 ruling collapsed both branches to
        `kind=prompt`).

        #4740: agent_name is now REQUIRED alongside sid — see
        :meth:`record_spawned_task`'s own docstring for why."""
        if not sid or not agent_name:
            return None
        inner = self._spawned_tasks.get(agent_name)
        if inner is None:
            return None
        task = inner.pop(sid, None)
        if task is not None:
            self._spawn_order.pop((agent_name, sid), None)
            if not inner:
                del self._spawned_tasks[agent_name]
        return task

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
        # #4759: routed through the owning Session's task funnel
        # (tracked_tasks.py), disposition="await" -- this task performs the
        # actual teardown work (remove_session closes this session's held
        # MCP connections among other things), so it must be allowed to run
        # to completion, not cancelled. self._vanish_task is ALSO kept below
        # (unconditionally, for the 3 existing tests that await it directly)
        # -- before #4759 it was a bare asyncio.create_task with ONLY that
        # ref, invisible to AgentRegistry.shutdown()'s own drain, which is
        # the root cause #4759 traced: a normal shutdown could return while
        # this was still mid-flight, orphaning whatever OS subprocess
        # remove_session -> aclose_mcp_connections was about to close.
        coro = self._registry.remove_session(self._agent_name, self._session_id_provider())
        self._vanish_task = self._task_tracker.spawn(
            coro, disposition="await", name="ephemeral-vanish",
        )
