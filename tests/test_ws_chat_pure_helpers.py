"""Tier 2: pure helpers in interfaces/web/ws/chat.py.

  ``_serialize(msg, *, session)``      — OutboxMessage → JSON wire frame string
  ``_decode_inbound_frame(raw)``       — raw WS text → dict | None
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from reyn.interfaces.web.ws.chat import _decode_inbound_frame, _serialize

# ---------------------------------------------------------------------------
# _serialize
# ---------------------------------------------------------------------------


def _msg(kind: str, text: str = "", meta: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(kind=kind, text=text, meta=meta)


def test_serialize_basic_structure() -> None:
    """Tier 2: serialized frame has kind, text, meta keys."""
    raw = _serialize(_msg("agent", "hello"))
    data = json.loads(raw)
    assert data["kind"] == "agent"
    assert data["text"] == "hello"
    assert "meta" in data


def test_serialize_none_meta_becomes_empty_dict() -> None:
    """Tier 2: None meta is serialized as an empty object."""
    raw = _serialize(_msg("status", "", meta=None))
    data = json.loads(raw)
    assert data["meta"] == {}


def test_serialize_meta_passthrough() -> None:
    """Tier 2: non-None meta dict is included verbatim."""
    raw = _serialize(_msg("agent", "hi", meta={"run_id": "abc"}))
    data = json.loads(raw)
    assert data["meta"]["run_id"] == "abc"


def test_serialize_intervention_without_session_no_queued_count() -> None:
    """Tier 2: intervention kind without session does not add queued_count."""
    raw = _serialize(_msg("intervention", "ask?"), session=None)
    data = json.loads(raw)
    assert "queued_count" not in data["meta"]


def test_serialize_intervention_with_session_adds_queued_count() -> None:
    """Tier 2: intervention kind with session injects queued_count from registry."""
    interventions = SimpleNamespace(queued_count=lambda: 3)
    session = SimpleNamespace(_interventions=interventions)
    raw = _serialize(_msg("intervention", "ask?"), session=session)
    data = json.loads(raw)
    assert data["meta"]["queued_count"] == 3


def test_serialize_intervention_does_not_overwrite_existing_queued_count() -> None:
    """Tier 2: existing queued_count in meta is preserved, not overwritten."""
    interventions = SimpleNamespace(queued_count=lambda: 5)
    session = SimpleNamespace(_interventions=interventions)
    raw = _serialize(
        _msg("intervention", "ask?", meta={"queued_count": 1}),
        session=session,
    )
    data = json.loads(raw)
    assert data["meta"]["queued_count"] == 1


def test_serialize_non_intervention_kind_no_queued_count() -> None:
    """Tier 2: non-intervention kind does not inject queued_count even with session."""
    session = SimpleNamespace(_interventions=SimpleNamespace(queued_count=lambda: 2))
    raw = _serialize(_msg("agent", "text"), session=session)
    data = json.loads(raw)
    assert "queued_count" not in data["meta"]


# ---------------------------------------------------------------------------
# _decode_inbound_frame
# ---------------------------------------------------------------------------


def test_decode_inbound_frame_valid_object() -> None:
    """Tier 2: valid JSON object returns a dict."""
    raw = json.dumps({"action": "submit", "text": "hello"})
    result = _decode_inbound_frame(raw)
    assert result == {"action": "submit", "text": "hello"}


def test_decode_inbound_frame_invalid_json_returns_none() -> None:
    """Tier 2: malformed JSON returns None."""
    assert _decode_inbound_frame("{not valid json}") is None


def test_decode_inbound_frame_json_array_returns_none() -> None:
    """Tier 2: JSON array (not an object) returns None."""
    assert _decode_inbound_frame("[1, 2, 3]") is None


def test_decode_inbound_frame_json_string_returns_none() -> None:
    """Tier 2: JSON string scalar returns None."""
    assert _decode_inbound_frame('"hello"') is None


def test_decode_inbound_frame_json_null_returns_none() -> None:
    """Tier 2: JSON null returns None."""
    assert _decode_inbound_frame("null") is None


def test_decode_inbound_frame_empty_object_is_valid() -> None:
    """Tier 2: empty JSON object {} returns empty dict (valid frame)."""
    assert _decode_inbound_frame("{}") == {}
