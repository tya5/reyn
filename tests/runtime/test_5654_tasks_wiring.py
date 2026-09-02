"""Tier 2: #5654 — the operator-facing `/tasks` wrapper over the SAME
`list_tasks`/`cancel_task` LLM ops, and the two durable fields (`registered_at`
/ `cancel_requested_at`) the "経過"/中断状況 columns are derived from.

The substrate itself (`ChainManager`, `list_tasks`/`cancel_task`,
`run_prompt(collect="async")`) predates this issue (#3978); what #5654 adds is:

- two persisted fields on `_PendingChain` — `registered_at` (set at register
  time from the injected clock, same seam `arm_at` already uses),
  `cancel_requested_at` (set once, via `ChainManager.update()`, the same call
  site every other post-register mutation already uses);
- `caller_kind="operator"` on the `tool_called`/`tool_returned`/`tool_failed`
  audit trail, so a slash-driven cancel is not misattributed to the router
  (widened from `Literal["router"]` in both `tools/types.ToolContext` and
  `core/dispatch/dispatcher.DispatchContext`);
- `slash/tasks.py`, which dispatches through `dispatch_tool` (never a bare
  `invoke_tool` — that would silently skip the audit trail this issue's own
  accept criterion (b) requires) with a real, minimal `tool_catalog` built
  from `LIST_TASKS`/`CANCEL_TASK`'s own `render_for_router()` projection.

Real `AgentRegistry` + two real `Session`s (a requester `alpha` and a target
`beta`) + `LLMStub(control="gated")` (via `@pytest.mark.llm_stub`) throughout
— no mocks, no fakes. `tests/_async_wait.py`'s `wait_until` is the unbounded,
non-duration polling helper CLAUDE.md's own testing policy requires in place
of a manual `sleep(N)`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_api import run_prompt_async
from tests._async_wait import wait_until
from tests._support.agent_session import make_session

pytestmark = pytest.mark.asyncio


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        return make_session(
            agent_name=profile.name, state_log=state_log, registry=holder.get("reg"),
        )

    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
    )
    holder["reg"] = reg
    return reg


def _seed(tmp_path: Path, name: str) -> None:
    AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


async def _tasks_ctx(session, *, caller_kind: 'Literal["router", "operator"]' = "operator"):
    """The same context ``slash/tasks.py`` builds — reproduced here rather
    than imported so this file can drive ``dispatch_tool`` directly (the
    accept/deny witnesses need the raw envelope, not the slash reply text)."""
    from reyn.tools.types import ToolContext, build_resource_caller_state

    host = session.router_host
    router_state = await build_resource_caller_state(host)
    return ToolContext(
        events=host.events,
        permission_resolver=getattr(host, "permission_resolver", None),
        workspace=getattr(host, "workspace", None),
        caller_kind=caller_kind,
        router_state=router_state,
        resolver=getattr(host, "resolver", None),
        hot_reloader=getattr(host, "hot_reloader", None),
        state_log=getattr(host, "state_log", None),
        agent_name=getattr(host, "agent_name", None),
    )


async def _dispatch(name: str, args: dict, session) -> dict:
    from reyn.core.dispatch.dispatcher import DispatchContext, dispatch_tool
    from reyn.tools import get_default_registry
    from reyn.tools.dispatch import invoke_tool
    from reyn.tools.task_verbs import CANCEL_TASK, LIST_TASKS

    tool_ctx = await _tasks_ctx(session)
    definition = {"list_tasks": LIST_TASKS, "cancel_task": CANCEL_TASK}[name]
    dispatch_ctx = DispatchContext(
        caller_kind="operator",
        caller_id=getattr(tool_ctx, "agent_name", None) or "",
        chain_id=None,
        tool_catalog={name: definition.render_for_router()},
        events=tool_ctx.events,
    )

    async def _invoker(call_args: dict) -> "object":
        return await invoke_tool(get_default_registry(), name, call_args, tool_ctx)

    return await dispatch_tool(name=name, args=args, ctx=dispatch_ctx, invoker=_invoker)


def _collect(session):
    from tests._support.events import collect_events
    return collect_events(session._audit_events)


async def _settle(session) -> None:
    from tests._support.events import settle
    await settle(session._audit_events)


# ---------------------------------------------------------------------------
# accept (a) — a running task appears in the snapshot's `tasks` and in
# list_tasks, carrying registered_at
# ---------------------------------------------------------------------------


async def test_a_running_prompt_task_appears_with_registered_at(tmp_path):
    """Tier 2: accept (a) — a running task appears in both the snapshot's
    `_task_rows` and the real `list_tasks` op, each carrying `registered_at`
    and the corrected `target` (an agent name, never a fabricated session
    id)."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")
    _seed(tmp_path, "beta")
    caller = reg.get_or_load("alpha")
    reg.get_or_load("beta")

    result = await run_prompt_async(
        reg, caller_agent="alpha", caller_sid="main",
        target_agent="beta", target_session="main",
        prompt="please help",
    )
    assert result["status"] == "started"
    task_id = result["data"]["task_id"]

    chain = caller.chains.get(task_id)
    assert chain.kind == "prompt"
    assert chain.registered_at is not None, (
        "a freshly-registered task must carry its own registered_at — "
        "#5654's own accept criterion"
    )

    from reyn.interfaces.repl.status import _task_rows
    rows = _task_rows(caller)
    (row,) = [r for r in rows if r["task_id"] == task_id]
    assert row["kind"] == "prompt"
    assert row["target"] == "beta", (
        "prompt's target cell is waiting_on's sole member — never a "
        "fabricated session id (architect correction, 2026-09-02)"
    )
    assert row["registered_at"] is not None
    assert row["cancellable"] is True

    list_result = await _dispatch("list_tasks", {}, caller)
    assert list_result["status"] == "ok"
    (op_row,) = [t for t in list_result["data"]["tasks"] if t["task_id"] == task_id]
    assert op_row["kind"] == "prompt"


