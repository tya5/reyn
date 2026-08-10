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
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol, runtime_checkable

from reyn.runtime.task_types import RunStatus

if TYPE_CHECKING:
    from reyn.core.events.agent_snapshot import AgentSnapshot
    from reyn.runtime.task_types import Requester

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
    the upstream ``origin_agent`` is held back until every entry in
    ``waiting_on`` has returned an ``agent_response`` for this chain_id.
    On the final response, the agent re-runs its router so the LLM can
    compose a synthesized answer with all delegate replies in history,
    then sends that answer to ``origin_agent`` at ``origin_depth``.

    Note: session.py retains an identical copy until wave 2 imports this.
    """

    chain_id: str
    origin_agent: str
    origin_depth: int
    original_request: str
    waiting_on: set[str] = field(default_factory=set)
    # #2130: the origin's session id — so the resolved chain reply routes back to the
    # specific (origin_agent, origin_sid), not the origin agent's main session. None →
    # main-case (user-initiated / external peer / pre-#2130 journal entries).
    origin_sid: "str | None" = None
    # proposal 0067 P4 (#3978): the task kind (prompt/pipeline/exec) this
    # handle represents — None for a legacy delegate-relay chain (no typed
    # task kind exists for that flow yet; P6 assigns one when
    # delegate_to_agent's own completion is folded into this same
    # substrate). ``describe_task``/``list_tasks`` read this; a handle
    # registered with no ``kind`` is not yet describable as a typed task.
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
    # (lead-coder's ruling: "the hole where a callback survives after its
    # chain is popped, and cancel_task can act on a task nothing runs
    # anymore, is structurally impossible when there is only ONE store —
    # not two that must be kept in sync"). `None` after a crash-recovered
    # restore (the live callable belonged to the dead process) — a caller
    # (cancel_task) that finds `cancel is None` MUST NOT report success:
    # nothing is actually listening.
    cancel: "Callable[[], None] | None" = field(default=None, compare=False, repr=False)

    @property
    def requester(self) -> "Requester":
        """ADR-0040 D6's ``Requester(agent_name, session_id)`` — derived
        from ``(origin_agent, origin_sid)``, NOT a second stored field.

        Measured (lead-coder, 2026-08-10): these two fields already ARE
        D6's Requester in substance — the docstring above already says
        "routes back to the specific (origin_agent, origin_sid)". Adding a
        separate ``requester`` field would duplicate storage for the same
        fact. The RENAME (making ``origin_agent``/``origin_sid`` literally
        ``requester.agent_name``/``requester.session_id``) is deferred to
        P6 (no consumer needs it before delegate_to_agent's own completion
        folds into this substrate) — this read accessor lets P4's
        ``describe_task`` consumer be satisfied today without committing to
        that rename now; P6 changing the field names underneath won't
        change this property's callers."""
        from reyn.runtime.task_types import Requester

        return Requester(agent_name=self.origin_agent, session_id=self.origin_sid or "main")


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
    """

    def __init__(
        self,
        *,
        journal: "_JournalLike",
        events: Any,
        chain_timeout_seconds: float,
        max_hop_depth: int,
    ) -> None:
        self._journal = journal
        self._events = events
        self._chain_timeout_seconds = chain_timeout_seconds
        self.max_hop_depth = max_hop_depth

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
        from_user: bool,
        depth: int,
        original_text: str,
        sender: str | None,
        waiting_on: set[str] | None = None,
        origin_agent: str = "",
        origin_depth: int = 0,
        origin_sid: "str | None" = None,
        kind: "str | None" = None,
        cancel: "Callable[[], None] | None" = None,
    ) -> _PendingChain:
        """Register a new pending chain and persist it via the journal.

        Parameters
        ----------
        chain_id:
            Unique identifier for the chain.
        from_user:
            True when the chain originates from a user message.
        depth:
            Current hop depth (used as ``origin_depth`` when not specified).
        original_text:
            The original request text.
        sender:
            Agent name that sent the request, or None for user-initiated chains.
        waiting_on:
            Set of agent names this chain is waiting for.
        origin_agent:
            The agent to reply to when the chain resolves.
        origin_depth:
            The depth at which to send the reply upstream.
        kind:
            proposal 0067 P4 (#3978): the task kind (``"prompt"`` /
            ``"pipeline"`` / ``"exec"``) this handle represents, or
            ``None`` for a legacy delegate-relay chain (no producer sets
            this today — P6 assigns one when ``delegate_to_agent``'s own
            completion folds into this substrate).
        cancel:
            proposal 0067 P4 (#3978): the task's cooperative cancel hook
            (argument-zero), or ``None`` if this task cannot be cancelled
            (e.g. a legacy delegate-relay chain). VOLATILE — never
            persisted (see ``_PendingChain.cancel``'s own docstring).
        """
        chain = _PendingChain(
            chain_id=chain_id,
            origin_agent=origin_agent or sender or "",
            origin_depth=origin_depth or depth,
            original_request=original_text,
            waiting_on=set(waiting_on or []),
            origin_sid=origin_sid,
            kind=kind,
            cancel=cancel,
        )
        self._chains[chain_id] = chain

        fields: dict[str, Any] = {
            "origin_agent": chain.origin_agent,
            "origin_depth": chain.origin_depth,
            "original_request": chain.original_request,
            "waiting_on": sorted(chain.waiting_on),
            "from_user": from_user,
            "origin_sid": chain.origin_sid,  # #2130
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
        its signature, so P6 can point ``delegate_to_agent``'s own
        chain-resolve completion at this SAME function later).

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

    def arm_timeout(
        self,
        chain_id: str,
        *,
        on_fire: Callable[[str], Awaitable[None]],
    ) -> None:
        """Start a watchdog task for ``chain_id``.

        No-op when timeouts are disabled (chain_timeout_seconds <= 0).
        Idempotent — replaces any existing timer for the same chain_id
        by cancelling it first.
        """
        if self._chain_timeout_seconds <= 0:
            return
        existing = self._timers.pop(chain_id, None)
        if existing is not None and not existing.done():
            existing.cancel()
        self._timers[chain_id] = asyncio.create_task(
            self._chain_timeout_watch(chain_id, on_fire=on_fire)
        )

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
        seq; ``restore()`` re-arms fresh watchdogs from the recovered snapshot.
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
    ) -> None:
        """Internal watchdog coroutine.

        Sleeps for ``chain_timeout_seconds``; if the chain is still pending
        on wake, fires the timeout by calling ``on_fire(chain_id)``.

        Cancellation (normal resolve path) raises CancelledError out of the
        sleep — this coroutine just exits cleanly.  Shutdown() gathers these
        tasks with ``return_exceptions=True`` so a late-fire during teardown
        is harmless.
        """
        try:
            await asyncio.sleep(self._chain_timeout_seconds)
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

        Reconstructs each _PendingChain and arms a fresh timeout watchdog.
        Call this after the journal has installed a recovered snapshot.
        """
        for cid, chain_dict in self._journal.snapshot.pending_chains.items():
            self._chains[cid] = _PendingChain(
                chain_id=chain_dict["chain_id"],
                origin_agent=chain_dict["origin_agent"],
                origin_depth=int(chain_dict["origin_depth"]),
                original_request=chain_dict["original_request"],
                waiting_on=set(chain_dict.get("waiting_on", [])),
                origin_sid=chain_dict.get("origin_sid"),  # #2130 (None for pre-#2130 entries)
                kind=chain_dict.get("task_kind"),  # #3978 P4 (None for pre-P4 entries)
            )
            self.arm_timeout(cid, on_fire=on_fire)
