"""Tier 2: OS invariant — the `parallel` HETEROGENEOUS NAMED-branch fan-out
primitive, the LAST non-linear primitive (Appendix B;
``docs/proposals/reyn-pipeline-spec-v0.8.md`` lines 737-741, Hard rule 6, N2).
Every Appendix-B non-linear primitive (``call``/``match``/``fold``/
``for_each``/``parallel``) is now implemented.

``parallel`` is ``for_each``'s static-branch-set sibling — it reuses the EXACT
SAME fan-out substrate (concurrent recovery via a single serializing
coordinator + a NO-OP recorder inside branch tasks, the
``__fan_out_dropped__`` kind-marker, the ``fan_out_depth``/``SpawnBudget`` S5
guards). Structural differences covered here:

  1. happy path — a STATIC dict of heterogeneous NAMED branches runs
     CONCURRENTLY; ``collect`` runs ONCE over the NAMED MAP ``results.NAME``
     (not an ordered list); ``collect``'s result is the primitive's N2 return
     / pipe-data; ``output`` threads out (R3 uniformity).
  2. ``on_error`` DEFAULT = ``abort`` when omitted (Appendix B's ``on_error?:``
     — a parser test, unlike ``for_each`` where it is required).
  3. ``on_error`` — ``continue`` DROPS a failed branch (a kind-marker, so
     resume never re-runs it; ``collect`` sees the surviving named map, MUST
     handle the absent branch) / ``abort`` fails the whole step / ``retry(n)``
     re-runs a flaky branch then succeeds, and a still-failing branch after
     ``n`` retries falls back to abort.
  4. the CLAUDE.md-mandated truncate-falsify recovery gate: mid-parallel
     crash (some branches done + 1 dropped, ``collect`` not yet recorded),
     WAL truncated below the source events, resume from the generation FILE →
     done branches replay exactly-once (their side-effect file is unchanged),
     the dropped branch stays dropped, ``collect`` runs ONCE over the named
     map.
  5. S5 guards — (b) a ``parallel`` nested deeper than ``max_fan_out_depth``
     FAILS the step; (c) branches whose Step is an ``agent`` step exceeding
     ``max_pipeline_spawns`` FAILS the step (the shared per-run
     ``SpawnBudget`` counter).

Real ``StateLog`` + ``PipelineRegistry`` + real generation files + a real
``AgentRegistry``/``Session``/``MessageBus`` (the S5-c spawn test) throughout —
no mocks, no private-state assertions (the ONE faked collaborator is the LLM
completion call, injected via the real ``_loop_observer`` seam, exactly as
``test_pipeline_for_each_primitive.py`` does).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import (
    AgentStep,
    CallStep,
    ParallelStep,
    Pipeline,
    PipelineExecutionError,
    PipelineExecutor,
    ToolStep,
    TransformStep,
)
from reyn.core.pipeline.parser import parse_pipeline_dsl
from reyn.core.pipeline.registry import PipelineRegistry
from reyn.core.pipeline.schema import SchemaRegistry
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session

# ── happy path: heterogeneous named branches, named-map collect, N2 out ──────


@pytest.mark.asyncio
async def test_parallel_runs_named_branches_and_collects_named_map():
    """Tier 2: each NAMED branch is its OWN heterogeneous Step, all run
    CONCURRENTLY; ``collect`` runs ONCE over the NAMED MAP ``{name: result}``
    (not an ordered list), and its result becomes the parallel step's N2
    return value + named output (R3)."""
    pipeline = Pipeline(steps=[
        TransformStep(value="'seed'"),  # step 0: pipe = 'seed'
        ParallelStep(
            on_error="abort",
            branches={
                "upper": TransformStep(value="pipe + '-UP'"),
                "lower": TransformStep(value="pipe + '-lo'"),
            },
            collect=TransformStep(value="pipe"),  # pipe == the named-map dict
            output="combined",
        ),
    ])

    result = await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=lambda *_a, **_k: None,
        state_log=None, run_id="run-par-happy",
    )

    assert result.named_stores["combined"] == {"upper": "seed-UP", "lower": "seed-lo"}
    assert result.pipe_data == {"upper": "seed-UP", "lower": "seed-lo"}
    # per-branch flat keys (by NAME, not index) + the collect key.
    assert result.completed_step_results["1.parallel.upper"] == "seed-UP"
    assert result.completed_step_results["1.parallel.lower"] == "seed-lo"
    assert result.completed_step_results["1.parallel.collect"] == {
        "upper": "seed-UP", "lower": "seed-lo",
    }


@pytest.mark.asyncio
async def test_parallel_branches_see_isolated_ctx_and_shared_pipe_at_call_site():
    """Tier 2: each branch's context is a COPY of the outer named stores
    (isolation, Hard rule 6) and the parallel step's OWN incoming pipe-data
    held constant across every branch (Hard rule 5's per-branch analog) — no
    sibling visibility (writes only happen in collect)."""
    pipeline = Pipeline(steps=[
        TransformStep(value="'PFX'"),  # step 0: pipe = 'PFX'
        ParallelStep(
            on_error="abort",
            branches={
                "a": TransformStep(value="pipe + '-a'", output="local_a"),
                "b": TransformStep(value="pipe + '-b'"),
            },
            collect=TransformStep(value="pipe"),
        ),
    ])
    result = await PipelineExecutor().run(
        pipeline, {"local_a": "outer-untouched"},
        tool_dispatch=lambda *_a, **_k: None,
        state_log=None, run_id="run-par-isolation",
    )
    assert result.pipe_data == {"a": "PFX-a", "b": "PFX-b"}
    # branch "a"'s own output write never leaked to the outer named stores.
    assert result.named_stores["local_a"] == "outer-untouched"


@pytest.mark.asyncio
async def test_parallel_empty_pipe_data_not_a_list_still_works_over_static_branches():
    """Tier 2: unlike ``for_each``, ``parallel`` never resolves a runtime list
    source — its branch set is a STATIC dict, so a pipeline whose incoming
    pipe-data is not list-shaped at all still fans out fine."""
    pipeline = Pipeline(steps=[
        ParallelStep(
            on_error="abort",
            branches={"only": TransformStep(value="1 + 1")},
            collect=TransformStep(value="pipe.only"),
        ),
    ])
    result = await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=lambda *_a, **_k: None,
        state_log=None, run_id="run-par-nolist",
    )
    assert result.pipe_data == 2


# ── on_error DEFAULT = abort when omitted (Appendix B's on_error?:) ──────────


@pytest.mark.asyncio
async def test_on_error_omitted_defaults_to_abort_dataclass_default():
    """Tier 1: ``ParallelStep.on_error`` defaults to ``"abort"`` (Appendix B's
    ``on_error?:`` — the ONE required-vs-optional divergence from
    ``for_each``)."""
    step = ParallelStep(branches={"x": TransformStep(value="1")}, collect=TransformStep(value="pipe"))
    assert step.on_error == "abort"


def test_parser_on_error_omitted_defaults_to_abort():
    """Tier 1: the DSL parser defaults ``on_error`` to ``"abort"`` when the
    ``parallel`` step body omits it entirely."""
    text = """
pipeline: par-default
steps:
  - parallel:
      branches:
        a:
          transform: {value: "1"}
        b:
          transform: {value: "2"}
      collect:
        transform: {value: "pipe"}
"""
    pipeline = parse_pipeline_dsl(text, SchemaRegistry())
    step = pipeline.steps[0]
    assert isinstance(step, ParallelStep)
    assert step.on_error == "abort"


def test_parser_on_error_explicit_continue_is_honored():
    """Tier 1: an explicit ``on_error`` value is parsed through unchanged (not
    silently overridden by the default)."""
    text = """
pipeline: par-explicit
steps:
  - parallel:
      on_error: continue
      branches:
        a:
          transform: {value: "1"}
      collect:
        transform: {value: "pipe"}
"""
    pipeline = parse_pipeline_dsl(text, SchemaRegistry())
    step = pipeline.steps[0]
    assert isinstance(step, ParallelStep)
    assert step.on_error == "continue"


def test_parser_rejects_empty_branches():
    """Tier 1: 'branches' must be a non-empty mapping."""
    text = """
pipeline: par-empty
steps:
  - parallel:
      branches: {}
      collect:
        transform: {value: "pipe"}
"""
    with pytest.raises(Exception):
        parse_pipeline_dsl(text, SchemaRegistry())


def test_parser_rejects_missing_collect():
    """Tier 1: 'collect' is required."""
    text = """
pipeline: par-nocollect
steps:
  - parallel:
      branches:
        a:
          transform: {value: "1"}
"""
    with pytest.raises(Exception):
        parse_pipeline_dsl(text, SchemaRegistry())


# ── on_error policies (parallel behavior over the runner, not the parser) ────


@pytest.mark.asyncio
async def test_on_error_continue_drops_failed_branch_and_collect_sees_survivors():
    """Tier 2: ``on_error:continue`` DROPS a failed branch from the named-map
    results — its branch key holds a kind-marker (so resume never re-runs
    it), and ``collect``'s input is the surviving named map, with the failed
    branch's name simply ABSENT (not a hole/None)."""
    def _dispatch(name: str, args: dict) -> Any:
        v = args["v"]
        if v == "bad":
            raise RuntimeError("boom")
        return v.upper()

    pipeline = Pipeline(steps=[
        ParallelStep(
            on_error="continue",
            branches={
                "good": ToolStep(name="work", args={"v": "ok"}),
                "bad": ToolStep(name="work", args={"v": "bad"}),
            },
            collect=TransformStep(value="pipe"),
        ),
    ])
    result = await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=_dispatch, state_log=None, run_id="run-par-continue",
    )
    # collect saw only the survivors — the failed branch is absent, not None.
    # #2425 PR-2: a str ToolStep result maps to the flat {"text": ...} ctx shape.
    # #3070: a genuine drop additionally exposes its real cause under the
    # reserved `__branch_errors__` key (additive — "good"'s own shape is
    # untouched).
    assert result.pipe_data["good"] == {"text": "OK"}
    assert "bad" not in result.pipe_data
    assert "boom" in result.pipe_data["__branch_errors__"]["bad"]
    dropped = result.completed_step_results["0.parallel.bad"]
    assert dropped["__fan_out_dropped__"] is True
    assert "boom" in dropped["error"]


