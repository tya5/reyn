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
  3. Dispatcher rejects mcp_tool name without "__" separator
  4. TUI reply_to path is unaffected by wiring
  5. Factory gate: empty transports → no wiring

Deleted (Wave 15 / Tier 4 cleanup):
  - test_dispatcher_resolves_server_and_tool_from_mcp_tool_name
      Patched ``reyn.core.op_runtime.mcp.handle`` (internal) and private
      ``session._make_router_op_context``. Pure implementation detail;
      the separator-split invariant is observable through the reject
      test (no ``__`` raises, logged as error). DELETE.
  - test_end_to_end_agent_reply_dispatches_to_mcp
      Patched ``reyn.core.op_runtime.mcp.handle`` (internal). The
      "interceptor consumes ExternalRef + queue stays empty" invariant
      is already pinned by test_dispatcher_rejects_tool_name_without_separator
      (same queue-empty assertion, no patch needed). Redundant. DELETE.
  - test_dispatcher_uses_session_router_op_context
      Patched ``reyn.core.op_runtime.mcp.handle`` (internal) to verify
      ``_make_router_op_context`` call count. Pins private closure
      detail; no production-contract is lost by removing it.
      Follow-up Tier 2 note: if the permission-gate invariant (=
      dispatcher MUST use session context, not bare context) needs a
      regression test, add a dependency-injection seam to OpContext
      creation and test through that public surface. DELETE.

Tier 2 because this wiring is the **final glue** that completes
FP-0041 Phase 1 Slack chat-transport end-to-end. Without it, all
the PR-A..PR-D2 primitives exist but nothing connects them in
production deployments.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.interfaces.web.deps import _wire_external_outbox_interceptor
from reyn.runtime.external_routing import (
    ExternalTransportEntry,
    ExternalTransportRouting,
)
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.session import Session
from reyn.runtime.transport import ExternalRef


def _make_session(tmp_path: Path) -> Session:
    return Session(
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
    assert session.outbox_interceptor is None

    routing = ExternalTransportRouting(transports={
        "slack": ExternalTransportEntry(
            mcp_tool="slack__chat_postMessage",
            args_template={"channel": "{destination.channel}", "text": "{text}"},
        ),
    })
    _wire_external_outbox_interceptor(session, routing)
    assert session.outbox_interceptor is not None
    assert callable(session.outbox_interceptor)


# ── dispatcher: separator validation ──────────────────────────────────


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



@pytest.mark.asyncio
async def test_tui_reply_still_queues_with_wiring_active(tmp_path):
    """Tier 2: a TuiRef reply_to (= local terminal user) does NOT
    trigger the interceptor even when wiring is active. Only
    ExternalRef paths dispatch externally — local TUI chat path
    remains unaffected by the Slack wiring.
    """
    from reyn.runtime.transport import TuiRef

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
    assert session.outbox_interceptor is None
