"""Tier 2: #4240/#4241/#4244 — a pipeline ``tool: hooks_add`` step.

History, because this file's OWN premise changed mid-arc:

- **#4240/#4241** fixed a real wiring gap: ``PipelineExecutorDriver.
  _make_dispatch`` built its ``ToolContext`` off a live session's
  ``RouterHostAdapter`` without threading ``session_state_dir`` (unlike
  ``router_loop.py``'s 3 sites, which already did) — so a LIVE session's
  pipeline ``tool: hooks_add`` step wrote to the GLOBAL
  ``.reyn/config/hooks.yaml`` instead of that session's own isolated
  layer. This module originally proved the fix: the step's write landed
  session-local, and did not leak to a sibling session.
- **#4244** (architect ruling, same night) found a SHARPER hazard the
  session_state_dir fix does not close: a confused deputy. The LLM can
  author a pipeline containing ``tool: hooks_add`` via the LLM-visible
  ``pipeline_install_local``/``_source`` tools; ``run_pipeline`` (LLM-
  triggered) threads ``session_state_dir`` correctly (#4241, safe), but
  ``reyn pipe run`` (operator-triggered, ``pipe.py``'s own session-less
  ``ToolContext``) does not and never will without its own live session
  to anchor a session-local layer to — so the step's author (the LLM) and
  the trigger (a wider-authority operator) can differ, and the operator's
  run would still write the LLM-authored hook into the GLOBAL file.
  Architect's fix: deny ``hooks_add`` from EVERY pipeline step outright
  (``pipeline_verbs._PIPELINE_STEP_DENY_TOOLS``) — the same "some tools
  must not be reachable from inside a pipeline step" mechanism
  ``run_pipeline`` already uses, for a different reason.

#4244's denial fires BEFORE ``_make_dispatch``'s ToolContext construction
even matters for ``hooks_add`` specifically — so #4240/#4241's own
session-local/no-leak assertions for THIS tool are no longer reachable
truths; this module now tests the CURRENT truth (denial), which is
strictly stronger (hooks_add is unreachable via any pipeline step,
regardless of who triggers it, not merely "reachable but safe" for one
of the two trigger paths). The ``session_state_dir`` wiring #4241 added
remains live, correct, general-purpose infrastructure for whichever OTHER
pipeline-dispatchable tools DO still reach ``_make_dispatch`` — it is
simply no longer witnessed BY hooks_add.

Real objects throughout — real ``AgentRegistry``/``Session``/
``RouterHostAdapter``/``PipelineExecutorDriver``, real filesystem
``tmp_path`` roots. No mocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import PipelineExecutionError
from reyn.core.pipeline.work_order import PipelineWorkOrder
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.services.pipeline_executor_driver import PipelineExecutorDriver
from reyn.runtime.session import Session
from tests._support.agent_session import make_session
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML


def _registry(tmp_path: Path) -> AgentRegistry:
    (tmp_path / "reyn.yaml").write_text(MINIMAL_REYN_YAML, encoding="utf-8")
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
    driver.bind_session(session, session.router_host)
    return driver


@pytest.mark.asyncio
async def test_a_pipeline_hooks_add_step_is_denied_outright(tmp_path: Path) -> None:
    """Tier 2: #4244 — a pipeline ``tool: hooks_add`` step is structurally
    denied (``PipelineExecutionError``) via the REAL
    ``PipelineExecutorDriver`` tool-dispatch seam, regardless of whether
    the dispatching session has a live ``session_state_dir`` — the
    confused-deputy fix does not depend on the caller's own safety.
    Falsify: revert the ``hooks_add`` addition to
    ``pipeline_verbs._PIPELINE_STEP_DENY_TOOLS`` and this goes RED (the
    step would dispatch to the real handler instead of raising)."""
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

    with pytest.raises(PipelineExecutionError, match="hooks_add"):
        await dispatch("hooks_add", {"on": "turn_end", "message": "from-pipeline"})

    session_hooks = Path(session._snapshot_path).parent / "hooks.yaml"
    global_hooks = tmp_path / ".reyn" / "config" / "hooks.yaml"
    assert not session_hooks.is_file()
    assert not global_hooks.is_file(), (
        "hooks_add reached the write path despite the deny — the "
        "confused-deputy fix did not actually block dispatch"
    )
