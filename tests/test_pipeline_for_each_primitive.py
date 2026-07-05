"""Tier 2: OS invariant — the `for_each` CONCURRENT fan-out primitive + fan-out
substrate (concurrent recovery + S5 spawn bounds).

Covers ``for_each`` (Appendix B; ``docs/proposals/reyn-pipeline-spec-v0.8.md``
Hard rule 6, N2) — ``fold``'s parallel, isolated sibling built on the same
``_run_scope`` + dotted-path recovery + dispatch-table foundation as
``call``/``match``/``fold``:

  1. happy path — ``do`` runs over each item as an ISOLATED sub-scope; ``collect``
     runs ONCE over the ORDERED results list (by item index, NOT completion
     order); ``collect``'s result is the primitive's N2 return / pipe-data.
  2. ``max_parallel`` bounds live concurrency (S5 guard a — bounded-by-
     construction: never more than ``max_parallel`` items in flight at once).
  3. ``on_error`` — ``continue`` DROPS a failed item (a kind-marker, so resume
     never re-runs it; ``collect`` sees the surviving list); ``abort`` fails the
     whole step; ``retry(n)`` re-runs a flaky item then succeeds, and a still-
     failing item after ``n`` retries falls back to abort.
  4. the CLAUDE.md-mandated truncate-falsify recovery gate over a SINGLE-STEP
     ``do`` (the exactly-once item-commit case): M/N items done + 1 dropped,
     crash before ``collect`` records, WAL truncated below the source events,
     resume from the generation FILE → completed items REPLAY exactly-once (their
     side-effect file is unchanged), the dropped item STAYS dropped (not re-run),
     only absent items run, ``collect`` runs ONCE.
  5. the documented COMPOSITIONAL-``do`` recovery gap: a ``call``-``do`` item
     in-flight at crash re-runs ATOMICALLY on resume (its completed internal side
     effects re-fire) — a known, TESTED contract, not a silent surprise (the
     module docstring's "commit unit = the ITEM"; follow-up issue tracks closing
     it).
  6. S5 guards — (b) a ``for_each`` nested deeper than ``max_fan_out_depth``
     FAILS the step (does not spawn); (c) a fan-out spawning more ephemeral
     sessions than ``max_pipeline_spawns`` FAILS the step (the per-run
     ``SpawnBudget`` counter is the only enforcement for lineage-less pipeline
     agent-steps).

Real ``StateLog`` + ``PipelineRegistry`` + real generation files + a real
``AgentRegistry``/``Session``/``MessageBus`` (the S5-c spawn test) throughout —
no mocks, no private-state assertions (the ONE faked collaborator is the LLM
completion call, injected via the real ``_loop_observer`` seam, exactly as
``test_pipeline_r5_agent_step_executor.py`` does).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import (
    AgentStep,
    CallStep,
    ExprRef,
    ForEachStep,
    Pipeline,
    PipelineExecutionError,
    PipelineExecutor,
    SpawnBudget,
    ToolStep,
    TransformStep,
)
from reyn.core.pipeline.registry import PipelineRegistry
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session

# ── happy path: concurrent isolated items, ordered collect, N2 out ───────────


@pytest.mark.asyncio
async def test_for_each_runs_items_and_collects_ordered_results():
    """Tier 2: ``do`` runs once per item (each seeing its own ``{item}`` and the
    fan-out step's pipe-data), and ``collect`` runs ONCE over the ORDERED results
    list (item-index order, regardless of completion order), its result becoming
    the for_each step's N2 return value + named output."""
    pipeline = Pipeline(steps=[
        TransformStep(value="'seed'", output="carried"),  # step 0: pipe = 'seed'
        ForEachStep(
            over="ctx.words",
            on_error="abort",
            do=TransformStep(value="item + '!'"),
            collect=TransformStep(value="pipe"),  # pipe == the ordered results list
            output="shouted",
        ),
    ])

    result = await PipelineExecutor().run(
        pipeline, {"words": ["a", "b", "c"]},
        tool_dispatch=lambda *_a, **_k: None,
        state_log=None, run_id="run-fe-happy",
    )

    assert result.named_stores["shouted"] == ["a!", "b!", "c!"]
    assert result.pipe_data == ["a!", "b!", "c!"]
    # per-item flat keys + the collect key.
    assert result.completed_step_results["1.for_each.0"] == "a!"
    assert result.completed_step_results["1.for_each.1"] == "b!"
    assert result.completed_step_results["1.for_each.2"] == "c!"
    assert result.completed_step_results["1.for_each.collect"] == ["a!", "b!", "c!"]


@pytest.mark.asyncio
async def test_for_each_items_source_and_pipe_data_at_fan_out_site():
    """Tier 2: a static ``items`` list is the source, and each item's ``do`` sees
    the fan-out step's OWN incoming pipe-data via bare ``pipe`` (Hard rule 5's
    per-item analog) held constant across items."""
    pipeline = Pipeline(steps=[
        TransformStep(value="'PFX'"),  # step 0: pipe = 'PFX' → each item sees it
        ForEachStep(
            items=["x", "y"],
            on_error="abort",
            do=TransformStep(value="pipe + '-' + item"),
            collect=TransformStep(value="pipe"),
        ),
    ])

    result = await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=lambda *_a, **_k: None,
        state_log=None, run_id="run-fe-items",
    )
    assert result.pipe_data == ["PFX-x", "PFX-y"]


