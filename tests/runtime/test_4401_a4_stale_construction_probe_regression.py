"""Tier 2: #4401 ① regression witness — driven reproduction of the exact
mechanism behind the 2 CI failures on PR #5763 (`test_2761_pr3_mcp_
immediate_probe.py::test_local_install_new_reachable_is_live_same_turn`,
`test_3475_mcp_probe_priming_all_turn_kinds.py`), written BEFORE choosing a
fix per lead-coder's explicit instruction.

★ This test DISCONFIRMS architect's own initial inferred mechanism (a
`grep`-only reading of the diff, explicitly caveated as unverified): NOT a
disk-warm-start REPLACE racing an install's own probe and discarding it via
the epoch check. Driving the actual production call chain (`RouterHost
Adapter.start_mcp_probe` → `await_mcp_probe_ready`) shows a DIFFERENT, more
precise mechanism:

1. `Session.__init__` kicks off `start_mcp_probe` against the roster AT
   CONSTRUCTION TIME. If that roster is empty (no mcp servers configured
   yet), the background task's own `ensure_mcp_tools_cached` finds
   `unanswered == []` and returns immediately — the Task object is DONE,
   but still sits in `self._mcp_probe_task` (not yet consumed).
2. A server is added to the roster AFTER that (e.g. `mcp_install`'s
   `refresh_mcp_servers` reassigning `self._mcp_servers`).
3. `await_mcp_probe_ready` is called: it finds `self._mcp_probe_task` is
   NOT None (still holding the ALREADY-FINISHED task from step 1), awaits
   it (resolves instantly, having done nothing for the new server), and
   RETURNS — without ever calling `ensure_mcp_tools_cached` again to catch
   the newly-unanswered server the finished task's own scan never covered.

No epoch discard is involved at all — the bug is that a stale, already-
resolved construction-time task is trusted as "the catalog is ready"
without re-checking whether new work appeared after it finished."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.llm.model_resolver import ModelResolver
from reyn.runtime.services import (
    LiveSessionIdInputs,
    McpGatewayInputs,
    MemoryService,
    PutOutboxInputs,
    RouterHostAdapter,
)
from tests._support.router_host_adapter import make_op_context_source

_SERVER = "pidsrv"
_TOOLS = [{"name": "convert_to_markdown", "description": "convert a uri to markdown"}]

_EMPTY_OP_CTX = make_op_context_source()
_EMPTY_MCP_GATEWAY = McpGatewayInputs(
    mcp_connection_service=None, mcp_agent_id=None, ephemeral_fn=None,
)


async def _null_file_read(path: str) -> dict:
    return {"content": ""}


async def _null_file_write(path: str, content: str) -> dict:
    return {"path": path, "written": True}


async def _null_file_delete(path: str) -> dict:
    return {"path": path, "deleted": True}


async def _null_file_regen(*, path, output_path, entry_template, header) -> dict:
    return {"path": path, "output_path": output_path, "entries": 0}


async def _null_mcp_call_tool(server: str, tool: str, args: dict) -> dict:
    return {}


async def _null_put_outbox(msg) -> None:
    pass


def _null_append_history(msg) -> None:
    pass


def _make_adapter(*, tmp_path: Path, mcp_servers: dict, probe) -> RouterHostAdapter:
    events = EventLog(subscribers=[])
    workspace = tmp_path / "agents" / "test-agent"
    memory = MemoryService(
        agent_workspace_dir=workspace,
        events=events,
        file_write=_null_file_write,
        file_read=_null_file_read,
        file_delete=_null_file_delete,
        file_regenerate_index=_null_file_regen,
    )
    adapter = RouterHostAdapter(
        agent_name="test-agent",
        agent_role="test",
        output_language="en",
        op_context_source=_EMPTY_OP_CTX,
        permission_resolver=None,
        mcp_servers=mcp_servers,
        project_context="",
        events=events,
        resolver=ModelResolver({}),
        memory=memory,
        journal=None,
        agent_registry=None,
        agent_workspace_dir=workspace,
        mcp_call_tool=_null_mcp_call_tool,
        mcp_gateway_inputs=_EMPTY_MCP_GATEWAY,
        put_outbox_inputs=PutOutboxInputs(
            put_outbox=_null_put_outbox, agent_replies_tracker=lambda: None,
        ),
        append_history=_null_append_history,
        live_session_id_inputs=LiveSessionIdInputs(
            session_id=None, live_session_id_fn=None,
        ),
        state_dir=tmp_path / "state",
        universal_wrappers_enabled=False,
    )
    adapter.mcp_list_tools = probe
    return adapter


@pytest.mark.asyncio
async def test_a_server_added_after_the_construction_probe_finished_is_still_probed(
    tmp_path: Path,
) -> None:
    """Tier 2: reproduces the exact #4401 A-4 regression (PR #5763 CI) via
    the real production seam (`start_mcp_probe` → `await_mcp_probe_ready`),
    mirroring `mcp_install`'s own real sequence: construct with an EMPTY
    roster (the construction-time probe finds nothing and finishes
    instantly), THEN add a server to the roster (mimics `refresh_mcp_
    servers`'s roster reassignment), THEN call `await_mcp_probe_ready`
    (mimics `refresh_mcp_servers`'s own consumption point) — the newly
    added server must still get probed, not silently skipped because the
    stale, already-finished construction task is trusted as sufficient."""
    class _AnsweringProbe:
        async def __call__(self, server_name: str) -> list[dict]:
            return [dict(t) for t in _TOOLS]

    adapter = _make_adapter(tmp_path=tmp_path, mcp_servers={}, probe=_AnsweringProbe())

    tracked: "list" = []
    adapter.start_mcp_probe(
        per_server_timeout=5.0,
        spawn=lambda coro, **kw: tracked.append(asyncio.create_task(coro)) or tracked[-1],
    )
    # Let the construction-time task actually run — with an empty roster it
    # finds nothing to probe and finishes immediately, exactly like the
    # real `mcp_install` sequence's own timing (the task gets its first
    # scheduling chance during some OTHER await in the real call chain,
    # e.g. `probe_mcp_server`'s own network round trip).
    await asyncio.sleep(0)
    assert tracked[0].done(), "the construction task must have already finished by this point"

    # Now a server is added — mirrors refresh_mcp_servers's own roster
    # reassignment (self._router_host._mcp_servers = fresh_roster).
    adapter._mcp_servers = {_SERVER: {"description": "pid server"}}

    await adapter.await_mcp_probe_ready(per_server_timeout=5.0)

    snapshot = adapter.mcp_tools_cache_snapshot
    assert snapshot is not None and _SERVER in snapshot, (
        "a server added to the roster AFTER the construction-time probe "
        f"already finished must still be probed by await_mcp_probe_ready; "
        f"got snapshot={snapshot!r}"
    )


@pytest.mark.asyncio
async def test_an_already_tried_unresponsive_server_is_not_reprobed_on_the_turn_path(
    tmp_path: Path,
) -> None:
    """Tier 2: the OTHER half of the fix (lead-coder/architect review,
    PR #5763) — the owner's own reported environment (every configured mcp
    server unresponsive). Once a server has been ATTEMPTED (even if it
    never answered and is sitting in its #5674 60s cooldown),
    `await_mcp_probe_ready` must NOT fall back to `ensure_mcp_tools_cached`
    for it on a later turn — that would pay the real network-timeout cost
    synchronously on the turn path, exactly what #4401 ① exists to remove.
    Only a server this session has NEVER attempted (`_mcp_attempted`)
    triggers the fallback — never "still unanswered" alone, which would
    also catch this case."""
    class _NeverAnsweringProbe:
        def __init__(self) -> None:
            self.calls: "list[str]" = []

        async def __call__(self, server_name: str) -> "list[dict] | None":
            self.calls.append(server_name)
            await asyncio.sleep(5.0)  # far beyond the per_server_timeout below
            return None  # unreachable — the timeout always fires first

    probe = _NeverAnsweringProbe()
    adapter = _make_adapter(
        tmp_path=tmp_path, mcp_servers={_SERVER: {"description": "pid server"}}, probe=probe,
    )

    tracked: "list" = []
    adapter.start_mcp_probe(
        per_server_timeout=0.05,
        spawn=lambda coro, **kw: tracked.append(asyncio.create_task(coro)) or tracked[-1],
    )
    await asyncio.wait_for(tracked[0], timeout=5.0)  # the construction probe times out and settles
    assert probe.calls == [_SERVER]

    # A later turn's own consumption point — the server is STILL unanswered
    # (it's in its cooldown window) but has been ATTEMPTED, so this must be
    # a fast no-op, never a second real probe dispatch.
    await adapter.await_mcp_probe_ready(per_server_timeout=0.05)

    assert probe.calls == [_SERVER], (
        "a server already attempted (and cooling down after failing) must "
        f"not be re-dispatched by a later await_mcp_probe_ready call; "
        f"probe.calls={probe.calls!r}"
    )
