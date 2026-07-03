"""Tier 2: ChatLifecycleForwarder.on_mcp_progress — MCP tool progress surfacing.

``op_runtime/mcp.py`` emits ``mcp_progress`` events for each
``notifications/progress`` callback during a tool call (issue #264).
``ChatLifecycleForwarder`` must bridge these into ``kind="status"`` outbox
messages so the sticky status bar shows live progress.

Pins:
  1. progress + total → percentage text in ``[mcp/<server>] <tool> · N%``
  2. progress only (no total) → raw progress value
  3. neither → bare ``[mcp/<server>] <tool>``
  4. message present → appended as ``· <message>``
  5. Output kind is ``"status"`` (not ``"system"``)
  6. meta contains source="mcp", server, tool
  7. Unrelated events are not forwarded
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from reyn.runtime.lifecycle_forwarder import ChatLifecycleForwarder, _format_mcp_progress
from reyn.schemas.models import Event


def _drain(q: asyncio.Queue) -> list[Any]:
    items: list[Any] = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


# ── _format_mcp_progress pure function ───────────────────────────────────────

def test_format_mcp_progress_percentage_when_both_numeric() -> None:
    """Tier 2: progress + total → percentage in the status text."""
    text = _format_mcp_progress("my-server", "search", 3, 10, None)
    assert "[mcp/my-server] search" in text
    assert "30%" in text


def test_format_mcp_progress_raw_when_no_total() -> None:
    """Tier 2: progress without total → raw progress value."""
    text = _format_mcp_progress("srv", "list", 7, None, None)
    assert "progress=7" in text
    assert "%" not in text


def test_format_mcp_progress_bare_when_no_progress() -> None:
    """Tier 2: no progress or total → bare server+tool indicator."""
    text = _format_mcp_progress("srv", "tool", None, None, None)
    assert text == "[mcp/srv] tool"


def test_format_mcp_progress_message_appended() -> None:
    """Tier 2: message is appended after the progress part."""
    text = _format_mcp_progress("srv", "tool", 1, 2, "indexing files")
    assert "indexing files" in text
    assert text.endswith("· indexing files")


def test_format_mcp_progress_zero_total_no_pct() -> None:
    """Tier 2: total=0 is treated as absent (no division by zero)."""
    text = _format_mcp_progress("srv", "tool", 5, 0, None)
    assert "%" not in text
    assert "progress=5" in text


# ── ChatLifecycleForwarder.on_mcp_progress ───────────────────────────────────

def test_mcp_progress_event_emits_status_kind() -> None:
    """Tier 2: mcp_progress event produces kind='status' outbox message."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="mcp_progress", data={
        "server": "search-srv", "tool": "web_search",
        "progress": 5, "total": 10, "message": None,
    }))
    msgs = _drain(q)
    (only,) = msgs
    assert only.kind == "status"


def test_mcp_progress_meta_source_and_fields() -> None:
    """Tier 2: mcp_progress meta carries source='mcp', server, tool."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="mcp_progress", data={
        "server": "my-server", "tool": "my-tool",
        "progress": 2, "total": 4, "message": "running",
    }))
    msgs = _drain(q)
    (only,) = msgs
    assert only.meta["source"] == "mcp"
    assert only.meta["server"] == "my-server"
    assert only.meta["tool"] == "my-tool"
    assert only.meta["progress"] == 2
    assert only.meta["total"] == 4
    assert only.meta["progress_text"] == "running"


def test_mcp_progress_text_contains_percentage() -> None:
    """Tier 2: status text surfaces the computed percentage."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="mcp_progress", data={
        "server": "srv", "tool": "fetch",
        "progress": 1, "total": 4, "message": None,
    }))
    msgs = _drain(q)
    assert "25%" in msgs[0].text


def test_mcp_progress_unrelated_event_dropped() -> None:
    """Tier 2: unrelated events produce no outbox messages."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="some_other_event", data={"x": 1}))
    assert q.empty()


@pytest.mark.parametrize("progress,total,expected_in_text", [
    (3, 10, "30%"),
    (1, 1, "100%"),
    (0, 5, "0%"),
    (7, None, "progress=7"),
    (None, None, "[mcp/s] t"),
])
def test_format_mcp_progress_parametrised(
    progress: object, total: object, expected_in_text: str,
) -> None:
    """Tier 2: parametrised coverage of the key _format_mcp_progress branches."""
    text = _format_mcp_progress("s", "t", progress, total, None)
    assert expected_in_text in text
