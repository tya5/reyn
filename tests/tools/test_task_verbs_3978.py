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

from reyn.core.events.state_log import StateLog
from reyn.runtime.services.chain_manager import ChainManager
from reyn.runtime.services.snapshot_journal import SnapshotJournal
from reyn.tools.task_verbs import (
    _handle_cancel_task,
    _handle_describe_task,
    _handle_list_tasks,
)
from reyn.tools.types import RouterCallerState, ToolContext


class _NullEvents:
    def emit(self, *_args, **_kwargs) -> None:
        pass


def _make_manager(tmp_path: Path) -> ChainManager:
    log = StateLog(tmp_path / "wal.jsonl")
    journal = SnapshotJournal(
        agent_name="alpha", snapshot_path=tmp_path / "snap.json", state_log=log,
    )
    return ChainManager(
        journal=journal, events=_NullEvents(), chain_timeout_seconds=0, max_hop_depth=10,
    )


def _ctx(chains) -> ToolContext:
    return ToolContext(
        events=_NullEvents(), permission_resolver=None, workspace=None,
        caller_kind="router",
        router_state=RouterCallerState(chains=chains),
    )


# ── describe_task ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_describe_task_returns_full_shape_for_a_running_task(tmp_path: Path):
    """Tier 2: proposal 0067's describe_task shape — {task_id, kind, status,
    session, requester} — for a real, registered running task."""
    mgr = _make_manager(tmp_path)
    await mgr.register(
        chain_id="run-1", from_user=False, depth=0, original_text="p", sender=None,
        origin_agent="worker", origin_sid="s1", kind="pipeline",
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
        chain_id="legacy-1", from_user=False, depth=0, original_text="p", sender="peer",
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


# ── list_tasks ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_tasks_lists_only_typed_running_tasks(tmp_path: Path):
    """Tier 2: list_tasks enumerates only typed (kind != None) handles, in
    the {task_id, kind, status, session} list shape — no requester field."""
    mgr = _make_manager(tmp_path)
    await mgr.register(
        chain_id="run-2", from_user=False, depth=0, original_text="p", sender=None,
        origin_agent="worker", origin_sid=None, kind="pipeline",
    )
    await mgr.register(
        chain_id="legacy-2", from_user=False, depth=0, original_text="p", sender="peer",
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
async def test_list_tasks_filters_by_kind(tmp_path: Path):
    """Tier 2: a kind filter narrows the listing to matching tasks only."""
    mgr = _make_manager(tmp_path)
    await mgr.register(
        chain_id="run-3", from_user=False, depth=0, original_text="p", sender=None,
        kind="pipeline",
    )
    await mgr.register(
        chain_id="run-4", from_user=False, depth=0, original_text="p", sender=None,
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
        chain_id="run-5", from_user=False, depth=0, original_text="p", sender=None,
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
        chain_id="run-6", from_user=False, depth=0, original_text="p", sender=None,
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
        chain_id="legacy-3", from_user=False, depth=0, original_text="p", sender="peer",
        cancel=lambda: calls.append("should not run"),
    )

    result = await _handle_cancel_task({"task_id": "legacy-3"}, _ctx(mgr))

    assert result["status"] == "error"
    assert calls == []
