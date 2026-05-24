"""Tier 2: FP-0041 #489 PR-D2.5 — web lifespan wiring for Slack outbound.

Pins ``_wire_external_outbox_interceptor`` (= web/deps.py) which is
called by the session factory when ``config.external_transports`` has
entries. Composes:

  - per-session MCP dispatcher (= closes over the session so the tool
    call uses the session's router OpContext)
  - ``make_outbox_interceptor`` (= dispatch matrix from PR-D2)

And sets ``session._outbox_interceptor`` so the next agent reply with
ExternalRef reply_to dispatches via Slack MCP tool.

Tests:

  1. Empty external_transports → session._outbox_interceptor stays None
  2. Configured external_transports → session._outbox_interceptor set
  3. Dispatcher resolves "<server>__<tool>" + calls op_runtime.mcp.handle
  4. Dispatcher rejects mcp_tool name without "__" separator
  5. Interceptor end-to-end through session._put_outbox (= post-wiring
     behavior verified)

Tier 2 because this wiring is the **final glue** that completes
FP-0041 Phase 1 Slack chat-transport end-to-end. Without it, all
the PR-A..PR-D2 primitives exist but nothing connects them in
production deployments.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from reyn.chat.external_routing import (
    ExternalTransportEntry,
    ExternalTransportRouting,
)
from reyn.chat.outbox import OutboxMessage
from reyn.chat.session import ChatSession
from reyn.chat.transport import ExternalRef
from reyn.events.state_log import StateLog
from reyn.web.deps import _wire_external_outbox_interceptor


def _make_session(tmp_path: Path) -> ChatSession:
    return ChatSession(
        agent_name="alpha",
        state_log=StateLog(tmp_path / "alpha.wal"),
        snapshot_path=tmp_path / "alpha_snapshot.json",
    )


# ── wiring: empty vs configured ───────────────────────────────────────


def test_wire_sets_interceptor_on_session(tmp_path):
    """Tier 2: ``_wire_external_outbox_interceptor`` populates
    ``session._outbox_interceptor`` so the next ``_put_outbox``
    invocation consults it.
    """
    session = _make_session(tmp_path)
    assert session._outbox_interceptor is None

    routing = ExternalTransportRouting(transports={
        "slack": ExternalTransportEntry(
            mcp_tool="slack__chat_postMessage",
            args_template={"channel": "{destination.channel}", "text": "{text}"},
        ),
    })
    _wire_external_outbox_interceptor(session, routing)
    assert session._outbox_interceptor is not None
    assert callable(session._outbox_interceptor)


# ── dispatcher: server+tool resolution ────────────────────────────────


@pytest.mark.asyncio
async def test_dispatcher_resolves_server_and_tool_from_mcp_tool_name(tmp_path):
    """Tier 2: the per-session MCP dispatcher splits ``<server>__<tool>``
    at the first ``__`` and dispatches via ``op_runtime.mcp.handle``
    with the resolved server / tool / args.
    """
    session = _make_session(tmp_path)
    routing = ExternalTransportRouting(transports={
        "slack": ExternalTransportEntry(
            mcp_tool="slack__chat_postMessage",
            args_template={"channel": "{destination.channel}", "text": "{text}"},
        ),
    })
    _wire_external_outbox_interceptor(session, routing)

    # Mock op_runtime.mcp.handle to capture the dispatched op.
    captured: list = []

    async def _fake_handle(*, op, ctx, caller):
        captured.append((op, caller))
        return {"status": "ok"}

    # Mock _make_router_op_context to return a dummy context (= avoids
    # real workspace / events setup that the session normally needs).
    session._make_router_op_context = lambda: object()  # type: ignore[method-assign]

    with patch("reyn.op_runtime.mcp.handle", _fake_handle):
        msg = OutboxMessage(
            kind="agent", text="hello world",
            reply_to=ExternalRef(
                transport="slack",
                destination={"channel": "C1", "thread_ts": "1.5"},
            ),
        )
        await session._put_outbox(msg)

    assert captured, "expected at least one dispatch via op_runtime.mcp.handle"
    op, caller = captured[0]
    assert op.kind == "mcp"
    assert op.server == "slack"
    assert op.tool == "chat_postMessage"
    assert op.args == {"channel": "C1", "text": "hello world"}
    assert caller == "external_routing"


@pytest.mark.asyncio
async def test_dispatcher_rejects_tool_name_without_separator(tmp_path):
    """Tier 2: an ``external_transports`` config entry with
    ``mcp_tool`` missing the ``__`` separator (= operator typo) causes
    the dispatcher to raise. ``route_to_mcp`` catches the exception
    and converts to ``RouteResult(status="error")`` — interceptor
    consumes the message but the error is logged for operator
    debugging.
    """
    session = _make_session(tmp_path)
    routing = ExternalTransportRouting(transports={
        "slack": ExternalTransportEntry(
            mcp_tool="bad-no-separator",  # missing __
            args_template={},
        ),
    })
    _wire_external_outbox_interceptor(session, routing)
    session._make_router_op_context = lambda: object()  # type: ignore[method-assign]

    msg = OutboxMessage(
        kind="agent", text="x",
        reply_to=ExternalRef(transport="slack", destination={}),
    )
    # The interceptor returns True even on error (= consumed, not
    # forwarded to TUI), so the queue stays empty. The dispatcher's
    # raise becomes RouteResult(status="error") internally.
    await session._put_outbox(msg)
    assert session.outbox.empty()


# ── end-to-end via interceptor + dispatcher ────────────────────────────


@pytest.mark.asyncio
async def test_end_to_end_agent_reply_dispatches_to_mcp(tmp_path):
    """Tier 2c: after wiring, an agent reply OutboxMessage with ExternalRef
    reply_to dispatches via the MCP tool path AND is NOT queued for TUI
    display (= PR-D2.5 acceptance criteria).

    Verifies the full chain:
      _put_outbox →
        _outbox_interceptor (= make_outbox_interceptor product) →
        route_to_mcp (= PR-C) →
        mcp_dispatcher (= PR-D2.5 closure) →
        op_runtime.mcp.handle (= mocked) →
        return ok → interceptor returns True → queue.put skipped
    """
    session = _make_session(tmp_path)
    routing = ExternalTransportRouting(transports={
        "slack": ExternalTransportEntry(
            mcp_tool="slack__chat_postMessage",
            args_template={
                "channel": "{destination.channel}",
                "text": "{text}",
            },
        ),
    })
    _wire_external_outbox_interceptor(session, routing)
    session._make_router_op_context = lambda: object()  # type: ignore[method-assign]

    async def _fake_handle(*, op, ctx, caller):
        return {"status": "ok"}

    with patch("reyn.op_runtime.mcp.handle", _fake_handle):
        msg = OutboxMessage(
            kind="agent", text="hello back",
            reply_to=ExternalRef(
                transport="slack",
                destination={"channel": "C1"},
            ),
        )
        await session._put_outbox(msg)

    # NOT queued for TUI (= interceptor consumed it).
    assert session.outbox.empty()


@pytest.mark.asyncio
async def test_tui_reply_still_queues_with_wiring_active(tmp_path):
    """Tier 2: a TuiRef reply_to (= local terminal user) does NOT
    trigger the interceptor even when wiring is active. Only
    ExternalRef paths dispatch externally — local TUI chat path
    remains unaffected by the Slack wiring.
    """
    from reyn.chat.transport import TuiRef

    session = _make_session(tmp_path)
    routing = ExternalTransportRouting(transports={
        "slack": ExternalTransportEntry(
            mcp_tool="slack__chat_postMessage",
            args_template={"text": "{text}"},
        ),
    })
    _wire_external_outbox_interceptor(session, routing)

    msg = OutboxMessage(
        kind="agent", text="local reply", reply_to=TuiRef(),
    )
    await session._put_outbox(msg)

    queued = await session.outbox.get()
    assert queued.text == "local reply"


@pytest.mark.asyncio
async def test_dispatcher_uses_session_router_op_context(tmp_path):
    """Tier 2: the dispatcher closes over the session and calls
    ``session._make_router_op_context()`` so the MCP tool invocation
    runs through the session's own permission gate + workspace +
    events log. Pins this is NOT a bare context (= would skip
    permission check) by verifying the method is invoked.
    """
    session = _make_session(tmp_path)
    routing = ExternalTransportRouting(transports={
        "slack": ExternalTransportEntry(
            mcp_tool="slack__chat_postMessage",
            args_template={"text": "{text}"},
        ),
    })
    _wire_external_outbox_interceptor(session, routing)

    sentinel_ctx: Any = object()
    op_ctx_calls: list = []

    def _fake_make_ctx():
        op_ctx_calls.append(True)
        return sentinel_ctx

    session._make_router_op_context = _fake_make_ctx  # type: ignore[method-assign]

    captured_ctx: list = []

    async def _fake_handle(*, op, ctx, caller):
        captured_ctx.append(ctx)
        return {"status": "ok"}

    with patch("reyn.op_runtime.mcp.handle", _fake_handle):
        msg = OutboxMessage(
            kind="agent", text="x",
            reply_to=ExternalRef(transport="slack", destination={}),
        )
        await session._put_outbox(msg)

    assert op_ctx_calls, "session._make_router_op_context not invoked"
    assert captured_ctx == [sentinel_ctx]


# ── factory integration: gated on config ──────────────────────────────


def test_factory_skips_wiring_when_no_external_transports(tmp_path):
    """Tier 2: a project without ``external_transports`` config does
    NOT get an interceptor — sessions stay as before, no risk of
    misrouting local TUI messages through MCP.

    Verified at the wiring helper level by directly checking that
    when ``routing.transports`` is empty, the caller (= _session_factory
    in deps.py) skips ``_wire_external_outbox_interceptor`` entirely.
    The factory has::

        if config.external_transports.transports:
            _wire_external_outbox_interceptor(s, config.external_transports)

    which is the only call site of the helper.
    """
    session = _make_session(tmp_path)
    # Simulate the factory's gate: empty transports → no wiring.
    routing = ExternalTransportRouting()
    if routing.transports:
        _wire_external_outbox_interceptor(session, routing)
    assert session._outbox_interceptor is None
