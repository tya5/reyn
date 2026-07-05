"""Tier 2: #2597 slice ③ — MCP elicitation (server->client structured input).

Real instances only, per the testing policy: no ``mock.patch`` / ``MagicMock``.
The server side is a REAL FastMCP stdio subprocess
(``tests/_support/mcp_elicitation_server.py``) that calls the genuine
``fastmcp.Context.elicit`` API (SEP-1686 ``elicitation/create``) — so every
test here exercises the actual MCP protocol exchange, not a hand-rolled fake
of it. The client side is a REAL ``MCPConnectionService`` +
``reyn.mcp.elicitation.build_elicitation_handler``. Only the HUMAN answer
source is a test double (``_ScriptedBus`` / ``_HangingBus``) — concrete
``RequestBus`` implementations (the same pattern
``test_2095_shell_hook_consent_bus.py``'s ``_RecordingBus`` uses), not mocks.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from reyn.intervention_choices import ACCEPT
from reyn.mcp.connection_service import MCPConnectionService
from reyn.user_intervention import InterventionAnswer, UserIntervention

_SUPPORT_DIR = Path(__file__).parent / "_support"
_ELICIT_SERVER = _SUPPORT_DIR / "mcp_elicitation_server.py"
_CFG = {"type": "stdio", "command": sys.executable, "args": [str(_ELICIT_SERVER)]}


class _ScriptedBus:
    """A real ``RequestBus`` that answers each ``request(iv)`` with the next
    preset answer in ``script`` (in call order) and records every iv seen —
    lets a test assert on prompt content (server attribution, sensitive-field
    warning) as well as drive the sequential per-field flow deterministically."""

    def __init__(self, script: list[InterventionAnswer]) -> None:
        self._script = list(script)
        self.seen: list[UserIntervention] = []

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.seen.append(iv)
        return self._script.pop(0)


class _HangingBus:
    """A real ``RequestBus`` whose ``request`` never resolves — simulates a
    listener that is attached but never answers, so the elicitation
    handler's OWN timeout (not the bus) is what fires."""

    def __init__(self) -> None:
        self.seen: list[UserIntervention] = []

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.seen.append(iv)
        await asyncio.sleep(9999)
        raise AssertionError("unreachable")  # pragma: no cover


class _EventRecorder:
    """A real emit_sink — records every (event_type, fields) call."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event_type: str, **fields) -> None:
        self.events.append((event_type, fields))


@pytest.mark.asyncio
async def test_bool_schema_accept_routes_through_bus_with_attribution():
    """Tier 2: (a) a bool-schema elicitation is routed through the intervention
    bus with forced server-attribution; a human ACCEPT (gate) + "true" (field)
    answer resolves to {action: accept, content: {value: True}} — observed via
    the REAL server's own rendering of the elicitation result."""
    bus = _ScriptedBus([
        InterventionAnswer(choice_id=ACCEPT),  # the gate: engage with this elicitation
        InterventionAnswer(choice_id="true"),  # the single bool field
    ])
    events = _EventRecorder()
    service = MCPConnectionService(
        emit_sink=events, elicitation_bus=bus, elicitation_gate=lambda: True,
    )
    try:
        client = await service.get("srv", _CFG)
        result = await client.call_tool("confirm", {"question": "Delete the file?"})
        assert result["content"][0]["text"] == "True", (
            "server observed action=accept, data=True"
        )
    finally:
        await service.aclose()

    # Server attribution: every prompt the human saw carries the server name
    # (either the full gate disclaimer, or the short per-field prefix); the
    # gate prompt specifically carries the explicit "NOT reyn" disclaimer.
    assert bus.seen, "the elicitation must route through the bus"
    for iv in bus.seen:
        assert "srv" in iv.prompt
    gate_ivs = [iv for iv in bus.seen if iv.kind == "mcp_elicitation"]
    assert gate_ivs and "NOT reyn" in gate_ivs[0].prompt

    event_names = [name for name, _ in events.events]
    assert "mcp_elicitation_requested" in event_names
    assert "mcp_elicitation_answered" in event_names


@pytest.mark.asyncio
async def test_headless_no_bus_auto_declines():
    """Tier 2: (b) no elicitation_bus wired at all (headless / no attached
    listener) -> auto-decline (never cancel) + mcp_elicitation_auto_declined,
    without ever touching a bus (there isn't one)."""
    events = _EventRecorder()
    service = MCPConnectionService(emit_sink=events)  # no elicitation_bus
    try:
        client = await service.get("srv", _CFG)
        result = await client.call_tool("confirm", {"question": "Delete the file?"})
        assert result["content"][0]["text"] == "decline"
    finally:
        await service.aclose()

    event_names = [name for name, _ in events.events]
    assert "mcp_elicitation_auto_declined" in event_names
    assert "mcp_elicitation_answered" not in event_names


@pytest.mark.asyncio
async def test_gate_false_auto_declines_even_with_bus_wired():
    """Tier 2: (b) variant — a bus IS wired but ``elicitation_gate`` reports no
    live listener right now (mirrors #2095's consent_gate re-check) -> same
    auto-decline path as no bus at all; the bus is never consulted."""
    bus = _ScriptedBus([])
    events = _EventRecorder()
    service = MCPConnectionService(
        emit_sink=events, elicitation_bus=bus, elicitation_gate=lambda: False,
    )
    try:
        client = await service.get("srv", _CFG)
        result = await client.call_tool("confirm", {"question": "Delete?"})
        assert result["content"][0]["text"] == "decline"
    finally:
        await service.aclose()
    assert bus.seen == [], "gate=False must never reach the bus"


