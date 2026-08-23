"""Tier 2: #5185 — a remote client's MCP/tool/skill visibility pane and
MCP subscription rows reflect the SERVER's real state, not an
unconditional "(not wired)"/empty literal.

Owner live-observed: a real remote session's MCP pane showed
``"(not wired)"`` even though the session's subscriptions were genuinely
live. Root cause, measured (issuecomment-5384575651): ``agui/state.py``'s
own ``project_status`` dict (the sole wire vocabulary, #5098) never
forwarded ``visibility_items``/``mcp_subscriptions`` past the server's
projection, even though ``status._snapshot``'s own
``_session_visibility_items``/``_session_mcp_subscriptions`` calls
already compute real values server-side — so
``project_remote_snapshot`` (client-side) had nothing to read and fell
back to hand-typed ``None``/``[]`` literals regardless of the session's
real state.

Architect ruling (issuecomment-5384583727 / issuecomment-5384627324),
4 acceptance criteria:
①remote and local render the same row for the same session state
②a reader that genuinely cannot answer shows unknown ("(not wired)"),
   never a fabricated "(none)" — ``visibility_items``'s ``None`` must
   survive the wire, never collapse to ``[]``
③a subscription row never appears without its server-row foundation
   (``visibility_items`` supplies the row; ``mcp_subscriptions`` only
   augments an EXISTING row)
④a ``ChatReadModel`` that drops a declared capability key fails to
   construct, not silently reverts to "unsupported"

Same real, end-to-end pattern
``test_5094_remote_agent_roster_and_model_catalog.py`` already
establishes for this wire (real ``AgUiEmitter`` → real SSE →
real ``AgUiTransport`` parsing it back) — extended through
``project_remote_snapshot`` for these 2 keys instead.
"""
from __future__ import annotations

import pytest

from reyn.interfaces.inline.textual_chat.chrome import _mcp_pane_entries, _visibility_pane_rows
from reyn.interfaces.repl.read_model import project_remote_snapshot
from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.agui.state import project_status
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


async def _sse_lines(text):
    for line in text.split("\n"):
        yield line


async def _project_over_the_wire(server_snap: dict) -> dict:
    """Drives the real emitter -> real SSE -> real transport -> the real
    ``project_remote_snapshot`` projection the TUI's own chrome reads —
    exactly the path a live ``--connect`` uses. ``server_snap`` stands in
    for ``status._snapshot()``'s own dict; ``project_status`` (the SOLE
    wire vocabulary, #5098) decides what actually crosses."""

    def status_provider():
        return dict(server_snap)

    async def _display_frames():
        yield DisplayFrame(OutboxMessage(kind="__end__", text=""))

    emitter = AgUiEmitter(_display_frames(), status_provider)
    sse = "".join([chunk async for chunk in emitter.stream()])
    assert "STATE_SNAPSHOT" in sse

    async def _noop_send(_payload):
        return None

    transport = AgUiTransport(_sse_lines(sse), _noop_send)
    async for _f in transport.frames():
        pass  # draining applies STATE_SNAPSHOT to transport.status

    return project_remote_snapshot(transport.status.values)


# ── acceptance① / ② — real per-session state rides the wire honestly ────


@pytest.mark.asyncio
async def test_populated_visibility_and_subscriptions_ride_the_wire_end_to_end():
    """Tier 2: acceptance① — a session with real, non-empty visibility
    items and subscriptions produces the SAME data remote-side that
    local's own ``status._snapshot()`` would have held server-side."""
    visibility_items = [
        {"kind": "mcp", "name": "broker", "on": True, "denied": False, "denied_reason": None},
        {"kind": "mcp", "name": "some-server", "on": False, "denied": True, "denied_reason": "turn_context"},
        {"kind": "tool", "name": "shell", "on": True, "denied": False, "denied_reason": None},
    ]
    mcp_subscriptions = [
        {"server": "broker", "uris": ["broker://inbox/reyn-reviewer"], "unhonored": []},
    ]
    server_snap = {
        "cost_agent": 0.0, "cost_total": 0.0, "ctx_used": 0, "ctx_window": 0,
        "agent_tokens": 0, "attached_name": "default", "model": "m",
        "visibility_items": visibility_items,
        "mcp_subscriptions": mcp_subscriptions,
    }

    projected = await _project_over_the_wire(server_snap)

    assert projected["visibility_items"] == visibility_items
    assert projected["mcp_subscriptions"] == mcp_subscriptions
    assert projected["visibility_items_reported"] is True
    assert projected["mcp_subscriptions_reported"] is True

    # ① — local and remote render the SAME row for the SAME state: feed
    # the real snapshot dict, unmodified except for the wire round-trip,
    # into the same rendering functions a local session's snap would hit.
    local_rows = _visibility_pane_rows(server_snap, "mcp", "mcp_servers")
    remote_rows = _visibility_pane_rows(projected, "mcp", "mcp_servers")
    assert [(r.label, r.state, r.note) for r in local_rows] == [
        (r.label, r.state, r.note) for r in remote_rows
    ]
    local_entries = _mcp_pane_entries(server_snap)
    remote_entries = _mcp_pane_entries(projected)
    assert local_entries == remote_entries
    assert local_entries == [
        ("[on] broker  · subscribed", "/visibility off mcp broker"),
        ("    broker://inbox/reyn-reviewer", ""),
        ("[--] some-server  · denied while untrusted content is in context", ""),
    ]


