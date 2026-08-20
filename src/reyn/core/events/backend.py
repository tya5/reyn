"""EventBackend — the audit-event WRITE-side abstraction (#4496 PR-2).

Lets an operator choose where `.reyn/events` audit-events are written:
local disk (default, current behavior), or discarded entirely. A
`network` backend is a deliberate, flagged scope cut — see below.

Deliberately THIN, per architect's #4496 design (issue comment, 2026-08-13):
a backend has exactly 2 responsibilities.

    1. receive an event and dispatch it (write / send / discard)
    2. name what it does NOT retain, so a consumer (`reyn events replay`,
       support-bundle, dogfood_trace) can tell "this backend doesn't keep
       that" apart from "nothing happened" (contract 2 — see `declare_gaps`)

The THIRD contract architect names — a monotonic `audit_seq` per emitter
so a subscriber can detect a gap — is NOT a backend responsibility. It is
already implemented in `EventLog.emit()` itself (#4496 PR-1): `audit_seq`
is stamped on every event regardless of backend, including `discard`
(measured: `emit()` does agent_id/run_id stamp -> emitter+audit_seq stamp
-> subscriber dispatch -> return; no file I/O happens inside `emit()` at
all — see `EventLog.emit`'s own docstring). A backend that skipped seq
under its own logic would be reimplementing (and could diverge from) a
guarantee `emit()` already provides for free — so backends never touch it.

## Not a subscriber (the structural guarantee this module exists for)

`EventLog.emit()` calls `self._backend.write(event)` directly — wrapped in
try/except — BEFORE it loops over `self._subscribers` (#4496 PR-2). This
is deliberate, not incidental:

    - the subscriber loop (`for sub in self._subscribers: sub(event)`) now
      HAS per-subscriber try/except (#4961 A — this used to be a real gap:
      a raising subscriber aborted the loop and every LATER subscriber in
      the list was silently skipped, `events.py`, measured). That fix
      isolates one subscriber's failure from the NEXT ones, but it does
      NOT make position in `self._subscribers` a safe place for the
      backend: inserting a backend as JUST ANOTHER subscriber would still
      make it position-dependent (registered early enough to run before
      whatever fills the list — including a future backend that changes
      registration order) instead of unconditional — the exact "discard
      silences the UI" failure mode the owner's #4496 ruling forbids
      ("emit は抽象に対して必ず行う、Backend 側で破棄するだけ") demands
      the backend write happen NO MATTER what's registered or in what
      order, not merely "isolated from subscribers that happen to raise".
    - calling the backend FIRST, outside the subscriber loop, with its own
      try/except, gets both halves of prohibition ③ (backend failure must
      not reach subscribers, and vice versa) from ORDERING alone: the
      backend has already run by the time any subscriber could raise, and
      a backend exception is caught right where it's raised, before the
      subscriber loop even starts.

## Scope: `network` is deferred, not silently dropped

The owner's #4496 write-up leaves one point genuinely undecided: what a
`network` backend does when the network call fails (discard-and-let-the-
seq-gap-show-it / spool-locally / halt-the-run — three real options, see
issue #4496's "決めるべき残り1点"). Building a `NetworkEventBackend` ahead
of that decision would mean guessing at owner-owned UX. `local` and
`discard` need no such decision (their only failure mode is disk I/O
raising, already covered by contract ③'s try/except at the call site) —
this PR ships those two; `network` is issue #4496's own next PR once the
owner has resolved the open question above.
"""
from __future__ import annotations

import time
from typing import Callable, Protocol, runtime_checkable

from reyn.schemas.models import Event

#: #4960 — architect ruling C: ``agent_delta`` (one audit-event per
#: streamed content chunk) is NOT durably written per-fragment. Live
#: subscriber dispatch (TUI / AG-UI) is completely unaffected — see
#: ``EventLog.emit()``'s own ordering (backend write, then subscriber
#: loop) — this constant only throttles what ``LocalEventBackend``
#: persists to disk.
#:
#: Measured (#4960, 2000-delta / 60KB streamed reply, the SAME real
#: transport/router/TUI path the #3570 repaint-budget precedent uses):
#: unthrottled, ``agent_delta`` writes are 99.4% of the audit file's
#: total bytes for that run (550,917 / 554,112 bytes), at a fixed
#: ~275 bytes/fragment and ~28-40us of backend-write time per fragment.
#: 100 caps that to ~1% of the unthrottled footprint (~20 durable
#: records instead of 2000 for that run) while keeping the loss-of-
#: recency bound small (at most 99 fragments, ~27KB of text, between
#: any two durable checkpoints for a chain still actively streaming).
_DEFAULT_AGENT_DELTA_COALESCE_FRAGMENTS = 100

