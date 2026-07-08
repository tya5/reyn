"""Tier 2: OS invariant — the `fold` compositional primitive (sequential
accumulator) + its dotted-path R4 recovery.

Covers the second consumer of the non-linear foundation (Appendix B ``fold =
{over?:PATH | items?:[LIT*] init:EXPR do:Step output:NAME max_items?}`` +
Hard rule 9 finiteness):

  1. happy path — `fold` walks a list IN ORDER, threading `acc` through `do`
     (a transform computing `acc + item`), `init` seeds the first `acc`, and
     the FINAL `acc` becomes the fold's return value + named output.
  2. `items:` (static literal source) is honored the same way `over:` is.
  3. an empty list leaves `acc` == `init`, unchanged.
  4. `max_items` caps the walk to the first N elements of a longer source.
  5. context injection — `do` sees bare `{item}`/`{acc}` (a transform `value`
     reads them as bare names; an agent-style prompt interpolation would too,
     though this exercises it via `transform`, the deterministic leaf).
  6. item failure fails the WHOLE fold (propagates, never swallowed).
  7. the CLAUDE.md-mandated truncate-falsify recovery gate: crash mid-fold
     (some iterations done), truncate the WAL below the source events, resume
     from the generation FILE, and prove the completed iterations REPLAY
     exactly-once (a `do` side effect does NOT re-fire for a finished
     iteration) while `acc` rebuilds correctly and only the remaining
     iterations execute once.

Real `StateLog` + real generation files throughout; the recovery tests build
the executor directly (the same discipline as `call`'s truncate-falsify test)
— no mocks, no private-state assertions.
"""
from __future__ import annotations

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import (
    FoldStep,
    Pipeline,
    PipelineExecutionError,
    PipelineExecutor,
    ToolStep,
    TransformStep,
)

# ── happy path: accumulation, over/items sources, empty list, max_items ─────


@pytest.mark.asyncio
async def test_fold_accumulates_sequentially_via_over():
    """Tier 2: `fold` walks `ctx.items` in order, threading `acc` through a
    transform `do` (`acc + item`); `init` seeds the first `acc`, and the FINAL
    `acc` threads out as the fold's return value + named output."""
    outer = Pipeline(steps=[
        FoldStep(
            init="0", do=TransformStep(value="acc + item"), output="total",
            over="ctx.numbers",
        ),
    ])
    result = await PipelineExecutor().run(
        outer, {"numbers": [1, 2, 3, 4]},
        tool_dispatch=lambda *_a, **_k: None,
        state_log=None, run_id="run-fold-over",
    )
    assert result.named_stores["total"] == 10
    assert result.pipe_data == 10
    assert result.completed_step_results["0.fold.0"] == 1
    assert result.completed_step_results["0.fold.1"] == 3
    assert result.completed_step_results["0.fold.2"] == 6
    assert result.completed_step_results["0.fold.3"] == 10
    assert result.completed_step_results["0"] == 10


@pytest.mark.asyncio
async def test_fold_accumulates_via_static_items_source():
    """Tier 2: `items:` (a static literal list) is an equally valid list
    source to `over:` — same accumulation semantics."""
    outer = Pipeline(steps=[
        FoldStep(
            init="''", do=TransformStep(value="acc + item"), output="joined",
            items=["a", "b", "c"],
        ),
    ])
    result = await PipelineExecutor().run(
        outer, None,
        tool_dispatch=lambda *_a, **_k: None,
        state_log=None, run_id="run-fold-items",
    )
    assert result.named_stores["joined"] == "abc"


@pytest.mark.asyncio
async def test_fold_empty_list_leaves_init_unchanged():
    """Tier 2: an empty list source means `do` never runs — the final `acc`
    is exactly `init`, and no `fold.{k}` dotted keys are recorded."""
    outer = Pipeline(steps=[
        FoldStep(
            init="42", do=TransformStep(value="acc + item"), output="total",
            items=[],
        ),
    ])
    result = await PipelineExecutor().run(
        outer, None,
        tool_dispatch=lambda *_a, **_k: None,
        state_log=None, run_id="run-fold-empty",
    )
    assert result.named_stores["total"] == 42
    assert not any(k.startswith("0.fold.") for k in result.completed_step_results)


@pytest.mark.asyncio
async def test_fold_max_items_caps_the_walk():
    """Tier 2: `max_items` truncates a longer source to its first N elements
    (Hard rule 9 — always finite) rather than erroring on a longer list."""
    outer = Pipeline(steps=[
        FoldStep(
            init="0", do=TransformStep(value="acc + item"), output="total",
            items=[1, 2, 3, 4, 5], max_items=2,
        ),
    ])
    result = await PipelineExecutor().run(
        outer, None,
        tool_dispatch=lambda *_a, **_k: None,
        state_log=None, run_id="run-fold-cap",
    )
    assert result.named_stores["total"] == 3  # 1 + 2, item 3/4/5 never run
    assert "0.fold.2" not in result.completed_step_results


