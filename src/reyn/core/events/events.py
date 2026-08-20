from __future__ import annotations

import contextvars
import logging
import secrets
from datetime import date
from pathlib import Path
from typing import Callable

from reyn.core.events.backend import EventBackend
from reyn.schemas.models import Event

logger = logging.getLogger(__name__)


# #1669: session-scoped ambient EventLog for the single LLM acompletion chokepoint.
# ``recorded_acompletion`` (reyn.llm.llm) is the one place ALL LLM calls funnel
# through (#1190 AST-guarded), but it receives no events sink (only budget /
# recorder). Threading one through its 9 call sites would be churn AND incomplete
# (judge / compaction / dogfood callers lack a sink). Instead the chat session /
# kernel runtime sets this ContextVar to its EventLog at creation; the chokepoint
# reads it and emits ``llm_request``. ContextVars copy into child asyncio tasks at
# spawn, so a set-before-the-run-loop propagates to every in-session LLM call.
# None (tests / dogfood / CLI, no active session) → the chokepoint skips the emit,
# mirroring the ``recorder=None`` graceful path.
_llm_request_event_log: contextvars.ContextVar["EventLog | None"] = contextvars.ContextVar(
    "reyn_llm_request_event_log", default=None,
)


def set_llm_request_event_log(log: "EventLog | None") -> contextvars.Token:
    """Set the ambient EventLog the LLM chokepoint emits ``llm_request`` to (#1669).

    Returns the token so a caller MAY reset to the prior value for a nested scope;
    the session / runtime set-at-creation sites do not reset (last-set-wins is the
    intended session-scoped lifetime — the active top-level run owns the sink)."""
    return _llm_request_event_log.set(log)


def get_llm_request_event_log() -> "EventLog | None":
    """Read the ambient EventLog for the LLM chokepoint (#1669); None when unset."""
    return _llm_request_event_log.get()


