"""Tier 2: #4240 — the ONE reachable gap architect's co-vet reachability
question found in #4230 (#4215①): a LIVE session's ``run_pipeline`` `tool:
hooks_add` step wrote to the GLOBAL ``.reyn/config/hooks.yaml``, leaking
into every sibling session — the exact owner concern #4215① exists to
close (issue #4215: "hooks は受動的なので、他の人による登録で自身に直接
影響を受けるのが良くない").

Root cause: ``PipelineExecutorDriver._make_dispatch`` builds the
``ToolContext`` a pipeline ``tool:`` step dispatches through directly off
``self._router_host`` — a REAL, live session's ``RouterHostAdapter`` (not a
CLI/non-session context, unlike ``reyn pipe run``'s own ``pipe.py``, out of
scope here per lead-coder's issue split). It already threads
``agent_name`` (comment: "#2088: scope-aware hooks_add") but never
``session_state_dir`` — a wiring gap, not a design exclusion, since the
value is READILY AVAILABLE on ``host`` (the same ``RouterHostAdapter``
property #4215① added). ``router_loop.py``'s 3 ToolContext construction
sites already thread it identically.

Companion to ``tests/core/test_4215_1_hooks_add_session_local.py``'s own
gate 5 (isolation) — same discriminator, exercised through the pipeline
dispatch seam instead of the direct-tool-call seam.

Real objects throughout — real ``AgentRegistry``/``Session``/
``RouterHostAdapter``/``PipelineExecutorDriver``, real filesystem
``tmp_path`` roots. No mocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.work_order import PipelineWorkOrder
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.services.pipeline_executor_driver import PipelineExecutorDriver
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _registry(tmp_path: Path) -> AgentRegistry:
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        return make_session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            workspace_state_dir=tmp_path / ".reyn",
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    return reg


def _bound_driver(reg: AgentRegistry, session: Session) -> PipelineExecutorDriver:
    """A real ``PipelineExecutorDriver`` bound to *session*, the same way
    ``Session.set_loop_driver`` binds the live one — the work-order content
    is irrelevant to ``_make_dispatch`` (it reads only ``self._router_host``),
    so a minimal one is enough."""
    work_order = PipelineWorkOrder(
        run_id="p4240-run", pipeline_name="p", pipeline={"steps": []},
        input=None, reply_to_agent=session.agent_name, reply_to_sid="main",
        driver_agent=session.agent_name, driver_sid="main",
    )
    driver = PipelineExecutorDriver(work_order, registry=reg, state_log=reg.state_log)
    driver.bind_session(session, session._router_host)
    return driver


@pytest.mark.asyncio
async def test_a_pipeline_hooks_add_step_lands_in_the_session_layer_not_global(
    tmp_path: Path,
) -> None:
    """Tier 2: a pipeline ``tool: hooks_add`` step, dispatched via the REAL
    ``PipelineExecutorDriver`` tool-dispatch seam, writes to THIS session's
    own <session_state_dir>/hooks.yaml — not the global
    .reyn/config/hooks.yaml. Falsify: revert the session_state_dir kwarg
    at pipeline_executor_driver.py's ToolContext construction and this
    goes RED (writes to global instead)."""
    reg = _registry(tmp_path)
    reg.create("worker")
    sid = await reg.spawn_session_recorded(
        "worker", mode="persistent",
        presentation_consumer=None, intervention_bridge=None,
    )
    session = reg.get_session("worker", sid)
    assert session is not None

    driver = _bound_driver(reg, session)
    dispatch = await driver._make_dispatch()
    result = await dispatch("hooks_add", {"on": "turn_end", "message": "from-pipeline"})

    assert result.get("status") == "ok", result
    session_hooks = Path(session._snapshot_path).parent / "hooks.yaml"
    global_hooks = tmp_path / ".reyn" / "config" / "hooks.yaml"
    assert session_hooks.is_file(), (
        "pipeline hooks_add step did not write the session-local hooks.yaml"
    )
    assert not global_hooks.is_file(), (
        "pipeline hooks_add step leaked into the GLOBAL hooks.yaml — the exact "
        "#4215① regression this test guards"
    )


@pytest.mark.asyncio
async def test_a_pipeline_hooks_add_step_does_not_leak_to_a_sibling_session(
    tmp_path: Path,
) -> None:
    """Tier 2: #4230's own gate 5 (isolation), through the pipeline dispatch
    seam — a hook written by one session's pipeline step must not appear in
    a SIBLING session's own combine, forced to independently reload (same
    care #4230's gate 5 needed: hooks_add's own reload-scheduling only
    touches the writing session's HotReloader)."""
    reg = _registry(tmp_path)
    reg.create("worker")
    sid_a = await reg.spawn_session_recorded(
        "worker", mode="persistent",
        presentation_consumer=None, intervention_bridge=None,
    )
    sid_b = await reg.spawn_session_recorded(
        "worker", mode="persistent",
        presentation_consumer=None, intervention_bridge=None,
    )
    session_a = reg.get_session("worker", sid_a)
    session_b = reg.get_session("worker", sid_b)
    assert session_a is not None and session_b is not None

    driver_a = _bound_driver(reg, session_a)
    dispatch_a = await driver_a._make_dispatch()
    await dispatch_a("hooks_add", {"on": "turn_end", "message": "only-for-a", "wake": True})

    await session_a._hot_reloader.apply_pending()
    session_b._hot_reloader.request_reload(source="test")
    await session_b._hot_reloader.apply_pending()
    await session_a._hook_dispatcher.dispatch("turn_end", {})
    await session_b._hook_dispatcher.dispatch("turn_end", {})

    texts_a = set()
    while not session_a.inbox.empty():
        _kind, payload = session_a.inbox.get_nowait()
        texts_a.add(payload.get("text"))
    texts_b = set()
    while not session_b.inbox.empty():
        _kind, payload = session_b.inbox.get_nowait()
        texts_b.add(payload.get("text"))

    assert "only-for-a" in texts_a
    assert "only-for-a" not in texts_b
