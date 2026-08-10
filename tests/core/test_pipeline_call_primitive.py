"""Tier 2: OS invariant — the `call` compositional primitive + dotted-path R7 recovery.

Covers the non-linear foundation's first consumer
(``docs/proposals/reyn-pipeline-improvements-proposal.md`` N2 + Appendix B ``call``,
built on the ``_run_scope`` + dotted-path recovery + dispatch-table foundation):

  1. happy path — a ``call`` runs its REGISTERED callee synchronously, ``pass:[...]``
     (a NAME -> R1-EXPRESSION mapping, e.g. ``[("brief", "ctx.brief")]``) projects
     ONLY the listed callee names into the callee, the callee's first step
     receives the caller's pipe-data at the call site (Hard rule 5), and the callee's
     FINAL output threads out as the ``call`` step's N2 return value.
  2. ``pass:[...]`` isolation — a callee referencing a caller store NOT in ``pass``
     fails the step (structural, not silent-None).
  3. callee failure fails the caller (the sub-scope's ``PipelineExecutionError``
     propagates out of the ``call`` step unchanged).
  4. dotted-path R4 keys — a mid-``call`` snapshot keys the callee's sub-steps under
     ``f"{i}.call.{j}"`` while keeping the OUTER ``named_stores`` free of callee
     leakage (``pass:[...]`` isolation on the recovery axis).
  5. the CLAUDE.md-mandated truncate-falsify recovery gate: crash mid-callee, truncate
     the WAL below the source events, resume from the generation FILE, and prove the
     completed callee sub-steps REPLAY exactly-once (a callee side-effect does not
     re-fire) while only the remaining callee step executes.
  6. an unregistered ``call`` target fails cleanly.

Real ``StateLog`` + ``PipelineRegistry`` + real generation files throughout; the
recovery tests build the executor directly (the same discipline as IS-2's
truncate-falsify) — no mocks, no private-state assertions.
"""
from __future__ import annotations

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import (
    CallStep,
    ExprRef,
    Pipeline,
    PipelineExecutionError,
    PipelineExecutor,
    ToolStep,
    TransformStep,
)
from reyn.core.pipeline.registry import PipelineRegistry

# ── happy path: callee runs, pass:[...] scoping, pipe-data-at-call-site, N2 out ──


@pytest.mark.asyncio
async def test_call_runs_callee_and_threads_final_output_with_pass_scoping():
    """Tier 2: a ``call`` runs its callee synchronously; ``pass:[brief]`` projects
    ONLY ``brief`` into the callee (``ctx.brief`` resolves; the caller's other store
    is invisible), the callee's first step sees the caller's pipe-data via bare
    ``pipe`` (Hard rule 5), and the callee's final output becomes the ``call`` step's
    return value + named output."""
    registry = PipelineRegistry()
    registry.register(
        "summarize",
        Pipeline(steps=[
            # first callee step: bare `pipe` == the caller's pipe-data at the call site
            TransformStep(value="pipe + '-bumped'", output="bumped"),
            # `ctx.brief` reaches the passed store; the caller's `secret` is NOT here
            TransformStep(value="ctx.brief + '::' + ctx.bumped", output="summary"),
        ]),
    )

    outer = Pipeline(steps=[
        TransformStep(value="ctx.n", output="carried"),      # step 0: pipe_data = "seven"
        CallStep(pipeline="summarize", pass_=[("brief", "ctx.brief")], output="called_out"),  # step 1
        TransformStep(value="ctx.called_out + '!'", output="final"),           # step 2
    ])

    result = await PipelineExecutor().run(
        outer,
        {"n": "seven", "brief": "hi", "secret": "nope"},
        tool_dispatch=lambda *_a, **_k: None,
        state_log=None,
        run_id="run-call-happy",
        pipeline_registry=registry,
    )

    # callee: bumped = "seven-bumped" ; summary = "hi::seven-bumped" ; threads out.
    assert result.named_stores["called_out"] == "hi::seven-bumped"
    assert result.named_stores["final"] == "hi::seven-bumped!"
    assert result.pipe_data == "hi::seven-bumped!"
    # Outer flat key for the call's N2 result; callee sub-steps under the dotted scope.
    assert result.completed_step_results["1"] == "hi::seven-bumped"
    assert result.completed_step_results["1.call.0"] == "seven-bumped"
    assert result.completed_step_results["1.call.1"] == "hi::seven-bumped"
    # pass:[...] isolation — the callee's local `bumped`/`summary` never leaked into
    # the OUTER named stores (only the declared `output` did).
    assert "bumped" not in result.named_stores
    assert "summary" not in result.named_stores


