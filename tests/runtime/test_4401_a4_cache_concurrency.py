"""Tier 2: #4401 A-4 — the concurrency-safety redesign the architect co-vet
required before landing the construction-time background probe (issue
#4401, F1/F2/F3/F5, plus R3's "witness state transitions, not private call
counts" correction).

Background: A-4 makes `RouterHostAdapter.ensure_mcp_tools_cached` run
concurrently with `invalidate_mcp_tools_cache`/`maybe_reload_mcp_tools_
cache_from_disk` for the first time (previously safe only because a turn's
own probe call was always synchronous/serialized). These tests pin the
observable behaviour the redesign promises:

- F1: the (cache, mtime) pair advances together (never one without the other).
- F3/F5: a probe cycle whose epoch went stale while it was in flight is
  discarded — no in-memory merge, no disk write.
- ②/R3: the 3 external drivers (probe merge / invalidate / disk reload) each
  produce their own observable state transition through the PUBLIC surface
  (`mcp_tools_cache_snapshot`, `mcp_probe_snapshot`) — never a private
  `_apply_mcp_cache` call count (CLAUDE.md: "a test must not depend on
  private state").

Same real-collaborators builder as ``test_4401_mcp_probe_snapshot_and_
retry.py`` — real `RouterHostAdapter`, a scripted real probe callable, no
mocks."""
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

_SERVER = "reyn_markitdown"
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


def _make_adapter(*, tmp_path: Path, state_dir: Path, probe) -> RouterHostAdapter:
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
        mcp_servers={_SERVER: {"description": "markitdown"}},
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
        state_dir=state_dir,
        universal_wrappers_enabled=False,
    )
    adapter.mcp_list_tools = probe
    return adapter


# ---------------------------------------------------------------------------
# F1: (cache, mtime) advance together
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_successful_probe_leaves_disk_reload_a_no_op(tmp_path: Path) -> None:
    """Tier 2: F1 — after a successful probe+persist, the in-memory mtime
    already reflects the write, so an immediate `maybe_reload_mcp_tools_
    cache_from_disk` finds nothing newer (proves the dict and its mtime
    advanced TOGETHER — a torn update, dict-ahead-of-mtime, would make this
    reload wrongly think the disk is newer and re-swap the cache)."""
    class _AnsweringProbe:
        async def __call__(self, server_name: str) -> list[dict]:
            return [dict(t) for t in _TOOLS]

    state_dir = tmp_path / "state"
    adapter = _make_adapter(tmp_path=tmp_path, state_dir=state_dir, probe=_AnsweringProbe())
    await adapter.ensure_mcp_tools_cached(per_server_timeout=5.0)
    before = adapter.mcp_tools_cache_snapshot

    adapter.maybe_reload_mcp_tools_cache_from_disk()
    after = adapter.mcp_tools_cache_snapshot
    assert after == before, "a no-op reload must not change the observable snapshot"


@pytest.mark.asyncio
async def test_a_failed_disk_write_still_advances_the_in_memory_answer(
    tmp_path: Path,
) -> None:
    """Tier 2: F1 — when `write_cache` raises (session correctness over
    disk durability, unchanged from before A-4), the in-memory cache still
    reflects the new answer — a failed persist must not silently drop the
    probe's own measurement."""
    class _AnsweringProbe:
        async def __call__(self, server_name: str) -> list[dict]:
            return [dict(t) for t in _TOOLS]

    # A state_dir that cannot be written to (a file where a directory is
    # expected) makes cache_file_path's own write step fail.
    state_dir = tmp_path / "not_a_real_dir"
    state_dir.parent.mkdir(parents=True, exist_ok=True)
    state_dir.write_text("blocking file, not a directory")

    adapter = _make_adapter(tmp_path=tmp_path, state_dir=state_dir, probe=_AnsweringProbe())
    await adapter.ensure_mcp_tools_cached(per_server_timeout=5.0)
    snapshot = adapter.mcp_tools_cache_snapshot
    assert snapshot is not None and _SERVER in snapshot, (
        "a probe answer must survive in-memory even if persisting it to "
        f"disk failed; got {snapshot!r}"
    )


