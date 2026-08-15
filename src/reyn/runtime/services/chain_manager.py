"""ChainManager — owns pending_chains lifecycle (extracted from Session wave 1B).

Manages: register / update / resolve / timeout + asyncio timer arm/cancel.
Persistent fields go through SnapshotJournal (via _JournalLike protocol).

Design notes
------------
- _PendingChain is moved here (session.py retains a duplicate until wave 2).
- ChainManager references SnapshotJournal only through the _JournalLike
  protocol so this module can be developed/tested without a concrete
  SnapshotJournal instance.
- max_hop_depth is stored for callers to inspect; depth-exceeded detection
  is the caller's responsibility (P7 — no domain-specific logic here).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol, runtime_checkable

from reyn.runtime.task_types import Requester, RunStatus

if TYPE_CHECKING:
    from reyn.core.events.agent_snapshot import AgentSnapshot
    from reyn.runtime.task_types import Requester
    from reyn.runtime.tracked_tasks import TrackedTaskSet

logger = logging.getLogger(__name__)


# ── _PendingChain ─────────────────────────────────────────────────────────────


@dataclass
class _PendingChain:
    """Multi-hop relay state held in a delegating agent — and, since proposal
    0067's settle path (#3978), also a task's collection handle
    (``pending_chains`` repurposed as the settle-path substrate, per the
    proposal's own P6 note).

    Created when an agent receives an ``agent_request`` and decides to
    further delegate (router emits ``messages_to_agents``). The reply to
    the upstream ``requester`` is held back until every entry in
    ``waiting_on`` has returned an ``agent_response`` for this chain_id.
    On the final response, the agent re-runs its router so the LLM can
    compose a synthesized answer with all delegate replies in history,
    then sends that answer to ``requester`` at ``origin_depth``.

    Note: session.py retains an identical copy until wave 2 imports this.
    """

    chain_id: str
    # proposal 0067 P4e (#3978), architect ruling 2026-08-10: ADR-0040 D6's
    # ``Requester(agent_name, session_id)`` stored as ONE nested field, not
    # two flat ones — "the pair is the address, not either half alone"
    # (``Requester``'s own docstring), and #2130 already lived the flat-2-key
    # failure mode once (a missing ``origin_sid`` silently misdelivered to
    # ``main``). A flat pair structurally permits writing one half without
    # the other; a nested value does not. Was ``origin_agent``/``origin_sid``
    # (two flat fields) plus a `.requester` DERIVED property (P4, #3978) —
    # this rename makes the property redundant by making the field itself
    # BE what the property used to compute. See ``register()``'s docstring
    # for the WAL-persisted shape.
    requester: Requester
    origin_depth: int
    original_request: str
    waiting_on: set[str] = field(default_factory=set)
    # proposal 0067 P4 (#3978): the task kind (prompt/pipeline/exec) this
    # handle represents — None for a delegate-relay chain, which stays
    # OUTSIDE the task vocabulary permanently. NOT because of its
    # |waiting_on| cardinality (architect ruling, #3978: |waiting_on| == 1
    # is a prompt task, |waiting_on| >= 2 is a join — a permanently
    # non-task shape by ITS OWN cardinality rule, independent of producer)
    # — a relay chain stays kind=None because P6 retired delegate_to_agent
    # with no fold, so no relay chain, single-waiter or not, ever receives
    # a kind through this arc. Already-registered/restored relay chains
    # keep kind=None for their remaining lifetime; no new ones form.
    # ``describe_task``/``list_tasks`` read this; a handle registered with
    # no ``kind`` is not describable as a typed task.
    kind: "str | None" = None
    # proposal 0067 P4 (#3978): describe_task's status field (architect
    # ruling, 2026-08-10) — typed RunStatus, not a bare string, so a LATER
    # value (INPUT_REQUIRED, once the ask_user bridge lands — a separate
    # step) is a value addition, not a caller-side type change. VOLATILE —
    # deliberately NOT threaded through register()'s persisted `fields`
    # dict, so it is never written to the journal/WAL. A crash-recovered
    # handle has no way to truthfully know it was mid-ask_user, so it
    # defaults back to RUNNING on restore() rather than persist a stale
    # claim.
    status: "RunStatus" = field(default=RunStatus.RUNNING, compare=False, repr=False)
    # proposal 0067 P4 (#3978), architect ruling 2026-08-10: the task's
    # cancel hook — cooperative, argument-zero (mirrors Session.
    # cancel_inflight / *Driver.request_cancel's own zero-arg shape; see
    # pipeline_executor_driver.PipelineExecutorDriver.request_cancel).
    # VOLATILE, held on THIS SAME dataclass rather than a second dict
    # (architect's ruling, #3978 issue comments, 2026-08-10 — lead-coder's
    # own contribution was narrower: that settle() clearing two stores in
    # the SAME function is correct; collapsing to ONE store so the hole
    # cannot exist structurally, rather than being closed by discipline,
    # was architect's call). `None` after a crash-recovered
    # restore (the live callable belonged to the dead process) — a caller
    # (cancel_task) that finds `cancel is None` MUST NOT report success:
    # nothing is actually listening.
    cancel: "Callable[[], None] | None" = field(default=None, compare=False, repr=False)
    # proposal 0067 P8 (#3978): the watchdog's absolute wall-clock deadline
    # (aware UTC datetime) — persisted so a crash+restart RECOVERS the
    # original deadline instead of extending it. Before P8, `restore()`
    # called `arm_timeout()` exactly like a fresh arm, sleeping the FULL
    # `chain_timeout_seconds` again regardless of how much of that window
    # had already elapsed pre-crash — a crash near a chain's deadline could
    # push it arbitrarily far out, silently. `None` for a chain that has
    # never been armed (timeouts disabled, or armed before this field
    # existed — pre-P8 WAL entries have no `arm_at` key; `restore()` falls
    # back to a fresh full-duration arm for those, matching the pre-P8
    # behavior exactly rather than guessing a deadline that was never
    # recorded).
    arm_at: "datetime | None" = None


# ── Journal protocol ──────────────────────────────────────────────────────────


@runtime_checkable
class _JournalLike(Protocol):
    """Subset of SnapshotJournal that ChainManager needs.

    Using a Protocol keeps ChainManager decoupled from the concrete
    SnapshotJournal class so tests can pass mocks freely.
    """

    @property
    def snapshot(self) -> "AgentSnapshot": ...

    async def record_chain_register(self, *, chain_id: str, fields: dict) -> None: ...

    async def record_chain_update(self, *, chain_id: str, fields: dict) -> None: ...

    async def record_chain_resolve(self, *, chain_id: str) -> None: ...

    async def record_chain_timeout_fired(self, *, chain_id: str) -> None: ...


# ── ChainManager ──────────────────────────────────────────────────────────────


class ChainManager:
    """Owns pending_chains lifecycle: register / update / resolve / timeout.

    Parameters
    ----------
    journal:
        Journal instance that ChainManager uses for WAL persistence.
        Must satisfy the _JournalLike protocol.
    events:
        EventLog (or compatible emitter) for observability.
    chain_timeout_seconds:
        How long to wait before firing a chain timeout.
        Values <= 0 disable timeouts entirely.
    max_hop_depth:
        Maximum allowed hop depth.  ChainManager stores this for callers;
        depth enforcement is the caller's responsibility.
    clock_fn:
        proposal 0067 P8 (#3978): callable returning an aware UTC
        ``datetime`` — injectable for tests (mirrors
        ``cron.scheduler.CronScheduler``'s identical seam: production omits
        it and gets ``datetime.now(timezone.utc)``).
    sleep_fn:
        proposal 0067 P8 (#3978, owner design): callable with
        ``asyncio.sleep``'s signature — injectable for tests, next to
        ``clock_fn``. What P8 actually changed is WHICH ``duration_seconds``
        ``_chain_timeout_watch`` sleeps for (the full window on a fresh
        arm, the REMAINING time on a restore with a persisted deadline, a
        fresh window again on a legacy restore with none) — the decision,
        not the sleep's own completion. A test asserting the decision
        needs to observe the ``duration_seconds`` a call was made with,
        not wait for a real (or even short) sleep to finish: CLAUDE.md's
        testing policy bans a wait-budget constant AND a straight-line
        ``sleep(N)`` as the thing that makes an assertion pass, and a
        bounded "fires within N seconds" proxy can only distinguish
        magnitudes coarser than N (60s vs 0.05s, never 0.05s vs 0.03s).
        Production omits it and gets real ``asyncio.sleep``.
    """

    def __init__(
        self,
        *,
        journal: "_JournalLike",
        events: Any,
        chain_timeout_seconds: float,
        max_hop_depth: int,
        clock_fn: "Callable[[], datetime] | None" = None,
        sleep_fn: "Callable[[float], Awaitable[None]] | None" = None,
        task_tracker: "TrackedTaskSet | None" = None,
    ) -> None:
        self._journal = journal
        self._events = events
        self._chain_timeout_seconds = chain_timeout_seconds
        self.max_hop_depth = max_hop_depth
        self._clock = clock_fn or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep_fn or asyncio.sleep
        # #4759: every watchdog task ALSO registers with the owning
        # Session's single task funnel (tracked_tasks.py) so
        # AgentRegistry.shutdown() can reach it without needing to know this
        # class exists.
        #
        # DELIBERATELY left None-tolerant (unlike SpawnTracker/OutboxHub,
        # whose #4759 fix made task_tracker a REQUIRED param): measured, the
        # ONE production construction site (session.py) always passes one,
        # so this class's own #4759 reachability is not at risk in
        # production either way. What's different here is the cost side —
        # ChainManager has 12 existing test call sites across 9 files, NONE
        # of which exercise #4759's teardown property (they test chain
        # register/resolve/timeout semantics); forcing all 12 to thread a
        # tracker through would touch files unrelated to this PR's own
        # concern for zero behavioural gain in THOSE tests. A caller that
        # doesn't pass one (every existing test) gets pre-#4759 behaviour
        # for its own watchdog tasks unchanged — this class's core function
        # (arm/cancel/fire a chain timeout) does not depend on the tracker
        # at all; only #4759's reachability property does, and that
        # property is not what these tests are checking. The dedicated
        # `_timers` dict below (chain_id-keyed lookup, needed by
        # `cancel_timeout`) stays the primary bookkeeping either way; this is
        # an ADDITIONAL registration for teardown-reachability, not a
        # replacement for it.
        self._task_tracker = task_tracker

        self._chains: dict[str, _PendingChain] = {}
        self._timers: dict[str, asyncio.Task] = {}

    # ── state queries ─────────────────────────────────────────────────────

    def has(self, chain_id: str) -> bool:
        """Return True if ``chain_id`` is currently pending."""
        return chain_id in self._chains

    def get(self, chain_id: str) -> _PendingChain | None:
        """Return the _PendingChain for ``chain_id``, or None."""
        return self._chains.get(chain_id)

    def all_chain_ids(self) -> list[str]:
        """Return a list of all currently pending chain IDs."""
        return list(self._chains.keys())

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def register(
        self,
        *,
        chain_id: str,
        depth: int,
        original_text: str,
        sender: str | None,
        waiting_on: set[str] | None = None,
        requester: "Requester | None" = None,
        origin_depth: int = 0,
        kind: "str | None" = None,
        cancel: "Callable[[], None] | None" = None,
    ) -> _PendingChain:
        """Register a new pending chain and persist it via the journal.

        Parameters
        ----------
        chain_id:
            Unique identifier for the chain.
        depth:
            Current hop depth (used as ``origin_depth`` when not specified).
        original_text:
            The original request text.
        sender:
            Agent name that sent the request, or None for user-initiated
            chains. Also the fallback for ``requester.agent_name`` when
            ``requester`` is not passed explicitly.
        waiting_on:
            Set of agent names this chain is waiting for.
        requester:
            proposal 0067 P4e (#3978), architect ruling 2026-08-10: ADR-0040
            D6's ``Requester(agent_name, session_id)`` — the address to
            reply to when the chain resolves, stored as ONE value (see
            ``_PendingChain.requester``'s own docstring for why a flat
            2-field pair was rejected). Defaults to
            ``Requester(sender or "", "main")`` when omitted, mirroring the
            pre-materialization defaulting (``origin_agent or sender or
            ""`` / an implicit ``origin_sid`` of ``None`` → ``"main"``) —
            every production call site passes this explicitly today; the
            default exists for callers (and tests) that don't need a
            specific reply target.
        origin_depth:
            The depth at which to send the reply upstream.
        kind:
            proposal 0067 P4 (#3978): the task kind (``"prompt"`` /
            ``"pipeline"`` / ``"exec"``) this handle represents, or
            ``None`` for a delegate-relay chain — permanently outside the
            task vocabulary (architect ruling, #3978: P6 retired
            delegate_to_agent, the sole producer, with no fold; nothing
            assigns this field for that flow, and nothing ever will).
        cancel:
            proposal 0067 P4 (#3978): the task's cooperative cancel hook
            (argument-zero), or ``None`` if this task cannot be cancelled
            (e.g. a legacy delegate-relay chain). VOLATILE — never
            persisted (see ``_PendingChain.cancel``'s own docstring).
        """
        resolved_requester = requester or Requester(
            agent_name=sender or "", session_id="main"
        )
        chain = _PendingChain(
            chain_id=chain_id,
            requester=resolved_requester,
            origin_depth=origin_depth or depth,
            original_request=original_text,
            waiting_on=set(waiting_on or []),
            kind=kind,
            cancel=cancel,
        )
        self._chains[chain_id] = chain

        fields: dict[str, Any] = {
            # proposal 0067 P4e (#3978): persisted as ONE nested value, not
            # two flat keys — #4110's field-name-independent WAL/snapshot
            # mirror makes nesting free (any JSON-serializable value passes
            # through unchanged); a flat pair would let a future caller omit
            # one half without a construction-time error, the exact shape
            # #2130 already misdelivered on once (a missing origin_sid
            # silently routed to "main").
            "requester": {
                "agent_name": chain.requester.agent_name,
                "session_id": chain.requester.session_id,
            },
            "origin_depth": chain.origin_depth,
            "original_request": chain.original_request,
            "waiting_on": sorted(chain.waiting_on),
            # Persisted key is "task_kind", not "kind" — SnapshotJournal's
            # own _wal_append_nowait(kind: str, **fields) takes "kind" as
            # the WAL EVENT type positional (e.g. "chain_register"); a
            # "kind" entry inside **fields collides with it.
            "task_kind": chain.kind,  # #3978 P4
        }
        await self._journal.record_chain_register(chain_id=chain_id, fields=fields)
        return chain

    async def update(self, chain_id: str, **fields: Any) -> None:
        """Update fields on a pending chain and persist via the journal.

        Only fields present in ``**fields`` are mutated.  The special key
        ``waiting_on`` expects a collection; it is coerced to a ``set``
        in-memory and a sorted list for the journal.
        """
        chain = self._chains.get(chain_id)
        if chain is None:
            return

        journal_fields: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "waiting_on":
                chain.waiting_on = set(value)
                journal_fields["waiting_on"] = sorted(chain.waiting_on)
            elif hasattr(chain, key):
                setattr(chain, key, value)
                journal_fields[key] = value

        if journal_fields:
            await self._journal.record_chain_update(
                chain_id=chain_id, fields=journal_fields
            )

    async def resolve(self, chain_id: str) -> _PendingChain | None:
        """Remove and return the pending chain, persist resolve, cancel timer.

        Returns the resolved _PendingChain, or None if not found.
        """
        chain = self._chains.pop(chain_id, None)
        self.cancel_timeout(chain_id)
        await self._journal.record_chain_resolve(chain_id=chain_id)
        return chain

    async def settle(
        self,
        chain_id: str,
        *,
        on_settle: str,
        deliver: "Callable[[], Awaitable[None]]",
        launch_pipeline: "Callable[[str], Awaitable[None]] | None" = None,
    ) -> _PendingChain | None:
        """Execute a task's settle disposition, then pop its handle — same
        function, mirroring ``resolve()``'s pop+cancel_timeout+journal shape
        (ADR-0040 D4: "push-at-settle with immediate deletion", "no
        retention, no clock"; the settle-path acceptance condition is
        exactly this — ONE settle function, no ``pipeline``/``run_id`` in
        its signature). delegate_to_agent's own chain-resolve completion
        never folds in here — architect ruling, #3978: P6 retired the tool
        with no replacement producer, so its (already permanently
        non-task, ``kind=None``) chains stay on ``resolve()``, not
        ``settle()``.

        ``on_settle`` (proposal 0067 § Issuing / ADR-0040 D4①):
          - ``"deliver"`` — await ``deliver()`` (the caller's own delivery
            callback — e.g. posting a ``pipeline_result``/``agent_response``
            inbox message; this method has no opinion on what "deliver"
            means for a given task kind).
          - ``"drop"`` — no-op; the disposition is intentionally discarded.
          - anything else — a pipeline NAME to launch via
            ``launch_pipeline`` (D4: "filters a large result before it
            reaches the issuer's context"). Scope of the actual launch is
            not yet decided (proposal 0067 P4/P7) — a caller that doesn't
            pass ``launch_pipeline`` gets ``NotImplementedError`` rather
            than a silent no-op, so an unimplemented disposition fails
            loud, not quiet.

        Tolerates a missing handle exactly like ``resolve()`` does (``pop``
        with a ``None`` default, no error) — the disposition still
        executes; only the bookkeeping (pop/cancel_timeout/journal) is a
        no-op when nothing was registered for ``chain_id`` (e.g. a run
        launched before this mechanism existed, mid-recovery from an older
        on-disk work-order)."""
        if on_settle == "drop":
            pass
        elif on_settle == "deliver":
            await deliver()
        else:
            if launch_pipeline is None:
                raise NotImplementedError(
                    f"ChainManager.settle(on_settle={on_settle!r}): pipeline-name "
                    "dispositions are not yet implemented (proposal 0067 P4/P7)"
                )
            await launch_pipeline(on_settle)
        chain = self._chains.pop(chain_id, None)
        self.cancel_timeout(chain_id)
        await self._journal.record_chain_resolve(chain_id=chain_id)
        return chain

    def find_chain(self, chain_id: str) -> _PendingChain | None:
        """Return the in-memory _PendingChain for ``chain_id``, or None.

        Read-only public API for cross-agent chain lookup (R-D14): the
        AgentRegistry's ``notify_chain_discarded`` scans every session's
        ChainManager via this method to find the upstream waiter for a
        chain whose downstream run was discarded.

        Distinct from ``resolve``: this does NOT mutate state nor emit
        WAL events — it just answers "do you track this chain_id?".
        """
        return self._chains.get(chain_id)

    async def fire_timeout(self, chain_id: str) -> _PendingChain | None:
        """Remove and return a timed-out pending chain, persist timeout event.

        Returns the resolved _PendingChain, or None if not found.
        """
        chain = self._chains.pop(chain_id, None)
        self._timers.pop(chain_id, None)
        await self._journal.record_chain_timeout_fired(chain_id=chain_id)
        return chain

    # ── timeout watchdog ──────────────────────────────────────────────────

    async def arm_timeout(
        self,
        chain_id: str,
        *,
        on_fire: Callable[[str], Awaitable[None]],
    ) -> None:
        """Start a FRESH watchdog task for ``chain_id`` — full
        ``chain_timeout_seconds`` from now.

        No-op when timeouts are disabled (chain_timeout_seconds <= 0).
        Idempotent — replaces any existing timer for the same chain_id
        by cancelling it first.

        proposal 0067 P8 (#3978): computes and PERSISTS ``arm_at`` (the
        absolute wall-clock deadline this arm targets) via ``update()``
        before scheduling the watchdog — async for this reason (was sync
        pre-P8; all 4 call sites already run inside an ``async def`` with
        nearby ``await``s). This is the FRESH-arm path: a chain being
        armed for the first time, or re-armed with a full new window
        (continuation / FP-0005 extension) — NOT the recovery path, which
        must schedule against the ALREADY-persisted deadline instead of
        computing a new one (see ``restore()``).
        """
        if self._chain_timeout_seconds <= 0:
            return
        arm_at = self._clock() + timedelta(seconds=self._chain_timeout_seconds)
        chain = self._chains.get(chain_id)
        if chain is not None:
            chain.arm_at = arm_at
            await self._journal.record_chain_update(
                chain_id=chain_id, fields={"arm_at": arm_at.isoformat()}
            )
        self._start_watchdog(
            chain_id, on_fire=on_fire, duration_seconds=self._chain_timeout_seconds
        )

    def _start_watchdog(
        self,
        chain_id: str,
        *,
        on_fire: Callable[[str], Awaitable[None]],
        duration_seconds: float,
    ) -> None:
        """Schedule the watchdog task for ``chain_id`` — no persistence,
        no ``arm_at`` computation, just the ``asyncio.Task`` bookkeeping
        ``arm_timeout()`` and ``restore()`` both need. Idempotent —
        replaces any existing timer for the same chain_id by cancelling
        it first."""
        existing = self._timers.pop(chain_id, None)
        if existing is not None and not existing.done():
            existing.cancel()
        coro = self._chain_timeout_watch(
            chain_id, on_fire=on_fire, duration_seconds=duration_seconds
        )
        # #4759: ALSO registered with the owning Session's task funnel
        # (tracked_tasks.py), disposition="cancel_join", appends_wal=True
        # -- this dict stays the primary chain_id-keyed lookup
        # `cancel_timeout` needs; the tracker registration is what makes
        # this watchdog reachable from AgentRegistry.shutdown() without it
        # needing to know ChainManager exists. appends_wal=True (NOT the
        # default False): firing appends "chain_timeout_fired" to the WAL,
        # and a chain-timeout watchdog is EXACTLY the class of task the
        # pre-#4759 `await_quiescent` always cancelled mid-rewind via its
        # own `cancel_and_join_timers()` call -- drop-safe, re-armed by
        # `restore()` from the recovered snapshot. Cancelling the same task
        # from both `cancel_and_join_timers` and the tracker's own
        # aclose() is safe -- Task.cancel() is idempotent.
        if self._task_tracker is not None:
            self._timers[chain_id] = self._task_tracker.spawn(
                coro, disposition="cancel_join", appends_wal=True,
                name=f"chain-timeout-{chain_id}",
            )
        else:
            # #4765 co-vet (architect): a caller that constructs a
            # ChainManager without a task_tracker (deliberately kept
            # optional -- see __init__'s own #4759 comment for the
            # measured cost/benefit) silently falls back to an untracked
            # task here, same as before this funnel existed. Warned (not
            # silent) so a future caller who SHOULD have passed one but
            # forgot has a signal to find, without forcing every existing
            # test call site (12, across 9 files) to thread one through.
            logger.warning(
                "ChainManager._start_watchdog: no task_tracker configured -- "
                "chain-timeout watchdog for %r is NOT reachable from "
                "AgentRegistry.shutdown()'s drain (see tracked_tasks.py).",
                chain_id,
            )
            self._timers[chain_id] = asyncio.create_task(coro)

    def cancel_timeout(self, chain_id: str) -> None:
        """Cancel the watchdog task for ``chain_id``, if any."""
        timer = self._timers.pop(chain_id, None)
        if timer is not None and not timer.done():
            timer.cancel()

    async def cancel_and_join_timers(self) -> None:
        """Cancel all timeout watchdogs and wait for them to settle.

        Idempotent. After this returns no watchdog can fire (no
        ``chain_timeout_fired`` append), and any callback already in-progress
        has settled. The chain state itself is untouched — ADR-0038 Stage 1c
        rewind quiescence uses this so no timeout append lands past the reset
        seq; ``restore()`` re-arms watchdogs from the recovered snapshot —
        against the REMAINING time on each chain's persisted ``arm_at``
        deadline where one exists (proposal 0067 P8, #3978), a fresh
        window only for a chain with no persisted deadline to recover.
        """
        for task in list(self._timers.values()):
            if not task.done():
                task.cancel()
        if self._timers:
            await asyncio.gather(
                *self._timers.values(), return_exceptions=True
            )
        self._timers.clear()

    async def shutdown(self) -> None:
        """Cancel all timeout watchdogs and wait for them to settle.

        Idempotent — safe to call from session drain on shutdown. Alias of
        ``cancel_and_join_timers`` (teardown-named seam for shutdown callers).
        """
        await self.cancel_and_join_timers()

    async def reset(self) -> None:
        """Drop all chain state + watchdogs (ADR-0038 Stage 1c-2 rewind).

        Cancels+joins every timeout watchdog and clears the pending-chain map.
        After this the manager holds no chains and no armed timers — the global
        rewind path re-populates via ``restore()`` from the reconstructed
        snapshot, leaving no pre-rewind residue. Idempotent.
        """
        await self.cancel_and_join_timers()
        self._chains.clear()

    async def _chain_timeout_watch(
        self,
        chain_id: str,
        *,
        on_fire: Callable[[str], Awaitable[None]],
        duration_seconds: float,
    ) -> None:
        """Internal watchdog coroutine.

        Sleeps for ``duration_seconds``; if the chain is still pending
        on wake, fires the timeout by calling ``on_fire(chain_id)``.

        proposal 0067 P8 (#3978): ``duration_seconds`` is a PARAMETER, not
        always ``self._chain_timeout_seconds`` — a fresh arm passes the
        full window, but ``restore()`` passes whatever's LEFT of an
        already-persisted ``arm_at`` deadline (possibly 0, if the deadline
        already passed while the process was down — ``asyncio.sleep(0)``
        yields once then fires immediately, so a past-due restored chain
        times out on its very next scheduler tick rather than getting a
        fresh full window).

        Cancellation (normal resolve path) raises CancelledError out of the
        sleep — this coroutine just exits cleanly.  Shutdown() gathers these
        tasks with ``return_exceptions=True`` so a late-fire during teardown
        is harmless.
        """
        try:
            await self._sleep(duration_seconds)
        except asyncio.CancelledError:
            return
        # Chain may have been resolved between sleep wake and pop.
        if chain_id not in self._chains:
            self._timers.pop(chain_id, None)
            return
        try:
            await on_fire(chain_id)
        except Exception:
            logger.exception("chain timeout on_fire callback raised for %s", chain_id)

    # ── restore from snapshot ─────────────────────────────────────────────

    def restore(
        self,
        *,
        on_fire: Callable[[str], Awaitable[None]],
    ) -> None:
        """Re-populate chains from journal.snapshot.pending_chains.

        Reconstructs each _PendingChain and arms its timeout watchdog.
        Call this after the journal has installed a recovered snapshot.

        Stays SYNC deliberately (unlike ``arm_timeout()``, made async by
        P8 to persist ``arm_at``): this method's own caller,
        ``Session.restore_state()``, is reached from
        ``AgentRegistry.get_or_load()`` — a synchronous method with its
        own wide, largely-synchronous caller graph (11 call sites across
        ``session.py``/``mcp/server.py``/``pipeline_executor_driver.py``/
        CLI commands). Making ``restore()`` async here would cascade into
        making ``get_or_load()`` async, a change with a blast radius far
        outside P8's own scope — not attempted without an explicit design
        decision (none was needed: the fix below needs no `await` at all).

        proposal 0067 P8 (#3978): a chain with a persisted ``arm_at``
        (present on every chain armed post-P8) schedules its watchdog for
        WHATEVER REMAINS of that deadline — ``max(0.0, arm_at - now)`` —
        via the low-level ``_start_watchdog()`` (no persistence, no new
        ``arm_at`` — the already-correct one from the snapshot is kept
        as-is). This is the fix P8 exists for: pre-P8, ``restore()``
        called ``arm_timeout()`` exactly like a brand-new arm, so a crash
        near a chain's deadline silently pushed it out by up to a full
        ``chain_timeout_seconds`` window every time the process
        restarted. A chain already past its deadline when restored
        (``remaining <= 0``) still gets a watchdog —
        ``asyncio.sleep(0)`` yields once then fires on the very next
        tick, rather than either firing synchronously inside restore() (a
        crash-recovery path has no business blocking on an on_fire
        callback) or silently dropping the now-overdue timeout.

        A chain with NO persisted ``arm_at`` (pre-P8 WAL entry, or armed
        while timeouts were disabled) falls back to a fresh full-window
        watchdog via ``_start_watchdog()`` directly — the exact pre-P8
        behavior for that one case, unchanged (no deadline was ever
        recorded to recover, and this path deliberately does NOT call
        the now-async ``arm_timeout()``, precisely so ``restore()`` never
        needs an ``await``).
        """
        for cid, chain_dict in self._journal.snapshot.pending_chains.items():
            # proposal 0067 P4e (#3978): the persisted "requester" key is a
            # nested {"agent_name", "session_id"} value (see register()'s
            # own docstring) — reconstructed here rather than read as a flat
            # pair. Pre-P4e journal entries (recorded under the old
            # "origin_agent"/"origin_sid" keys) have no "requester" key at
            # all; that legacy shape is read as a fallback so an
            # already-persisted chain from before this rename still
            # restores correctly rather than losing its reply address.
            requester_dict = chain_dict.get("requester")
            if requester_dict is not None:
                requester = Requester(
                    agent_name=requester_dict["agent_name"],
                    session_id=requester_dict["session_id"],
                )
            else:
                requester = Requester(
                    agent_name=chain_dict.get("origin_agent", ""),
                    session_id=chain_dict.get("origin_sid") or "main",
                )
            # proposal 0067 P8 (#3978): "arm_at" is an ISO-8601 string on
            # the wire (JSON has no datetime type); absent on a pre-P8
            # entry or a chain never armed (timeouts were disabled at
            # arm-time). fromisoformat() round-trips isoformat()'s own
            # output exactly (both sides of this pair are this module).
            arm_at_raw = chain_dict.get("arm_at")
            arm_at = datetime.fromisoformat(arm_at_raw) if arm_at_raw else None
            self._chains[cid] = _PendingChain(
                chain_id=chain_dict["chain_id"],
                requester=requester,
                origin_depth=int(chain_dict["origin_depth"]),
                original_request=chain_dict["original_request"],
                waiting_on=set(chain_dict.get("waiting_on", [])),
                kind=chain_dict.get("task_kind"),  # #3978 P4 (None for pre-P4 entries)
                arm_at=arm_at,
            )
            if self._chain_timeout_seconds <= 0:
                continue
            if arm_at is not None:
                remaining = max(0.0, (arm_at - self._clock()).total_seconds())
                self._start_watchdog(
                    cid, on_fire=on_fire, duration_seconds=remaining
                )
            else:
                self._start_watchdog(
                    cid, on_fire=on_fire, duration_seconds=self._chain_timeout_seconds
                )
