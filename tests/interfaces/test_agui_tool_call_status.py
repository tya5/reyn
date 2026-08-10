"""Tier 2: TOOL_CALL_END carries a standard status field (ADR-0039 P4).

A generic AG-UI client needs to see a tool failure. P4 adds a standard
``status`` (``"ok"`` / ``"error"``) to ``TOOL_CALL_END``, derived from the frame
etype (``tool_failed`` → ``error``, ``tool_returned`` → ``ok``). The reyn client
is unaffected — it still exact-recovers the precise etype from ``_reyn``.

Real instances only — the real codec; no mocks.
"""
from __future__ import annotations

from reyn.core.events.events import Event
from reyn.interfaces.transport.agui.protocol import (
    TOOL_CALL_END,
    decode_event,
    encode_frame,
)
from reyn.interfaces.transport.frames import EventFrame


def _encode(etype: str):
    return encode_frame(EventFrame(Event(type=etype, data={"tool": "grep_files"})))


def test_tool_failed_maps_to_status_error() -> None:
    """Tier 2: a tool_failed frame's standard TOOL_CALL_END status is 'error'."""
    ev = _encode("tool_failed")
    assert ev.type == TOOL_CALL_END
    assert ev.data["status"] == "error"


def test_tool_returned_maps_to_status_ok() -> None:
    """Tier 2: a tool_returned frame's standard TOOL_CALL_END status is 'ok'."""
    ev = _encode("tool_returned")
    assert ev.type == TOOL_CALL_END
    assert ev.data["status"] == "ok"


def test_reyn_client_recovers_exact_etype_regardless_of_status() -> None:
    """Tier 2: the reyn client reconstructs the precise etype from _reyn — the
    standard status is a generic-surface addition, not the reyn recovery source."""
    for etype in ("tool_failed", "tool_returned"):
        ev = _encode(etype)
        decoded = decode_event(ev.type, ev.data)
        assert isinstance(decoded, EventFrame)
        assert decoded.event.type == etype
        assert decoded.event.data == {"tool": "grep_files"}
