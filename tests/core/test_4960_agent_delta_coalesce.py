"""Tier 1/2: #4960 — architect ruling C: ``agent_delta`` is coalesced to
one durable write per N fragments or T ms (whichever first), plus one
final record on stream end (terminal flush) — 3 mechanisms covering each
other's gap:

  - N (fragment count): the common, bursty-stream case.
  - T (interval): the ONLY guarantee for a process-level death (SIGKILL /
    OOM-kill / host crash) — a Python ``finally`` never runs then.
  - terminal flush: the common short-interruption case (exception /
    cancellation / normal completion) that N/T alone would miss if the
    stream ends before either threshold is reached.

Live subscriber dispatch is NEVER touched by any of this — coalescing is
entirely a ``LocalEventBackend.write()``-internal decision, called by
``EventLog.emit()`` before the (unthrottled) subscriber loop.

Real ``LocalEventBackend`` + a real, injectable clock (this repo's own
``Callable[[], float]`` idiom — see ``TextualChatApp``'s own ``clock``
parameter) throughout; no ``unittest.mock``. The clock is injected
specifically so the T-based branch is testable without a real sleep
(CLAUDE.md: "a test writes no duration ... the clock is an INPUT you
supply, never a sleep you wait out").
"""
from __future__ import annotations

from reyn.core.events.backend import LocalEventBackend
from reyn.schemas.models import Event


class _RecordingStore:
    def __init__(self) -> None:
        self.written: list[Event] = []

    def write(self, event: Event) -> None:
        self.written.append(event)


