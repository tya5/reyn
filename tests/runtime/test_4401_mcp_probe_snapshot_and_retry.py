"""Tier 2: #4401 ②③ — ``RouterHostAdapter.mcp_probe_snapshot``'s 3-state
read model, and the ``retry_mcp_probe`` per-server retry action.

② owner symptom: 8 configured MCP servers reading as unusable in the mcp
tab, when the real state was "not yet probed" or "probed, did not answer" —
never surfaced distinctly from a genuine zero-tool answer. This pins the
snapshot method the mcp pane's read model (`Session.mcp_probe_state`,
`status.py`'s `_session_mcp_probe_states`) forwards.

③ retry: bypasses the #5674 60s failure cooldown (a manual retry that
silently no-ops until it elapses would look like nothing happened), marks
the server ``"retrying"`` for the duration, and — per the #4401 A-4 co-vet
(architect) — is a single SYNCHRONOUSLY-AWAITED call, never backgrounded
(a background retry would reintroduce the concurrent-cache-mutation hazard
that co-vet found; A-4's own construction-time kickoff is deferred to a
follow-up PR for exactly that reason).

Same real-collaborators builder as ``test_3520_unknown_probe_is_not_an_
answer.py`` (``_make_adapter``/``_FlakyProbe``) — real `RouterHostAdapter`,
no mocks."""
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
_OTHER_SERVER = "reyn_broker"
_TOOL = "convert_to_markdown"
_TOOLS = [{"name": _TOOL, "description": "convert a uri to markdown"}]

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


class _ScriptedProbe:
    """A real async probe whose per-server outcome is driven by a plain
    dict the test sets up front — not a mock, a closure-based stand-in over
    real per-server behaviour (mirrors `_FlakyProbe` in test_3520, but keyed
    per-server rather than by one global health flag, since these tests
    need DIFFERENT servers to answer/fail independently)."""

    def __init__(self) -> None:
        self.healthy: "dict[str, bool]" = {}
        self.calls: list[str] = []

    async def __call__(self, server_name: str) -> list[dict]:
        self.calls.append(server_name)
        if not self.healthy.get(server_name, False):
            await asyncio.sleep(5.0)  # far beyond any timeout these tests pass
        return [dict(t) for t in _TOOLS]


def _make_adapter(*, tmp_path: Path, state_dir: Path, probe, clock=None) -> RouterHostAdapter:
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
        mcp_servers={
            _SERVER: {"description": "markitdown"},
            _OTHER_SERVER: {"description": "broker"},
        },
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
        clock=clock,
    )
    adapter.mcp_list_tools = probe
    return adapter


def _states(adapter: RouterHostAdapter) -> "dict[str, dict]":
    return {row["name"]: row for row in adapter.mcp_probe_snapshot()}


# ---------------------------------------------------------------------------
# ② mcp_probe_snapshot: the 3 states, never conflated
# ---------------------------------------------------------------------------


def test_a_never_probed_server_reads_not_probed_before_any_probe_runs(tmp_path: Path) -> None:
    """Tier 2: a freshly-constructed adapter, before ``ensure_mcp_tools_
    cached`` has ever run, reports every configured server "not_probed" —
    never "answered" (nothing measured yet) or "failed" (nothing attempted
    yet either)."""
    probe = _ScriptedProbe()
    adapter = _make_adapter(tmp_path=tmp_path, state_dir=tmp_path / "state", probe=probe)
    states = _states(adapter)
    assert states[_SERVER]["state"] == "not_probed"
    assert states[_OTHER_SERVER]["state"] == "not_probed"


@pytest.mark.asyncio
async def test_an_answered_server_reads_answered_with_its_real_tool_count(
    tmp_path: Path,
) -> None:
    """Tier 2: after a probe cycle, an answering server reads "answered"
    with its real tool count, and a timed-out sibling in the SAME cycle
    reads "failed" with the timeout reason — the two outcomes of one
    ``ensure_mcp_tools_cached`` call must not blur into each other."""
    probe = _ScriptedProbe()
    probe.healthy[_SERVER] = True
    now = [0.0]
    adapter = _make_adapter(
        tmp_path=tmp_path, state_dir=tmp_path / "state", probe=probe, clock=lambda: now[0],
    )
    await adapter.ensure_mcp_tools_cached(per_server_timeout=0.05)
    states = _states(adapter)
    assert states[_SERVER] == {"name": _SERVER, "state": "answered", "tool_count": 1}
    # the OTHER server timed out this same cycle — must read "failed", never
    # "answered" (the #4401 core conflation) and never "not_probed" either
    # (a probe DID attempt it).
    assert states[_OTHER_SERVER]["state"] == "failed"
    assert states[_OTHER_SERVER]["reason"] == "timeout"


