"""Tier 2: OS invariant -- #5769 stage 3 (ADR-0047 decision 7) + #5781.

`latest_pipeline_state(run_id, state_log, *, scope)` no longer discovers
its own owner by re-reading `invocation.json` (architect's (c) ruling on
PR #5778's review, superseding an earlier, disclosed (a)/(b)-shaped
deviation): its one real caller (`PipelineExecutorDriver.run_turn`)
already holds the run's own `PipelineWorkOrder`, so `scope` is simply
handed over as a fact, never re-derived.

#5781: `PipelineExecutor.resume` no longer calls `latest_pipeline_state`
at all -- it used to (forwarding its own `scope` parameter through), so
`run_turn`'s one real call site read the SAME generation twice per
resume (once directly to decide run-vs-resume, once again inside
`resume`, discarding the first read). `resume` now takes the
already-looked-up `snapshot` directly, removing both the second read and
the `scope` parameter (which existed only to make that internal lookup).

Real `StateLog` + real `PipelineStateStore` (no mocks). Covers:
`latest_pipeline_state`'s `scope` remains a required keyword-only
argument (no default, no silent fallback -- unaffected by #5781, since
`scope` only ever existed to make `resume`'s now-removed internal lookup,
never `latest_pipeline_state`'s own contract); `resume`'s new `snapshot`
parameter is ALSO required, no default (matching decision 2/7's own
no-silent-default precedent -- a `None` default would be indistinguishable
from an intentional "always run from scratch"), and `resume` uses
EXACTLY the `snapshot` its caller passes, never re-deriving one from
`state_log` itself (the #5781 witness below).
"""
from __future__ import annotations

import pytest

from reyn.core.events.pipeline_recovery import latest_pipeline_state, record_pipeline_state
from reyn.core.events.snapshot_generations import GLOBAL_SCOPE, REWIND_KIND
from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import Pipeline, PipelineExecutor, ToolStep, TransformStep
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
async def test_resume_requires_snapshot_kwarg(tmp_path):
    """Tier 2: no default -- #5781 replaced `resume()`'s `scope` parameter
    with `snapshot` (the caller's own already-looked-up
    `latest_pipeline_state` result); this pins the same no-silent-default
    contract onto the new parameter (decision 2/7's own precedent: two real
    meanings -- "here is the run's state" vs "this is a fresh run" -- and
    the one real caller always knows which one applies, so a default would
    only ever paper over a forgotten call, not serve a caller with nothing
    to say). A call site that forgets `snapshot` must fail at the call."""
    log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")
    pipeline = Pipeline(steps=[TransformStep(value="1 + 1", output="x")])

    with pytest.raises(TypeError):
        await PipelineExecutor().resume(  # type: ignore[call-arg]
            "run-y", pipeline=pipeline, tool_dispatch=lambda *_a, **_k: None, state_log=log,
        )


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
async def test_resume_uses_exactly_the_snapshot_the_caller_passes(tmp_path, monkeypatch):
    """Tier 2: #5781's own witness -- `resume()` uses EXACTLY the `snapshot`
    its caller hands it, and never re-derives one from `state_log` itself
    (the double-read #5781 removed). Proven by a REAL, observable behavioral
    difference (no patch of any collaborator this test itself depends on --
    the only monkeypatch here registers a genuine tool handler, the same
    idiom test_pipeline_is6_attached.py's own sibling helper uses, not a
    stand-in for the thing under test).

    A real generation carrying an impossible `step_index` (5, for a 1-step
    pipeline where only 0/1 are ever real) is recorded and durably on disk.
    `resume()` is then called with `snapshot=None` anyway -- if `resume`
    still consulted `state_log` on its own (the pre-#5781 shape), it would
    find that impossible generation and try to replay from it. Because it
    now trusts ONLY the `snapshot` argument, it falls through to a genuine
    fresh run instead: the one real step executes for real (the counting
    file gets exactly one line) and `step_index == 1` -- not the impossible
    on-disk 5, and not a second read of it."""
    out_file = tmp_path / "out.txt"
    _install_counting_tool(monkeypatch, out_file)
    log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")
    pipeline = _one_step_pipeline()
    dispatch = _make_tool_dispatch(_bare_ctx())

    await _put(log, "worker")
    await record_pipeline_state(log, "run-snapshot-check", {"step_index": 5}, durable=True)

    result = await PipelineExecutor().resume(
        "run-snapshot-check",
        pipeline=pipeline,
        tool_dispatch=dispatch,
        state_log=log,
        snapshot=None,
    )

    # A real (step_index=5) generation sits on disk for this run_id, but
    # `resume` was told `snapshot=None` -- it must honor THAT, not go look.
    assert result.step_index == 1
    assert out_file.read_text(encoding="utf-8").splitlines() == ["x"]


@pytest.mark.asyncio
async def test_resume_replays_from_the_snapshot_the_caller_passes(tmp_path, monkeypatch):
    """Tier 2: the complementary half of the witness above -- when the
    caller DOES pass a real snapshot (its own `latest_pipeline_state`
    lookup, exactly as `PipelineExecutorDriver.run_turn` does), `resume`
    replays from it (exactly-once: the completed step is NOT re-executed;
    only `step_index` advances)."""
    out_file = tmp_path / "out.txt"
    _install_counting_tool(monkeypatch, out_file)
    log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")
    pipeline = _one_step_pipeline()
    dispatch = _make_tool_dispatch(_bare_ctx())

    await _put(log, "worker")
    await record_pipeline_state(
        log, "run-replay-check",
        {"step_index": 1, "pipe_data": None, "named_stores": {}, "completed_step_results": {"0": {}}},
        durable=True,
    )
    snapshot = latest_pipeline_state("run-replay-check", log, scope=GLOBAL_SCOPE)
    assert snapshot is not None  # the fixture above; a real lookup, not asserted-as-given

    result = await PipelineExecutor().resume(
        "run-replay-check",
        pipeline=pipeline,
        tool_dispatch=dispatch,
        state_log=log,
        snapshot=snapshot,
    )

    # Already at step_index 1 (the pipeline's only step) -> resume is a
    # replay-to-completion with ZERO new tool executions -- the counting
    # tool's own side effect (creating out_file) never fires.
    assert result.step_index == 1
    assert not out_file.exists()

    # Population witness (lead-coder-30 review, PR #5784): the negative
    # assertion above is only meaningful if the counting tool actually
    # WORKS -- fire it directly, once, and confirm it creates the file.
    # Distinguishes "resume chose not to execute it" from "it could never
    # have executed regardless" (a broken _install_counting_tool would
    # pass the assertion above for the wrong reason).
    await dispatch("stage3_step", {})
    assert out_file.exists()