@pytest.mark.asyncio
async def test_for_each_empty_list_runs_collect_once_over_empty():
    """Tier 2: an empty item list fans out nothing and runs ``collect`` ONCE over
    the empty list — the primitive still produces its N2 result (no items ≠ no
    collect)."""
    pipeline = Pipeline(steps=[
        ForEachStep(
            items=[],
            on_error="abort",
            do=TransformStep(value="item"),
            collect=TransformStep(value="count(pipe)"),
        ),
    ])
    result = await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=lambda *_a, **_k: None,
        state_log=None, run_id="run-fe-empty",
    )
    assert result.pipe_data == 0
    assert result.completed_step_results["0.for_each.collect"] == 0


# ── S5 guard (a): max_parallel bounds live concurrency ───────────────────────


@pytest.mark.asyncio
async def test_max_parallel_bounds_live_concurrency():
    """Tier 2: (S5 guard a — bounded-by-construction) with ``max_parallel=2`` over
    6 items, never more than 2 items' ``do`` are in flight at once. Proven by a
    real async tool tracking peak concurrency — the Semaphore is the bound, not a
    reject."""
    live = 0
    peak = 0

    async def _dispatch(name: str, args: dict) -> Any:
        nonlocal live, peak
        assert name == "work"
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)  # hold the slot so siblings would pile up if unbounded
        live -= 1
        return args["v"]

    pipeline = Pipeline(steps=[
        ForEachStep(
            items=[1, 2, 3, 4, 5, 6],
            max_parallel=2,
            on_error="abort",
            do=ToolStep(name="work", args={"v": ExprRef("item")}),
            collect=TransformStep(value="pipe"),
        ),
    ])
    result = await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=_dispatch, state_log=None, run_id="run-fe-parallel",
    )
    assert result.pipe_data == [1, 2, 3, 4, 5, 6]  # ordered, all processed
    assert peak <= 2, f"live concurrency {peak} exceeded max_parallel=2 (S5 guard a)"
    assert peak == 2, "with 6 items and a held slot, concurrency should reach the cap"


# ── on_error policies ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_error_continue_drops_failed_item_and_collect_sees_survivors():
    """Tier 2: ``on_error:continue`` DROPS a failed item from the results — its
    item key holds a kind-marker (so resume never re-runs it), and ``collect``'s
    input is the surviving ordered list, not a hole/None."""
    def _dispatch(name: str, args: dict) -> Any:
        v = args["v"]
        if v == "bad":
            raise RuntimeError("boom")
        return v.upper()

    pipeline = Pipeline(steps=[
        ForEachStep(
            items=["a", "bad", "c"],
            on_error="continue",
            do=ToolStep(name="work", args={"v": ExprRef("item")}),
            collect=TransformStep(value="pipe"),
        ),
    ])
    result = await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=_dispatch, state_log=None, run_id="run-fe-continue",
    )
    # collect saw only the survivors, in item order.
    assert result.pipe_data == ["A", "C"]
    # the dropped item's key holds the kind-marker (NOT absent, NOT bare None).
    dropped = result.completed_step_results["0.for_each.1"]
    assert dropped["__fan_out_dropped__"] is True
    assert "boom" in dropped["error"]