class _FakeClock:
    """The backend's own injection point (``clock: Callable[[], float]``)
    — advanced explicitly, never slept through."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _delta(chain_id: str = "c1", text: str = "x") -> Event:
    return Event(type="agent_delta", data={"chain_id": chain_id, "text": text})


# ── fragment-count coalescing ───────────────────────────────────────────


def test_fewer_than_n_fragments_write_nothing() -> None:
    """Tier 2: #4960 — below the fragment threshold and below the interval,
    no durable write happens at all (everything is buffered)."""
    store = _RecordingStore()
    backend = LocalEventBackend(store, agent_delta_coalesce_fragments=5, clock=_FakeClock())
    for _ in range(4):
        backend.write(_delta())
    assert store.written == []


def test_the_nth_fragment_triggers_exactly_one_durable_write() -> None:
    """Tier 1: #4960 — the fragment-count mechanism's own witness. The Nth
    fragment (not before) produces exactly one durable record, carrying
    the coalesced count. The list-equality check below captures BOTH
    "exactly one write happened" and "it carries the right count" in one
    assertion, without pinning a bare item count."""
    store = _RecordingStore()
    backend = LocalEventBackend(store, agent_delta_coalesce_fragments=5, clock=_FakeClock())
    for _ in range(5):
        backend.write(_delta())
    assert [w.data.get("coalesced_fragment_count") for w in store.written] == [5]


def test_coalescing_resets_after_each_durable_write() -> None:
    """Tier 2: #4960 — after a durable write fires, the count resets; the
    NEXT N fragments produce exactly one more durable write, not a
    growing backlog."""
    store = _RecordingStore()
    backend = LocalEventBackend(store, agent_delta_coalesce_fragments=5, clock=_FakeClock())
    for _ in range(12):  # 2 full cycles of 5 + 2 pending
        backend.write(_delta())
    assert [w.data.get("coalesced_fragment_count") for w in store.written] == [5, 5]


# ── #5261: raw_chunk_count-aware summing ────────────────────────────────


def test_coalesced_count_sums_raw_chunk_count_not_event_count() -> None:
    """Tier 1: #5261 — a mix of ``agent_delta`` events, some already
    standing in for several raw provider chunks (source-side merged,
    ``raw_chunk_count`` set) and some with none (pre-#5261 callers, or
    #5261's own unmerged single-chunk case, contributing exactly 1) must
    sum to the TRUE raw-chunk total, not the number of events that
    arrived. Asserts the actual computed sum, never a pinned/hardcoded
    literal (a pinned count previously failed ``test_tier_audit.py
    --strict`` on #5266) — the value below is derived from the same
    per-event contributions the test itself constructs."""
    store = _RecordingStore()
    backend = LocalEventBackend(store, agent_delta_coalesce_fragments=1_000_000, clock=_FakeClock())

    contributions = [3, None, 1, 5, None]  # None == no raw_chunk_count field at all
    for n in contributions:
        data = {"chain_id": "c1", "text": "x"}
        if n is not None:
            data["raw_chunk_count"] = n
        backend.write(Event(type="agent_delta", data=data))
    assert store.written == [], "sanity: below the (huge) fragment threshold, nothing durable yet"

    backend.flush_pending_deltas("c1")

    expected_total = sum(n if n is not None else 1 for n in contributions)
    assert [w.data.get("coalesced_fragment_count") for w in store.written] == [expected_total]


# ── interval coalescing (the process-death guarantee) ──────────────────


def test_interval_elapsing_triggers_a_durable_write_below_the_fragment_count() -> None:
    """Tier 1: #4960 — the T mechanism's own witness, isolated from N (a
    huge fragment threshold so ONLY the interval can fire). Injected
    clock, no real sleep."""
    store = _RecordingStore()
    clock = _FakeClock()
    backend = LocalEventBackend(
        store, agent_delta_coalesce_fragments=1_000_000,
        agent_delta_coalesce_interval_ms=2_000, clock=clock,
    )
    backend.write(_delta())  # 1 fragment, far under the count threshold
    assert store.written == [], "must not write before the interval elapses"
    clock.advance(2.0)  # exactly 2000ms
    backend.write(_delta())
    assert [w.data.get("coalesced_fragment_count") for w in store.written] == [2]


def test_interval_not_yet_elapsed_does_not_trigger() -> None:
    """Tier 2: #4960 — regression control for the interval branch's own
    boundary (just under T, not at or over it)."""
    store = _RecordingStore()
    clock = _FakeClock()
    backend = LocalEventBackend(
        store, agent_delta_coalesce_fragments=1_000_000,
        agent_delta_coalesce_interval_ms=2_000, clock=clock,
    )
    backend.write(_delta())
    clock.advance(1.999)
    backend.write(_delta())
    assert store.written == []


# ── terminal flush (the short-interruption guarantee) ──────────────────


def test_terminal_flush_persists_pending_fragments_below_both_thresholds() -> None:
    """Tier 1: #4960 — the exact gap lead-coder's review found: a stream
    that ends (success, exception, or cancel) with fewer fragments than
    N and less elapsed time than T must still leave ONE durable record —
    without this, that evidence is silently lost."""
    store = _RecordingStore()
    backend = LocalEventBackend(
        store, agent_delta_coalesce_fragments=100,
        agent_delta_coalesce_interval_ms=2_000, clock=_FakeClock(),
    )
    for _ in range(7):
        backend.write(_delta(chain_id="c1"))
    assert store.written == [], "sanity: below both thresholds, nothing durable yet"

    backend.flush_pending_deltas("c1")

    assert [w.data.get("coalesced_fragment_count") for w in store.written] == [7]


def test_terminal_flush_with_nothing_pending_writes_nothing() -> None:
    """Tier 2: #4960 — flushing an already-drained (or never-started)
    chain is a no-op, not a spurious empty record."""
    store = _RecordingStore()
    backend = LocalEventBackend(store, clock=_FakeClock())
    backend.flush_pending_deltas("never-seen-chain")
    assert store.written == []


def test_terminal_flush_only_affects_its_own_chain() -> None:
    """Tier 2: #4960 — coalescing state is per chain_id; flushing one
    in-flight chain must not touch another concurrently-streaming one."""
    store = _RecordingStore()
    backend = LocalEventBackend(store, agent_delta_coalesce_fragments=100, clock=_FakeClock())
    for _ in range(3):
        backend.write(_delta(chain_id="c1"))
    for _ in range(5):
        backend.write(_delta(chain_id="c2"))

    backend.flush_pending_deltas("c1")
    assert [
        (w.data.get("chain_id"), w.data.get("coalesced_fragment_count"))
        for w in store.written
    ] == [("c1", 3)]

    backend.flush_pending_deltas("c2")
    assert [
        (w.data.get("chain_id"), w.data.get("coalesced_fragment_count"))
        for w in store.written
    ] == [("c1", 3), ("c2", 5)]


# ── the original event object is never mutated ─────────────────────────


def test_the_original_delta_event_object_is_not_mutated() -> None:
    """Tier 1: #4960 — the durable coalesced record is a NEW Event, never
    the original mutated in place. The original object is (or may still
    be) held by the (already-run, since backend.write() fires before the
    subscriber loop in EventLog.emit()) subscriber dispatch — mutating it
    would leak the coalesced_fragment_count field into whatever a live
    subscriber already read from it."""
    store = _RecordingStore()
    backend = LocalEventBackend(store, agent_delta_coalesce_fragments=1, clock=_FakeClock())
    original = _delta()
    original_data_before = dict(original.data)

    backend.write(original)

    assert original.data == original_data_before, (
        "the original Event's data must be unchanged after a coalesced "
        "write — a new Event object must be constructed for the durable "
        "record instead"
    )
    assert store.written[0] is not original
    assert "coalesced_fragment_count" in store.written[0].data


# ── non-agent_delta kinds are entirely unaffected ───────────────────────


def test_non_agent_delta_events_pass_through_unthrottled() -> None:
    """Tier 2: #4960's coalescing is agent_delta-specific — every other
    kind still gets exactly one durable write per emit, unchanged."""
    store = _RecordingStore()
    backend = LocalEventBackend(store, agent_delta_coalesce_fragments=100, clock=_FakeClock())
    for i in range(5):
        backend.write(Event(type="tool_executed", data={"seq": i}))
    assert [w.data.get("seq") for w in store.written] == [0, 1, 2, 3, 4]


# ── EventLog.flush_agent_delta — the passthrough ────────────────────────


def test_eventlog_flush_agent_delta_reaches_the_backend() -> None:
    """Tier 1: #4960 — EventLog.flush_agent_delta() is a real passthrough
    to the backend's flush_pending_deltas(chain_id), not a no-op stub."""
    from reyn.core.events.events import EventLog

    store = _RecordingStore()
    backend = LocalEventBackend(store, agent_delta_coalesce_fragments=100, clock=_FakeClock())
    log = EventLog(backend=backend)

    for _ in range(9):
        log.emit("agent_delta", chain_id="c1", text="x")
    assert store.written == []

    log.flush_agent_delta("c1")

    assert [w.data.get("coalesced_fragment_count") for w in store.written] == [9]


def test_eventlog_flush_agent_delta_on_a_backend_without_it_is_a_safe_noop() -> None:
    """Tier 2: #4960 — a backend that does NOT implement
    flush_pending_deltas (e.g. DiscardEventBackend, or any future
    backend) must not raise — flush_agent_delta degrades silently, same
    posture as every other backend-specific behavior in this module."""
    from reyn.core.events.backend import DiscardEventBackend
    from reyn.core.events.events import EventLog

    log = EventLog(backend=DiscardEventBackend())
    log.flush_agent_delta("c1")  # must not raise