@pytest.mark.asyncio
async def test_on_error_abort_fails_the_whole_step():
    """Tier 2: ``on_error:abort`` — a single branch failure fails the whole
    parallel step (``PipelineExecutionError``), not a silent drop."""
    def _dispatch(name: str, args: dict) -> Any:
        if args["v"] == "bad":
            raise RuntimeError("boom")
        return args["v"]

    pipeline = Pipeline(steps=[
        ParallelStep(
            on_error="abort",
            branches={
                "good": ToolStep(name="work", args={"v": "ok"}),
                "bad": ToolStep(name="work", args={"v": "bad"}),
            },
            collect=TransformStep(value="pipe"),
        ),
    ])
    with pytest.raises(PipelineExecutionError):
        await PipelineExecutor().run(
            pipeline, None,
            tool_dispatch=_dispatch, state_log=None, run_id="run-par-abort",
        )


@pytest.mark.asyncio
async def test_on_error_retry_reruns_flaky_branch_until_success():
    """Tier 2: ``on_error:retry(2)`` re-runs a flaky branch (fails twice,
    succeeds on the 3rd attempt) — the branch lands, not dropped, so
    ``collect`` sees it."""
    attempts: dict[str, int] = {}

    def _dispatch(name: str, args: dict) -> Any:
        v = args["v"]
        attempts[v] = attempts.get(v, 0) + 1
        if v == "flaky" and attempts[v] < 3:
            raise RuntimeError("transient")
        return v

    pipeline = Pipeline(steps=[
        ParallelStep(
            on_error="retry(2)",
            branches={
                "ok": ToolStep(name="work", args={"v": "ok"}),
                "flaky": ToolStep(name="work", args={"v": "flaky"}),
            },
            collect=TransformStep(value="pipe"),
        ),
    ])
    result = await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=_dispatch, state_log=None, run_id="run-par-retry-ok",
    )
    assert result.pipe_data == {"ok": {"text": "ok"}, "flaky": {"text": "flaky"}}
    assert attempts["flaky"] == 3  # 1 initial + 2 retries