class EventLog:
    def __init__(
        self,
        subscribers: list[Callable[[Event], None]] | None = None,
        *,
        agent_id: str | None = None,
        run_id: str | None = None,
        emitter: str | None = None,
        track_audit_seq: bool = True,
        backend: "EventBackend | None" = None,
    ) -> None:
        # #3868 PR-1: a folded derived state for `present`'s "was this ref
        # already read this session?" question (source.py's compute_ingested),
        # built incrementally in emit() instead of re-scanned from an
        # unbounded full-history list on every present call. Keyed on the
        # read's own `path`; "full" is STICKY — a later truncated read on the
        # same path never downgrades it, because the operator (or a prior
        # full read) already saw the whole thing. What changed is the GROWTH
        # CLASS this dict is subject to: O(distinct paths ever read), not
        # O(every event ever emitted) — see compute_ingested's docstring for
        # why that is still unbounded in principle but bounded by real work
        # (a file read + permission gate) rather than by talk. #3868 PR-3:
        # the unbounded `_events` full-history list this fold replaced is
        # gone — `all()`/`to_json()` (its only readers) retired in PR-2's
        # collect_events() migration first.
        self._ingested: dict[str, str] = {}
        self._subscribers: list[Callable[[Event], None]] = list(subscribers or [])
        # FP-0016 Component E: agent_id is auto-injected into every event
        # payload when set. None preserves prior behaviour for callers
        # (= tests + emit_cli_event) that don't have a session identity.
        self._agent_id = agent_id
        # Issue #134: run_id is auto-injected into every event payload
        # when set, mirroring the agent_id pattern. The run that
        # emits the event is recorded so that subscribers (= forwarder /
        # TUI) can distinguish events from a parent agent turn versus a
        # sub-agent turn (which inherits the parent's subscriber list).
        self._run_id = run_id
        # #4496 PR-1 (architect's contract 3): every audit-event carries a
        # monotonic `audit_seq` per `emitter`, so a subscriber can detect a
        # gap (a dropped event) without needing delivery-order guarantees —
        # NOT the WAL's own `seq` (a different concept: WAL seq is the
        # recovery coordinate; audit_seq is purely an audit-continuity
        # witness — see events.md and CLAUDE.md's own warning against
        # reusing that name for a second thing).
        #
        # `emitter` identifies ONE execution of a session, not the session
        # itself (a session_id is stable across restarts — reusing it would
        # let two different process runs both emit `audit_seq` 1..N under
        # the same emitter, making a genuine gap indistinguishable from "a
        # new run started"). Measured directly, not assumed: the session's
        # own audit EventLog (session.py's `audit_events = EventLog(...)`)
        # passes `agent_id` only, `run_id` stays unset there — so `run_id`
        # is NOT a reliable per-execution identity for this specific
        # EventLog instance, and this can't just alias it. A fresh EventLog
        # is constructed exactly once per real process execution (never
        # reloaded/reused across a restart), so a random token minted HERE,
        # once, at construction, is already unique per execution BY
        # CONSTRUCTION — no dependency on any other field being populated.
        # `emitter=` lets a caller override this (e.g. `emit_cli_event`
        # passing the literal `"cli"` label for its one-off, non-
        # continuous events — see that function's own construction site).
        self._emitter = emitter if emitter is not None else secrets.token_hex(8)
        # CLI one-off events have no continuity to protect (one event, one
        # process, never a second call from the same EventLog instance) —
        # architect's own ruling: "a series of exactly one has no meaning
        # for gap-detection", so `track_audit_seq=False` omits the key
        # entirely rather than always stamping a meaningless `1`.
        self._track_audit_seq = track_audit_seq
        self._audit_seq = 0
        # #4496 PR-2: the WRITE-side backend (local disk / discard — see
        # `backend.py`'s module docstring for the full contract and why
        # this is NOT a subscriber). None preserves the pre-PR-2 shape
        # (no write side at all — callers that want persistence add an
        # EventStore as a plain subscriber, same as before this PR).
        self._backend = backend

    @property
    def subscribers(self) -> list[Callable[[Event], None]]:
        return self._subscribers

    @property
    def ingested_path_count(self) -> int:
        """How many DISTINCT paths :meth:`compute_ingested`'s derived state
        currently tracks (#3868 PR-1) — the public witness for its growth
        class: this grows with the number of unique paths ever read, never
        with the number of events emitted (a non-read event, or a REPEAT
        read of an already-tracked path, leaves this unchanged). Exists so
        that claim is testable without reading the private ``_ingested``
        dict directly.
        """
        return len(self._ingested)

    @property
    def agent_id(self) -> str | None:
        """The agent_id this EventLog stamps onto emitted events (FP-0016 E).

        Public read-only view of the constructor-injected agent_id so
        downstream consumers (= kernel executors building OpContext) can
        pick it up without a separate threading parameter.
        """
        return self._agent_id

    @property
    def run_id(self) -> str | None:
        """The run_id this EventLog stamps onto emitted events (issue #134)."""
        return self._run_id

    @property
    def backend(self) -> "EventBackend | None":
        """The active write-side backend (#4496 PR-2), or None (no write
        side — pre-PR-2 shape). Public read-only view so a consumer
        (`reyn events` CLI) can call `declare_gaps()` without reaching into
        private state."""
        return self._backend

    @property
    def emitter(self) -> str:
        """The emitter label this EventLog stamps onto every emitted event
        (#4496 PR-1, contract 3) — auto-generated at construction when not
        explicitly given. Public read-only view, same rationale as
        ``agent_id``/``run_id`` above: lets a caller confirm identity
        without reaching into private state."""
        return self._emitter

    def add_subscriber(self, fn: Callable[[Event], None]) -> None:
        self._subscribers.append(fn)

    def set_backend(self, backend: "EventBackend | None") -> None:
        """Swap the WRITE-side backend (#4496 PR-2) — e.g. re-pointing to a
        new `EventStore`-backed `LocalEventBackend` after `set_events_dir`
        re-keys a spawned session's events directory. Deliberately a plain
        setter, not add/remove: exactly one backend is active at a time
        (unlike subscribers, which are a list) — the PREVIOUS backend is
        simply dropped, no explicit "remove" step needed."""
        self._backend = backend

    def remove_subscriber(self, fn: Callable[[Event], None]) -> bool:
        """Detach a previously added subscriber.

        Returns True iff the subscriber was found and removed. Used by
        scoped consumers that subscribe for the duration of one call
        (= e.g. issue #271 M1 MCP progress bridge: subscribe in
        ``_call_tool``, unsubscribe in ``finally``) so the subscriber
        list doesn't grow unboundedly across many calls.
        """
        try:
            self._subscribers.remove(fn)
            return True
        except ValueError:
            return False

    def emit(self, type: str, **data) -> Event:
        # FP-0016 Component E: stamp the session's agent_id onto every
        # event payload so the P6 audit trail can answer "which agent
        # did this?" without correlating across multiple logs.  Caller-
        # provided ``agent_id`` wins (= delegation flows may preserve
        # the upstream origin's identity).
        if self._agent_id and "agent_id" not in data:
            data = {**data, "agent_id": self._agent_id}
        # Issue #134: stamp run_id with the same caller-wins convention
        # as agent_id. Lets subscribers route events to the correct
        # row when a child agent shares the parent's subscriber list.
        if self._run_id and "run_id" not in data:
            data = {**data, "run_id": self._run_id}
        # #4496 PR-1: emitter + audit_seq are ALWAYS this EventLog's own —
        # deliberately NOT caller-wins (unlike agent_id/run_id above).
        # Letting a caller supply either would open a path to skip a
        # number or forge one, which would make a real gap indistinguishable
        # from an intentional skip — the exact failure mode contract 3
        # exists to prevent. Overwrites any caller-supplied same-named key
        # rather than merely defaulting it.
        data = {**data, "emitter": self._emitter}
        if self._track_audit_seq:
            self._audit_seq += 1
            data["audit_seq"] = self._audit_seq
        event = Event(type=type, data=data)
        # #3868 PR-1: fold this event's contribution to `_ingested` at emit
        # time (a dict update) instead of re-scanning full history at every
        # `present` call (was O(session length) per call, source.py:154).
        # Early-return on the common case (not a read) first — `emit` is a
        # hot path (every op, every tool call) and this only has work to do
        # for a specific op kind.
        if type == "tool_executed":
            op = data.get("op")
            if op in ("read_file", "read"):
                path = data.get("path")
                if path is not None:
                    if data.get("truncated"):
                        # Sticky full: a later partial read on a path already
                        # seen in full does not downgrade it — the operator
                        # (or a prior read) already has the whole thing.
                        if self._ingested.get(path) != "full":
                            self._ingested[path] = "partial"
                    else:
                        self._ingested[path] = "full"
        # #4496 PR-2: the backend writes BEFORE the subscriber loop runs,
        # and its own exception is caught right here — never let a backend
        # failure (e.g. a future network backend's connection error) reach
        # a subscriber, and (by running first) never let a raising
        # subscriber prevent the backend from having already written. See
        # `backend.py`'s module docstring for the full "not a subscriber"
        # rationale.
        if self._backend is not None:
            try:
                self._backend.write(event)
            except Exception:
                logger.exception(
                    "event backend write failed (emitter=%s type=%s) — "
                    "continuing to subscriber dispatch",
                    self._emitter, type,
                )
        # #4961 A: per-subscriber isolation. Before this, a raising
        # subscriber aborted the WHOLE loop — every LATER subscriber in
        # ``self._subscribers`` (registration order) was silently skipped,
        # and the exception propagated all the way to `emit()`'s own
        # caller (an op/tool's execution path). Registration order is not
        # under this method's control, and transport forwarders (AG-UI,
        # A2A, MCP) share this exact list with the OTEL exporter
        # (session.py) — a raising transport could silently blind
        # observability with no exception anywhere to notice by (#4961's
        # own "silent" framing). Failure is logged, never swallowed —
        # same posture ``self._backend.write`` above already takes for its
        # own failure, and NOT a change to registration semantics
        # (``add_subscriber`` stays synchronous — #3310 N3(a)'s barrier
        # ordering is untouched; this is purely a dispatch-loop isolation,
        # per-subscriber, sequential and in the SAME order as before).
        for sub in self._subscribers:
            try:
                sub(event)
            except Exception:
                logger.exception(
                    "event subscriber failed (emitter=%s type=%s) — "
                    "continuing to the next subscriber",
                    self._emitter, type,
                )
        return event

    def compute_ingested(self, data_ref: str, resolved: str) -> str:
        """``ingested`` ∈ ``{none, partial, full}`` for a present ``data_ref``
        (#3868 PR-1) — an O(1) lookup into the state :meth:`emit` folds
        incrementally, replacing source.py's former O(session length) scan
        over the full event history.

        Checked under BOTH keys a caller might resolve a ref by (the raw
        ``data_ref`` and its ``resolved`` form — source.py's own pre-existing
        two-key check, unchanged here), with ``full`` winning if the two keys
        disagree.

        Blindness is an audit annotation, not a permission mode: this
        reports whether a prior ``read_file`` on this ref appears earlier in
        the session — never LLM-self-reported.

        Still unbounded in principle (read enough distinct paths and this
        dict grows without limit) — NOT bounded to a fixed size, deliberately:
        a ``deque(maxlen=N)``-style cap would make an old path's entry
        silently vanish, and a caller re-presenting that ref would then see
        ``none`` instead of ``full`` — a false "you haven't read this yet"
        for a ref that WAS fully read, which is worse than the unbounded
        growth it would avoid. What bounds it in practice is that every
        entry costs a real ``file.read`` (I/O + the permission gate) to
        create — growth is bounded by actual work done, not by how much an
        agent can emit.
        """
        a = self._ingested.get(data_ref)
        b = self._ingested.get(resolved)
        if a == "full" or b == "full":
            return "full"
        if a == "partial" or b == "partial":
            return "partial"
        return "none"