# ---------------------------------------------------------------------------
# F3/F5: a stale probe cycle is discarded — no merge, no persist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_probe_that_is_invalidated_mid_flight_is_discarded_not_merged(
    tmp_path: Path,
) -> None:
    """Tier 2: F3/F5 — an invalidate landing WHILE a probe is in flight
    bumps the epoch; when that probe's own results become ready, they are
    discarded (never merged into memory, never written to disk) rather
    than applied over state the probe cycle never saw."""
    gate = asyncio.Event()

    class _GatedProbe:
        async def __call__(self, server_name: str) -> list[dict]:
            await gate.wait()
            return [dict(t) for t in _TOOLS]

    state_dir = tmp_path / "state"
    adapter = _make_adapter(tmp_path=tmp_path, state_dir=state_dir, probe=_GatedProbe())

    probe_task = asyncio.create_task(adapter.ensure_mcp_tools_cached(per_server_timeout=5.0))
    await asyncio.sleep(0)  # let the probe cycle capture its epoch and start gathering

    adapter.invalidate_mcp_tools_cache(_SERVER)  # bumps the epoch mid-flight
    gate.set()
    await probe_task

    assert adapter.mcp_tools_cache_snapshot is None, (
        "a stale probe cycle's answer must not be merged after an "
        "invalidate landed mid-flight"
    )
    from reyn.runtime.services.mcp_cache_file import cache_file_path
    assert not cache_file_path(state_dir).exists(), (
        "a stale probe cycle must not persist its discarded answer to disk either"
    )


@pytest.mark.asyncio
async def test_a_waiter_does_not_re_wait_when_the_epoch_moves_mid_await(
    tmp_path: Path,
) -> None:
    """Tier 2: F3 — `await_mcp_probe_ready` is a SINGLE await, never a
    loop: even though the in-flight task it awaits gets its own results
    discarded as stale (the test above), the waiter itself still returns
    normally (no exception, no hang) the moment that task settles."""
    gate = asyncio.Event()

    class _GatedProbe:
        async def __call__(self, server_name: str) -> list[dict]:
            await gate.wait()
            return [dict(t) for t in _TOOLS]

    state_dir = tmp_path / "state"
    adapter = _make_adapter(tmp_path=tmp_path, state_dir=state_dir, probe=_GatedProbe())

    tracked: "list" = []
    adapter.start_mcp_probe(  # the real, public kickoff — not a private poke
        per_server_timeout=5.0,
        spawn=lambda coro, **kw: tracked.append(asyncio.create_task(coro)) or tracked[-1],
    )
    waiter = asyncio.create_task(adapter.await_mcp_probe_ready(per_server_timeout=5.0))
    await asyncio.sleep(0)

    adapter.invalidate_mcp_tools_cache(_SERVER)
    gate.set()

    await asyncio.wait_for(waiter, timeout=5.0)  # must not hang


# ---------------------------------------------------------------------------
# ②/R3: 3 external drivers, each its own observable state transition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_of_the_three_external_drivers_produces_its_own_observable_transition(
    tmp_path: Path,
) -> None:
    """Tier 2: ②/R3 — the probe's own merge, `invalidate_mcp_tools_cache`,
    and `maybe_reload_mcp_tools_cache_from_disk` each drive the cache
    through ONE shared apply point, witnessed here via PUBLIC state
    transitions (`mcp_tools_cache_snapshot`) — never by counting calls to
    the private `_apply_mcp_cache` (CLAUDE.md: a test must not depend on
    private state)."""
    class _AnsweringProbe:
        async def __call__(self, server_name: str) -> list[dict]:
            return [dict(t) for t in _TOOLS]

    state_dir = tmp_path / "state"
    adapter = _make_adapter(tmp_path=tmp_path, state_dir=state_dir, probe=_AnsweringProbe())

    # Driver 1: the probe's own merge — None -> populated.
    assert adapter.mcp_tools_cache_snapshot is None
    await adapter.ensure_mcp_tools_cached(per_server_timeout=5.0)
    assert adapter.mcp_tools_cache_snapshot is not None and _SERVER in adapter.mcp_tools_cache_snapshot

    # Driver 2: invalidate — populated -> None.
    adapter.invalidate_mcp_tools_cache(_SERVER)
    assert adapter.mcp_tools_cache_snapshot is None

    # Driver 3: disk reload — None -> whatever the (still-present, unwritten-
    # over) file holds, without a live probe running at all.
    adapter.maybe_reload_mcp_tools_cache_from_disk()
    assert adapter.mcp_tools_cache_snapshot is not None and _SERVER in adapter.mcp_tools_cache_snapshot, (
        "the disk-reload driver must independently reach the same "
        "observable populated state, without any probe call in this step"
    )