# ---------------------------------------------------------------------------
# accept (b) — cancel reaches the TARGET session's in-flight turn, records
# cancel_requested_at, and audits caller_kind="operator"
# ---------------------------------------------------------------------------


@pytest.mark.llm_stub(control="gated")
async def test_cancel_task_hard_cancels_the_target_sessions_inflight_turn(
    tmp_path, _llm_stub,
):
    """Tier 2: accept (b), the strongest witness in this file — real cross-
    session cancellation, driven through the exact op an operator's
    ``/tasks cancel`` reaches.

    Strip-falsifier (architect's reviewer strip ②, PR-body): replace the
    ``dispatch_tool`` call with a direct ``chain.cancel()`` — the cancellation
    itself would still land (this test's OWN assertions on ``turn_task``
    would stay green), but the ``caller_kind="operator"`` `tool_called` audit
    event below would vanish, which is the assertion that actually catches
    that regression.
    """
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")
    _seed(tmp_path, "beta")
    caller = reg.get_or_load("alpha")
    target = reg.get_or_load("beta")
    events = _collect(caller)

    result = await run_prompt_async(
        reg, caller_agent="alpha", caller_sid="main",
        target_agent="beta", target_session="main",
        prompt="please help",
    )
    task_id = result["data"]["task_id"]
    chain = caller.chains.get(task_id)
    assert chain.cancel is not None

    # Put the TARGET's own turn in flight, hung at the gated LLM boundary —
    # mirrors test_2242_hard_cancel.py's own drive.
    await target._put_inbox("user", {"text": "please help", "chain_id": "c-target"})
    import asyncio
    turn_task = asyncio.create_task(target.run_one_iteration())
    await _llm_stub.call_started.wait()

    cancel_result = await _dispatch("cancel_task", {"task_id": task_id}, caller)
    assert cancel_result["status"] == "ok"
    assert cancel_result["data"]["status"] == "cancel_requested"

    # Unbounded poll (CLAUDE.md: no manual sleep(N)/attempts=N a test's own
    # assertion depends on) for the fire-and-forget cross-session
    # cancel-forward (`asyncio.ensure_future(target.cancel_inflight())`,
    # session_api.py's own `_cancel_hook`) to actually run and land.
    await wait_until(lambda: turn_task.done())

    # Release AFTER the cancel has landed — mirrors #2242's own ordering: if
    # the cancellation were merely cooperative or delayed, releasing now
    # would let the hung call resume and its reply WOULD land.
    _llm_stub.release.set()

    completed = await turn_task
    assert completed is True
    assert not any(m.content for m in target.history if m.role == "assistant"), (
        "the target's hard-cancelled turn must never land a reply"
    )

    await _settle(caller)
    tool_called = [e for e in events if e.type == "tool_called" and e.data.get("tool") == "cancel_task"]
    (cancel_call,) = tool_called
    assert cancel_call.data.get("caller_kind") == "operator", (
        "a slash-driven cancel must be attributed to the OPERATOR, not the "
        "router — #5654's own widening of caller_kind exists for this"
    )

    reloaded = caller.chains.get(task_id)
    assert reloaded is not None
    assert reloaded.cancel_requested_at is not None, (
        "cancel_requested_at must be recorded the moment cancellation is "
        "REQUESTED, independent of whether/when the target's turn settles"
    )