@pytest.mark.asyncio
async def test_on_error_abort_fails_the_whole_step():
    """Tier 2: ``on_error:abort`` — a single item failure fails the whole for_each
    step (``PipelineExecutionError``), not a silent drop."""
    def _dispatch(name: str, args: dict) -> Any:
        if args["v"] == "bad":
            raise RuntimeError("boom")
        return args["v"]

    pipeline = Pipeline(steps=[
        ForEachStep(
            items=["a", "bad", "c"],
            on_error="abort",
            do=ToolStep(name="work", args={"v": ExprRef("item")}),
            collect=TransformStep(value="pipe"),
        ),
    ])
    with pytest.raises(PipelineExecutionError):
        await PipelineExecutor().run(
            pipeline, None,
            tool_dispatch=_dispatch, state_log=None, run_id="run-fe-abort",
        )


@pytest.mark.asyncio
async def test_on_error_retry_reruns_flaky_item_until_success():
    """Tier 2: ``on_error:retry(2)`` re-runs a flaky item (fails twice, succeeds on
    the 3rd attempt) — the item lands, not dropped, so ``collect`` sees it."""
    attempts: dict[Any, int] = {}

    def _dispatch(name: str, args: dict) -> Any:
        v = args["v"]
        attempts[v] = attempts.get(v, 0) + 1
        if v == "flaky" and attempts[v] < 3:
            raise RuntimeError("transient")
        return v

    pipeline = Pipeline(steps=[
        ForEachStep(
            items=["ok", "flaky"],
            on_error="retry(2)",
            do=ToolStep(name="work", args={"v": ExprRef("item")}),
            collect=TransformStep(value="pipe"),
        ),
    ])
    result = await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=_dispatch, state_log=None, run_id="run-fe-retry-ok",
    )
    assert sorted(result.pipe_data) == ["flaky", "ok"]
    assert attempts["flaky"] == 3  # 1 initial + 2 retries


@pytest.mark.asyncio
async def test_on_error_retry_exhausted_falls_back_to_abort():
    """Tier 2: ``retry(1)`` on an ALWAYS-failing item exhausts its retries and then
    falls back to ABORT (only ``continue`` ever silently drops) — the step fails."""
    calls: dict[Any, int] = {}

    def _dispatch(name: str, args: dict) -> Any:
        v = args["v"]
        calls[v] = calls.get(v, 0) + 1
        if v == "always":
            raise RuntimeError("permanent")
        return v

    pipeline = Pipeline(steps=[
        ForEachStep(
            items=["always"],
            on_error="retry(1)",
            do=ToolStep(name="work", args={"v": ExprRef("item")}),
            collect=TransformStep(value="pipe"),
        ),
    ])
    with pytest.raises(PipelineExecutionError):
        await PipelineExecutor().run(
            pipeline, None,
            tool_dispatch=_dispatch, state_log=None, run_id="run-fe-retry-exhaust",
        )
    assert calls["always"] == 2  # 1 initial + 1 retry, then abort


# ── CLAUDE.md truncate-falsify recovery gate (single-step do = exactly-once) ──