#: Measured burst rate through a proxy (#3570's own comment): up to
#: ~1000 deltas/s, so at the fragment default above a burst is governed
#: by the FRAGMENT count (~100ms between durable writes), never by this
#: timer. This interval exists for the cases the fragment count cannot
#: reach on its own:
#:
#:   ① (primary) a process-level death (SIGKILL / OOM-kill / host
#:      crash) that the terminal flush below CANNOT catch — a Python
#:      ``finally`` block never runs when the process itself is killed,
#:      so this interval is the ONLY durable-record guarantee left for a
#:      chain that dies mid-stream with fewer than the fragment count
#:      accumulated (architect's #4960 ruling: this is the scenario a
#:      short interruption is MOST likely to hit, and losing it defeats
#:      the whole reason C was chosen over B — cost accountability for a
#:      call whose usage record never lands).
#:   ② (secondary) an idle-but-long-lived stream (few deltas over a long
#:      wall-clock span) still leaves periodic evidence of progress.
#:
#: 2 seconds is far above the measured per-fragment cost (tens of
#: microseconds) so it never fires under normal bursty streaming, and
#: short enough that an operator inspecting mid-crash state is not
#: staring at a multi-minute-old durable record.
_DEFAULT_AGENT_DELTA_COALESCE_INTERVAL_MS = 2_000


@runtime_checkable
class EventBackend(Protocol):
    """The write-side surface `EventLog.emit()` calls into (#4496 PR-2)."""

    def write(self, event: Event) -> None:
        """Persist / send / discard *event*.

        May raise — the caller (`EventLog.emit`) catches and logs; a
        backend must never assume its own exception reaches anything
        downstream (subscribers included)."""
        ...

    def declare_gaps(self) -> list[str]:
        """Human-readable statements of what this backend does NOT retain.

        Empty list = no gaps (this backend keeps everything a consumer
        might expect). A consumer reading `[]` sees "nothing missing",
        never confuses it with an empty EVENT list (contract 2: "empty"
        and "unsupported" must be told apart)."""
        ...


