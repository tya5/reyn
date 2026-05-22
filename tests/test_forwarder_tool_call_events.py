"""Tier 2: ChatEventForwarder relays tool-call lifecycle events to outbox.

issue #427 L4 step 3 — forwarder subscribes to the existing
``dispatch/dispatcher.py`` ``tool_called`` / ``tool_returned`` /
``tool_failed`` events and emits ``OutboxMessage`` (kind=
``tool_call_started`` / ``tool_call_completed`` / ``tool_call_failed``)
so the conv pane (= step 4) can mount + drive ToolCallRow widgets.

Contract pinned here:

1. ``on_tool_called`` enqueues ``OutboxMessage(kind="tool_call_started")``
   with the tool name as text + ``op_id`` (= source ``args_hash``) in meta.
2. ``on_tool_returned`` enqueues ``tool_call_completed`` with result in meta.
3. ``on_tool_failed`` enqueues ``tool_call_failed`` with error_kind / message
   in meta.
4. The same ``op_id`` flows across the three lifecycle phases for a
   given tool call so the consumer can correlate them.
5. Standard provenance fields (``run_id``, ``parent_run_id``,
   ``skill_name``) appear in meta — same shape as ``_enqueue`` for
   trace messages.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.forwarder import ChatEventForwarder  # noqa: E402
from reyn.schemas.models import Event  # noqa: E402


def _drain(q: asyncio.Queue) -> list:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_on_tool_called_enqueues_tool_call_started_outbox_message() -> None:
    """Tier 2: ``on_tool_called`` emits ``OutboxMessage(kind='tool_call_started')``."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("test_skill", q, run_id="parent-run")
    fwd(Event(
        type="tool_called",
        data={
            "caller_kind": "skill_phase",
            "caller_id": "child-run-1",
            "tool": "read_file",
            "chain_id": "chain-abc",
            "args": {"path": "/tmp/example.txt"},
            "args_hash": "hash-xyz",
            "run_id": "child-run-1",
        },
    ))
    msgs = _drain(q)
    assert len(msgs) == 1
    msg = msgs[0]
    assert msg.kind == "tool_call_started"
    assert msg.text == "read_file"
    assert msg.meta["op_id"] == "hash-xyz"
    assert msg.meta["tool"] == "read_file"
    assert msg.meta["args"] == {"path": "/tmp/example.txt"}


def test_on_tool_returned_enqueues_tool_call_completed_with_result_in_meta() -> None:
    """Tier 2: ``on_tool_returned`` emits ``tool_call_completed`` with result."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("test_skill", q, run_id="parent-run")
    fwd(Event(
        type="tool_returned",
        data={
            "caller_kind": "skill_phase",
            "caller_id": "child-run-2",
            "tool": "web_fetch",
            "chain_id": "chain-abc",
            "args_hash": "hash-fetch",
            "result": {"status": "ok", "preview": "200 OK 1.2KB"},
            "run_id": "child-run-2",
        },
    ))
    msgs = _drain(q)
    assert len(msgs) == 1
    msg = msgs[0]
    assert msg.kind == "tool_call_completed"
    assert msg.text == "web_fetch"
    assert msg.meta["op_id"] == "hash-fetch"
    assert msg.meta["result"] == {"status": "ok", "preview": "200 OK 1.2KB"}


def test_on_tool_failed_enqueues_tool_call_failed_with_error_in_meta() -> None:
    """Tier 2: ``on_tool_failed`` emits ``tool_call_failed`` with error fields."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("test_skill", q, run_id="parent-run")
    fwd(Event(
        type="tool_failed",
        data={
            "caller_kind": "skill_phase",
            "caller_id": "child-run-3",
            "tool": "shell",
            "chain_id": "chain-abc",
            "args_hash": "hash-shell",
            "error_kind": "permission_denied",
            "message": "shell outside cwd: /etc/passwd",
            "run_id": "child-run-3",
        },
    ))
    msgs = _drain(q)
    assert len(msgs) == 1
    msg = msgs[0]
    assert msg.kind == "tool_call_failed"
    assert msg.text == "shell"
    assert msg.meta["op_id"] == "hash-shell"
    assert msg.meta["error_kind"] == "permission_denied"
    assert msg.meta["error_message"] == "shell outside cwd: /etc/passwd"


def test_op_id_correlates_across_three_lifecycle_events() -> None:
    """Tier 2: same ``args_hash`` source → same ``op_id`` across phases.

    Lets the TUI consumer match start → end without ambiguity even when
    multiple tool calls are in flight concurrently.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("test_skill", q, run_id="parent-run")

    fwd(Event(type="tool_called", data={
        "tool": "read_file", "args_hash": "matched-id",
        "args": {"path": "/x"}, "run_id": "r1",
    }))
    fwd(Event(type="tool_returned", data={
        "tool": "read_file", "args_hash": "matched-id",
        "result": {"status": "ok"}, "run_id": "r1",
    }))
    fwd(Event(type="tool_failed", data={
        "tool": "read_file", "args_hash": "matched-id",
        "error_kind": "exception", "message": "ignored",
        "run_id": "r1",
    }))

    msgs = _drain(q)
    assert len(msgs) == 3
    assert all(m.meta["op_id"] == "matched-id" for m in msgs)


def test_sub_skill_attribution_stamps_parent_run_id_on_tool_call_messages() -> None:
    """Tier 2: when source run_id differs from forwarder's own, stamp parent_run_id.

    Same provenance discipline as ``_enqueue`` for trace messages so TUI
    consumers can render nested tool-call rows under their parent skill
    when a sub-skill spawned via ``run_skill`` issues its own tool calls.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("parent_skill", q, run_id="parent-run")
    fwd(Event(type="tool_called", data={
        "tool": "read_file", "args_hash": "h1",
        "args": {"path": "/x"}, "run_id": "child-run",
    }))
    msg = _drain(q)[0]
    assert msg.meta["run_id"] == "child-run"
    assert msg.meta["parent_run_id"] == "parent-run"


def test_run_id_short_appears_for_compact_display() -> None:
    """Tier 2: ``run_id_short`` (last 4 chars) lands in meta for compact prefix."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("test_skill", q, run_id="parent-run")
    fwd(Event(type="tool_called", data={
        "tool": "read_file", "args_hash": "h",
        "args": {}, "run_id": "20260522T123456_skill_abcdEFGH",
    }))
    msg = _drain(q)[0]
    assert msg.meta["run_id_short"] == "EFGH"


def test_missing_args_hash_does_not_crash_and_uses_none_op_id() -> None:
    """Tier 2: degrade-safe when source event lacks ``args_hash``.

    Should never happen with dispatcher.py's well-formed emissions, but
    the forwarder must not crash on a stripped / partial event.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("test_skill", q, run_id="parent-run")
    fwd(Event(type="tool_called", data={
        "tool": "read_file", "args": {"path": "/x"},
        "run_id": "r1",
    }))
    msgs = _drain(q)
    assert len(msgs) == 1
    assert msgs[0].meta["op_id"] is None
    assert msgs[0].meta["tool"] == "read_file"