def _append_dispatch(state_log: StateLog, out_file: Path, crash: set):
    """A REAL side-effecting tool (mirrors the call primitive's ``is2_append``):
    each call appends a line to ``out_file`` AND a WAL entry (so R4 gens land at
    distinct durable seqs). The exactly-once probe is the FILE. A line in
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
    """for_each over 4 items (append each), collect appends a COLLECT marker line."""
    return Pipeline(steps=[
        ForEachStep(
            items=["A", "B", "C", "D"],
            on_error=on_error,
            do=ToolStep(name="append", args={"line": ExprRef("item")}),
            collect=ToolStep(name="append", args={"line": "COLLECT"}),
        ),
    ])


@pytest.mark.asyncio
async def test_truncate_falsify_mid_fan_out_replays_items_exactly_once(tmp_path: Path):
    """Tier 2: MANDATORY CLAUDE.md recovery gate for the fan-out substrate over a
    SINGLE-STEP ``do`` (the exactly-once item-commit case). Phase 1: items A/B/D
    write + record their item keys; item C fails (``on_error:continue`` → dropped
    marker recorded); ``collect`` then crashes BEFORE recording its key. The WAL is
    truncated BELOW all source events. Resume from the generation FILE →

      - the completed items REPLAY exactly-once (A/B/D are NOT re-written — the
        file proves it),
      - the dropped item C STAYS dropped (NOT re-run — its marker key is present),
      - ``collect`` runs ONCE (writes COLLECT), threading its result out.

    RED if an item rode a truncatable WAL event, if resume re-ran the whole
    fan-out, or if the dropped item was re-run as an absent-keyed pending item."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"
    # C fails (→ dropped); COLLECT crashes phase 1 AFTER all items are recorded.
    crash = {"C", "COLLECT"}
    dispatch = _append_dispatch(state_log, out_file, crash)

    pipeline = _fan_out_pipeline("continue")

    # The collect tool raises a raw RuntimeError (tool-dispatch failures are not
    # wrapped) — the same crash shape the call primitive's truncate-falsify uses.
    with pytest.raises(RuntimeError):
        await PipelineExecutor().run(
            pipeline, None,
            tool_dispatch=dispatch, state_log=state_log, run_id="run-fe-tf",
        )
    await state_log.flush()
    # A/B/D written (C dropped, COLLECT crashed) — order is completion-order, so sort.
    assert sorted(out_file.read_text(encoding="utf-8").splitlines()) == ["A", "B", "D"]

    # Latest generation on disk: all 4 item keys present, collect key absent.
    from reyn.core.events.pipeline_recovery import latest_pipeline_state
    snap = latest_pipeline_state("run-fe-tf", state_log)
    for idx in range(4):
        assert f"0.for_each.{idx}" in snap["completed_step_results"]
    assert snap["completed_step_results"]["0.for_each.2"]["__fan_out_dropped__"] is True
    assert "0.for_each.collect" not in snap["completed_step_results"]

    # WAL head climbs past the crash-state seqs, then GC truncates below 40 — every
    # item's own WAL entry is dropped from wal.jsonl.
    for i in range(50):
        await state_log.append("inbox_put", n=100 + i)
    await state_log.truncate_below(40)
    await state_log.flush()
    surviving = {e["seq"] for e in state_log.iter_from(0)}
    assert all(s >= 40 for s in surviving), "the items' WAL entries are truncated away"

    # RESUME with COLLECT disarmed (C stays armed — it must NOT be re-run anyway).
    crash.discard("COLLECT")
    resumed = await PipelineExecutor().resume(
        "run-fe-tf", pipeline=pipeline,
        tool_dispatch=dispatch, state_log=state_log,
    )

    lines = out_file.read_text(encoding="utf-8").splitlines()
    # Exactly-once: A/B/D appear ONCE each (replayed, not re-written); C NEVER
    # (stayed dropped, not re-run despite still being armed); COLLECT once.
    assert sorted(lines) == ["A", "B", "COLLECT", "D"]
    assert lines.count("A") == 1 and lines.count("B") == 1 and lines.count("D") == 1
    assert "C" not in lines
    # collect ran once; its result threads out as the for_each N2 return.
    assert resumed.pipe_data == {"wrote": "COLLECT"}
    assert resumed.completed_step_results["0.for_each.collect"] == {"wrote": "COLLECT"}


