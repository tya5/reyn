"""Tier 1/2: #4666 ② — the completed model→user text is opt-in, default
OFF, its OWN config knob (``audit_events.completed_response_include_text``)
— deliberately NOT tied to ①'s ``agent_delta_include_text`` (owner ruling:
each content opt-in gets its own toggle).

Two events are gated by this ONE knob (architect ruling: "②と③は1つの
やり取りの両端" — the model's question and the user's answer are two
ends of one exchange and must share a knob, or a half-recorded exchange
survives):

  - ``agent_response_committed`` (new kind) — emitted unconditionally from
    ``Session._put_outbox`` filtered on ``msg.kind == "agent"``.
  - ``user_intervention_requested`` (pre-existing kind, ``ask_user.py``) —
    its ``question``/``suggestions``/``options`` fields.

Both fire unconditionally either way; the opt-in only decides whether
``LocalEventBackend.write()`` drops the free-text field(s) from the
DURABLE record — same shape as ①. Live subscriber dispatch is unaffected
regardless of this flag.

Scope: this is #4666's item ② only. Item ③ (user input / the eventual
``user_intervention_received.answer`` opt-in) is separate, later work and
is NOT touched here. The tool-path leak (``tool_called.args`` /
``tool_returned.result`` duplicating the same content) is explicitly OUT
of ②'s scope — see ``docs/reference/runtime/events.md``'s own
``agent_response_committed`` row.

Real ``LocalEventBackend`` + a real ``Session`` (via ``tests._support.
agent_session.make_session``) throughout — no ``unittest.mock``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config.infra import AuditEventsConfig
from reyn.core.events.backend import LocalEventBackend
from reyn.core.events.event_schema import AUDIT_EVENT_KINDS
from reyn.core.events.state_log import StateLog
from reyn.runtime.outbox import OutboxMessage
from reyn.schemas.models import Event
from tests._support.agent_session import make_session
from tests._support.events import collect_events, settle


class _RecordingStore:
    def __init__(self) -> None:
        self.written: list[Event] = []

    def write(self, event: Event) -> None:
        self.written.append(event)


# ── vocabulary: the new kind is declared ─────────────────────────────────


def test_agent_response_committed_is_a_declared_kind() -> None:
    """Tier 1: #4666② — ``agent_response_committed`` must be in the closed
    vocabulary (the AST-census gate, test_audit_event_kind_vocabulary_3410,
    separately checks it has a producer AND an events.md entry — this pins
    only the declaration half from THIS test's own referent)."""
    assert "agent_response_committed" in AUDIT_EVENT_KINDS


# ── LocalEventBackend: default (off) drops only the free-text field(s) ──


def test_default_drops_text_from_agent_response_committed() -> None:
    """Tier 1: #4666② — with no explicit
    completed_response_include_text argument, the default is False
    (matches AuditEventsConfig's own default), and the durable record
    does NOT carry `text`."""
    store = _RecordingStore()
    backend = LocalEventBackend(store)
    backend.write(Event(
        type="agent_response_committed",
        data={"text": "the completed reply", "chain_id": "c1"},
    ))

    assert store.written, "sanity: a durable record was written"
    assert "text" not in store.written[0].data
    assert store.written[0].data.get("chain_id") == "c1", (
        "chain_id must survive the drop — 'a response was committed' "
        "must remain provable without the reply's own content"
    )


def test_default_drops_question_fields_from_user_intervention_requested() -> None:
    """Tier 1: #4666② — same drop, for the SECOND kind this knob gates.
    `intervention_id` (not a free-text field) must survive."""
    store = _RecordingStore()
    backend = LocalEventBackend(store)
    backend.write(Event(
        type="user_intervention_requested",
        data={
            "question": "which file?", "suggestions": ["a.py"],
            "options": ["a.py", "b.py"], "intervention_id": "iv1",
        },
    ))

    record = store.written[0].data
    assert "question" not in record
    assert "suggestions" not in record
    assert "options" not in record
    assert record.get("intervention_id") == "iv1"


def test_opting_in_keeps_text_on_agent_response_committed() -> None:
    """Tier 1: #4666② — completed_response_include_text=True restores
    `text` on the durable record."""
    store = _RecordingStore()
    backend = LocalEventBackend(store, completed_response_include_text=True)
    backend.write(Event(
        type="agent_response_committed",
        data={"text": "the completed reply", "chain_id": "c1"},
    ))

    assert store.written[0].data.get("text") == "the completed reply"


def test_opting_in_keeps_question_fields_on_user_intervention_requested() -> None:
    """Tier 1: #4666② — same, inverse direction, for the second kind."""
    store = _RecordingStore()
    backend = LocalEventBackend(store, completed_response_include_text=True)
    backend.write(Event(
        type="user_intervention_requested",
        data={"question": "which file?", "intervention_id": "iv1"},
    ))

    assert store.written[0].data.get("question") == "which file?"


def test_the_raw_event_passed_to_write_keeps_its_own_text_regardless() -> None:
    """Tier 1: #4666② — this flag governs only what gets PERSISTED (a NEW
    Event is constructed for the durable record). The RAW event object
    `write()` was called with — the same object EventLog.emit()'s
    subscriber loop already dispatched to TUI/AG-UI/any opt-in OTEL
    subscriber BEFORE backend.write() ever ran — is never mutated,
    opt-in off or on."""
    store = _RecordingStore()
    backend = LocalEventBackend(store)
    raw = Event(type="agent_response_committed", data={"text": "live text unaffected"})

    backend.write(raw)

    assert raw.data.get("text") == "live text unaffected"


# ── declare_gaps(): dynamic, not static ──────────────────────────────────


def test_declare_gaps_names_the_completed_response_drop_when_off() -> None:
    """Tier 1: #4666② — with the default (off), declare_gaps() must
    explicitly name the config-driven drop, distinguishable from ①'s own
    agent_delta gap (a reader must tell "declared and dropped" apart
    from "never existed", per architect's #4960 ruling this mirrors)."""
    backend = LocalEventBackend(_RecordingStore())
    gaps = backend.declare_gaps()
    assert any(
        "agent_response_committed" in g and "completed_response_include_text" in g
        for g in gaps
    ), f"expected a gap naming the config-driven drop; got {gaps!r}"


def test_declare_gaps_does_not_claim_the_gap_when_on() -> None:
    """Tier 1: #4666② — inverse: opted in, declare_gaps() must not claim
    this gap (it would be false — the content IS retained). ①'s own
    agent_delta gap is a separate fact and unaffected by this flag."""
    backend = LocalEventBackend(_RecordingStore(), completed_response_include_text=True)
    gaps = backend.declare_gaps()
    assert not any("completed_response_include_text" in g for g in gaps), (
        f"opted-in backend must not claim the ② gap; got {gaps!r}"
    )


# ── config parsing ────────────────────────────────────────────────────────


def test_config_default_is_off() -> None:
    """Tier 1: #4666② — AuditEventsConfig's own default matches the
    backend's own default (both False) — the two constructors must never
    silently drift apart."""
    assert AuditEventsConfig().completed_response_include_text is False


# ── Session._put_outbox: the choke point actually fires ─────────────────


def _make_session(tmp_path: Path, **kwargs):
    return make_session(
        agent_name="alpha",
        state_log=StateLog(tmp_path / "alpha.wal"),
        snapshot_path=tmp_path / "alpha_snapshot.json",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_put_outbox_emits_agent_response_committed_for_kind_agent(tmp_path):
    """Tier 2: #4666② — ``Session._put_outbox`` emits
    ``agent_response_committed`` for a ``kind="agent"`` message, carrying
    the message's own text and chain_id."""
    session = _make_session(tmp_path)
    collected = collect_events(session._audit_events)

    await session._put_outbox(OutboxMessage(
        kind="agent", text="the final reply", meta={"chain_id": "c1"},
    ))
    await settle(session._audit_events)

    committed = [e for e in collected if e.type == "agent_response_committed"]
    assert committed
    assert not committed[1:]
    assert committed[0].data.get("text") == "the final reply"
    assert committed[0].data.get("chain_id") == "c1"


@pytest.mark.asyncio
async def test_put_outbox_does_not_emit_for_non_agent_kinds(tmp_path):
    """Tier 2: #4666② — a transient kind (``status``) must NOT trigger
    ``agent_response_committed`` — this event is scoped to what the
    model actually said to the user, not every outbox traffic kind."""
    session = _make_session(tmp_path)
    collected = collect_events(session._audit_events)

    await session._put_outbox(OutboxMessage(kind="status", text="thinking..."))
    await settle(session._audit_events)

    assert not any(e.type == "agent_response_committed" for e in collected)