# ---------------------------------------------------------------------------
# deny siblings
# ---------------------------------------------------------------------------


async def test_unknown_task_id_is_an_error(tmp_path):
    """Tier 2: deny — an unresolvable task_id is a typed error, never a
    silent no-op."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")
    caller = reg.get_or_load("alpha")

    result = await _dispatch("cancel_task", {"task_id": "no-such-task"}, caller)
    assert result["status"] == "error"


async def test_a_kind_none_relay_chain_is_excluded_from_the_task_list(tmp_path):
    """Tier 2: a proposal-0067-P4-pre-existing relay chain (kind=None) is
    NOT a task — #3978's own permanent exclusion. Registered the legacy way
    (mirrors test_multi_agent_p7.py's own manual register)."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")
    caller = reg.get_or_load("alpha")

    await caller.chains.register(
        chain_id="relay-1", depth=1, original_text="relay", sender="alpha",
        waiting_on={"someone"},
    )

    result = await _dispatch("list_tasks", {}, caller)
    assert result["data"]["tasks"] == []

    from reyn.interfaces.repl.status import _task_rows
    assert _task_rows(caller) == []


async def test_a_settled_task_is_no_longer_listed(tmp_path):
    """Tier 2: ADR-0040 D4 — a settled task's handle is gone; there is no
    terminal-status listing (deny, distinct from the kind=None case above)."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")
    _seed(tmp_path, "beta")
    caller = reg.get_or_load("alpha")
    reg.get_or_load("beta")

    result = await run_prompt_async(
        reg, caller_agent="alpha", caller_sid="main",
        target_agent="beta", target_session="main",
        prompt="please help",
    )
    task_id = result["data"]["task_id"]
    await caller.chains.resolve(task_id)

    list_result = await _dispatch("list_tasks", {}, caller)
    assert list_result["data"]["tasks"] == []


# ---------------------------------------------------------------------------
# truncate-falsify (CLAUDE.md recovery-feature PR gate)
# ---------------------------------------------------------------------------


async def test_registered_at_and_cancel_requested_at_survive_wal_truncation(tmp_path):
    """Tier 2: CLAUDE.md's recovery-feature gate — set X (registered_at, then
    cancel_requested_at), truncate the WAL past X's own events, reconstruct,
    assert X survives.

    Mirrors ``chain_manager.py``'s own established shape for `arm_at`
    (restore() reads it back explicitly) — same journal/WAL mechanism, two
    new fields riding it."""
    wal = tmp_path / "state.wal"
    snapshot_path = tmp_path / "snapshot.json"
    agent_name = "trunc-agent"
    state_log = StateLog(wal)
    session = make_session(agent_name=agent_name, state_log=state_log, snapshot_path=snapshot_path)

    await session.chains.register(
        chain_id="task-1", depth=1, original_text="hello", sender="trunc-agent",
        waiting_on={"peer"}, kind="prompt", cancel=lambda: None,
    )
    await session.chains.mark_cancel_requested("task-1")
    await state_log.aclose()

    reloaded = StateLog(wal)
    snap = AgentSnapshot.load(agent_name, snapshot_path)
    snap.apply_events(list(reloaded.iter_from(snap.applied_seq)))

    chain_dict = snap.pending_chains["task-1"]
    assert chain_dict.get("registered_at") is not None, (
        f"registered_at must survive reconstruction: {chain_dict!r}"
    )
    assert chain_dict.get("cancel_requested_at") is not None, (
        f"cancel_requested_at must survive reconstruction: {chain_dict!r}"
    )

    await reloaded.aclose()


# ---------------------------------------------------------------------------
# the real slash command itself — closes a gap the tests above cannot: none
# of them import or drive reyn.interfaces.slash.tasks, so a real regression
# there (e.g. reverting to a bare invoke_tool, silently dropping the audit
# trail) would pass every test above unnoticed.
# ---------------------------------------------------------------------------


async def test_the_real_slash_command_lists_and_cancels_through_the_real_op(tmp_path):
    """Tier 2: drives `reyn.interfaces.slash.tasks.tasks_cmd` itself (the
    registered `/tasks` handler) — not a reproduction of its dispatch logic.
    Confirms the real module still routes through `dispatch_tool` (the
    `caller_kind="operator"` audit event fires) and still applies the
    two-step destructive-confirm pattern (`test_slash_destructive_confirm_
    parity.py`'s own convention) for `cancel`.
    """
    from reyn.interfaces.slash import REGISTRY
    from tests._support.slash import slash_ctx

    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")
    _seed(tmp_path, "beta")
    caller = reg.get_or_load("alpha")
    reg.get_or_load("beta")
    events = _collect(caller)

    result = await run_prompt_async(
        reg, caller_agent="alpha", caller_sid="main",
        target_agent="beta", target_session="main",
        prompt="please help",
    )
    task_id = result["data"]["task_id"]

    cmd = REGISTRY.get("tasks")
    assert cmd is not None, "/tasks must be registered"

    outbox: list = []
    ctx = slash_ctx(caller, recorder=outbox)
    await cmd.handler(ctx, "")
    assert any("running tasks" in getattr(m, "text", "") for m in outbox)
    assert any(task_id in getattr(m, "text", "") for m in outbox)

    outbox.clear()
    await cmd.handler(ctx, f"cancel {task_id}")
    assert any("confirm" in getattr(m, "text", "").lower() for m in outbox), (
        "the first invocation must warn and NOT cancel yet — the same "
        "two-step destructive-confirm pattern /reset and /pending discard use"
    )
    await _settle(caller)
    tool_called_before_confirm = [
        e for e in events if e.type == "tool_called" and e.data.get("tool") == "cancel_task"
    ]
    assert tool_called_before_confirm == [], (
        "no op call may happen before the operator explicitly confirms"
    )

    outbox.clear()
    await cmd.handler(ctx, f"cancel {task_id} confirm")
    assert any("cancel requested" in getattr(m, "text", "").lower() for m in outbox)

    await _settle(caller)
    tool_called = [
        e for e in events if e.type == "tool_called" and e.data.get("tool") == "cancel_task"
    ]
    (cancel_call,) = tool_called
    assert cancel_call.data.get("caller_kind") == "operator", (
        "the REAL /tasks module must route through dispatch_tool with "
        "caller_kind='operator' — a regression to a bare invoke_tool (or a "
        "chains.get(id).cancel() reimplementation) would silently drop this"
    )
