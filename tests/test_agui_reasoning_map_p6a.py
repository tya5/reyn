"""Tier 2: reyn reasoning frames ride the standard AG-UI Reasoning* events (P6a).

ADR-0039 P6a turns reyn's private reasoning-as-CUSTOM into a standard AG-UI
signal: a ``kind="reasoning"`` DisplayFrame maps to the canonical Reasoning
message lifecycle — ``REASONING_MESSAGE_START`` → ``REASONING_MESSAGE_CONTENT`` →
``REASONING_MESSAGE_END``, correlated by a shared ``messageId`` with
``role: "reasoning"`` — so a generic AG-UI client (CopilotKit) renders reasoning.
reyn is whole-message, so there is no token streaming; the triplet mirrors the P4
TEXT triplet discipline exactly.

The gates pinned here:

- **Generic surface valid**: the wire sequence is START → CONTENT → END, one
  shared ``messageId``, CONTENT ``delta`` = the whole reasoning text, ``role`` =
  the spec-mandated ``"reasoning"``.
- **reyn invariant preserved (SR6)**: only the CONTENT event carries ``_reyn``
  (START/END decode to ``None``), so the invariant stays 1 frame ⇄ 1
  ``_reyn``-bearing event and the reyn client reconstructs the SAME single frame.
- **SR1 (display-gate by construction)**: reasoning frames only exist when
  reasoning display is on (the #1652 host chokepoint). Display off ⇒ no reasoning
  frame ⇒ zero Reasoning* events — no new gate, proven end-to-end through the REAL
  RouterHostAdapter + the REAL emitter.
- **Bit-identical**: the reyn client's render of a reasoning frame decoded off the
  P6a wire is byte-identical to the direct-renderer baseline (the standard-surface
  widening did not perturb reyn).

Real instances only — the real codec, the real RouterHostAdapter + emitter, a real
InlineChatRenderer over real SSE text; no mocks.
"""
from __future__ import annotations

import asyncio
import sys
import time
from io import StringIO
from pathlib import Path

import pytest

from reyn.config import ReasoningConfig
from reyn.core.events.events import EventLog
from reyn.interfaces.repl.renderer import InlineChatRenderer
from reyn.interfaces.repl.stream_client import run_output_loop
from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.agui.protocol import (
    CUSTOM,
    REASONING_MESSAGE_CONTENT,
    REASONING_MESSAGE_END,
    REASONING_MESSAGE_START,
    decode_event,
    encode_frame,
    encode_frame_wire,
    parse_sse_blocks,
)
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.llm.model_resolver import ModelResolver
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.services import MemoryService, RouterHostAdapter

_REASONING_TEXT = "step 1: 17*23 = 391; therefore the answer is 391."


# ── generic-surface + SR6 (codec) ────────────────────────────────────────────


def test_reasoning_frame_expands_to_standard_reasoning_triplet() -> None:
    """Tier 2: a reasoning frame's wire sequence is the canonical Reasoning
    message triplet START→CONTENT→END, one shared messageId, role "reasoning",
    CONTENT delta = the whole reasoning text (what a generic client renders)."""
    start, content, end = encode_frame_wire(
        DisplayFrame(OutboxMessage(kind="reasoning", text=_REASONING_TEXT))
    )

    assert [start.type, content.type, end.type] == [
        REASONING_MESSAGE_START,
        REASONING_MESSAGE_CONTENT,
        REASONING_MESSAGE_END,
    ]
    mid = content.data.get("messageId")
    assert mid, "the CONTENT event carries a non-empty messageId"
    assert {start.data.get("messageId"), end.data.get("messageId")} == {mid}
    assert start.data.get("role") == "reasoning"
    assert content.data.get("role") == "reasoning"
    assert content.data["delta"] == _REASONING_TEXT


def test_reasoning_is_standard_not_custom() -> None:
    """Tier 2: the reasoning frame maps to a STANDARD Reasoning* event, not a
    reyn.* CUSTOM one — the P6a private→standard move (see the profile-completeness
    gate for the profile-entry consequence)."""
    ev = encode_frame(DisplayFrame(OutboxMessage(kind="reasoning", text="t")))
    assert ev.type == REASONING_MESSAGE_CONTENT
    assert ev.type != CUSTOM


def test_only_content_carries_reyn_start_end_decode_to_none() -> None:
    """Tier 2: SR6 — the reasoning START/END are generic scaffold (no _reyn →
    decode None); only CONTENT reconstructs the reyn frame, preserving meta — the
    1 frame ⇄ 1 _reyn-event invariant that keeps reyn bit-identical."""
    seq = encode_frame_wire(
        DisplayFrame(
            OutboxMessage(kind="reasoning", text="hi", meta={"chain_id": "c1"})
        )
    )
    start, content, end = seq

    assert decode_event(start.type, start.data) is None
    assert decode_event(end.type, end.data) is None

    (reyn_bearing,) = [e for e in seq if "_reyn" in e.data]
    assert reyn_bearing is content
    decoded = decode_event(content.type, content.data)
    assert isinstance(decoded, DisplayFrame)
    assert decoded.message.kind == "reasoning"
    assert decoded.message.text == "hi"
    assert decoded.message.meta == {"chain_id": "c1"}


# ── SR1: display-off → zero Reasoning* events (by construction) ───────────────


async def _noop(*a, **k):
    return {}