@pytest.mark.asyncio
async def test_on_error_retry_exhausted_falls_back_to_abort():
    """Tier 2: ``retry(1)`` on an ALWAYS-failing branch exhausts its retries
    and then falls back to ABORT (only ``continue`` ever silently drops) —
    the step fails."""
    calls: dict[str, int] = {}

    def _dispatch(name: str, args: dict) -> Any:
        v = args["v"]
        calls[v] = calls.get(v, 0) + 1
        if v == "always":
            raise RuntimeError("permanent")
        return v

    pipeline = Pipeline(steps=[
        ParallelStep(
            on_error="retry(1)",
            branches={"always": ToolStep(name="work", args={"v": "always"})},
            collect=TransformStep(value="pipe"),
        ),
    ])
    with pytest.raises(PipelineExecutionError):
        await PipelineExecutor().run(
            pipeline, None,
            tool_dispatch=_dispatch, state_log=None, run_id="run-par-retry-exhaust",
        )
    assert calls["always"] == 2  # 1 initial + 1 retry, then abort


# ── CLAUDE.md truncate-falsify recovery gate ─────────────────────────────────


def _append_dispatch(state_log: StateLog, out_file: Path, crash: set):
    """A REAL side-effecting tool (mirrors for_each's truncate-falsify probe):
    each call appends a line to ``out_file`` AND a WAL entry (so R4 gens land
    at distinct durable seqs). The exactly-once probe is the FILE. A line in
    ``crash`` raises BEFORE its write, arming a genuine failure."""

    async def _dispatch(name: str, args: dict) -> Any:
        assert name == "append"
        line = str(args["line"])
        if line in crash:
            raise RuntimeError(f"simulated crash before writing {line!r}")
        with out_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        await state_log.append("inbox_put", note=line)
        return {"wrote": line}

    return _dispatch


