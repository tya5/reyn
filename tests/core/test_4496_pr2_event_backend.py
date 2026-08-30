"""Tier 2: #4496 PR-2 — audit-event WRITE-side backend abstraction.

Architect's acceptance criteria (issue #4496 comment, falsifiable form):

  1. ``backend=discard`` — subscribers still receive events (the witness
     MUST be actual subscriber delivery, not "emit was called").
  2. ``backend=discard`` — audit_seq still increments 1, 2, 3, ... (discard
     does not also discard sequence continuity).
  3. a raising backend does not stop subscriber delivery.
  4. falsify: moving the backend call INTO the subscriber loop (so a
     raising backend aborts delivery to later subscribers) flips ①/③ red
     — the two tests above already ARE that falsify: if a future edit put
     the backend in ``self._subscribers`` with no isolating try/except,
     ``test_a_raising_backend_does_not_stop_subscriber_delivery`` goes red
     immediately (a raising subscriber-list entry aborts every LATER
     entry — see ``backend.py``'s module docstring for the measured
     mechanism).

#2 (Session-level, real production path): ``backend=discard`` — the real
AG-UI/CUI subscriber wiring (``ChatLifecycleForwarder`` et al., attached by
``Session.__init__`` itself) keeps receiving events, driven through
``make_session`` (the same helper 282+ other tests use, mirroring
production's ``scoped_session_factory.py`` shape) — not a hand-built
EventLog that bypasses the real construction path.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.config import AuditEventsConfig
from reyn.core.events.backend import DiscardEventBackend, LocalEventBackend
from reyn.core.events.events import EventLog
from reyn.schemas.models import Event
from tests._support.agent_session import make_session
from tests._support.events import settle


class _RaisingBackend:
    def write(self, _event) -> None:
        raise RuntimeError("backend write failed")

    def declare_gaps(self) -> list[str]:
        return []


# ── 1. discard preserves subscriber delivery ──────────────────────────────


@pytest.mark.asyncio
async def test_discard_backend_subscribers_still_receive_events() -> None:
    """Tier 2: witness ① — with backend=discard, a subscriber still gets
    every emitted event. The witness is the subscriber's own received list,
    not "emit returned an Event".

    #4961 C: dispatch moved off of `emit()`'s own (synchronous) caller
    onto a queue-consumer task — `emit()` still returns synchronously, but
    subscriber delivery is now a SEPARATE async step, so this test awaits
    `log.drain()` after emitting (deterministic regardless of how many
    events are queued — see `EventLog.drain()`'s own docstring for why a
    bare `asyncio.sleep(0)` was rejected as non-deterministic)."""
    log = EventLog(backend=DiscardEventBackend())
    received = []
    log.add_subscriber(received.append)

    e1 = log.emit("tool_executed", op="read")
    e2 = log.emit("tool_executed", op="read")
    await log.drain()

    assert received == [e1, e2]


def test_discard_backend_declares_its_gap() -> None:
    """Tier 2: contract 2 — discard names what it does NOT retain, so a
    consumer (`reyn events`/support-bundle/dogfood_trace) can tell "empty"
    apart from "unsupported"."""
    backend = DiscardEventBackend()
    gaps = backend.declare_gaps()
    assert gaps
    assert all(isinstance(g, str) and g for g in gaps)


def test_local_backend_declares_no_gaps_for_non_delta_kinds() -> None:
    """Tier 2: contract 2, accept-side — every OTHER event kind is retained
    unchanged (#4960's coalescing is agent_delta-specific), witnessed
    directly by writing a non-agent_delta event and reading it back from
    the real store, not by an empty declare_gaps() list (#4960 correctly
    added one real gap — see test_local_backend_declares_the_4960_agent_
    delta_gap below for that gap's own witness)."""
    written = []

    class _RecordingStore:
        def write(self, event) -> None:
            written.append(event)

    backend = LocalEventBackend(_RecordingStore())
    backend.write(Event(type="tool_executed", data={"op": "read"}))
    assert len(written) >= 1 and written[0].type == "tool_executed"


def test_local_backend_declares_the_4960_agent_delta_gap() -> None:
    """Tier 1: #4960 — contract 2 requires this backend to NAME what it no
    longer retains per-fragment now that agent_delta is coalesced;
    declare_gaps() returning [] here would be the exact "declared no gaps
    while actually having one" defect contract 2 exists to prevent."""
    class _StubStore:
        def write(self, _event) -> None:
            pass

    backend = LocalEventBackend(_StubStore())
    gaps = backend.declare_gaps()
    assert gaps, "expected a declared gap for agent_delta coalescing (#4960)"
    assert any("agent_delta" in g for g in gaps)


# ── 2. discard preserves audit_seq continuity ──────────────────────────────


def test_discard_backend_audit_seq_still_monotonic() -> None:
    """Tier 2: witness ② — audit_seq keeps incrementing under discard; a
    subscriber can still detect a gap by watching this number, even though
    nothing is written to disk."""
    log = EventLog(backend=DiscardEventBackend())
    e1 = log.emit("tool_executed", op="read")
    e2 = log.emit("tool_executed", op="read")
    e3 = log.emit("tool_executed", op="read")
    assert (e1.data["audit_seq"], e2.data["audit_seq"], e3.data["audit_seq"]) == (1, 2, 3)


# ── 3. backend failure isolation (prohibition ③, both directions) ────────


@pytest.mark.asyncio
async def test_a_raising_backend_does_not_stop_subscriber_delivery() -> None:
    """Tier 2: witness ③ / falsify ④ — a backend that raises on write()
    must not prevent subscribers from receiving the event. This is the
    test that goes RED if a future edit ever makes the backend "just
    another subscriber" — inserted somewhere in ``self._subscribers``
    instead of called separately, BEFORE that loop, with its own
    try/except (see backend.py's module docstring: #4961 A gave the
    subscriber loop its own per-subscriber isolation too, but that does
    NOT make position in the list a safe substitute for the backend's
    unconditional, order-independent guarantee).

    #4961 C: yields once after emit — see the sibling test above for why."""
    log = EventLog(backend=_RaisingBackend())
    received = []
    log.add_subscriber(received.append)

    emitted = log.emit("tool_executed", op="read")
    await log.drain()

    assert received == [emitted]


def test_a_raising_subscriber_does_not_stop_the_backend_from_having_written() -> None:
    """Tier 2: witness ③, the other direction — a raising subscriber must
    not prevent the backend from having already written (ordering
    guarantee: backend runs BEFORE the subscriber loop)."""
    written = []

    class _RecordingBackend:
        def write(self, event) -> None:
            written.append(event)

        def declare_gaps(self) -> list[str]:
            return []

    def _raising_subscriber(_event) -> None:
        raise RuntimeError("subscriber failed")

    log = EventLog(backend=_RecordingBackend())
    log.add_subscriber(_raising_subscriber)

    # #4961 A: the subscriber loop now isolates each subscriber's own
    # failure (previously it did not, and the raise propagated all the
    # way out of emit() — this test used to assert exactly that
    # propagation; #4961 A closed it, so this test's OWN premise changed).
    # What still matters here, unaffected by #4961 A: the backend, called
    # BEFORE the subscriber loop, has already written regardless of
    # whether a later subscriber raises OR that raise is now caught.
    log.emit("tool_executed", op="read")

    assert written and written[0].type == "tool_executed"


# ── 4. backend is a settable singleton, not a subscriber-list entry ──────


def test_backend_property_reflects_the_active_backend() -> None:
    """Tier 2: public read-only view — a caller (`reyn events` CLI) reads
    the active backend without reaching into private state."""
    backend = DiscardEventBackend()
    log = EventLog(backend=backend)
    assert log.backend is backend


@pytest.mark.asyncio
async def test_set_backend_swaps_without_touching_subscribers() -> None:
    """Tier 2: `set_backend` (used by `Session.set_events_dir`'s live
    re-key) replaces the backend only — every subscriber registered before
    the swap keeps receiving events after it.

    #4961 C: yields once after emit — see the file's first test for why."""
    log = EventLog(backend=DiscardEventBackend())
    received = []
    log.add_subscriber(received.append)

    before_swap = log.emit("tool_executed", op="read")
    log.set_backend(DiscardEventBackend())
    after_swap = log.emit("tool_executed", op="read")
    await log.drain()

    assert received == [before_swap, after_swap]


@pytest.mark.asyncio
async def test_no_backend_omits_write_side_entirely() -> None:
    """Tier 2: accept-side — None (the pre-PR-2 default) means no write
    side at all; emit + subscriber dispatch behave exactly as before this
    PR existed.

    #4961 C: yields once after emit — see the file's first test for why."""
    log = EventLog()  # backend=None, the default
    received = []
    log.add_subscriber(received.append)
    emitted = log.emit("tool_executed", op="read")
    await log.drain()
    assert received == [emitted]
    assert log.backend is None


# ── 5. config: `audit_events.backend` parses to a real EventBackend ──────


def test_audit_events_config_backend_defaults_to_local() -> None:
    """Tier 2: default preserves current behavior — no operator action
    needed to keep the pre-PR-2 shape."""
    assert AuditEventsConfig().backend == "local"


def test_audit_events_config_backend_accepts_discard() -> None:
    """Tier 2: `discard` is a real, wired value."""
    cfg = AuditEventsConfig(backend="discard")
    assert cfg.backend == "discard"


# ── 6. Session-level, real production path (not a hand-built EventLog) ────


@pytest.mark.asyncio
async def test_session_with_discard_backend_still_delivers_to_real_subscribers(
    tmp_path,
) -> None:
    """Tier 2: the real production-path witness — driven through
    `make_session` (`tests/_support/agent_session.py`, mirroring production's
    `scoped_session_factory.py` construction shape: builds a real `Agent`,
    calls `Session(agent=agent, **kwargs)`), not a hand-built EventLog that
    bypasses `_build_audit_event_bundle` / `_build_events_backend`.

    With `audit_events.backend=discard` threaded through at construction, a
    subscriber attached via `subscribe_audit_events` (Session's own public
    API — the same method `ChatLifecycleForwarder` et al. are wired through
    inside `_build_audit_event_bundle`) still receives an event emitted
    through the session's real `_audit_events.emit()` (the same EventLog
    `_build_events_backend` wired the discard backend into — accessing it
    here mirrors `tests/core/test_session_lifecycle_events_1800.py`'s own
    established pattern for observing session-level emits, not a private-
    state assertion: `.emit()` is EventLog's public production entry point).
    """
    session = make_session(
        agent_name="discard-backend-test",
        workspace_state_dir=tmp_path / ".reyn",
        events_config=AuditEventsConfig(backend="discard"),
    )

    received = []
    session.subscribe_audit_events(received.append)
    emitted = session._audit_events.emit("test_event", foo="bar")
    await settle(session)  # #4961 C: see the file's first test

    assert received == [emitted]
    assert emitted.data.get("foo") == "bar"


# ── 5. #4961 A — per-subscriber isolation in the dispatch loop ───────────


@pytest.mark.asyncio
async def test_a_raising_subscriber_does_not_skip_a_later_subscriber(caplog) -> None:
    """Tier 1: #4961 A — the actual gap this fix closes. Before it, a
    raising subscriber aborted the ENTIRE dispatch loop, silently
    skipping every subscriber registered AFTER it (registration order is
    not under the caller's control at the emit() call site — e.g. the
    OTEL exporter could sit after a raising transport forwarder). The
    witness is the LATER subscriber's own received list — "no exception
    escaped" is NOT sufficient (that would also be true of a loop that
    quietly skipped it), so this asserts actual delivery. Also witnesses
    lead-coder's second acceptance condition in the same test: the
    failure is LOGGED, not silently swallowed (#4961's own framing is
    "silent" — closing delivery while going quiet would trade one
    unobservable gap for another).

    #4961 C: dispatch moved to a queue-consumer task — yields once after
    emit (see the file's first test) so the consumer actually runs before
    the assertions."""
    import logging

    log = EventLog()
    later_received = []

    def _raising_subscriber(_event) -> None:
        raise RuntimeError("earlier subscriber failed")

    log.add_subscriber(_raising_subscriber)
    log.add_subscriber(later_received.append)

    with caplog.at_level(logging.ERROR, logger="reyn.core.events.events"):
        emitted = log.emit("tool_executed", op="read")
        await log.drain()

    assert later_received == [emitted], (
        "the subscriber registered AFTER the raising one must still "
        "receive the event — this is #4961 A's own gap, closed"
    )
    assert any(
        "event subscriber failed" in r.getMessage() for r in caplog.records
    ), "a raising subscriber's failure must be logged, not silently discarded"


@pytest.mark.asyncio
async def test_subscribers_are_still_dispatched_in_registration_order() -> None:
    """Tier 1: #4961 A's other acceptance condition (lead-coder) —
    per-subscriber isolation must not reorder dispatch. agui/endpoint.py's
    own barrier ordering (co-vet #3310 N3(a)) depends on `add_subscriber`
    staying synchronous and subscribers firing in registration order.

    #4961 C: awaits `log.drain()` after emit — see the file's first test
    for why a bare yield was rejected."""
    log = EventLog()
    order: list[str] = []
    log.add_subscriber(lambda _e: order.append("first"))
    log.add_subscriber(lambda _e: order.append("second"))
    log.add_subscriber(lambda _e: order.append("third"))

    log.emit("tool_executed", op="read")
    await log.drain()

    assert order == ["first", "second", "third"]


# ── #4965/#4966: consumer cancel-flush — delivery survives cancellation ───


@pytest.mark.asyncio
async def test_dispatch_consumer_flushes_queued_event_on_cancellation() -> None:
    """Tier 2: #4966 (architect ruling) — a queued-but-not-yet-dispatched
    event is still delivered to subscribers even if the dispatch consumer
    task is cancelled while genuinely suspended mid-loop. This closes the
    class ``asyncio.run(coro)``'s own teardown belongs to: it cancels
    every still-running task (this EventLog's consumer included) BEFORE
    the wrapped coroutine's own task is considered fully done — without
    this flush, whatever is queued at that instant is lost forever, not
    merely delayed.

    Cancellation is given directly as an INPUT (``task.cancel()`` on the
    real consumer task), not awaited via a sleep — the six-questions
    review's own warning against a duration the assertion depends on
    (CLAUDE.md testing policy: "no sleep the assertion depends on, in
    EITHER direction"). ``await events.drain()`` first (not a sleep)
    deterministically lets the consumer process an initial event and
    settle into its normal suspended-at-``queue.get()`` state — the
    REALISTIC shape cancellation actually lands in production (an
    in-flight consumer, not one that never started) — before a second
    event is queued and the task is cancelled while genuinely waiting for
    it.

    Falsifying witness (lead-coder's six-questions #4 concern, verified
    directly, not asserted): the SAME synthetic probe with the
    `task.cancel()`-before-first-run shape (cancelling before the
    consumer ever executes at all) passes on BOTH the pre-#4966 and
    post-#4966 code — that shape does not exercise this fix and would be
    a non-discriminating test; this test's own emit-drain-emit-cancel
    sequence was confirmed, by running it against the pre-#4966
    consumer (no `except asyncio.CancelledError:` flush), to fail
    (`delivered == ['warmup']`, missing 'probe') — it does discriminate."""
    events = EventLog()
    delivered: list = []
    events.add_subscriber(lambda e: delivered.append(e.type))

    events.emit("warmup")
    await events.drain()  # consumer processed 'warmup', now suspended at queue.get()

    events.emit("probe")  # queued; consumer is suspended waiting for exactly this
    task = events._consumer_task
    assert task is not None

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert delivered == ["warmup", "probe"], (
        "the event queued right before cancellation must still be "
        f"delivered — got {delivered}"
    )


# ── #4966 (architect ruling): `_force_inline` bounded to ONE call site ───


def test_force_inline_has_exactly_one_call_site_in_src() -> None:
    """Tier 2: the bounding subject for ``EventLog.__init__``'s private
    ``_force_inline`` parameter — architect's ruling on why a TEST (not a
    gate) is the right shape here.

    ``_force_inline`` declares "no owner will ever call drain()/
    stop_dispatch() on this EventLog" at construction time — information
    the mechanism itself cannot infer (an owner can attach later; a
    mechanism that GUESSES "unowned" from "no loop is running right now"
    fails SILENTLY the moment that guess is wrong, the exact shape this
    arc kept re-finding). A declaration is honest; keeping it private
    (``_force_inline``, not a public constructor parameter) and pinned to
    ONE legitimate call site (``emit_cli_event``'s own one-off,
    no-continuity EventLog) is what keeps it from becoming a general
    escape hatch that re-opens the queue/consumer coupling #4961 C
    removed. This is a single enumerable FACT ("how many call sites pass
    it"), not an open population a static gate would need to sweep for —
    hence a test, not a gate (architect's own distinction).

    Real collaborator: parses the actual `src/reyn/core/events/events.py``
    source text (AST, not a regex/substring match — a comment merely
    MENTIONING ``_force_inline=True`` in prose must not count as a call
    site), not a mock of it.
    """
    import ast

    from tests._support.paths import REPO_ROOT

    src = REPO_ROOT / "src" / "reyn" / "core" / "events" / "events.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    call_sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.keyword)
        and node.arg == "_force_inline"
        and isinstance(node.value, ast.Constant)
        and node.value.value is True
    ]
    assert call_sites, (
        "expected the ONE known `_force_inline=True` call site "
        "(emit_cli_event's own one-off EventLog) — found none. Either "
        "emit_cli_event's construction site was edited to drop the flag, "
        "or this test's own detection broke."
    )
    extra_sites = call_sites[1:]
    assert not extra_sites, (
        "expected exactly ONE `_force_inline=True` call site "
        f"(emit_cli_event's own one-off EventLog); found "
        f"{len(extra_sites)} more, at lines "
        f"{[n.lineno for n in extra_sites]}. `_force_inline` declares "
        "'no owner will ever drain this EventLog' — a construction-time "
        "fact the mechanism cannot infer safely (see EventLog.__init__'s "
        "own docstring). If you have a genuine second one-off, "
        "no-continuity EventLog with no owner, that is a real, separate "
        "instance of this same fact — update this bound deliberately, "
        "with the same justification, rather than letting the count "
        "silently grow."
    )


# ── #4966: a stale consumer/queue across separate asyncio.run() calls ────


def test_second_asyncio_run_through_the_same_eventlog_still_delivers() -> None:
    """Tier 2: found via CI (test_catalog_search_actions_emits_complete_on_
    query_failure, which drives 4 separate ``asyncio.run()`` calls through
    one ``EventLog``) — driving the SAME EventLog through two SEPARATE
    ``asyncio.run()`` calls (each opening and closing its own loop) must
    still deliver events emitted in the second call.

    Falsifying witness (architect/lead-coder's own six-questions concern,
    e2e-coder's own TESTS-READ finding): reverting the fix to
    ``_ensure_consumer_started``'s old ``if self._consumer_task is None``
    check DOES turn this red — the mechanism this test targets is real —
    but the observed failure mode is NOT "the second call's events queue
    forever with nobody draining them". ``_dispatch_queue`` is ALSO
    loop-bound (``asyncio.Queue`` binds to whichever loop first calls one
    of its async methods), so with the old check treating the FIRST
    (now-dead) loop's task as "already running", no fresh consumer is
    even attempted for the second loop — but `drain()`'s own
    `_ensure_consumer_started()` call still runs and still touches the
    STALE queue via its own internals, raising ``RuntimeError: Event
    loop is closed`` synchronously rather than hanging or silently
    dropping. Measured directly (reverted the fix, ran this test): that
    is the actual traceback, not a silent no-op. This is not a
    test-authoring gap (this test drives no `emit()`-then-read race a
    `settle()` could fix) — it is architect-ruled mechanism territory:
    `_ensure_consumer_started` now asks `task.done()` /
    `task.get_loop().is_closed()`, a fact, not a guess."""
    log = EventLog()
    delivered: list = []
    log.add_subscriber(lambda e: delivered.append(e.type))

    async def _first_run() -> None:
        log.emit("first")
        await log.drain()

    asyncio.run(_first_run())
    assert delivered == ["first"]

    async def _second_run() -> None:
        log.emit("second")
        await log.drain()

    asyncio.run(_second_run())
    assert delivered == ["first", "second"], (
        "the second asyncio.run()'s own event must still be delivered — "
        f"got {delivered}"
    )