def _find_reyn_dir(start: Path) -> Path | None:
    """Walk up from *start* until finding a directory containing `.reyn/`, or return None."""
    current = start.resolve()
    while True:
        candidate = current / ".reyn"
        if candidate.is_dir():
            return candidate
        parent = current.parent
        if parent == current:
            return None
        current = parent


def emit_cli_event(kind: str, **payload) -> None:
    """Emit a one-off P6 event from a CLI context (no active session).

    Routes to ``.reyn/events/direct/cli/<YYYY-MM-DD>.jsonl``. Locates the
    ``.reyn/`` dir by walking up from ``Path.cwd()``. If no ``.reyn/``
    directory is found, logs a warning and returns silently — the caller's
    operation is the primary action; audit-emit failure must not propagate.

    The file is appended to (P6 append-only contract). Dir creation is
    idempotent (``mkdir(parents=True, exist_ok=True)``).
    """
    from reyn.core.events.event_store import EventStore

    reyn_dir = _find_reyn_dir(Path.cwd())
    if reyn_dir is None:
        logger.warning(
            "emit_cli_event: no .reyn/ directory found from %s; "
            "skipping P6 audit emit for event %r",
            Path.cwd(),
            kind,
        )
        return

    cli_dir = reyn_dir / "events" / "direct" / "cli"
    today = date.today().isoformat()  # YYYY-MM-DD
    # Use a date-named suffix so each day's CLI events land in one predictable file.
    # max_bytes=0 / max_age_seconds=0 disables rotation — the suffix IS the date.
    store = EventStore(cli_dir, max_bytes=0, max_age_seconds=0, suffix=f"_{today}")
    # #4496 PR-1: a one-off CLI event has no continuity to protect — a
    # single event from a single process is not a series a gap can be
    # detected in, so audit_seq is omitted entirely (architect's ruling)
    # rather than always stamping a meaningless `1`. `emitter="cli"` is a
    # legible label (not a random per-instance token — nothing needs to
    # distinguish one CLI invocation's emitter from another's, since
    # neither carries a sequence to compare).
    event_log = EventLog(subscribers=[store], emitter="cli", track_audit_seq=False)
    event_log.emit(kind, **payload)