@pytest.mark.asyncio
async def test_call_pass_isolation_denies_unpassed_store():
    """Tier 2: a callee that references a caller store NOT in ``pass:[...]`` fails the
    step — the isolation is structural (a Key-projected context), not a silent None."""
    registry = PipelineRegistry()
    registry.register(
        "leaky",
        Pipeline(steps=[TransformStep(value="ctx.secret", output="x")]),
    )
    outer = Pipeline(steps=[CallStep(pipeline="leaky", pass_=[], output="o")])

    with pytest.raises(PipelineExecutionError):
        await PipelineExecutor().run(
            outer, {"secret": "s"},
            tool_dispatch=lambda *_a, **_k: None, state_log=None,
            run_id="run-iso", pipeline_registry=registry,
        )


@pytest.mark.asyncio
async def test_callee_failure_fails_the_call_step():
    """Tier 2: a callee step failure propagates out of the ``call`` as a caller
    step failure (Hard rule 5) — never silently swallowed."""
    registry = PipelineRegistry()
    registry.register(
        "boom",
        Pipeline(steps=[TransformStep(value="ctx.missing_field", output="x")]),
    )
    outer = Pipeline(steps=[CallStep(pipeline="boom", pass_=[], output="o")])

    with pytest.raises(PipelineExecutionError):
        await PipelineExecutor().run(
            outer, None,
            tool_dispatch=lambda *_a, **_k: None, state_log=None,
            run_id="run-boom", pipeline_registry=registry,
        )


@pytest.mark.asyncio
async def test_unregistered_call_target_fails_cleanly():
    """Tier 2: a ``call`` to an unregistered pipeline name fails the step with a
    clear error, not an unhandled KeyError."""
    outer = Pipeline(steps=[CallStep(pipeline="nope", pass_=[], output="o")])
    with pytest.raises(PipelineExecutionError) as exc:
        await PipelineExecutor().run(
            outer, None,
            tool_dispatch=lambda *_a, **_k: None, state_log=None,
            run_id="run-missing", pipeline_registry=PipelineRegistry(),
        )
    assert "not registered" in str(exc.value)


# ── dotted-path R4 recovery: the CLAUDE.md truncate-falsify gate ─────────────────


def _append_dispatch(state_log: StateLog, out_file, crash):
    """A REAL side-effecting tool (mirrors IS-2's ``is2_append``): each call appends
    a line to ``out_file`` AND a WAL entry (so R4 gens land at distinct durable
    seqs). The exactly-once probe is the FILE — a re-fired step leaves a duplicate
    line. ``crash["line"]`` arms a mid-execution failure BEFORE that line's side
    effect, so a snapshot with the prior sub-step recorded but this one absent is a
    genuine mid-callee crash."""

    async def _dispatch(name: str, args: dict):
        assert name == "append"
        line = str(args["line"])
        if crash.get("line") == line:
            raise RuntimeError(f"simulated crash before writing {line!r}")
        with out_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        await state_log.append("inbox_put", note=line)
        return {"wrote": line}

    return _dispatch


def _two_write_callee() -> Pipeline:
    """A 2-step callee that writes ``A`` then ``B`` — both side-effecting, so a crash
    between them leaves exactly ``A`` on disk."""
    return Pipeline(steps=[
        ToolStep(name="append", args={"line": "A"}, output="a"),
        ToolStep(name="append", args={"line": "B"}, output="b"),
    ])


def _outer_calling(callee_name: str) -> Pipeline:
    return Pipeline(steps=[
        TransformStep(value="ctx.seed", output="carried"),
        CallStep(pipeline=callee_name, pass_=[("seed", "ctx.seed")], output="call_out"),
    ])


@pytest.mark.asyncio
async def test_mid_call_snapshot_uses_dotted_keys_and_isolates_outer_stores(tmp_path):
    """Tier 2: a genuine MID-CALLEE crash (sub-step 0 recorded, sub-step 1 failed)
    records the finished sub-step under ``f"{i}.call.0"`` and keeps ``step_index`` at
    the OUTER ``call`` index (the call is not done) — and the callee's local ``a``
    store does NOT appear in the persisted OUTER ``named_stores`` (pass:[...]
    isolation on the recovery axis)."""
    from reyn.core.events.pipeline_recovery import latest_pipeline_state

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"
    crash = {"line": "B"}  # sub-step 1 (writes B) fails mid-callee
    dispatch = _append_dispatch(state_log, out_file, crash)

    registry = PipelineRegistry()
    registry.register("callee", _two_write_callee())
    with pytest.raises(RuntimeError):
        await PipelineExecutor().run(
            _outer_calling("callee"), {"seed": 5},
            tool_dispatch=dispatch, state_log=state_log,
            run_id="run-dotted", pipeline_registry=registry,
        )
    await state_log.flush()

    snap = latest_pipeline_state("run-dotted", state_log)
    # The finished callee sub-step is present under the dotted key; the failed one
    # and the outer call's own key are absent (the call is not done).
    assert "1.call.0" in snap["completed_step_results"]
    assert "1.call.1" not in snap["completed_step_results"]
    assert "1" not in snap["completed_step_results"]
    # step_index stayed at the outer call index — the callee's progress lives in the
    # dotted keys, not the outer cursor.
    assert snap["step_index"] == 1
    # pass:[...] isolation: the callee's local `a` never leaked into OUTER stores.
    assert "a" not in snap["named_stores"]
    assert snap["named_stores"].get("carried") == 5
    # exactly one line on disk pre-crash.
    assert out_file.read_text(encoding="utf-8").splitlines() == ["A"]


