"""Tier 2: #4401 ③ — the ``/mcp retry <server>`` slash command end-to-end.

The mcp pane's "↻ retry probe" row (chrome.py, appended only under a
"failed" server row — see ``tests/interfaces/test_4401_mcp_pane_probe_
states.py``) submits this command. This pins the wire from the slash
handler through ``ClientTransport.request_mcp_retry`` (#3595 S4: the
BLOCKING'd shape from PR #5761's own review — ``Session.retry_mcp_probe``
was a public member added ONLY so a slash handler could reach it, folded
into a private ``Session._retry_mcp_probe`` reachable only from the
transport's own production implementations) to the real
``RouterHostAdapter`` — a real Session (`make_session`) bound through a
real ``SessionBoundTransport`` (the production ``ClientTransport`` a local
CUI attach actually holds — the SAME shape ``Session._slash_context``
builds, per its own docstring, not the test-only ``RecordingTransport``,
which stubs this method to a bare ``False`` and would never drive the
session at all), a real probe callable shadowing the bound method (same
established technique ``test_3520_unknown_probe_is_not_an_answer.py``
uses — a real callable, not a mock), never a hand-rolled fake Session."""
from __future__ import annotations

import asyncio

import pytest

from reyn.interfaces.slash import REGISTRY, SlashContext
from reyn.interfaces.slash.mcp import mcp_cmd
from reyn.interfaces.transport.session_bound import SessionBoundTransport
from tests._support.agent_session import make_session
from tests._support.slash import slash_ctx

_SERVER = "reyn_markitdown"
_TOOLS = [{"name": "convert_to_markdown", "description": "convert a uri to markdown"}]


def test_mcp_command_is_registered():
    """Tier 2: the /mcp command is registered on import (the mcp pane's
    retry row can dispatch it)."""
    assert REGISTRY.get("mcp") is not None


class _FailThenSucceedProbe:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.healthy = False

    async def __call__(self, server_name: str) -> list[dict]:
        self.calls.append(server_name)
        if not self.healthy:
            await asyncio.sleep(5.0)
        return [dict(t) for t in _TOOLS]


@pytest.mark.asyncio
async def test_retry_command_re_probes_and_the_pane_read_model_reflects_it():
    """Tier 2: end-to-end — the /mcp retry command, through a real
    production ``ClientTransport``, drives a real re-probe on the real
    Session, and the pane's own read model (mcp_probe_state) sees the
    result, not a stale "failed" left over from the first probe."""
    session = make_session(
        agent_name="alice", mcp_servers={_SERVER: {"description": "markitdown"}},
    )
    probe = _FailThenSucceedProbe()
    session._router_host.mcp_list_tools = probe

    await session._router_host.ensure_mcp_tools_cached(per_server_timeout=0.05)
    states_before = {row["name"]: row for row in session.mcp_probe_state()}
    assert states_before[_SERVER]["state"] == "failed"

    probe.healthy = True
    transport = SessionBoundTransport(session, display_sink=lambda msg: None)
    ctx = SlashContext(transport=transport, session=session)
    await mcp_cmd(ctx, f"retry {_SERVER}")

    states_after = {row["name"]: row for row in session.mcp_probe_state()}
    assert states_after[_SERVER] == {"name": _SERVER, "state": "answered", "tool_count": 1}


@pytest.mark.asyncio
async def test_a_transport_that_does_not_support_retry_reports_it_as_unavailable():
    """Tier 2: ``request_mcp_retry`` returning ``False`` (the base stub's
    own default — e.g. a remote AG-UI client, #4401's own disclosed scope
    boundary) surfaces as a clear error, never a silent success claim."""
    session = make_session(agent_name="alice")
    ctx = slash_ctx(session)  # RecordingTransport: request_mcp_retry stubs False
    await mcp_cmd(ctx, f"retry {_SERVER}")
    errors = [msg for msg in ctx.transport.displayed if msg.kind == "error"]
    assert any("not available" in msg.text for msg in errors)


@pytest.mark.asyncio
async def test_retry_command_rejects_a_malformed_usage():
    """Tier 2: a malformed /mcp invocation (no server name) reports a
    usage error rather than raising or silently no-op'ing."""
    session = make_session(agent_name="alice")
    ctx = slash_ctx(session)
    await mcp_cmd(ctx, "retry")
    errors = [msg for msg in ctx.transport.displayed if msg.kind == "error"]
    assert any("usage:" in msg.text for msg in errors)
