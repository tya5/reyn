"""Tier 1/2: #4666 ③ — the content-bearing field on 6 user-input audit-
event kinds is opt-in, default OFF, its OWN config knob
(``audit_events.user_input_include_text``) — deliberately NOT tied to
①'s ``agent_delta_include_text`` (owner ruling: each content opt-in gets
its own toggle).

The 6 kinds (AST census, lead-coder, #4666 — an earlier pass found 3 and
undercounted) and the field dropped on each:

    user_submitted                 -> text
    user_message_received          -> text
    intervention_answer_submitted  -> text
    user_answered_intervention     -> answer_text
    user_intervention_received     -> answer
    router_retry_exhausted         -> user_message

Scope: this is #4666's item ③ only. Item ② (completed conversation) is
separate, later work. A KNOWN gap this flag does NOT close (architect +
lead-coder, #4666): ask_user's question/answer also reach the audit log
unconditionally via ``tool_called.args``/``tool_returned.result`` (a
different emit path, ``dispatch_tool``) — not touched here, and not
claimed closed by any test below.

Live subscriber dispatch (TUI/AG-UI/peer broadcast) is unaffected
regardless of this flag — only what LocalEventBackend persists to disk
is conditional. Real LocalEventBackend throughout, no unittest.mock.
"""
from __future__ import annotations

import pytest

from reyn.core.events.backend import LocalEventBackend
from reyn.schemas.models import Event

# (kind, content_field, other_field_name, other_field_value)
_CASES = [
    ("user_submitted", "text", "chain_id", "c1"),
    ("user_message_received", "text", "chain_id", "c1"),
    ("intervention_answer_submitted", "text", "intervention_id", "iv1"),
    ("user_answered_intervention", "answer_text", "intervention_id", "iv1"),
    ("user_intervention_received", "answer", "intervention_id", "iv1"),
    ("router_retry_exhausted", "user_message", "count", 3),
]


class _RecordingStore:
    def __init__(self) -> None:
        self.written: list[Event] = []

    def write(self, event: Event) -> None:
        self.written.append(event)


def _event(kind: str, content_field: str, other_field: str, other_value: object) -> Event:
    return Event(
        type=kind,
        data={content_field: "the user's own words", other_field: other_value},
    )


# ── default (off): content field dropped, everything else kept ─────────


@pytest.mark.parametrize("kind,content_field,other_field,other_value", _CASES)
def test_default_drops_the_content_field(
    kind: str, content_field: str, other_field: str, other_value: object,
) -> None:
    """Tier 1: #4666 ③ — with no explicit user_input_include_text
    argument, the default is False, and the durable record for each of
    the 6 kinds does NOT carry its content field."""
    store = _RecordingStore()
    backend = LocalEventBackend(store)

    backend.write(_event(kind, content_field, other_field, other_value))

    assert [content_field in w.data for w in store.written] == [False]


@pytest.mark.parametrize("kind,content_field,other_field,other_value", _CASES)
def test_default_still_keeps_the_non_content_fields(
    kind: str, content_field: str, other_field: str, other_value: object,
) -> None:
    """Tier 1: #4666 ③ — dropping the content field must not drop the
    kind's other fields (chain_id/intervention_id/count/etc.) — those are
    what a consumer needs to correlate "an answer/submission happened"
    even without its own text."""
    store = _RecordingStore()
    backend = LocalEventBackend(store)

    backend.write(_event(kind, content_field, other_field, other_value))

    assert store.written[0].data.get(other_field) == other_value


# ── opt-in (on): content field kept ─────────────────────────────────────


@pytest.mark.parametrize("kind,content_field,other_field,other_value", _CASES)
def test_opting_in_keeps_the_content_field(
    kind: str, content_field: str, other_field: str, other_value: object,
) -> None:
    """Tier 1: #4666 ③ — user_input_include_text=True restores each
    kind's content field on the durable record (the pre-#4666 behavior)."""
    store = _RecordingStore()
    backend = LocalEventBackend(store, user_input_include_text=True)

    backend.write(_event(kind, content_field, other_field, other_value))

    assert store.written[0].data.get(content_field) == "the user's own words"


# ── live dispatch is unaffected either way (the raw event, not the record) ──


def test_the_raw_event_passed_to_write_keeps_its_own_text_regardless() -> None:
    """Tier 1: #4666 ③ — this flag governs only what gets PERSISTED. The
    RAW event object `write()` was called with — the same object
    EventLog.emit()'s subscriber loop already dispatched to TUI/AG-UI/a
    peer broadcast BEFORE backend.write() ever ran — is never mutated,
    with the opt-in off or on."""
    store = _RecordingStore()
    backend = LocalEventBackend(store)
    raw = _event("user_submitted", "text", "chain_id", "c1")

    backend.write(raw)

    assert raw.data.get("text") == "the user's own words", (
        "the caller's own raw event object must never be mutated by the "
        "opt-in decision — only a NEW durable-record object may differ"
    )


def test_a_kind_outside_the_six_is_never_touched() -> None:
    """Tier 1: #4666 ③ — a kind NOT in _USER_INPUT_CONTENT_FIELDS (e.g.
    turn_completed) must pass through write() completely unchanged,
    default or opt-in, since the redaction is keyed by kind."""
    store = _RecordingStore()
    backend = LocalEventBackend(store)
    event = Event(type="turn_completed", data={"chain_id": "c1"})

    backend.write(event)

    assert store.written[0] is event, (
        "an unrelated kind must be written as the SAME object, not even "
        "copied — this backend has no reason to touch it at all"
    )


# ── declare_gaps() — dynamic, not static (architect's #4960 ruling) ─────


def test_declare_gaps_names_the_content_drop_when_off() -> None:
    """Tier 1: #4666 ③ — with the default (off), declare_gaps() must
    explicitly name that these 6 kinds' content fields are dropped by
    CONFIG, distinct from ①'s agent_delta gap — a reader must be able to
    tell "declared and dropped" apart from "never existed"."""
    backend = LocalEventBackend(_RecordingStore())
    gaps = backend.declare_gaps()
    assert any(
        "user_input_include_text" in g and "user_submitted" in g for g in gaps
    ), f"expected a gap explicitly naming the config-driven content drop; got {gaps!r}"


def test_declare_gaps_does_not_claim_the_content_gap_when_on() -> None:
    """Tier 1: #4666 ③ — the inverse: once opted in, declare_gaps() must
    NOT claim the user-input content gap (it would be false — the content
    IS retained). ①'s agent_delta gap is a separate, unaffected fact."""
    backend = LocalEventBackend(_RecordingStore(), user_input_include_text=True)
    gaps = backend.declare_gaps()
    assert not any("user_input_include_text" in g for g in gaps), (
        f"opted-in backend must not claim the user-input content gap; got {gaps!r}"
    )
    assert any("agent_delta" in g for g in gaps), (
        "the #4960 per-fragment coalescing gap must still be declared "
        "regardless of this separate opt-in — it is a different fact"
    )


def test_declare_gaps_names_the_known_ask_user_dispatcher_leak() -> None:
    """Tier 1: #4666 ③ — declare_gaps() must not overclaim closure: it
    must name that ask_user's question/answer also reach the audit log
    via tool_called.args/tool_returned.result, a path this flag does not
    touch either way (architect + lead-coder ruling, #4666)."""
    backend = LocalEventBackend(_RecordingStore())
    gaps = backend.declare_gaps()
    assert any("tool_called" in g and "tool_returned" in g for g in gaps), (
        f"expected the known-gap disclosure naming the dispatcher leak; got {gaps!r}"
    )