@pytest.mark.asyncio
async def test_answered_with_zero_tools_is_distinct_from_not_probed(tmp_path: Path) -> None:
    """Tier 2: THE core #4401 conflation — a real "zero tools" answer must
    never be indistinguishable from "nothing has probed this server"."""
    class _EmptyProbe:
        async def __call__(self, server_name: str) -> list[dict]:
            return []

    adapter = _make_adapter(tmp_path=tmp_path, state_dir=tmp_path / "state", probe=_EmptyProbe())
    await adapter.ensure_mcp_tools_cached(per_server_timeout=5.0)
    states = _states(adapter)
    assert states[_SERVER] == {"name": _SERVER, "state": "answered", "tool_count": 0}
    assert states[_SERVER]["state"] != "not_probed"


@pytest.mark.asyncio
async def test_a_failed_server_clears_back_to_answered_once_it_later_succeeds(
    tmp_path: Path,
) -> None:
    """Tier 2: a "failed" state is not sticky — once the server later
    answers (past the #5674 cooldown), the failure record clears and the
    row reads "answered", never a stale "failed" alongside a real answer."""
    probe = _ScriptedProbe()
    now = [0.0]
    adapter = _make_adapter(
        tmp_path=tmp_path, state_dir=tmp_path / "state", probe=probe, clock=lambda: now[0],
    )
    await adapter.ensure_mcp_tools_cached(per_server_timeout=0.05)
    assert _states(adapter)[_SERVER]["state"] == "failed"

    probe.healthy[_SERVER] = True
    now[0] = 61.0  # past the #5674 60s failure cooldown
    await adapter.ensure_mcp_tools_cached(per_server_timeout=5.0)
    assert _states(adapter)[_SERVER] == {"name": _SERVER, "state": "answered", "tool_count": 1}


# ---------------------------------------------------------------------------
# ③ retry_mcp_probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_bypasses_the_failure_cooldown(tmp_path: Path) -> None:
    """Tier 2: #4401 ③ — a manual retry succeeds even though the #5674 60s
    cooldown has not elapsed — the whole point of an explicit "try again
    now" action; a retry that silently no-ops until the cooldown clears
    would look like the button did nothing."""
    probe = _ScriptedProbe()
    now = [0.0]
    adapter = _make_adapter(
        tmp_path=tmp_path, state_dir=tmp_path / "state", probe=probe, clock=lambda: now[0],
    )
    await adapter.ensure_mcp_tools_cached(per_server_timeout=0.05)
    assert _states(adapter)[_SERVER]["state"] == "failed"
    calls_before_retry = list(probe.calls)

    probe.healthy[_SERVER] = True
    # clock unchanged — still well inside the cooldown window.
    await adapter.retry_mcp_probe(_SERVER, per_server_timeout=5.0)

    assert probe.calls != calls_before_retry, "retry must actually re-probe, cooldown or not"
    assert _states(adapter)[_SERVER] == {"name": _SERVER, "state": "answered", "tool_count": 1}


@pytest.mark.asyncio
async def test_the_row_reads_retrying_while_the_retry_is_in_flight(tmp_path: Path) -> None:
    """Tier 2: while a retry's own probe call is still awaiting the (gated)
    server, the snapshot reads "retrying" — not "not_probed" (a probe
    genuinely IS running) and not a stale "failed" (the retry hasn't
    resolved either way yet)."""
    gate = asyncio.Event()

    class _GatedProbe:
        async def __call__(self, server_name: str) -> list[dict]:
            await gate.wait()
            return [dict(t) for t in _TOOLS]

    adapter = _make_adapter(tmp_path=tmp_path, state_dir=tmp_path / "state", probe=_GatedProbe())
    retry_task = asyncio.create_task(
        adapter.retry_mcp_probe(_SERVER, per_server_timeout=5.0),
    )
    await asyncio.sleep(0)  # let the retry reach its in-flight marker
    assert _states(adapter)[_SERVER]["state"] == "retrying"

    gate.set()
    await retry_task
    assert _states(adapter)[_SERVER]["state"] == "answered"


@pytest.mark.asyncio
async def test_in_flight_marker_clears_even_when_the_probe_raises(tmp_path: Path) -> None:
    """Tier 2: a retry that raises must not leave the row stuck showing
    "retrying…" forever — the in-flight marker is cleared in `finally`."""
    class _RaisingProbe:
        async def __call__(self, server_name: str) -> list[dict]:
            raise RuntimeError("boom")

    adapter = _make_adapter(tmp_path=tmp_path, state_dir=tmp_path / "state", probe=_RaisingProbe())
    # RuntimeError inside the real probe is caught by ensure_mcp_tools_cached's
    # own except-arm (returns ToolsUnknown) — this exercises the finally path
    # via a NORMAL failed-probe return, not a raise past retry_mcp_probe itself.
    await adapter.retry_mcp_probe(_SERVER, per_server_timeout=5.0)
    state = _states(adapter)[_SERVER]["state"]
    assert state != "retrying", (
        f"the in-flight marker must clear once the retry settles; still {state!r}"
    )
    assert state == "failed"
