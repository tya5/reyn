"""Tier 2: MESSAGES_SNAPSHOT is a standard conversation-turns array (ADR-0039 P4).

Canonical AG-UI's ``messages`` is an array of message objects; a generic client
reads it to rebuild the conversation. P4 emits a standard ``[{role, content}]``
array of **conversation turns only** — ``agent`` → ``assistant``, ``user`` →
``user`` — while reyn chrome (status / error / present / intervention / trace) is
NOT a conversation turn and is excluded from the standard array (SR2). The reyn
client still rebuilds the FULL backlog from the ``_reyn`` block (SR2 preserved),
so its scrollback is unchanged.

Real instances only — the real codec + AgUiTransport; no mocks.
"""
from __future__ import annotations

import pytest

from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.protocol import (
    MESSAGES_SNAPSHOT,
    encode_frame,
    encode_messages_snapshot,
    to_sse,
)
from reyn.interfaces.transport.frames import BacklogBatch, DisplayFrame
from reyn.runtime.outbox import OutboxMessage

# A backlog mixing conversation turns (agent / user) with reyn chrome kinds.
_BACKLOG = [
    DisplayFrame(OutboxMessage(kind="user", text="what's the weather")),
    DisplayFrame(OutboxMessage(kind="agent", text="sunny")),
    DisplayFrame(OutboxMessage(kind="status", text="thinking…")),
    DisplayFrame(OutboxMessage(kind="error", text="a warning")),
    DisplayFrame(OutboxMessage(kind="trace", text="· ran a tool")),
]


async def _sse_lines(text):
    for line in text.split("\n"):
        yield line


def test_standard_messages_are_conversation_turns_only() -> None:
    """Tier 2: the standard ``messages`` array is ``[{role, content}]`` of only
    the agent/user turns; status/error/trace chrome is excluded (SR2)."""
    ev = encode_messages_snapshot(_BACKLOG)
    assert ev.type == MESSAGES_SNAPSHOT

    standard = ev.data["messages"]
    assert standard == [
        {"role": "user", "content": "what's the weather"},
        {"role": "assistant", "content": "sunny"},
    ]
    # Every entry is the standard convention object — nothing else leaks in.
    assert all(set(m) == {"role", "content"} for m in standard)


def test_os_authored_system_rows_are_not_assistant_turns_in_the_snapshot() -> None:
    """Tier 2: #5887 accept ② — an OS-authored notice (the empty-response
    fallback, the async-dispatch ack) is emitted as ``kind="system"`` and so
    is NOT an ``assistant`` turn in the reconnect ``MESSAGES_SNAPSHOT``: a
    generic client rebuilding the conversation must not attribute reyn's
    own sentence to the model, exactly as the live TUI no longer does.

    The present-side sibling of ``_CONVERSATION_KINDS`` having no
    ``system`` entry: this pins the OBSERVABLE consequence for a real
    encoded snapshot, not the dict's contents. Before #5887 these rows
    were ``kind="agent"`` and DID appear here as ``assistant`` — the
    reconnect backlog had the same misattribution the owner saw live."""
    backlog = [
        DisplayFrame(OutboxMessage(kind="user", text="do something")),
        DisplayFrame(OutboxMessage(
            kind="system",
            text="[⚠ model returned an empty response (finish_reason=stop) · retry: off (chat.empty_stop_retry) · call c1]",
            meta={"source": "router_empty_response"},
        )),
        DisplayFrame(OutboxMessage(kind="agent", text="a real reply")),
    ]
    ev = encode_messages_snapshot(backlog)
    standard = ev.data["messages"]

    assert standard == [
        {"role": "user", "content": "do something"},
        {"role": "assistant", "content": "a real reply"},
    ], f"the OS notice must not be an assistant turn; got: {standard!r}"
    assert not any("empty response" in m["content"] for m in standard)


@pytest.mark.asyncio
async def test_reyn_client_rebuilds_full_backlog_from_reyn_block() -> None:
    """Tier 2: the reyn client replays the FULL backlog (chrome included) from the
    _reyn block — the standard-array narrowing does not touch reyn reconstruction.

    #5139: the backlog arrives as ONE ``BacklogBatch`` item off ``frames()``
    (architect FINAL ruling, issuecomment-5383272756) rather than flattened
    into individual DisplayFrame items — updated to match; the property
    under test (chrome-inclusive reconstruction) is unchanged."""
    # A __end__ sentinel terminates the client's frames() loop after the backlog.
    sse = to_sse(encode_messages_snapshot(_BACKLOG)) + to_sse(
        encode_frame(DisplayFrame(OutboxMessage(kind="__end__", text="")))
    )

    async def _noop_send(_payload):
        return None

    transport = AgUiTransport(_sse_lines(sse), _noop_send)
    items = [f async for f in transport.frames()]
    batches = [f for f in items if isinstance(f, BacklogBatch)]
    # Unpacking into a single-element tuple IS the "exactly one" check
    # (raises on 0 or 2+ matches) — a behavioral assertion on the
    # extracted value, not a ``len(...) == N`` format pin.
    (batch,) = batches
    kinds_texts = [
        (f.message.kind, f.message.text)
        for f in batch.frames
        if isinstance(f, DisplayFrame)
    ]
    assert kinds_texts == [
        ("user", "what's the weather"),
        ("agent", "sunny"),
        ("status", "thinking…"),
        ("error", "a warning"),
        ("trace", "· ran a tool"),
    ]