@pytest.mark.asyncio
async def test_truncate_falsify_call_resumes_callee_substeps_exactly_once(tmp_path):
    """Tier 2: MANDATORY CLAUDE.md recovery gate for the `call` primitive. A run
    crashes MID-CALLEE (callee sub-step 0 wrote ``A`` + recorded its R4 gen + dotted
    key; sub-step 1 failed before writing ``B``). The WAL is then truncated BELOW
    those source events. ``resume`` must reconstruct from the generation FILE, REPLAY
    the finished sub-step exactly-once (``A`` is NOT written again — the file proves
    it), execute ONLY the remaining sub-step (writes ``B`` once), and complete —
    threading the callee's final output out. RED if a callee sub-step rode a
    truncatable WAL event, or if resume re-ran the whole ``call`` instead of
    replaying the completed sub-steps."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"
    crash = {"line": "B"}
    dispatch = _append_dispatch(state_log, out_file, crash)

    registry = PipelineRegistry()
    registry.register("callee", _two_write_callee())
    outer = _outer_calling("callee")

    # CRASH STATE: sub-step 0 writes A + records its gen at a real WAL seq; sub-step
    # 1 raises before writing B.
    with pytest.raises(RuntimeError):
        await PipelineExecutor().run(
            outer, {"seed": 5},
            tool_dispatch=dispatch, state_log=state_log,
            run_id="run-tf", pipeline_registry=registry,
        )
    await state_log.flush()
    assert out_file.read_text(encoding="utf-8").splitlines() == ["A"]

    # The WAL head climbs past the crash-state seqs, then GC truncates below 40 —
    # the callee sub-step's own WAL entry is dropped from wal.jsonl.
    for i in range(50):
        await state_log.append("inbox_put", n=100 + i)
    await state_log.truncate_below(40)
    await state_log.flush()
    surviving = {e["seq"] for e in state_log.iter_from(0)}
    assert all(s >= 40 for s in surviving), "the callee sub-step's WAL entry is truncated away"

    # RESUME with the crash DISARMED: the finished sub-step replays from the gen
    # FILE (no re-write of A), only sub-step 1 executes (writes B once).
    crash.clear()
    resumed = await PipelineExecutor().resume(
        "run-tf", pipeline=outer,
        tool_dispatch=dispatch, state_log=state_log, pipeline_registry=registry,
    )

    # Exactly-once: A appears ONCE (replayed, not re-executed); B once (resumed).
    assert out_file.read_text(encoding="utf-8").splitlines() == ["A", "B"]
    assert resumed.completed_step_results["1.call.0"] == {"text": "", "structured": {"wrote": "A"}}
    assert resumed.completed_step_results["1.call.1"] == {"text": "", "structured": {"wrote": "B"}}
    # The callee's final output (sub-step 1's result) threads out as the call's N2.
    assert resumed.named_stores["call_out"] == {"text": "", "structured": {"wrote": "B"}}
    assert resumed.pipe_data == {"text": "", "structured": {"wrote": "B"}}
    assert resumed.step_index == 2


@pytest.mark.asyncio
async def test_call_resume_after_full_run_replays_with_zero_new_side_effects(tmp_path):
    """Tier 2: resuming a run whose ``call`` already completed replays every callee
    sub-step from the snapshot (zero new writes) and re-threads the N2 result. RED if
    resume re-ran a completed callee sub-step's side effect."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"
    dispatch = _append_dispatch(state_log, out_file, {})

    registry = PipelineRegistry()
    registry.register("callee", _two_write_callee())
    outer = _outer_calling("callee")

    await PipelineExecutor().run(
        outer, {"seed": 5},
        tool_dispatch=dispatch, state_log=state_log,
        run_id="run-done", pipeline_registry=registry,
    )
    await state_log.flush()
    assert out_file.read_text(encoding="utf-8").splitlines() == ["A", "B"]

    resumed = await PipelineExecutor().resume(
        "run-done", pipeline=outer,
        tool_dispatch=dispatch, state_log=state_log, pipeline_registry=registry,
    )
    # No new lines — a fully-completed run replays with zero side effects.
    assert out_file.read_text(encoding="utf-8").splitlines() == ["A", "B"]
    assert resumed.named_stores["call_out"] == {"text": "", "structured": {"wrote": "B"}}