@pytest.mark.asyncio
async def test_resume_after_full_fan_out_replays_with_zero_new_side_effects(tmp_path: Path):
    """Tier 2: resuming a fully-completed for_each replays every item + collect from
    the snapshot with ZERO new writes. RED if resume re-fired a completed item or
    the collect."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"
    dispatch = _append_dispatch(state_log, out_file, set())
    pipeline = _fan_out_pipeline("abort")

    await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=dispatch, state_log=state_log, run_id="run-fe-done",
    )
    await state_log.flush()
    before = sorted(out_file.read_text(encoding="utf-8").splitlines())
    assert before == ["A", "B", "C", "COLLECT", "D"]

    resumed = await PipelineExecutor().resume(
        "run-fe-done", pipeline=pipeline,
        tool_dispatch=dispatch, state_log=state_log,
    )
    after = sorted(out_file.read_text(encoding="utf-8").splitlines())
    assert after == before, "a fully-completed fan-out must replay with zero side effects"
    assert resumed.pipe_data == {"wrote": "COLLECT"}


# ── the DOCUMENTED compositional-do atomic-re-run gap (commit unit = the ITEM) ─


@pytest.mark.asyncio
async def test_compositional_do_item_reruns_atomically_on_crash(tmp_path: Path):
    """Tier 2: DOCUMENTS the known fan-out recovery gap (module docstring's "commit
    unit = the ITEM"). A ``call``-``do`` item crashed MID-ITEM (its callee wrote
    sub-step 0's side effect, sub-step 1 failed) re-runs ATOMICALLY on resume —
    because item tasks record through a NO-OP recorder, the item's internal
    progress is never journaled, so the completed internal side effect RE-FIRES.
    This is a TESTED contract, not a silent surprise; a follow-up issue tracks
    closing it (per-internal-sub-step durability). A single-step ``do`` has no such
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
        ForEachStep(
            items=["X"],
            on_error="abort",
            do=CallStep(pipeline="worker"),
            collect=TransformStep(value="pipe"),
        ),
    ])

    with pytest.raises(PipelineExecutionError):
        await PipelineExecutor().run(
            pipeline, None,
            tool_dispatch=dispatch, state_log=state_log, run_id="run-fe-comp",
            pipeline_registry=registry,
        )
    await state_log.flush()
    assert out_file.read_text(encoding="utf-8").splitlines() == ["X-a"]

    # RESUME disarmed: the item re-runs WHOLE (its key was never recorded), so X-a
    # is written AGAIN (the documented internal-side-effect re-fire).
    crash.clear()
    await PipelineExecutor().resume(
        "run-fe-comp", pipeline=pipeline,
        tool_dispatch=dispatch, state_log=state_log, pipeline_registry=registry,
    )
    lines = out_file.read_text(encoding="utf-8").splitlines()
    assert lines == ["X-a", "X-a", "X-b"], (
        "a compositional-do item re-runs atomically on resume — X-a re-fires "
        "(the documented commit-unit=ITEM gap; single-step do has no such gap)"
    )


# ── S5 guard (b): fan_out_depth cap FAILS the step (does not spawn) ───────────


@pytest.mark.asyncio
async def test_fan_out_depth_cap_fails_a_too_deeply_nested_for_each():
    """Tier 2: (S5 guard b — bounded-by-construction) a ``for_each`` nested deeper
    than ``max_fan_out_depth`` FAILS the step rather than spawning. A top-level
    for_each = depth 1; a for_each inside its ``do`` = depth 2. With
    ``max_fan_out_depth=1`` the inner one exceeds the cap and fails."""
    inner = ForEachStep(
        items=[1],
        on_error="abort",
        do=TransformStep(value="item"),
        collect=TransformStep(value="pipe"),
    )
    outer = Pipeline(steps=[
        ForEachStep(
            items=[1],
            on_error="abort",  # so the inner depth-failure propagates as a step failure
            do=inner,
            collect=TransformStep(value="pipe"),
        ),
    ])
    with pytest.raises(PipelineExecutionError) as exc:
        await PipelineExecutor().run(
            outer, None,
            tool_dispatch=lambda *_a, **_k: None,
            state_log=None, run_id="run-fe-depth",
            max_fan_out_depth=1,
        )
    assert "depth" in str(exc.value)