def _fan_out_pipeline(on_error: str) -> Pipeline:
    """parallel over 4 named branches (append each), collect appends a
    COLLECT marker line."""
    return Pipeline(steps=[
        ParallelStep(
            on_error=on_error,
            branches={
                "a": ToolStep(name="append", args={"line": "A"}),
                "b": ToolStep(name="append", args={"line": "B"}),
                "c": ToolStep(name="append", args={"line": "C"}),
                "d": ToolStep(name="append", args={"line": "D"}),
            },
            collect=ToolStep(name="append", args={"line": "COLLECT"}),
        ),
    ])


@pytest.mark.asyncio
async def test_truncate_falsify_mid_parallel_replays_branches_exactly_once(tmp_path: Path):
    """Tier 2: MANDATORY CLAUDE.md recovery gate for the parallel fan-out
    substrate. Phase 1: branches a/b/d write + record their branch keys;
    branch c fails (``on_error:continue`` -> dropped marker recorded);
    ``collect`` then crashes BEFORE recording its key. The WAL is truncated
    BELOW all source events. Resume from the generation FILE ->

      - the completed branches REPLAY exactly-once (a/b/d are NOT re-written
        — the file proves it),
      - the dropped branch c STAYS dropped (NOT re-run — its marker key is
        present),
      - ``collect`` runs ONCE (writes COLLECT), threading its result out over
        the named map.

    RED if a branch rode a truncatable WAL event, if resume re-ran the whole
    fan-out, or if the dropped branch was re-run as an absent-keyed pending
    branch."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"
    # C fails (→ dropped); COLLECT crashes phase 1 AFTER all branches recorded.
    crash = {"C", "COLLECT"}
    dispatch = _append_dispatch(state_log, out_file, crash)

    pipeline = _fan_out_pipeline("continue")

    with pytest.raises(RuntimeError):
        await PipelineExecutor().run(
            pipeline, None,
            tool_dispatch=dispatch, state_log=state_log, run_id="run-par-tf",
        )
    await state_log.flush()
    # a/b/d written (c dropped, COLLECT crashed) — order is completion-order.
    assert sorted(out_file.read_text(encoding="utf-8").splitlines()) == ["A", "B", "D"]

    from reyn.core.events.pipeline_recovery import latest_pipeline_state
    snap = latest_pipeline_state("run-par-tf", state_log)
    for name in ("a", "b", "c", "d"):
        assert f"0.parallel.{name}" in snap["completed_step_results"]
    assert snap["completed_step_results"]["0.parallel.c"]["__fan_out_dropped__"] is True
    assert "0.parallel.collect" not in snap["completed_step_results"]

    # WAL head climbs past the crash-state seqs, then GC truncates below 40 —
    # every branch's own WAL entry is dropped from wal.jsonl.
    for i in range(50):
        await state_log.append("inbox_put", n=100 + i)
    await state_log.truncate_below(40)
    await state_log.flush()
    surviving = {e["seq"] for e in state_log.iter_from(0)}
    assert all(s >= 40 for s in surviving), "the branches' WAL entries are truncated away"

    # RESUME with COLLECT disarmed (C stays armed — it must NOT be re-run anyway).
    crash.discard("COLLECT")
    resumed = await PipelineExecutor().resume(
        "run-par-tf", pipeline=pipeline,
        tool_dispatch=dispatch, state_log=state_log,
    )

    lines = out_file.read_text(encoding="utf-8").splitlines()
    # Exactly-once: a/b/d appear ONCE each (replayed, not re-written); c NEVER
    # (stayed dropped, not re-run despite still being armed); COLLECT once.
    assert sorted(lines) == ["A", "B", "COLLECT", "D"]
    assert lines.count("A") == 1 and lines.count("B") == 1 and lines.count("D") == 1
    assert "C" not in lines
    # collect ran once; its result threads out as the parallel N2 return.
    assert resumed.pipe_data == {"text": "", "structured": {"wrote": "COLLECT"}}
    assert resumed.completed_step_results["0.parallel.collect"] == {"text": "", "structured": {"wrote": "COLLECT"}}


@pytest.mark.asyncio
async def test_resume_after_full_parallel_replays_with_zero_new_side_effects(tmp_path: Path):
    """Tier 2: resuming a fully-completed parallel replays every branch +
    collect from the snapshot with ZERO new writes. RED if resume re-fired a
    completed branch or the collect."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"
    dispatch = _append_dispatch(state_log, out_file, set())
    pipeline = _fan_out_pipeline("abort")

    await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=dispatch, state_log=state_log, run_id="run-par-done",
    )
    await state_log.flush()
    before = sorted(out_file.read_text(encoding="utf-8").splitlines())
    assert before == ["A", "B", "C", "COLLECT", "D"]

    resumed = await PipelineExecutor().resume(
        "run-par-done", pipeline=pipeline,
        tool_dispatch=dispatch, state_log=state_log,
    )
    after = sorted(out_file.read_text(encoding="utf-8").splitlines())
    assert after == before, "a fully-completed fan-out must replay with zero side effects"
    assert resumed.pipe_data == {"text": "", "structured": {"wrote": "COLLECT"}}


