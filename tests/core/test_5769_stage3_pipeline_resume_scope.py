"""Tier 2: OS invariant -- #5769 stage 3, ADR-0047 decision 7, architect's
(c) ruling on PR #5778's review (superseding an earlier, disclosed
(a)/(b)-shaped deviation).

`latest_pipeline_state(run_id, state_log, *, scope)` no longer discovers
its own owner by re-reading `invocation.json` -- both real callers
(`PipelineExecutorDriver.run_turn`'s direct call, and
`PipelineExecutor.resume`, which now forwards its own `scope` parameter)
already hold the run's own `PipelineWorkOrder` when they call this, so
`scope` is simply handed over as a fact, never re-derived. This closes
the whole class of problem the earlier version had (an `invocation.json`
read, a warning log, a `GLOBAL_SCOPE` fallback, and a disclosed decision-7
exception) -- there is now exactly one path, no cases to reconcile.

Real `StateLog` + real `PipelineStateStore` (no mocks). Covers: `scope`
is a required keyword-only argument (no default, no silent fallback);
`resume()` forwards its own `scope` straight through with its own
default of `GLOBAL_SCOPE` (safe for the many pre-existing R3/R4 executor
tests with no owning-driver concept at all -- GLOBAL_SCOPE is the
honestly correct answer for those, not a silently-forgotten one, since
nothing in this codebase can yet write a scoped pipeline-state record
without going through the one real production caller, which always
passes its real scope).
"""
from __future__ import annotations

import pytest

from reyn.core.events.pipeline_recovery import latest_pipeline_state, record_pipeline_state
from reyn.core.events.snapshot_generations import GLOBAL_SCOPE, REWIND_KIND
from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import Pipeline, PipelineExecutor, ToolStep
from reyn.tools.pipeline_verbs import _make_tool_dispatch


async def _put(log: StateLog, agent: str) -> int:
    return await log.append(
        "inbox_put", target=agent, msg_id="x", msg_kind="user", payload={"text": "x"},
    )


@pytest.mark.asyncio
async def test_latest_pipeline_state_requires_scope_kwarg(tmp_path):
    """Tier 2: no default -- a call site that forgets scope fails at the
    call, matching PipelineStateStore.latest_active's own required-kwarg
    contract this function now delegates to directly."""
    log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")

    with pytest.raises(TypeError):
        latest_pipeline_state("run-x", log)  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_latest_pipeline_state_uses_exactly_the_scope_the_caller_passes(tmp_path):
    """Tier 2: strip-falsifier target. The caller's own `scope` (not any
    file this function might read) is the only thing that decides
    visibility -- a rewind scoped to a DIFFERENT (agent, sid) than the
    one the CALLER passes must not hide the generation; one matching the
    CALLER's own passed scope must."""
    log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")
    s1 = await _put(log, "worker")
    await record_pipeline_state(log, "run-1", {"step_index": 1}, durable=True)
    s2 = await _put(log, "worker")
    await record_pipeline_state(log, "run-1", {"step_index": 2}, durable=True)

    # A rewind scoped to a DIFFERENT (agent, sid) than the one the caller
    # will pass -- must not affect what the caller sees.
    await log.append(
        REWIND_KIND, target_n=s1, supersedes=None, scope=["someone-else", "sidZ"],
    )
    still_visible = latest_pipeline_state("run-1", log, scope=("worker", "sidA"))
    assert still_visible == {"step_index": 2}

    # A rewind scoped to EXACTLY the scope the caller passes DOES abandon
    # the later generation.
    await log.append(
        REWIND_KIND, target_n=s1, supersedes=None, scope=["worker", "sidA"],
    )
    now_hidden = latest_pipeline_state("run-1", log, scope=("worker", "sidA"))
    assert now_hidden == {"step_index": 1}


