"""Tier 2: #4401 ③ — the ``/mcp retry <server>`` slash command end-to-end.

The mcp pane's "↻ retry probe" row (chrome.py, appended only under a
"failed" server row — see ``tests/interfaces/test_4401_mcp_pane_probe_
states.py``) submits this command. This pins the wire from the slash
handler through ``Session.retry_mcp_probe`` to the real
``RouterHostAdapter`` — a real Session (`make_session`), a real probe
callable shadowing the bound method (same established technique
``test_3520_unknown_probe_is_not_an_answer.py`` uses — a real callable, not
a mock), never a hand-rolled fake Session."""
from __future__ import annotations

import asyncio

import pytest

from reyn.interfaces.slash import REGISTRY
from reyn.interfaces.slash.mcp import mcp_cmd
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
    """Tier 2: end-to-end — the /mcp retry command drives a real re-probe
    through Session, and the pane's own read model (mcp_probe_state) sees
    the result, not a stale "failed" left over from the first probe."""
    session = make_session(
        agent_name="alice", mcp_servers={_SERVER: {"description": "markitdown"}},
    )
    probe = _FailThenSucceedProbe()
    session._router_host.mcp_list_tools = probe

    await session._router_host.ensure_mcp_tools_cached(per_server_timeout=0.05)
    states_before = {row["name"]: row for row in session.mcp_probe_state()}
    assert states_before[_SERVER]["state"] == "failed"

    probe.healthy = True
    await mcp_cmd(slash_ctx(session), f"retry {_SERVER}")

    states_after = {row["name"]: row for row in session.mcp_probe_state()}
    assert states_after[_SERVER] == {"name": _SERVER, "state": "answered", "tool_count": 1}


@pytest.mark.asyncio
async def test_retry_command_rejects_a_malformed_usage():
    """Tier 2: a malformed /mcp invocation (no server name) reports a
    usage error rather than raising or silently no-op'ing."""
    session = make_session(agent_name="alice")
    ctx = slash_ctx(session)
    await mcp_cmd(ctx, "retry")
    errors = [msg for msg in ctx.transport.displayed if msg.kind == "error"]
    assert any("usage:" in msg.text for msg in errors)