@pytest.mark.asyncio
async def test_fold_item_failure_fails_the_whole_fold():
    """Tier 2: a `do` step failure (an unresolvable expression) propagates as
    a `PipelineExecutionError` out of the fold — never silently dropped."""
    outer = Pipeline(steps=[
        FoldStep(
            init="0", do=TransformStep(value="acc + ctx.missing"), output="total",
            items=[1, 2],
        ),
    ])
    with pytest.raises(PipelineExecutionError):
        await PipelineExecutor().run(
            outer, None,
            tool_dispatch=lambda *_a, **_k: None,
            state_log=None, run_id="run-fold-fail",
        )


# ── dotted-path R4 recovery: the CLAUDE.md truncate-falsify gate ────────────


def _append_dispatch(state_log: StateLog, out_file, crash):
    """A REAL side-effecting tool (mirrors the `call` truncate-falsify test's
    own fixture): each call appends `args["line"]` to `out_file` (the
    exactly-once probe — a re-fired step leaves a duplicate line) AND a WAL
    entry, so R4 gens land at distinct durable seqs. `crash["line"]` arms a
    mid-execution failure BEFORE that line's side effect. The tool RETURNS
    `args["acc"] + args["line"]` — a `do` that passes `{acc}`/`{item}` as
    `acc`/`line` args threads the running concatenation the same way a
    transform `do`'s `acc + item` would, so this doubles as the fold's `do`
    for the recovery gate."""

    async def _dispatch(name: str, args: dict):
        assert name == "append"
        line = str(args["line"])
        if crash.get("line") == line:
            raise RuntimeError(f"simulated crash before writing {line!r}")
        with out_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        await state_log.append("inbox_put", note=line)
        # #2425 PR-2: a prior ToolStep iteration's ctx value is the flat
        # {"text": ...} shape (this dispatch's own str return), not a bare str.
        acc = args["acc"]
        acc_str = acc["text"] if isinstance(acc, dict) else str(acc)
        return acc_str + line

    return _dispatch


@pytest.mark.asyncio
async def test_truncate_falsify_fold_resumes_completed_iterations_exactly_once(tmp_path):
    """Tier 2: MANDATORY CLAUDE.md recovery gate for the `fold` primitive. A run
    crashes MID-FOLD (iteration 0 wrote 'A' + recorded its R4 gen + dotted key
    `0.fold.0`; iteration 1 failed before writing 'B'). The WAL is then
    truncated BELOW those source events. `resume` must reconstruct from the
    generation FILE, REPLAY the finished iteration exactly-once ('A' is NOT
    written again — the file proves it), rebuild `acc` from the replayed
    result, execute ONLY the remaining iteration (writes 'B' once), and
    complete with the correct final `acc`. RED if an iteration's side effect
    rode a truncatable WAL event, or if resume re-ran the whole fold instead of
    replaying the completed iteration."""
    from reyn.core.events.pipeline_recovery import latest_pipeline_state
    from reyn.core.pipeline.executor import ExprRef

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"
    crash = {"line": "B"}
    dispatch = _append_dispatch(state_log, out_file, crash)

    outer = Pipeline(steps=[
        FoldStep(
            init="''",
            do=ToolStep(name="append", args={"line": ExprRef("item"), "acc": ExprRef("acc")}),
            output="joined",
            items=["A", "B"],
        ),
    ])

    # CRASH STATE: iteration 0 writes A + records its gen at a real WAL seq;
    # iteration 1 raises before writing B.
    with pytest.raises(RuntimeError):
        await PipelineExecutor().run(
            outer, None,
            tool_dispatch=dispatch, state_log=state_log,
            run_id="run-fold-tf",
        )
    await state_log.flush()
    assert out_file.read_text(encoding="utf-8").splitlines() == ["A"]

    snap_mid = latest_pipeline_state("run-fold-tf", state_log)
    # #2425 PR-2: a ToolStep `do`'s ctx value is the flat {"text": ...} shape.
    assert snap_mid["completed_step_results"]["0.fold.0"] == {"text": "A"}
    assert "0.fold.1" not in snap_mid["completed_step_results"]
    assert "0" not in snap_mid["completed_step_results"]
    assert snap_mid["step_index"] == 0  # the fold step itself is not done

    # The WAL head climbs past the crash-state seqs, then GC truncates below
    # 40 — the finished iteration's own WAL entry is dropped from wal.jsonl.
    for i in range(50):
        await state_log.append("inbox_put", n=100 + i)
    await state_log.truncate_below(40)
    await state_log.flush()
    surviving = {e["seq"] for e in state_log.iter_from(0)}
    assert all(s >= 40 for s in surviving), "iteration 0's WAL entry is truncated away"

    # RESUME with the crash DISARMED: iteration 0 replays from the gen FILE
    # (no re-write of A), only iteration 1 executes (writes B once).
    crash.clear()
    resumed = await PipelineExecutor().resume(
        "run-fold-tf", pipeline=outer,
        tool_dispatch=dispatch, state_log=state_log,
    )

    # Exactly-once: A appears ONCE (replayed, not re-executed); B once (resumed).
    assert out_file.read_text(encoding="utf-8").splitlines() == ["A", "B"]
    assert resumed.completed_step_results["0.fold.0"] == {"text": "A"}
    # iteration 1's `do` receives the REPLAYED acc ("A") + item "B" == "AB" —
    # proof the accumulator was correctly rebuilt from the replayed result
    # before the remaining iteration ran, not re-derived from scratch.
    assert resumed.completed_step_results["0.fold.1"] == {"text": "AB"}
    assert resumed.named_stores["joined"] == {"text": "AB"}
    assert resumed.pipe_data == {"text": "AB"}
    assert resumed.completed_step_results["0"] == {"text": "AB"}