@pytest.mark.asyncio
async def test_latest_pipeline_state_global_scope_ignores_any_scoped_rewind(tmp_path):
    """Tier 2: a caller that genuinely has no owner concept (matching
    resume()'s own default) passes GLOBAL_SCOPE -- a rewind scoped to ANY
    particular session is invisible to it, by GLOBAL_SCOPE's own contract
    (`record_scope is None or record_scope == scope`,
    snapshot_generations.py)."""
    log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")
    s1 = await _put(log, "worker")
    await record_pipeline_state(log, "run-2", {"step_index": 1}, durable=True)

    # target_n=0 abandons (0, R) -- which genuinely includes s1, so a
    # broken implementation that let this leak into a GLOBAL query would
    # hide the generation -- a real discriminator, not a no-op interval.
    await log.append(
        REWIND_KIND, target_n=0, supersedes=None, scope=["someone-else", "sidZ"],
    )

    result = latest_pipeline_state("run-2", log, scope=GLOBAL_SCOPE)

    assert result == {"step_index": 1}


def _install_counting_tool(monkeypatch, out_file) -> None:
    """A REAL side-effecting tool registered on the real ToolRegistry (same
    idiom as test_pipeline_is6_attached.py's own helper of the same name):
    every call appends a line to `out_file` -- line count == real
    execution count, the exactly-once/fresh-run-vs-resume probe."""
    import reyn.tools as tools_pkg
    from reyn.tools.types import ToolDefinition, ToolGates

    async def _handler(args, ctx):
        out_file.parent.mkdir(parents=True, exist_ok=True)
        with out_file.open("a", encoding="utf-8") as f:
            f.write("x\n")
        return {}

    tool = ToolDefinition(
        name="stage3_step", description="test tool", parameters={"type": "object", "properties": {}},
        gates=ToolGates(router="allow"), handler=_handler, category="io", purity="side_effect",
    )
    base = tools_pkg.get_default_registry

    def _with_tool():
        registry = base()
        registry.register(tool)
        return registry

    monkeypatch.setattr(tools_pkg, "get_default_registry", _with_tool)


def _one_step_pipeline() -> Pipeline:
    return Pipeline(steps=[ToolStep(name="stage3_step", args={}, output="o0")])


def _bare_ctx():
    from reyn.core.events.events import EventLog
    from reyn.tools.types import ToolContext
    return ToolContext(
        events=EventLog(), permission_resolver=None, workspace=None,
        caller_kind="router", router_state=None, state_log=None,
    )


@pytest.mark.asyncio
async def test_resume_forwards_its_scope_real_witness(tmp_path, monkeypatch):
    """Tier 2: `PipelineExecutor.resume`'s own `scope` parameter really
    reaches `latest_pipeline_state` -- proven by a REAL, observable
    behavioral difference (no patch of any collaborator this test itself
    depends on -- the only monkeypatch here registers a genuine tool
    handler, the same idiom test_pipeline_is6_attached.py's own sibling
    helper uses, not a stand-in for the thing under test).

    A recorded generation carries an impossible `step_index` (5, for a
    1-step pipeline where only 0/1 are ever real) and is then hidden by a
    rewind scoped to EXACTLY the scope this test passes to `resume()`. If
    `resume()` genuinely forwarded that scope to `latest_pipeline_state`,
    the impossible generation is invisible -> resume() falls through to a
    fresh run -> the one real step executes for real (the counting file
    gets exactly one line) -> `step_index == 1`. If scope were silently
    dropped (always seeing the record), resume() would instead try to
    replay from the impossible snapshot -- observably NOT "one real
    execution, step_index == 1"."""
    out_file = tmp_path / "out.txt"
    _install_counting_tool(monkeypatch, out_file)
    log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")
    pipeline = _one_step_pipeline()
    dispatch = _make_tool_dispatch(_bare_ctx())

    await _put(log, "worker")
    await record_pipeline_state(log, "run-scope-check", {"step_index": 5}, durable=True)

    # Scoped to hide the impossible generation FOR EXACTLY (worker, sidA).
    await log.append(
        REWIND_KIND, target_n=0, supersedes=None, scope=["worker", "sidA"],
    )

    result = await PipelineExecutor().resume(
        "run-scope-check",
        pipeline=pipeline,
        tool_dispatch=dispatch,
        state_log=log,
        scope=("worker", "sidA"),
    )

    # Hidden for (worker, sidA) -> resume() fell through to a genuine
    # fresh run -> the one real step executed once -> step_index == 1.
    assert result.step_index == 1
    assert out_file.read_text(encoding="utf-8").splitlines() == ["x"]