def _mk_host(reasoning_config, *, outbox: list, history: list) -> RouterHostAdapter:
    events = EventLog(subscribers=[])
    workspace = Path(".reyn") / "agents" / "t"
    return RouterHostAdapter(
        agent_name="t", agent_role="r", output_language="en",
        allowed_mcp=None, permission_resolver=None,
        mcp_servers=None, project_context="", events=events,
        resolver=ModelResolver({}),
        memory=MemoryService(
            agent_workspace_dir=workspace, events=events,
            file_write=_noop, file_read=_noop, file_delete=_noop,
            file_regenerate_index=_noop,
        ),
        journal=None, agent_registry=None,
        agent_workspace_dir=workspace,
        file_read=_noop, file_write=_noop, file_delete=_noop,
        file_regenerate_index=_noop,
        mcp_list_servers=_noop, mcp_list_tools=_noop, mcp_call_tool=_noop,
        send_to_agent=_noop,
        put_outbox=lambda msg: outbox.append(msg) or _noop(),
        append_history=lambda msg: history.append(msg),
        delegation_tracker=lambda: [],
        agent_replies_tracker=lambda: [], turn_budget_engine=None,
        environment_backend=None,
        reasoning_config=reasoning_config,
        reasoning_continuity_section_fn=lambda: "",
    )


async def _wire_types_for(frames: list) -> set:
    async def _src():
        for f in frames:
            yield f

    emitter = AgUiEmitter(_src(), lambda: None)
    sse = "".join([chunk async for chunk in emitter.stream()])
    return {ev.type for ev in parse_sse_blocks(sse.split("\n"))}


def test_display_off_yields_zero_reasoning_events() -> None:
    """Tier 2: SR1 — reasoning display OFF ⇒ the host chokepoint emits NO
    kind="reasoning" frame ⇒ the emitter puts ZERO Reasoning* events on the wire.
    The gate holds by construction (no reasoning frame exists), not via a new
    codec-side suppression."""
    outbox: list = []
    host = _mk_host(
        ReasoningConfig(continuity=True, display=False), outbox=outbox, history=[]
    )
    asyncio.run(
        host.put_outbox(
            kind="agent", text="answer",
            meta={"chain_id": "c1", "reasoning": _REASONING_TEXT},
        )
    )
    # By construction: no reasoning frame was emitted (display off).
    assert [m.kind for m in outbox] == ["agent"]

    frames = [DisplayFrame(m) for m in outbox]
    frames.append(DisplayFrame(OutboxMessage(kind="__end__", text="")))
    wire_types = asyncio.run(_wire_types_for(frames))

    reasoning_events = {
        REASONING_MESSAGE_START, REASONING_MESSAGE_CONTENT, REASONING_MESSAGE_END,
    }
    assert not (wire_types & reasoning_events), wire_types


def test_display_on_yields_reasoning_triplet_on_the_wire() -> None:
    """Tier 2: SR1 falsification-companion — display ON ⇒ a reasoning frame IS
    emitted ⇒ the Reasoning* triplet appears on the wire (so the display-off
    assertion above is not vacuous)."""
    outbox: list = []
    host = _mk_host(
        ReasoningConfig(continuity=True, display=True), outbox=outbox, history=[]
    )
    asyncio.run(
        host.put_outbox(
            kind="agent", text="answer",
            meta={"chain_id": "c1", "reasoning": _REASONING_TEXT},
        )
    )
    assert "reasoning" in [m.kind for m in outbox]

    frames = [DisplayFrame(m) for m in outbox]
    frames.append(DisplayFrame(OutboxMessage(kind="__end__", text="")))
    wire_types = asyncio.run(_wire_types_for(frames))

    assert {
        REASONING_MESSAGE_START, REASONING_MESSAGE_CONTENT, REASONING_MESSAGE_END,
    } <= wire_types


# ── bit-identical: reyn render off the P6a wire == direct baseline ────────────


_NOW = 10_000.0


async def _sse_lines(text):
    for line in text.split("\n"):
        yield line


async def _frame_source(frames):
    for f in frames:
        yield f


@pytest.mark.asyncio
async def test_reyn_render_of_reasoning_is_bit_identical_through_wire(monkeypatch) -> None:
    """Tier 2: the reyn client rendering a reasoning frame decoded off the P6a
    Reasoning* wire produces byte-identical display output to the direct-renderer
    baseline — the standard-surface widening is additive and did not perturb reyn.
    (The reasoning triplet is genuinely present on the wire, else this is vacuous.)"""
    monkeypatch.setattr(time, "monotonic", lambda: _NOW)

    script = [
        DisplayFrame(OutboxMessage(kind="reasoning", text=_REASONING_TEXT)),
        DisplayFrame(OutboxMessage(kind="agent", text="the answer is 391")),
    ]

    # Baseline: drive the renderer directly, no transport at all.
    base_r = InlineChatRenderer()
    base_buf = StringIO()
    monkeypatch.setattr(sys, "__stdout__", base_buf)
    for f in script:
        base_r.message(f.message)
    base_stdout = base_buf.getvalue()
    assert base_stdout, "the baseline rendered something non-trivial"

    # Server side: emit the P6a wire.
    end = DisplayFrame(OutboxMessage(kind="__end__", text=""))
    emitter = AgUiEmitter(_frame_source([*script, end]), lambda: None)
    sse = "".join([chunk async for chunk in emitter.stream()])
    wire_types = {ev.type for ev in parse_sse_blocks(sse.split("\n"))}
    assert REASONING_MESSAGE_START in wire_types
    assert REASONING_MESSAGE_END in wire_types

    # Client side: decode the SAME wire, drive the reyn renderer.
    async def _noop_send(_payload):
        return None

    transport = AgUiTransport(_sse_lines(sse), _noop_send)
    ag_r = InlineChatRenderer()
    ag_buf = StringIO()
    monkeypatch.setattr(sys, "__stdout__", ag_buf)
    await asyncio.wait_for(run_output_loop(transport, ag_r), timeout=2.0)
    ag_stdout = ag_buf.getvalue()

    assert ag_stdout == base_stdout