# ── S5 guard (b): fan_out_depth cap FAILS the step (does not spawn) ──────────


@pytest.mark.asyncio
async def test_fan_out_depth_cap_fails_a_too_deeply_nested_parallel():
    """Tier 2: (S5 guard b — bounded-by-construction) a ``parallel`` nested
    deeper than ``max_fan_out_depth`` FAILS the step rather than spawning. A
    top-level parallel = depth 1; a parallel inside one of its branches =
    depth 2. With ``max_fan_out_depth=1`` the inner one exceeds the cap and
    fails."""
    inner = ParallelStep(
        on_error="abort",
        branches={"only": TransformStep(value="1")},
        collect=TransformStep(value="pipe"),
    )
    outer = Pipeline(steps=[
        ParallelStep(
            on_error="abort",  # so the inner depth-failure propagates as a step failure
            branches={"nested": inner},
            collect=TransformStep(value="pipe"),
        ),
    ])
    with pytest.raises(PipelineExecutionError) as exc:
        await PipelineExecutor().run(
            outer, None,
            tool_dispatch=lambda *_a, **_k: None,
            state_log=None, run_id="run-par-depth",
            max_fan_out_depth=1,
        )
    assert "depth" in str(exc.value)


@pytest.mark.asyncio
async def test_nested_parallel_within_depth_cap_succeeds():
    """Tier 2: the depth guard is a CAP, not a blanket ban — a parallel nested
    to depth 2 runs fine under ``max_fan_out_depth=2`` (bounded, not
    forbidden)."""
    outer = Pipeline(steps=[
        ParallelStep(
            on_error="abort",
            branches={
                "nested": ParallelStep(
                    on_error="abort",
                    branches={"x": TransformStep(value="1"), "y": TransformStep(value="2")},
                    collect=TransformStep(value="pipe.x + pipe.y"),
                ),
                "flat": TransformStep(value="10"),
            },
            collect=TransformStep(value="pipe.nested + pipe.flat"),
        ),
    ])
    result = await PipelineExecutor().run(
        outer, None,
        tool_dispatch=lambda *_a, **_k: None,
        state_log=None, run_id="run-par-depth-ok",
        max_fan_out_depth=2,
    )
    assert result.pipe_data == 13  # (1 + 2) + 10


