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

from typing import Protocol, runtime_checkable

from reyn.schemas.models import Event


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
    change). No gaps: replay / support-bundle / dogfood_trace all work
    normally against this backend's output."""

    def __init__(self, store: "EventStoreLike") -> None:
        self._store = store

    def write(self, event: Event) -> None:
        self._store.write(event)

    def declare_gaps(self) -> list[str]:
        return []


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