@pytest.mark.asyncio
async def test_timeout_returns_cancel():
    """Tier 2: (c) no answer within the per-server deadline -> cancel (nobody
    judged), observed via the server's own action-branch rendering, plus a
    mcp_elicitation_timed_out event."""
    bus = _HangingBus()
    events = _EventRecorder()
    cfg = {**_CFG, "elicitation_timeout_seconds": 0.2}
    service = MCPConnectionService(
        emit_sink=events, elicitation_bus=bus, elicitation_gate=lambda: True,
    )
    try:
        client = await service.get("srv", cfg)
        result = await client.call_tool("confirm", {"question": "Delete?"})
        assert result["content"][0]["text"] == "cancel"
    finally:
        await service.aclose()

    event_names = [name for name, _ in events.events]
    assert "mcp_elicitation_timed_out" in event_names


@pytest.mark.asyncio
async def test_sensitive_field_name_triggers_extra_warning():
    """Tier 2: (d) a field named ``api_key`` (matches the sensitive-keyword
    list) gets an EXTRA confirmation intervention (kind
    ``mcp_elicitation_sensitive_field``) before the free-text prompt, carrying
    the "sent to server" warning — distinct from a non-sensitive field name,
    which skips straight to the free-text prompt."""
    bus = _ScriptedBus([
        InterventionAnswer(choice_id=ACCEPT),   # gate
        InterventionAnswer(choice_id=ACCEPT),   # sensitive-field extra confirm
        InterventionAnswer(text="sekrit-val"),  # the free-text value itself
    ])
    service = MCPConnectionService(elicitation_bus=bus, elicitation_gate=lambda: True)
    try:
        client = await service.get("srv", _CFG)
        result = await client.call_tool("ask_credential", {"field_name": "api_key"})
        assert result["content"][0]["text"] == "sekrit-val"
    finally:
        await service.aclose()

    sensitive_ivs = [iv for iv in bus.seen if iv.kind == "mcp_elicitation_sensitive_field"]
    assert sensitive_ivs, "a sensitive field name must trigger the extra warning"
    assert "sent" in sensitive_ivs[0].prompt.lower()
    assert "srv" in sensitive_ivs[0].prompt


@pytest.mark.asyncio
async def test_non_sensitive_field_name_skips_warning():
    """Tier 2: (d) control case — a field named ``comment`` (no sensitive
    keyword match) never gets the extra warning step."""
    bus = _ScriptedBus([
        InterventionAnswer(choice_id=ACCEPT),   # gate
        InterventionAnswer(text="just a note"),  # straight to free text
    ])
    service = MCPConnectionService(elicitation_bus=bus, elicitation_gate=lambda: True)
    try:
        client = await service.get("srv", _CFG)
        result = await client.call_tool("ask_credential", {"field_name": "comment"})
        assert result["content"][0]["text"] == "just a note"
    finally:
        await service.aclose()

    assert not [iv for iv in bus.seen if iv.kind == "mcp_elicitation_sensitive_field"]


@pytest.mark.asyncio
async def test_audit_events_record_field_keys_not_values():
    """Tier 2: (e) the requested/answered events record the schema's field KEY
    names (for observability) but never the user's answer VALUES — a
    completed elicitation with a sensitive-looking answer must not leak that
    value into the EventLog."""
    bus = _ScriptedBus([
        InterventionAnswer(choice_id=ACCEPT),
        InterventionAnswer(choice_id=ACCEPT),
        InterventionAnswer(text="super-secret-value-12345"),
    ])
    events = _EventRecorder()
    service = MCPConnectionService(
        emit_sink=events, elicitation_bus=bus, elicitation_gate=lambda: True,
    )
    try:
        client = await service.get("srv", _CFG)
        result = await client.call_tool("ask_credential", {"field_name": "api_key"})
        assert result["content"][0]["text"] == "super-secret-value-12345"
    finally:
        await service.aclose()

    for _name, fields in events.events:
        serialized = repr(fields)
        assert "super-secret-value-12345" not in serialized, (
            "the answer VALUE must never appear in an emitted event"
        )
        if "field_keys" in fields:
            assert "api_key" in fields["field_keys"], (
                "the field KEY name is expected observability content"
            )


@pytest.mark.asyncio
async def test_multi_field_flat_schema_prompts_sequentially():
    """Tier 2: D1 — a THREE-field flat schema is answered ONE FIELD AT A TIME
    (never crammed into one free-text-parsed message): the gate, then exactly
    one intervention per field, in order."""
    bus = _ScriptedBus([
        InterventionAnswer(choice_id=ACCEPT),   # gate
        InterventionAnswer(text="alice"),       # name
        InterventionAnswer(text="3"),           # count
        InterventionAnswer(choice_id="true"),   # proceed
    ])
    service = MCPConnectionService(elicitation_bus=bus, elicitation_gate=lambda: True)
    try:
        client = await service.get("srv", _CFG)
        result = await client.call_tool("ask_multi", {})
        assert result["content"][0]["text"] == "alice|3|True"
    finally:
        await service.aclose()

    field_ivs = [iv for iv in bus.seen if iv.kind == "mcp_elicitation_field"]
    # Each prompt is "[MCP server 'srv'] <field>: <description>" — strip the
    # server-attribution prefix before extracting the field name.
    field_names = [iv.prompt.split("] ", 1)[1].split(":")[0] for iv in field_ivs]
    assert field_names == ["name", "count", "proceed"], (
        "one intervention per field, in the schema's declared order"
    )