@pytest.mark.asyncio
async def test_nested_for_each_within_depth_cap_succeeds():
    """Tier 2: the depth guard is a CAP, not a blanket ban — a for_each nested to
    depth 2 runs fine under ``max_fan_out_depth=2`` (bounded, not forbidden)."""
    outer = Pipeline(steps=[
        ForEachStep(
            items=[[1, 2], [3]],
            on_error="abort",
            do=ForEachStep(
                over="item",
                on_error="abort",
                do=TransformStep(value="item"),
                collect=TransformStep(value="count(pipe)"),
            ),
            collect=TransformStep(value="pipe"),
        ),
    ])
    result = await PipelineExecutor().run(
        outer, None,
        tool_dispatch=lambda *_a, **_k: None,
        state_log=None, run_id="run-fe-depth-ok",
        max_fan_out_depth=2,
    )
    assert result.pipe_data == [2, 1]  # inner collect = count of each sublist


# ── S5 guard (c): per-run spawn budget ───────────────────────────────────────


def test_spawn_budget_consume_rejects_past_cap():
    """Tier 1: the ``SpawnBudget`` contract (S5 guard c mechanism) — ``consume``
    charges monotonically and raises once the cap is reached; ``cap=0`` is
    unlimited."""
    budget = SpawnBudget(2)
    budget.consume(label="0")
    budget.consume(label="1")
    assert budget.spent == 2
    with pytest.raises(PipelineExecutionError):
        budget.consume(label="2")

    unlimited = SpawnBudget(0)
    for i in range(100):
        unlimited.consume(label=str(i))
    assert unlimited.spent == 100


class _ScriptedAgentReply:
    """Always answers with one fixed plain-text turn (no tool_calls) — the ONLY
    faked collaborator (see module docstring), injected via the real
    ``_loop_observer`` seam exactly as ``test_pipeline_r5_agent_step_executor``."""

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

    def _factory(profile) -> Session:
        s = Session(
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
async def test_spawn_budget_cap_fails_a_fan_out_over_too_many_agent_items(tmp_path: Path):
    """Tier 2: (S5 guard c — bounded-by-construction, real spawn path) a for_each
    whose ``do`` is an ``agent`` step, fanned out over 3 items with
    ``max_pipeline_spawns=2``, FAILS the step — the 3rd item's spawn exceeds the
    per-run budget (the ONLY spawn-count enforcement for lineage-less pipeline
    agent-steps). ``max_parallel=1`` makes the budget exhaustion deterministic."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    scripted = _ScriptedAgentReply("done")
    reg = _agent_registry(tmp_path, state_log, scripted)

    pipeline = Pipeline(steps=[
        ForEachStep(
            items=["a", "b", "c"],
            max_parallel=1,
            on_error="abort",  # so the budget-exceed failure fails the step
            do=AgentStep(prompt="{item}", identity="worker"),
            collect=TransformStep(value="pipe"),
        ),
    ])
    with pytest.raises(PipelineExecutionError) as exc:
        await PipelineExecutor().run(
            pipeline, None,
            tool_dispatch=lambda *_a, **_k: None,
            state_log=state_log, run_id="run-fe-spawns",
            registry=reg, max_pipeline_spawns=2,
        )
    assert "spawn cap" in str(exc.value)
    assert scripted.calls == 2, "exactly the 2 budgeted spawns ran before the cap failed the 3rd"


@pytest.mark.asyncio
async def test_fan_out_of_agent_items_within_spawn_cap_succeeds(tmp_path: Path):
    """Tier 2: within ``max_pipeline_spawns``, an agent fan-out runs every item and
    collects — the budget is a cap, not a ban (bounded, not forbidden)."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    scripted = _ScriptedAgentReply("ok")
    reg = _agent_registry(tmp_path, state_log, scripted)

    pipeline = Pipeline(steps=[
        ForEachStep(
            items=["a", "b", "c"],
            max_parallel=1,
            on_error="abort",
            do=AgentStep(prompt="{item}", identity="worker"),
            collect=TransformStep(value="count(pipe)"),
        ),
    ])
    result = await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=lambda *_a, **_k: None,
        state_log=state_log, run_id="run-fe-spawns-ok",
        registry=reg, max_pipeline_spawns=5,
    )
    assert result.pipe_data == 3
    assert scripted.calls == 3
