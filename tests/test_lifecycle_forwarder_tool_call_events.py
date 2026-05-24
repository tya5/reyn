"""Tier 2: ChatLifecycleForwarder relays tool-call lifecycle events to outbox.

issue #427 wiring fix (= post wave-#427 smoke finding 2026-05-22):

Original step 3 (PR #433) added these handlers to ``ChatEventForwarder``
(= per-skill subscriber), but ``dispatch/dispatcher.py:200-274`` emits
``tool_called`` / ``tool_returned`` / ``tool_failed`` against the
session's ``_chat_events`` log — and that log is subscribed by
``ChatLifecycleForwarder``, not ``ChatEventForwarder``. The handlers
never fired, ToolCallRow never mounted, end-to-end functional path
broken in production despite Tier 2 tests passing in isolation.

This module re-pins the contract against the correct subscriber:

1. ``on_tool_called`` enqueues ``OutboxMessage(kind="tool_call_started")``
   with the tool name as text + ``op_id`` (= source ``args_hash``) in meta.
2. ``on_tool_returned`` enqueues ``tool_call_completed`` with result.
3. ``on_tool_failed`` enqueues ``tool_call_failed`` with error fields.
4. The same ``op_id`` flows across the three lifecycle phases.
5. ``run_id`` falls back to ``caller_id`` (= dispatcher's source identifier
   for sub-skill / router callers) so consumers can attribute the row.
6. Empty ``run_id`` / ``caller_id`` degrades safely (= no crash, ``run_id``
   omitted from meta).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.lifecycle_forwarder import ChatLifecycleForwarder  # noqa: E402
from reyn.schemas.models import Event  # noqa: E402


def _drain(q: asyncio.Queue) -> list:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_on_tool_called_enqueues_tool_call_started() -> None:
    """Tier 2: dispatcher's ``tool_called`` event → ``tool_call_started`` outbox."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="tool_called",
        data={
            "caller_kind": "router",
            "caller_id": "agent_default",
            "tool": "read_file",
            "chain_id": "chain-abc",
            "args": {"path": "/tmp/x.txt"},
            "args_hash": "hash-xyz",
        },
    ))
    msgs = _drain(q)
    assert msgs, "expected at least one outbox message"
    msg = msgs[0]
    assert msg.kind == "tool_call_started"
    assert msg.text == "read_file"
    assert msg.meta["op_id"] == "hash-xyz"
    assert msg.meta["tool"] == "read_file"
    assert msg.meta["args"] == {"path": "/tmp/x.txt"}


def test_on_tool_returned_enqueues_tool_call_completed() -> None:
    """Tier 2: dispatcher's ``tool_returned`` event → ``tool_call_completed`` outbox."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="tool_returned",
        data={
            "caller_kind": "router",
            "caller_id": "agent_default",
            "tool": "web_fetch",
            "chain_id": "chain-abc",
            "args_hash": "hash-fetch",
            "result": {"status": "ok", "preview": "200 OK 1.2KB"},
        },
    ))
    msgs = _drain(q)
    assert msgs, "expected at least one outbox message"
    msg = msgs[0]
    assert msg.kind == "tool_call_completed"
    assert msg.text == "web_fetch"
    assert msg.meta["op_id"] == "hash-fetch"
    assert msg.meta["result"] == {"status": "ok", "preview": "200 OK 1.2KB"}


def test_on_tool_failed_enqueues_tool_call_failed_with_error_in_meta() -> None:
    """Tier 2: dispatcher's ``tool_failed`` event → ``tool_call_failed`` outbox."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="tool_failed",
        data={
            "caller_kind": "router",
            "caller_id": "agent_default",
            "tool": "shell",
            "chain_id": "chain-abc",
            "args_hash": "hash-shell",
            "error_kind": "permission_denied",
            "message": "shell outside cwd: /etc/passwd",
        },
    ))
    msgs = _drain(q)
    assert msgs, "expected at least one outbox message"
    msg = msgs[0]
    assert msg.kind == "tool_call_failed"
    assert msg.text == "shell"
    assert msg.meta["op_id"] == "hash-shell"
    assert msg.meta["error_kind"] == "permission_denied"
    assert msg.meta["error_message"] == "shell outside cwd: /etc/passwd"


def test_op_id_correlates_across_three_lifecycle_events() -> None:
    """Tier 2: same ``args_hash`` source → same ``op_id`` across phases."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)

    fwd(Event(type="tool_called", data={
        "tool": "read_file", "args_hash": "matched-id",
        "args": {"path": "/x"}, "caller_id": "agent_default",
    }))
    fwd(Event(type="tool_returned", data={
        "tool": "read_file", "args_hash": "matched-id",
        "result": {"status": "ok"}, "caller_id": "agent_default",
    }))
    fwd(Event(type="tool_failed", data={
        "tool": "read_file", "args_hash": "matched-id",
        "error_kind": "exception", "message": "ignored",
        "caller_id": "agent_default",
    }))

    msgs = _drain(q)
    assert msgs, "expected outbox messages for three lifecycle events"
    assert all(m.meta["op_id"] == "matched-id" for m in msgs)


def test_run_id_falls_back_to_caller_id_when_run_id_absent() -> None:
    """Tier 2: dispatcher events omit ``run_id`` → forwarder uses ``caller_id``.

    The router-level dispatcher carries ``caller_id`` (= the agent /
    skill that issued the call) but not always a separate ``run_id``;
    the forwarder treats them as equivalent for provenance display.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="tool_called", data={
        "tool": "read_file", "args_hash": "h",
        "args": {}, "caller_id": "agent_default",
        # No explicit run_id field.
    }))
    msg = _drain(q)[0]
    assert msg.meta["run_id"] == "agent_default"
    assert msg.meta["run_id_short"] == "ault"


def test_missing_run_id_and_caller_id_degrades_safely() -> None:
    """Tier 2: no ``run_id`` AND no ``caller_id`` → meta omits run_id, no crash."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="tool_called", data={
        "tool": "read_file", "args_hash": "h",
        "args": {},
    }))
    msgs = _drain(q)
    assert msgs, "expected at least one outbox message"
    assert "run_id" not in msgs[0].meta or msgs[0].meta.get("run_id") is None


def test_explicit_run_id_takes_precedence_over_caller_id() -> None:
    """Tier 2: when ``run_id`` is explicitly set, it wins over ``caller_id``."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="tool_called", data={
        "tool": "read_file", "args_hash": "h",
        "args": {}, "caller_id": "agent_default",
        "run_id": "explicit-run-id",
    }))
    msg = _drain(q)[0]
    assert msg.meta["run_id"] == "explicit-run-id"