# ── S5 guard (c): per-run spawn budget ────────────────────────────────────────


class _ScriptedAgentReply:
    """Always answers with one fixed plain-text turn (no tool_calls) — the
    ONLY faked collaborator (see module docstring), injected via the real
    ``_loop_observer`` seam exactly as
    ``test_pipeline_for_each_primitive.py`` does."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        self.calls += 1
        return LLMToolCallResult(
            content=self.content, tool_calls=[], finish_reason="stop", usage=TokenUsage(),
        )


def _agent_registry(tmp_path: Path, state_log: StateLog, scripted: _ScriptedAgentReply) -> AgentRegistry:
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        s = Session(
            presentation_consumer=presentation_consumer,
            intervention_bridge=intervention_bridge,
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
        )
        s._loop_driver._loop_observer = (lambda loop: setattr(loop, "_llm_caller", scripted))
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    reg.create("worker")
    return reg


@pytest.mark.asyncio
async def test_spawn_budget_cap_fails_a_parallel_over_too_many_agent_branches(tmp_path: Path):
    """Tier 2: (S5 guard c — bounded-by-construction, real spawn path) a
    parallel with 3 agent-step branches and ``max_pipeline_spawns=2`` FAILS
    the step — a 3rd branch's spawn exceeds the per-run budget (the ONLY
    spawn-count enforcement for lineage-less pipeline agent-steps). Unlike
    ``for_each`` (which has ``max_parallel=1`` to make the ordering
    deterministic), ``parallel`` has NO concurrency-limiting field — all
    branches race, so the assertion only pins the INVARIANT the budget
    guarantees (never more spawns COMPLETE than the cap), not a specific
    completion count (that race is exactly why the cap, not a count, is the
    enforcement mechanism)."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    scripted = _ScriptedAgentReply("done")
    reg = _agent_registry(tmp_path, state_log, scripted)

    pipeline = Pipeline(steps=[
        ParallelStep(
            on_error="abort",  # so the budget-exceed failure fails the step
            branches={
                "a": AgentStep(prompt="a", identity="worker"),
                "b": AgentStep(prompt="b", identity="worker"),
                "c": AgentStep(prompt="c", identity="worker"),
            },
            collect=TransformStep(value="pipe"),
        ),
    ])
    with pytest.raises(PipelineExecutionError) as exc:
        await PipelineExecutor().run(
            pipeline, None,
            tool_dispatch=lambda *_a, **_k: None,
            state_log=state_log, run_id="run-par-spawns",
            registry=reg, max_pipeline_spawns=2,
        )
    assert "spawn cap" in str(exc.value)
    assert scripted.calls <= 2, (
        "the SpawnBudget cap must never let more than max_pipeline_spawns "
        "(2) agent branches actually complete their spawn"
    )


