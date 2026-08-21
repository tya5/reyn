"""Tier 1/2: #4666 ① — ``agent_delta``'s durable `text` field (the
streamed reply content itself) is opt-in, default OFF, its OWN config
knob (``audit_events.agent_delta_include_text``) — deliberately NOT tied
to #4960's own coalescing knobs (owner ruling: each content opt-in gets
its own toggle, never a single switch covering more than one).

Scope: this is #4666's item ① only (agent_delta). Items ② (completed
conversation) and ③ (user input) are separate, later work — ② is blocked
on an owner clarification, ③ needs its own full census — and are NOT
touched here.

Live subscriber dispatch (TUI/AG-UI) is unaffected regardless of this
flag: every raw fragment (`text` included) still reaches subscribers —
only what LocalEventBackend persists to disk is conditional. Real
LocalEventBackend + a real injectable clock throughout (this repo's own
Callable[[], float] idiom), no unittest.mock.
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
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _delta(chain_id: str = "c1", text: str = "the reply content") -> Event:
    return Event(type="agent_delta", data={"chain_id": chain_id, "text": text})


# ── default (off): text dropped, everything else kept ──────────────────


def test_default_drops_text_from_the_coalesced_record() -> None:
    """Tier 1: #4666 — with no explicit agent_delta_include_text
    argument, the default is False (matches AuditEventsConfig's own
    default), and the durable record does NOT carry `text`."""
    store = _RecordingStore()
    backend = LocalEventBackend(store, agent_delta_coalesce_fragments=3, clock=_FakeClock())
    for _ in range(3):
        backend.write(_delta())

    assert store.written, "sanity: a durable record was written"
    assert "text" not in store.written[0].data


def test_default_still_keeps_the_non_content_fields() -> None:
    """Tier 1: #4666's own non-negotiable — dropping `text` must NOT drop
    chain_id/round_index/coalesced_fragment_count/audit_seq. #4960's own
    reason for coalescing (cost accountability: "a partial reply of N
    fragments existed") depends entirely on these surviving; losing them
    too would silently reopen the gap #4960 closed."""
    store = _RecordingStore()
    backend = LocalEventBackend(store, agent_delta_coalesce_fragments=5, clock=_FakeClock())
    for _ in range(5):
        backend.write(Event(
            type="agent_delta",
            data={"chain_id": "c1", "text": "x", "round_index": 2},
        ))

    record = store.written[0]
    assert record.data.get("chain_id") == "c1"
    assert record.data.get("round_index") == 2
    assert record.data.get("coalesced_fragment_count") == 5
    assert "text" not in record.data


def test_terminal_flush_also_drops_text_by_default() -> None:
    """Tier 2: #4666 — the terminal-flush path (flush_pending_deltas)
    goes through the SAME _persist_coalesced_delta helper as the N/T
    paths, so the opt-in applies there too, not just the count/interval
    branch."""
    store = _RecordingStore()
    backend = LocalEventBackend(store, agent_delta_coalesce_fragments=100, clock=_FakeClock())
    for _ in range(4):
        backend.write(_delta(chain_id="c1"))
    assert store.written == [], "sanity: below the fragment threshold, nothing durable yet"

    backend.flush_pending_deltas("c1")

    assert store.written, "expected the terminal flush's own durable record"
    assert "text" not in store.written[0].data
    assert store.written[0].data.get("coalesced_fragment_count") == 4


# ── opt-in (on): text kept ───────────────────────────────────────────────


def test_opting_in_keeps_the_text_field() -> None:
    """Tier 1: #4666 — agent_delta_include_text=True restores the text
    field on the durable record (the pre-#4666 behavior)."""
    store = _RecordingStore()
    backend = LocalEventBackend(
        store, agent_delta_coalesce_fragments=3,
        agent_delta_include_text=True, clock=_FakeClock(),
    )
    for _ in range(3):
        backend.write(_delta(text="the reply content"))

    assert store.written[0].data.get("text") == "the reply content"


def test_opting_in_applies_to_the_terminal_flush_too() -> None:
    """Tier 2: #4666 — same coverage check as the default-off terminal-
    flush test above, opposite direction."""
    store = _RecordingStore()
    backend = LocalEventBackend(
        store, agent_delta_coalesce_fragments=100,
        agent_delta_include_text=True, clock=_FakeClock(),
    )
    for _ in range(4):
        backend.write(_delta(chain_id="c1", text="partial reply"))

    backend.flush_pending_deltas("c1")

    assert store.written[0].data.get("text") == "partial reply"


# ── live dispatch is unaffected either way (the raw event, not the record) ──


def test_the_raw_event_passed_to_write_keeps_its_own_text_regardless() -> None:
    """Tier 1: #4666 — this flag governs only what gets PERSISTED
    (_persist_coalesced_delta constructs a NEW, separate Event for the
    durable record — see #4960's own "never mutate the original" test).
    The RAW event object `write()` was called with — the same object
    EventLog.emit()'s subscriber loop already dispatched to TUI/AG-UI
    BEFORE backend.write() ever ran — is never touched, with the opt-in
    off or on."""
    store = _RecordingStore()
    backend = LocalEventBackend(store, agent_delta_coalesce_fragments=1, clock=_FakeClock())
    raw = _delta(text="live text unaffected")

    backend.write(raw)

    assert raw.data.get("text") == "live text unaffected", (
        "the caller's own raw event object must never be mutated by the "
        "opt-in decision — only a NEW durable-record object may differ"
    )


# ── declare_gaps() — dynamic, not static (architect's #4960 ruling) ─────


def test_declare_gaps_names_the_text_drop_when_off() -> None:
    """Tier 1: #4666 — with the default (off), declare_gaps() must
    explicitly name that `text` is dropped by CONFIG, distinct from
    #4960's own coalescing gap — a reader must be able to tell "declared
    and dropped" apart from "never existed" (architect's #4960 ruling on
    this exact backend)."""
    backend = LocalEventBackend(_RecordingStore(), clock=_FakeClock())
    gaps = backend.declare_gaps()
    assert any("text" in g and "agent_delta_include_text" in g for g in gaps), (
        f"expected a gap explicitly naming the config-driven text drop; got {gaps!r}"
    )


def test_declare_gaps_does_not_claim_the_text_gap_when_on() -> None:
    """Tier 1: #4666 — the inverse: once opted in, declare_gaps() must
    NOT claim the text-content gap (it would be false — text IS
    retained). The #4960 coalescing gap (a DIFFERENT, always-true claim)
    is unaffected and still present."""
    backend = LocalEventBackend(
        _RecordingStore(), agent_delta_include_text=True, clock=_FakeClock(),
    )
    gaps = backend.declare_gaps()
    assert not any("agent_delta_include_text" in g for g in gaps), (
        f"opted-in backend must not claim the text-content gap; got {gaps!r}"
    )
    assert any("agent_delta" in g for g in gaps), (
        "the #4960 per-fragment coalescing gap must still be declared "
        "regardless of the text opt-in — it is a separate fact"
    )
