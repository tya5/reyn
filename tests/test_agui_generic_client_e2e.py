"""Tier 2: a stock AG-UI client renders a functional chat off reyn (ADR-0039 P4).

The phase proof. A **standard-only consumer** — zero reyn knowledge, reads ONLY
standard AG-UI fields, ignores every ``CUSTOM`` / ``reyn.*`` event and never
touches the private ``_reyn`` block — is pointed at the reyn server emitter and
must reconstruct a functional chat:

- **text** via the canonical ``TEXT_MESSAGE_START`` → ``…_CONTENT`` → ``…_END``
  triplet (and NO bare CONTENT — the strict-client validity condition);
- **tool** calls with a standard ``status`` (a failure is visible);
- **run lifecycle** via ``RUN_STARTED`` / ``RUN_FINISHED``;
- **status** via ``STATE_SNAPSHOT`` / ``STATE_DELTA``;
- **backlog** via the standard ``MESSAGES_SNAPSHOT`` ``[{role, content}]`` array;
- and reyn chrome (a ``trace`` → ``CUSTOM``) is silently ignored, not fatal.

Real instances only — the real AgUiEmitter over the real codec; the generic
client below is a plain standard-field consumer written for the test (no mocks).
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.core.events.events import Event
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.agui.protocol import (
    MESSAGES_SNAPSHOT,
    RUN_FINISHED,
    RUN_STARTED,
    STATE_DELTA,
    STATE_SNAPSHOT,
    TEXT_MESSAGE_CONTENT,
    TEXT_MESSAGE_END,
    TEXT_MESSAGE_START,
    TOOL_CALL_END,
    TOOL_CALL_RESULT,
    TOOL_CALL_START,
    parse_sse_blocks,
)
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
from reyn.runtime.outbox import OutboxMessage

_REYN = "_reyn"


class _GenericAgUiClient:
    """A stock AG-UI consumer with ZERO reyn knowledge — standard fields only."""

    def __init__(self) -> None:
        self.messages: list[dict] = []          # finalized text messages
        self.tool_events: list[tuple] = []      # ("start"|"end"|"result", tool/id, status)
        self.runs: list[str] = []               # "started" / "finished"
        self.status: dict = {}                  # merged STATE_* read-model
        self.backlog: list[dict] = []           # standard MESSAGES_SNAPSHOT array
        self.ignored: int = 0                   # CUSTOM / reyn.* / unknown skipped
        self.bare_content: int = 0              # CONTENT with no prior START (invalid)
        self._open: dict[str, dict] = {}
        self._started: set[str] = set()

    def consume(self, ag_type: str, data: dict) -> None:
        # A generic client never reads reyn's private reconstruction block.
        data = {k: v for k, v in data.items() if k != _REYN}
        if ag_type == TEXT_MESSAGE_START:
            mid = data.get("messageId")
            self._started.add(mid)
            self._open[mid] = {"role": data.get("role"), "content": ""}
        elif ag_type == TEXT_MESSAGE_CONTENT:
            mid = data.get("messageId")
            if mid not in self._started:
                self.bare_content += 1
            buf = self._open.setdefault(mid, {"role": data.get("role"), "content": ""})
            buf["content"] += data.get("delta", "")
        elif ag_type == TEXT_MESSAGE_END:
            mid = data.get("messageId")
            if mid in self._open:
                self.messages.append(self._open.pop(mid))
        elif ag_type == TOOL_CALL_START:
            self.tool_events.append(("start", data.get("toolName"), None))
        elif ag_type == TOOL_CALL_END:
            self.tool_events.append(("end", data.get("toolName"), data.get("status")))
        elif ag_type == TOOL_CALL_RESULT:
            # The terminal result of a (frontend-)tool call, keyed by toolCallId.
            self.tool_events.append(("result", data.get("toolCallId"), None))
        elif ag_type == RUN_STARTED:
            self.runs.append("started")
        elif ag_type == RUN_FINISHED:
            self.runs.append("finished")
        elif ag_type == STATE_SNAPSHOT:
            self.status.update(data.get("snapshot") or {})
        elif ag_type == STATE_DELTA:
            self.status.update(data.get("delta") or {})
        elif ag_type == MESSAGES_SNAPSHOT:
            self.backlog = list(data.get("messages") or [])
        else:
            # CUSTOM / reyn.* / any event a stock client does not model → ignore.
            self.ignored += 1


_BACKLOG = [
    DisplayFrame(OutboxMessage(kind="user", text="earlier question")),
    DisplayFrame(OutboxMessage(kind="agent", text="earlier answer")),
]


def _script_frames() -> list:
    return [
        EventFrame(Event(type="turn_started", data={})),
        DisplayFrame(OutboxMessage(kind="agent", text="the answer text")),
        EventFrame(Event(type="tool_called", data={"tool": "grep_files"})),
        EventFrame(Event(type="tool_failed", data={"tool": "grep_files"})),
        DisplayFrame(OutboxMessage(kind="trace", text="· internal trace")),  # reyn chrome
        # HITL: an intervention rides as a display frame (reyn-native, CUSTOM →
        # ignored by the generic client) PLUS a companion frontend-tool the
        # generic client CAN render + answer.
        DisplayFrame(
            OutboxMessage(
                kind="intervention",
                text="Approve deploy?",
                meta={
                    "intervention_id": "iv-9",
                    "intervention_kind": "ask_user",
                    "prompt": "Approve deploy?",
                },
            )
        ),
        EventFrame(Event(type="user_answered_intervention", data={"intervention_id": "iv-9"})),
        EventFrame(Event(type="turn_settled", data={})),
        DisplayFrame(OutboxMessage(kind="__end__", text="")),
    ]


async def _frame_source(frames):
    for f in frames:
        yield f


@pytest.mark.asyncio
async def test_generic_client_renders_functional_chat_from_standard_fields() -> None:
    """Tier 2: the standard-only client reconstructs text (valid triplet) + tool
    (+status) + run + status + backlog, and ignores reyn chrome — off standard
    fields alone (the private _reyn block is stripped before it ever sees it)."""
    emitter = AgUiEmitter(
        _frame_source(_script_frames()),
        lambda: {"attached_name": "demo", "model": "m", "ctx_window": 200},
        backlog=_BACKLOG,
    )
    sse = "".join([chunk async for chunk in emitter.stream()])

    client = _GenericAgUiClient()
    for ev in parse_sse_blocks(sse.split("\n")):
        client.consume(ev.type, ev.data)

    # Text: the live agent reply is assembled from a VALID triplet (START before
    # CONTENT before END) — no bare CONTENT, the strict-client validity floor.
    assert client.bare_content == 0
    assert client.messages == [{"role": "assistant", "content": "the answer text"}]

    # Tool: the failure is visible via the standard status field.
    assert ("start", "grep_files", None) in client.tool_events
    assert ("end", "grep_files", "error") in client.tool_events

    # HITL coexists with the triplet: the generic client sees the intervention as
    # a frontend-tool (TOOL_CALL_START, toolName reyn.intervention.<kind>) and its
    # terminal TOOL_CALL_RESULT — the triplet synthesis did not disturb this path.
    assert ("start", "reyn.intervention.ask_user", None) in client.tool_events
    assert ("result", "iv-9", None) in client.tool_events
    # ...and the text triplet is still valid alongside it.
    assert client.bare_content == 0

    # Run lifecycle: started then finished.
    assert client.runs == ["started", "finished"]

    # Status read-model: seeded from the snapshot (+ deltas merged).
    assert client.status.get("attached_name") == "demo"
    assert client.status.get("ctx_window") == 200

    # Backlog: the standard conversation-turns array rebuilds the prior turns.
    assert client.backlog == [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
    ]

    # reyn chrome (the trace frame → CUSTOM) is silently ignored, not fatal, and
    # never leaks into the conversation.
    assert client.ignored >= 1
    assert all("trace" not in m["content"] for m in client.messages)
