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

import asyncio

import pytest

from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.protocol import (
    MESSAGES_SNAPSHOT,
    encode_frame,
    encode_messages_snapshot,
    to_sse,
)
from reyn.interfaces.transport.frames import DisplayFrame
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


@pytest.mark.asyncio
async def test_reyn_client_rebuilds_full_backlog_from_reyn_block() -> None:
    """Tier 2: the reyn client replays the FULL backlog (chrome included) from the
    _reyn block — the standard-array narrowing does not touch reyn reconstruction."""
    # A __end__ sentinel terminates the client's frames() loop after the backlog.
    sse = to_sse(encode_messages_snapshot(_BACKLOG)) + to_sse(
        encode_frame(DisplayFrame(OutboxMessage(kind="__end__", text="")))
    )

    async def _noop_send(_payload):
        return None

    transport = AgUiTransport(_sse_lines(sse), _noop_send)
    kinds_texts = [
        (f.message.kind, f.message.text)
        async for f in transport.frames()
        if isinstance(f, DisplayFrame) and f.message.kind != "__end__"
    ]
    assert kinds_texts == [
        ("user", "what's the weather"),
        ("agent", "sunny"),
        ("status", "thinking…"),
        ("error", "a warning"),
        ("trace", "· ran a tool"),
    ]
