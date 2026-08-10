"""Tier 2: #3475 — the MCP-tools-cache priming chain must run before the
FIRST LLM call of a turn regardless of the turn's `kind`, not just `user`.

Background (#3429 arc / #3463 review / #3475 investigation): the FP-0037
lazy MCP-tools-cache priming chain (`maybe_refresh_mcp_tools_from_yaml` →
`maybe_reload_mcp_tools_cache_from_disk` → `ensure_mcp_tools_cached`) used to
run ONLY inside `Session._handle_user_message` — the `kind="user"` turn
handler. `Session._handle_hook_message` (`kind="hook"`) and
`InterAgentMessaging.handle_agent_request` (`kind="agent_request"`, the path
a freshly `spawn_ephemeral_session`-ed worker's first inbound message takes)
both call `Session._run_router_loop` directly and never ran the chain. A
session whose FIRST turn arrives as one of those two kinds therefore built
its first `tools=` payload (and thus `call_mcp_tool`/`describe_mcp_tool`'s
`mcp_tool_name` enum) against a never-primed (`None`) cache — permanently for
that session's life, since `ensure_mcp_tools_cached`'s populated-guard is
one-shot (`if self._mcp_tools_cache is not None: return`).

This is the production-side question #3475 asks to settle: is the priming
chain await-ordered ahead of the first LLM call, or does it merely "usually
run in time"? Answer for `hook`/`agent_request`-first sessions: neither —
it never ran at all. The fix moves the chain from `_handle_user_message`
into `Session._run_router_loop` itself — the one seam every turn kind
(user / hook / agent_request / pipeline_result) funnels through before its
first LLM call (see that method's own docstring) — making the guarantee
structural instead of kind-dependent.

Verified here via the SAME real-callable seam
`tests/test_2242_hard_cancel.py` uses to isolate the mechanism under test
from RouterLoop's own internals: `Session._loop_driver.run_turn` is replaced
with a plain async function (method-assigned onto the instance — not a
mock) that captures `session.router_host.mcp_tools_cache_snapshot` (public
property) the INSTANT it is invoked. Because the real `_run_router_loop`
awaits the priming chain immediately before calling `run_turn`, the captured
snapshot proves whether priming already ran by then.

No unittest.mock/AsyncMock/MagicMock/patch. Private-state access: NONE —
the only session internals read are the public `router_host` property and
its public `mcp_tools_cache_snapshot`.

strip-falsify: reverting the `_run_router_loop`/`_handle_user_message` change
(the chain moved back to only firing for kind="user") turns
`test_agent_request_first_turn_primes_mcp_cache_before_first_llm_call` and
`test_hook_first_turn_primes_mcp_cache_before_first_llm_call` RED — the
captured snapshot is `None` because priming never ran for those kinds.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.services.mcp_cache_file import (
    ToolsAnswered,
    cache_file_path,
    write_cache,
)
from reyn.runtime.session import Session
from tests._support.agent_session import make_session

_SERVER = "reyn_markitdown"
_TOOLS = [{"name": "convert_to_markdown", "description": "convert a uri to markdown"}]


def _answers(by_server: dict[str, list[dict]]) -> dict[str, ToolsAnswered]:
    """Lift plain tool lists to the ANSWER variant the cache file accepts (#3520)."""
    return {name: ToolsAnswered(tools=tools) for name, tools in by_server.items()}


def _make_session(tmp_path: Path) -> Session:
    """A session with one configured MCP server, warm-startable from disk so
    `ensure_mcp_tools_cached()` never needs a real subprocess probe (mirrors
    `tests/test_session_refresh_mcp_servers.py`'s own technique)."""
    write_cache(cache_file_path(tmp_path / ".reyn" / "state"), _answers({_SERVER: _TOOLS}))
    return make_session(
        agent_name="fp3475-agent",
        mcp_servers={_SERVER: {"description": "markitdown"}},
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snapshot.json",
    )


def _install_snapshot_capturing_run_turn(session: Session) -> dict:
    """Replace `_loop_driver.run_turn` with a real, plain async function
    (instance method-assignment, not a mock) that snapshots the MCP tools
    cache the instant it is invoked, then ends the turn immediately (no LLM
    call, no tool loop) — the test only cares about ordering, not the turn's
    outcome."""
    box: dict = {"snapshot": "UNSET"}

    async def _capturing_run_turn(user_text: str, chain_id: str) -> None:
        box["snapshot"] = session.router_host.mcp_tools_cache_snapshot

    session._loop_driver.run_turn = _capturing_run_turn
    return box


@pytest.mark.asyncio
async def test_agent_request_first_turn_primes_mcp_cache_before_first_llm_call(
    tmp_path: Path,
) -> None:
    """Tier 2: a session's FIRST turn arriving as `kind="agent_request"`
    (the `spawn_ephemeral_session` + inbound-request shape a worker session
    sees) has the MCP tools cache populated before `run_turn` — its first
    LLM call — is invoked."""
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        session = _make_session(tmp_path)
        box = _install_snapshot_capturing_run_turn(session)

        assert session.router_host.mcp_tools_cache_snapshot is None, (
            "cache must be unprimed before the first turn runs"
        )

        await session.submit_agent_request(
            from_agent="peer", request="hello", depth=1, chain_id="chain-1",
        )
        await session.run_one_iteration()

        assert box["snapshot"] != "UNSET", "run_turn was never invoked"
        assert box["snapshot"] is not None, (
            "MCP tools cache must be primed before the first LLM call of an "
            "agent_request-kind first turn — it was never populated"
        )
        assert _SERVER in box["snapshot"], (
            f"expected {_SERVER!r} in the primed cache, got {box['snapshot']!r}"
        )
    finally:
        os.chdir(old_cwd)


@pytest.mark.asyncio
async def test_hook_first_turn_primes_mcp_cache_before_first_llm_call(
    tmp_path: Path,
) -> None:
    """Tier 2: a session's FIRST turn arriving as `kind="hook"` (an E
    self-continuation push) also has the MCP tools cache populated before
    `run_turn` is invoked."""
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        session = _make_session(tmp_path)
        box = _install_snapshot_capturing_run_turn(session)

        assert session.router_host.mcp_tools_cache_snapshot is None, (
            "cache must be unprimed before the first turn runs"
        )

        await session.inbox.put((
            "hook",
            {"name": "session_start", "text": "wake up", "chain_id": "chain-2"},
        ))
        await session.run_one_iteration()

        assert box["snapshot"] != "UNSET", "run_turn was never invoked"
        assert box["snapshot"] is not None, (
            "MCP tools cache must be primed before the first LLM call of a "
            "hook-kind first turn — it was never populated"
        )
        assert _SERVER in box["snapshot"], (
            f"expected {_SERVER!r} in the primed cache, got {box['snapshot']!r}"
        )
    finally:
        os.chdir(old_cwd)
