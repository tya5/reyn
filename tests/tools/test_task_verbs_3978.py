"""Tier 2: describe_task / list_tasks / cancel_task (proposal 0067 P4, #3978).

Real ``ChainManager``/``SnapshotJournal``/``StateLog`` throughout — no
mocks, matching ``test_chain_manager_settle_3978.py``'s established
pattern. The handlers are driven directly (``_handle_describe_task`` etc.)
against a real ``ToolContext``/``RouterCallerState`` carrying a real
``ChainManager``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog
from reyn.runtime.router_loop import RouterLoop
from reyn.runtime.services.chain_manager import ChainManager
from reyn.runtime.services.snapshot_journal import SnapshotJournal
from reyn.runtime.task_types import Requester
from reyn.tools.task_verbs import (
    _handle_cancel_task,
    _handle_describe_task,
    _handle_list_tasks,
)
from reyn.tools.types import RouterCallerState, ToolContext
from tests._support.router_loop import FakeRouterHost


def _make_manager(tmp_path: Path) -> ChainManager:
    log = StateLog(tmp_path / "wal.jsonl")
    journal = SnapshotJournal(
        agent_name="alpha", snapshot_path=tmp_path / "snap.json", state_log=log,
    )
    return ChainManager(
        journal=journal, events=EventLog(), chain_timeout_seconds=0, max_hop_depth=10,
    )


def _ctx(chains, *, inbox_depth: "int | None" = None) -> ToolContext:
    return ToolContext(
        events=EventLog(), permission_resolver=None, workspace=None,
        caller_kind="router",
        router_state=RouterCallerState(chains=chains, session_inbox_depth=inbox_depth),
    )


# ── describe_task ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_describe_task_returns_full_shape_for_a_running_task(tmp_path: Path):
    """Tier 2: proposal 0067's describe_task shape — {task_id, kind, status,
    session, requester} — for a real, registered running task."""
    mgr = _make_manager(tmp_path)
    await mgr.register(
        chain_id="run-1", depth=0, original_text="p", sender=None,
        requester=Requester(agent_name="worker", session_id="s1"), kind="pipeline",
    )

    result = await _handle_describe_task({"task_id": "run-1"}, _ctx(mgr))

    assert result["ok"] is True
    assert result["task_id"] == "run-1"
    assert result["kind"] == "pipeline"
    assert result["status"] == "running"
    assert result["session"] == "s1"
    assert result["requester"] == {"agent_name": "worker", "session_id": "s1"}


@pytest.mark.asyncio
async def test_describe_task_unknown_id_returns_error_not_a_crash(tmp_path: Path):
    """Tier 2: an unregistered task_id degrades to an explicit error, not a
    KeyError/crash (mirrors ChainManager's own tolerant lookups)."""
    mgr = _make_manager(tmp_path)

    result = await _handle_describe_task({"task_id": "never-registered"}, _ctx(mgr))

    assert result["ok"] is False
    assert "never-registered" in result["error"]


@pytest.mark.asyncio
async def test_describe_task_excludes_untyped_legacy_delegate_chains(tmp_path: Path):
    """Tier 2: a chain registered with no ``kind`` (every EXISTING
    delegate-relay call site, unchanged by P4) is not yet a typed task —
    describe_task must not describe it (it would report ``kind: None``,
    which is not one of prompt/pipeline/exec and answers a question the
    caller didn't ask)."""
    mgr = _make_manager(tmp_path)
    await mgr.register(
        chain_id="legacy-1", depth=0, original_text="p", sender="peer",
    )

    result = await _handle_describe_task({"task_id": "legacy-1"}, _ctx(mgr))

    assert result["ok"] is False


@pytest.mark.asyncio
async def test_describe_task_with_no_chains_source_returns_error(tmp_path: Path):
    """Tier 2: a narrow test host (or a host that doesn't support this
    substrate) degrades to 'not found', not a crash — mirrors
    ``pipeline_list``'s own ``registry is None`` degrade."""
    result = await _handle_describe_task({"task_id": "run-1"}, _ctx(None))
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_describe_task_reports_session_inbox_depth(tmp_path: Path):
    """Tier 2: proposal 0067 P9 (#3978), architect ruling 2026-08-10 —
    ``describe_task`` echoes ``RouterCallerState.session_inbox_depth``
    verbatim (the instantaneous read is done at caller-state build time;
    the handler itself does no resolution)."""
    mgr = _make_manager(tmp_path)
    await mgr.register(
        chain_id="run-depth", depth=0, original_text="p", sender=None,
        requester=Requester(agent_name="worker", session_id="s1"), kind="pipeline",
    )

    result = await _handle_describe_task({"task_id": "run-depth"}, _ctx(mgr, inbox_depth=3))

    assert result["session_inbox_depth"] == 3


@pytest.mark.asyncio
async def test_describe_task_session_inbox_depth_defaults_to_none(tmp_path: Path):
    """Tier 2: falsify pair — when the caller state carries no depth (the
    default), the field is explicitly None, never a silently-wrong 0 (which
    would misread as "definitely empty")."""
    mgr = _make_manager(tmp_path)
    await mgr.register(
        chain_id="run-nodepth", depth=0, original_text="p", sender=None,
        kind="pipeline",
    )

    result = await _handle_describe_task({"task_id": "run-nodepth"}, _ctx(mgr))

    assert result["session_inbox_depth"] is None


# ── list_tasks ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_tasks_lists_only_typed_running_tasks(tmp_path: Path):
    """Tier 2: list_tasks enumerates only typed (kind != None) handles, in
    the {task_id, kind, status, session} list shape — no requester field."""
    mgr = _make_manager(tmp_path)
    await mgr.register(
        chain_id="run-2", depth=0, original_text="p", sender=None,
        requester=Requester(agent_name="worker", session_id="main"), kind="pipeline",
    )
    await mgr.register(
        chain_id="legacy-2", depth=0, original_text="p", sender="peer",
    )

    result = await _handle_list_tasks({}, _ctx(mgr))

    ids = {t["task_id"] for t in result["tasks"]}
    assert ids == {"run-2"}
    (task,) = result["tasks"]
    assert task["kind"] == "pipeline"
    assert task["status"] == "running"
    assert task["session"] == "main"
    assert "requester" not in task  # list view omits it (describe_task carries it)


@pytest.mark.asyncio
async def test_list_tasks_session_inbox_depth_same_for_every_entry(tmp_path: Path):
    """Tier 2: proposal 0067 P9 (#3978) — every entry in list_tasks carries
    the SAME session_inbox_depth, because ``chains`` is scoped to THIS
    calling session's own ChainManager (module docstring) — every task
    list_tasks can even see was registered with the same origin session."""
    mgr = _make_manager(tmp_path)
    await mgr.register(
        chain_id="run-5", depth=0, original_text="p", sender=None, kind="pipeline",
    )
    await mgr.register(
        chain_id="run-6b", depth=0, original_text="p", sender=None, kind="prompt",
    )

    result = await _handle_list_tasks({}, _ctx(mgr, inbox_depth=7))

    depths = {t["session_inbox_depth"] for t in result["tasks"]}
    assert depths == {7}


@pytest.mark.asyncio
async def test_list_tasks_filters_by_kind(tmp_path: Path):
    """Tier 2: a kind filter narrows the listing to matching tasks only."""
    mgr = _make_manager(tmp_path)
    await mgr.register(
        chain_id="run-3", depth=0, original_text="p", sender=None,
        kind="pipeline",
    )
    await mgr.register(
        chain_id="run-4", depth=0, original_text="p", sender=None,
        kind="prompt",
    )

    result = await _handle_list_tasks({"kind": "prompt"}, _ctx(mgr))

    assert {t["task_id"] for t in result["tasks"]} == {"run-4"}


@pytest.mark.asyncio
async def test_list_tasks_with_no_chains_source_returns_empty(tmp_path: Path):
    """Tier 2: a narrow test host with no chains substrate returns an empty
    list, not a crash (mirrors pipeline_list's own None-registry degrade)."""
    result = await _handle_list_tasks({}, _ctx(None))
    assert result["tasks"] == []


# ── cancel_task ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_task_calls_the_hook_and_returns_cancel_requested(tmp_path: Path):
    """Tier 2: a task with a live cancel hook has it called, and the tool
    returns {task_id, status: cancel_requested}."""
    mgr = _make_manager(tmp_path)
    calls = []
    await mgr.register(
        chain_id="run-5", depth=0, original_text="p", sender=None,
        kind="pipeline", cancel=lambda: calls.append("cancelled"),
    )

    result = await _handle_cancel_task({"task_id": "run-5"}, _ctx(mgr))

    assert result == {"task_id": "run-5", "status": "cancel_requested"}
    assert calls == ["cancelled"]


@pytest.mark.asyncio
async def test_cancel_task_without_a_live_hook_never_reports_success(tmp_path: Path):
    """Tier 2: architect's witness requirement (#3978) — a handle whose
    ``cancel`` is None (a crash-recovered chain; the live callable belonged
    to the dead process) must get an explicit, distinguishable error, NOT a
    silent 'cancelled' while the task keeps running."""
    mgr = _make_manager(tmp_path)
    await mgr.register(
        chain_id="run-6", depth=0, original_text="p", sender=None,
        kind="pipeline",  # no cancel= — mirrors a recovered handle
    )

    result = await _handle_cancel_task({"task_id": "run-6"}, _ctx(mgr))

    assert result["status"] == "error"
    assert result["status"] != "cancel_requested"
    assert "run-6" in result["error"]


@pytest.mark.asyncio
async def test_cancel_task_unknown_id_returns_error(tmp_path: Path):
    """Tier 2: falsification pair to the accept-side cancel test — an
    unregistered task_id also gets an explicit error, not a crash."""
    mgr = _make_manager(tmp_path)

    result = await _handle_cancel_task({"task_id": "never-registered"}, _ctx(mgr))

    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_cancel_task_untyped_legacy_chain_returns_error(tmp_path: Path):
    """Tier 2: strip-falsify sibling of the describe_task legacy-chain
    test — cancel_task must not act on an untyped chain either (a task_id
    that resolves to a delegate-relay entry, not a typed task)."""
    mgr = _make_manager(tmp_path)
    calls = []
    await mgr.register(
        chain_id="legacy-3", depth=0, original_text="p", sender="peer",
        cancel=lambda: calls.append("should not run"),
    )

    result = await _handle_cancel_task({"task_id": "legacy-3"}, _ctx(mgr))

    assert result["status"] == "error"
    assert calls == []


# ── production-wiring witness (lead-coder block on #4106) ──────────────────


@pytest.mark.asyncio
async def test_describe_task_reaches_a_real_chain_manager_through_the_real_router_loop(
    tmp_path: Path,
):
    """Tier 2: every test above builds ``RouterCallerState(chains=mgr)``
    directly — proving the HANDLER is correct, but never exercising the
    production WIRING (``build_resource_caller_state``'s own
    ``host.get_chains()`` read, the seam ``RouterHostAdapter.get_chains()``
    /``Session``'s ``chains=self.chains`` construction-time wiring mirrors
    in production). Dropping that ONE wiring line degrades SILENTLY — no
    exception, `chains=None` renders as an ordinary "no running task"
    error the handler itself already covers — so a test that never drives
    the real seam cannot tell "wiring removed" from "task genuinely not
    found". This drives a REAL ``RouterLoop._invoke_router_tool`` call
    (the exact production dispatch path — REGISTRY_DISPATCH_TOOLS →
    ``_invoke_via_registry`` → ``_build_router_caller_state`` →
    ``build_resource_caller_state`` → ``host.get_chains()``) against a
    ``FakeRouterHost`` carrying a REAL, task-registered ``ChainManager`` —
    same shape as ``test_session_spawn_dispatches_to_host_not_unhandled``
    (#2120's own spawn_session-advertised-but-undispatched precedent)."""
    mgr = _make_manager(tmp_path)
    await mgr.register(
        chain_id="run-e2e", depth=0, original_text="p", sender=None,
        requester=Requester(agent_name="worker", session_id="s1"), kind="pipeline",
    )
    host = FakeRouterHost(chains=mgr)
    loop = RouterLoop(host=host, chain_id="chain-test")

    result = await loop._invoke_router_tool("describe_task", {"task_id": "run-e2e"})

    assert not (isinstance(result, dict) and "unhandled tool" in str(result.get("error", ""))), (
        f"describe_task hit the unhandled-tool fall-through: {result}"
    )
    assert result.get("ok") is True, (
        f"describe_task did not reach the real ChainManager through production "
        f"wiring (chains=None would render as ok=False, not a crash): {result}"
    )
    assert result["task_id"] == "run-e2e"
    assert result["kind"] == "pipeline"


@pytest.mark.asyncio
async def test_describe_task_session_inbox_depth_reaches_production_wiring(
    tmp_path: Path,
):
    """Tier 2: production-wiring witness for ``session_inbox_depth`` —
    proposal 0067 P9 (#3978). Mirrors the ``chains`` witness above (same
    class of gap: every OTHER test here builds ``RouterCallerState``
    directly, bypassing ``build_resource_caller_state``'s own
    ``host.get_inbox_depth()`` read entirely). Drives the SAME real
    ``RouterLoop._invoke_router_tool`` path against a ``FakeRouterHost``
    constructed with ``inbox_depth=``."""
    mgr = _make_manager(tmp_path)
    await mgr.register(
        chain_id="run-depth-e2e", depth=0, original_text="p", sender=None,
        requester=Requester(agent_name="worker", session_id="s1"), kind="pipeline",
    )
    host = FakeRouterHost(chains=mgr, inbox_depth=5)
    loop = RouterLoop(host=host, chain_id="chain-test")

    result = await loop._invoke_router_tool("describe_task", {"task_id": "run-depth-e2e"})

    assert result.get("ok") is True
    assert result["session_inbox_depth"] == 5