@pytest.mark.asyncio
async def test_fold_resume_after_full_run_replays_with_zero_new_side_effects(tmp_path):
    """Tier 2: resuming a run whose fold already completed replays every
    iteration from the snapshot (zero new writes) and re-threads the final
    `acc`. RED if resume re-ran a completed iteration's side effect."""
    from reyn.core.pipeline.executor import ExprRef

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"
    dispatch = _append_dispatch(state_log, out_file, {})

    outer = Pipeline(steps=[
        FoldStep(
            init="''",
            do=ToolStep(name="append", args={"line": ExprRef("item"), "acc": ExprRef("acc")}),
            output="joined",
            items=["A", "B"],
        ),
    ])

    await PipelineExecutor().run(
        outer, None,
        tool_dispatch=dispatch, state_log=state_log,
        run_id="run-fold-done",
    )
    await state_log.flush()
    assert out_file.read_text(encoding="utf-8").splitlines() == ["A", "B"]

    resumed = await PipelineExecutor().resume(
        "run-fold-done", pipeline=outer,
        tool_dispatch=dispatch, state_log=state_log,
    )
    assert out_file.read_text(encoding="utf-8").splitlines() == ["A", "B"]
    assert resumed.named_stores["joined"] == {"text": "AB"}


# ── context injection: {item}/{acc} available to `do` ───────────────────────


@pytest.mark.asyncio
async def test_do_context_binds_item_and_acc_in_the_right_order():
    """Tier 2: `do` resolves `{item}`/`{acc}` as DISTINCT bare top-level
    context names, bound in the documented order (never swapped) — proven with
    an order-sensitive op (`acc - item`, not the commutative `acc + item`
    the other happy-path tests use): 100, then 100-1=99, 99-2=97, 97-3=94.
    A swapped binding (`item - acc`) would yield a different, easily
    distinguishable result."""
    outer = Pipeline(steps=[
        FoldStep(
            init="100", do=TransformStep(value="acc - item"), output="total",
            items=[1, 2, 3],
        ),
    ])
    result = await PipelineExecutor().run(
        outer, None,
        tool_dispatch=lambda *_a, **_k: None,
        state_log=None, run_id="run-fold-order",
    )
    assert result.named_stores["total"] == 94


@pytest.mark.asyncio
async def test_do_output_grows_local_ctx_across_iterations_not_outer_scope():
    """Tier 2: a `do` with `output: seen` writes into fold's LOCAL ctx copy,
    readable by a LATER iteration's `do` via `ctx.seen` (proven: iteration 1's
    `do` adds `ctx.seen` — iteration 0's stashed running total — on top of the
    plain `acc + item` sum) — but that name never reaches the outer pipeline's
    named stores (only `fold.output` does; `get(ctx, 'seen', 'ABSENT')` outside
    the fold resolves to the default)."""
    outer = Pipeline(steps=[
        FoldStep(
            init="0",
            do=TransformStep(value="acc + item + get(ctx, 'seen', 0)", output="seen"),
            output="total",
            items=[1, 2, 3],
        ),
        # A later top-level step: `seen` must NOT be visible outside the fold.
        TransformStep(value="get(ctx, 'seen', 'ABSENT')", output="leaked"),
    ])
    result = await PipelineExecutor().run(
        outer, None,
        tool_dispatch=lambda *_a, **_k: None,
        state_log=None, run_id="run-fold-local-ctx",
    )
    # iter0: acc=0+1+get(seen,0)=1 -> seen=1
    # iter1: acc=1+2+get(seen,0)=1+2+1=4 -> seen=4
    # iter2: acc=4+3+get(seen,0)=4+3+4=11 -> seen=11
    assert result.named_stores["total"] == 11
    assert result.named_stores["leaked"] == "ABSENT"