@pytest.mark.asyncio
async def test_parallel_of_agent_branches_within_spawn_cap_succeeds(tmp_path: Path):
    """Tier 2: within ``max_pipeline_spawns``, an agent-branch parallel runs
    every branch and collects — the budget is a cap, not a ban (bounded, not
    forbidden)."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    scripted = _ScriptedAgentReply("ok")
    reg = _agent_registry(tmp_path, state_log, scripted)

    pipeline = Pipeline(steps=[
        ParallelStep(
            on_error="abort",
            branches={
                "a": AgentStep(prompt="a", identity="worker"),
                "b": AgentStep(prompt="b", identity="worker"),
                "c": AgentStep(prompt="c", identity="worker"),
            },
            collect=TransformStep(value="count([pipe.a, pipe.b, pipe.c])"),
        ),
    ])
    result = await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=lambda *_a, **_k: None,
        state_log=state_log, run_id="run-par-spawns-ok",
        registry=reg, max_pipeline_spawns=5,
    )
    assert result.pipe_data == 3
    assert scripted.calls == 3


# ── compositional-do (branch) recovery gap, documented, mirrors for_each ─────


@pytest.mark.asyncio
async def test_compositional_branch_reruns_atomically_on_crash(tmp_path: Path):
    """Tier 2: DOCUMENTS the known fan-out recovery gap (module docstring's
    "commit unit = the ITEM/BRANCH"). A ``call``-branch crashed MID-BRANCH
    (its callee wrote sub-step 0's side effect, sub-step 1 failed) re-runs
    ATOMICALLY on resume — because branch tasks record through a NO-OP
    recorder, the branch's internal progress is never journaled, so the
    completed internal side effect RE-FIRES. A single-step branch has no such
    gap (see the truncate-falsify test above)."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"
    crash = {"X-b"}  # the callee's 2nd sub-step fails in phase 1
    dispatch = _append_dispatch(state_log, out_file, crash)

    registry = PipelineRegistry()
    registry.register("worker", Pipeline(steps=[
        ToolStep(name="append", args={"line": "X-a"}),
        ToolStep(name="append", args={"line": "X-b"}),
    ]))
    pipeline = Pipeline(steps=[
        ParallelStep(
            on_error="abort",
            branches={"x": CallStep(pipeline="worker")},
            collect=TransformStep(value="pipe"),
        ),
    ])

    with pytest.raises(PipelineExecutionError):
        await PipelineExecutor().run(
            pipeline, None,
            tool_dispatch=dispatch, state_log=state_log, run_id="run-par-comp",
            pipeline_registry=registry,
        )
    await state_log.flush()
    assert out_file.read_text(encoding="utf-8").splitlines() == ["X-a"]

    # RESUME disarmed: the branch re-runs WHOLE (its key was never recorded),
    # so X-a is written AGAIN (the documented internal-side-effect re-fire).
    crash.clear()
    await PipelineExecutor().resume(
        "run-par-comp", pipeline=pipeline,
        tool_dispatch=dispatch, state_log=state_log, pipeline_registry=registry,
    )
    lines = out_file.read_text(encoding="utf-8").splitlines()
    assert lines == ["X-a", "X-a", "X-b"], (
        "a compositional branch re-runs atomically on resume — X-a re-fires "
        "(the documented commit-unit=BRANCH gap; single-step branch has no "
        "such gap)"
    )