class LocalEventBackend:
    """Writes to local disk via an `EventStore` (the default, current
    behavior — #4496 PR-2 wraps the EXISTING EventStore.write, no I/O
    change), EXCEPT ``agent_delta`` (#4960, architect ruling C): coalesced
    to one durable record per ``agent_delta_coalesce_fragments`` fragments
    OR ``agent_delta_coalesce_interval_ms`` milliseconds, whichever comes
    first, per streaming chain (``event.data["chain_id"]``) — see the two
    module-level constants above for the measured rationale, and
    :meth:`flush_pending_deltas` for the terminal-flush half of the
    guarantee (the 3 mechanisms — fragment count, interval, terminal
    flush — cover each other's gap; see each one's own docstring for
    which failure mode it alone covers).

    Live subscriber dispatch is completely unaffected: this coalescing
    lives entirely inside ``write()``, called by ``EventLog.emit()``
    BEFORE the (unthrottled) subscriber loop — every raw fragment still
    reaches the TUI/AG-UI exactly as before #4960.

    All OTHER event kinds: unchanged, no gaps — replay / support-bundle /
    dogfood_trace work normally against this backend's output for them."""

    def __init__(
        self,
        store: "EventStoreLike",
        *,
        agent_delta_coalesce_fragments: int = _DEFAULT_AGENT_DELTA_COALESCE_FRAGMENTS,
        agent_delta_coalesce_interval_ms: int = _DEFAULT_AGENT_DELTA_COALESCE_INTERVAL_MS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._agent_delta_coalesce_fragments = agent_delta_coalesce_fragments
        self._agent_delta_coalesce_interval_ms = agent_delta_coalesce_interval_ms
        # Test seam (mirrors this repo's existing ``clock: Callable[[],
        # float]`` idiom, e.g. TextualChatApp) — production always passes
        # the default ``time.monotonic``; a test can inject a fake to
        # exercise the interval branch without a real sleep (CLAUDE.md:
        # "a test writes no duration ... the clock is an INPUT you
        # supply, never a sleep you wait out").
        self._clock = clock
        # Per-chain_id coalescing state (#4960). "" buckets any
        # agent_delta that, unexpectedly, carries no chain_id — never
        # silently dropped, just coalesced under one shared key instead
        # of per-chain isolation.
        self._delta_pending_count: dict[str, int] = {}
        self._delta_last_persisted_at: dict[str, float] = {}
        self._delta_last_event: dict[str, Event] = {}

    def write(self, event: Event) -> None:
        if event.type != "agent_delta":
            self._store.write(event)
            return
        chain_id = event.data.get("chain_id")
        key = chain_id if isinstance(chain_id, str) else ""
        now = self._clock()
        if key not in self._delta_last_persisted_at:
            self._delta_last_persisted_at[key] = now
        self._delta_last_event[key] = event
        pending = self._delta_pending_count.get(key, 0) + 1
        elapsed_ms = (now - self._delta_last_persisted_at[key]) * 1000
        if (
            pending >= self._agent_delta_coalesce_fragments
            or elapsed_ms >= self._agent_delta_coalesce_interval_ms
        ):
            self._persist_coalesced_delta(event, coalesced_fragment_count=pending)
            self._delta_pending_count[key] = 0
            self._delta_last_persisted_at[key] = now
        else:
            self._delta_pending_count[key] = pending

    def flush_pending_deltas(self, chain_id: str) -> None:
        """#4960 — the terminal-flush mechanism: called once a streaming
        chain ends (success, exception, or cancellation — see
        ``EventLog.flush_agent_delta``'s own call site in
        ``RouterLoop.run()``'s ``finally``). Persists one final coalesced
        record for any fragments accumulated since the last durable write,
        so a SHORT interruption (fewer fragments than the coalesce count,
        less wall-clock than the coalesce interval — the interruption
        shape most likely to occur, per architect's #4960 ruling) still
        leaves durable evidence that partial output existed.

        Does NOT cover a process-level death (SIGKILL / OOM-kill / host
        crash) — a Python ``finally`` never runs in that case; the
        coalesce-interval mechanism in :meth:`write` is the ONLY durable-
        record guarantee for THAT failure mode. The two mechanisms
        deliberately cover different, non-overlapping failure classes."""
        key = chain_id if isinstance(chain_id, str) else ""
        pending = self._delta_pending_count.get(key, 0)
        last_event = self._delta_last_event.get(key)
        if pending > 0 and last_event is not None:
            self._persist_coalesced_delta(last_event, coalesced_fragment_count=pending)
        # Drop this chain's state — a chain_id is not reused after its
        # stream ends, so keeping it would grow these dicts unbounded
        # across a long-lived process handling many turns.
        self._delta_pending_count.pop(key, None)
        self._delta_last_persisted_at.pop(key, None)
        self._delta_last_event.pop(key, None)

    def _persist_coalesced_delta(self, event: Event, *, coalesced_fragment_count: int) -> None:
        """Write ONE durable record standing in for *coalesced_fragment_count*
        raw ``agent_delta`` fragments — the most recently arrived fragment's
        own event, with the coalesced count added to ``data`` (a new field,
        not a mutation of *event* itself — *event* is the SAME object the
        (already-completed) subscriber dispatch loop may still be holding a
        reference to, so a new object is written, never the original
        mutated in place)."""
        self._store.write(
            event.model_copy(
                update={"data": {**event.data, "coalesced_fragment_count": coalesced_fragment_count}},
            )
        )

    def declare_gaps(self) -> list[str]:
        return [
            "agent_delta (streamed reply content, one per chunk) is not "
            "retained per-fragment — coalesced to one durable record per "
            f"{self._agent_delta_coalesce_fragments} fragments or "
            f"{self._agent_delta_coalesce_interval_ms}ms, whichever comes "
            "first, plus one final record when a stream ends (#4960). "
            "Live TUI/AG-UI delivery is unaffected — this is a durable-"
            "write-only gap. `reyn events replay` sees fewer agent_delta "
            "records than fragments actually streamed.",
        ]


class DiscardEventBackend:
    """Writes nothing (sink-null). `emit()` and subscriber dispatch are
    UNCHANGED when this backend is active (#4496's structural guarantee,
    see module docstring) — only the write-to-disk step becomes a no-op.

    `reyn events replay` / support-bundle / dogfood_trace must consult
    `declare_gaps()` and report it explicitly rather than reading an
    empty local events tree as "nothing happened" (contract 2)."""

    def write(self, event: Event) -> None:
        return None

    def declare_gaps(self) -> list[str]:
        return [
            "this backend does not retain events locally (audit_events."
            "backend: discard) — `reyn events replay`, support-bundle, "
            "and dogfood_trace have nothing to read for this run",
        ]


class EventStoreLike(Protocol):
    """The one method `LocalEventBackend` needs from `EventStore` — kept
    separate from importing `EventStore` directly so this module has no
    dependency on `event_store.py`'s file-rotation machinery (P7:
    OS-level generic infrastructure stays decoupled from any one backend's
    implementation details)."""

    def write(self, event: Event) -> None: ...