@pytest.mark.asyncio
async def test_unwired_visibility_survives_the_wire_as_none_not_empty_list():
    """Tier 2: acceptance② — the CRITICAL witness. A session whose
    visibility seam is genuinely unwired (``visibility_items=None``,
    #3378) must reach the remote projection as ``None``, never ``[]``.
    Collapsing to ``[]`` would fabricate "nothing is narrowed"
    (renders "(none)") where the true fact is "cannot say"
    (renders "(not wired)") — exactly the lie #3378 exists to prevent,
    now reachable through the wire path instead of only the local one."""
    server_snap = {
        "cost_agent": 0.0, "cost_total": 0.0, "ctx_used": 0, "ctx_window": 0,
        "agent_tokens": 0, "attached_name": "default", "model": "m",
        "visibility_items": None,
        "mcp_subscriptions": [],
    }

    projected = await _project_over_the_wire(server_snap)

    assert projected["visibility_items"] is None, (
        f"None must survive the wire as None, got {projected['visibility_items']!r}"
    )
    assert projected["mcp_subscriptions"] == []

    rows = _visibility_pane_rows(projected, "mcp", "mcp_servers")
    assert [r.label for r in rows] == ["(not wired)"], (
        "a genuinely unwired seam must render '(not wired)', not '(none)'"
    )


@pytest.mark.asyncio
async def test_wired_but_empty_visibility_renders_none_not_not_wired():
    """Tier 2: acceptance②'s other half — a session whose seam IS wired
    and genuinely has nothing narrowed (``visibility_items=[]``) must
    render "(none)", distinguishably from the unwired "(not wired)" case
    above — the two must never collapse to the same rendered text."""
    server_snap = {
        "cost_agent": 0.0, "cost_total": 0.0, "ctx_used": 0, "ctx_window": 0,
        "agent_tokens": 0, "attached_name": "default", "model": "m",
        "visibility_items": [],
        "mcp_subscriptions": [],
    }

    projected = await _project_over_the_wire(server_snap)

    assert projected["visibility_items"] == []
    rows = _visibility_pane_rows(projected, "mcp", "mcp_servers")
    assert [r.label for r in rows] == ["(none)"]


# ── acceptance③ — a subscription row never appears without its server row ──


@pytest.mark.asyncio
async def test_subscriptions_without_a_visibility_foundation_render_nothing():
    """Tier 2: acceptance③ — the ordering dependency lead-coder's own
    measurement found (issuecomment-5384575651): ``mcp_subscriptions``
    augments a row ``visibility_items`` must supply first. Real
    subscription data with NO matching visibility item for that server
    must not produce a floating/orphaned row."""
    server_snap = {
        "cost_agent": 0.0, "cost_total": 0.0, "ctx_used": 0, "ctx_window": 0,
        "agent_tokens": 0, "attached_name": "default", "model": "m",
        "visibility_items": None,  # unwired — no server rows to attach to
        "mcp_subscriptions": [
            {"server": "broker", "uris": ["broker://inbox/x"], "unhonored": []},
        ],
    }

    projected = await _project_over_the_wire(server_snap)

    entries = _mcp_pane_entries(projected)
    assert entries == [("(not wired)", "")], (
        "a subscription with no visibility-item foundation must not "
        f"produce its own row; got {entries!r}"
    )


# ── strip-falsifier — same shape #5094's own test uses ───────────────────


@pytest.mark.asyncio
async def test_strip_falsifier_absent_wire_keys_revert_to_the_old_shape():
    """Tier 2: strip-falsifier — with the 2 keys absent from the server's
    OWN status dict (simulating ``project_status`` reverted to not carry
    them, the pre-#5185 shape), the client falls back to the SAME
    graceful defaults the bug reproduced — confirming the positive
    witnesses above genuinely depend on the wire carrying real data."""
    server_snap = {
        "cost_agent": 0.0, "cost_total": 0.0, "ctx_used": 0, "ctx_window": 0,
        "agent_tokens": 0, "attached_name": "default", "model": "m",
        # visibility_items/mcp_subscriptions deliberately absent — the
        # pre-#5185 server-side shape.
    }

    projected = await _project_over_the_wire(server_snap)

    assert projected["visibility_items"] is None
    assert projected["mcp_subscriptions"] == []


# ── the wire vocabulary itself (agui/state.py's project_status) ──────────


def test_project_status_forwards_visibility_items_preserving_none():
    """Tier 1: ``project_status`` (the sole wire vocabulary, #5098) must
    itself carry ``visibility_items`` through with NO ``[]`` default —
    a direct, no-transport-roundtrip pin of the exact line #5185 fixes,
    independent of the emitter/transport machinery the tests above also
    exercise."""
    out = project_status({"visibility_items": None, "mcp_subscriptions": []})
    assert "visibility_items" in out
    assert out["visibility_items"] is None
    assert out["mcp_subscriptions"] == []

    real_items = [{"kind": "mcp", "name": "x", "on": True, "denied": False, "denied_reason": None}]
    real_subs = [{"server": "x", "uris": [], "unhonored": None}]
    out2 = project_status({"visibility_items": real_items, "mcp_subscriptions": real_subs})
    assert out2["visibility_items"] == real_items
    assert out2["mcp_subscriptions"] == real_subs
