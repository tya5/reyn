"""Tier 2: untrusted A2A/WS inbound frame decode rejects non-object JSON.

``ws_chat`` (web WebSocket handler) parses each client text frame then calls
``payload.get(...)``. A valid-JSON but NON-object frame (``123`` / ``[]`` /
``null`` / a bare string) would reach ``.get`` → AttributeError on this untrusted
inbound boundary (only ``JSONDecodeError`` was handled). ``_decode_inbound_frame``
centralises the parse and returns ``None`` for BOTH malformed-JSON and non-object
frames so the handler rejects either with an error frame.

Policy: tests the module-level pure helper directly — mirrors
``test_ws_chat_serialize_queued_count`` testing ``_serialize`` (no handler /
websocket mock needed). Tier line first.
"""
from __future__ import annotations

import json

import pytest

from reyn.interfaces.web.ws.chat import _decode_inbound_frame


@pytest.mark.parametrize("raw", ["123", "[]", '"x"', "null", "true", "3.14"])
def test_valid_json_but_non_object_rejected(raw: str) -> None:
    """Tier 2: valid JSON that is not an object → None (caller rejects; no .get crash)."""
    assert _decode_inbound_frame(raw) is None


@pytest.mark.parametrize("raw", ["{bad", "", '{"a": ', "not json"])
def test_malformed_json_rejected(raw: str) -> None:
    """Tier 2: unparseable JSON → None (no exception escapes the boundary)."""
    assert _decode_inbound_frame(raw) is None


def test_json_object_accepted() -> None:
    """Tier 2: (regression) a JSON object round-trips to a dict the handler can
    ``.get`` safely."""
    frame = _decode_inbound_frame(json.dumps({"type": "user_message", "text": "hi"}))
    assert frame == {"type": "user_message", "text": "hi"}
    assert frame.get("type") == "user_message"  # reachable without AttributeError
