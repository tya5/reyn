"""Tier 2: OS invariant — the `match` compositional primitive + dotted-path R7
recovery.

Covers `call`'s runtime-selected sibling (Appendix B `match = {on: PATH, cases:
{LABEL: {pipeline: LIT, pass: [{NAME: EXPR}*]}}+, default?: {...}, output?: NAME}`,
built on the same `_run_scope` + dotted-path recovery + dispatch-table
foundation `call` uses):

  1. happy path — the ``on`` VALUE selects a case LABEL by string equality;
     that ONE case's REGISTERED callee runs synchronously exactly like
     ``call``'s callee (``pass:[...]`` projection, pipe-data-at-call-site,
     N2 final-output threading).
  2. no case matches but ``default`` is present — ``default``'s callee runs.
  3. no case matches and no ``default`` — the step fails cleanly.
  4. ``pass:[...]`` isolation on the selected case.
  5. the CLAUDE.md-mandated truncate-falsify recovery gate: crash mid-selected-
     case, truncate the WAL below the source events, resume from the
     generation FILE, and prove the completed case sub-steps replay
     exactly-once while only the remaining sub-step executes.
  6. an unregistered case-target pipeline fails cleanly.

Real ``StateLog`` + ``PipelineRegistry`` + real generation files throughout;
the recovery tests build the executor directly (the same discipline as
``call``'s truncate-falsify test) — no mocks, no private-state assertions.
"""
from __future__ import annotations

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import (
    MatchCase,
    MatchStep,
    Pipeline,
    PipelineExecutionError,
    PipelineExecutor,
    ToolStep,
    TransformStep,
)
from reyn.core.pipeline.registry import PipelineRegistry

# ── happy path: on-value selects a case, pass:[...] scoping, N2 out ─────────


@pytest.mark.asyncio
async def test_match_selects_case_by_on_value_and_threads_final_output():
    """Tier 2: a ``match`` evaluates ``on``, selects the case whose LABEL
    string-equals the value, runs ONLY that case's callee (``pass:[brief]``
    projects ONLY ``brief`` — the caller's other store is invisible), the
    callee's first step sees the caller's pipe-data via bare ``pipe`` (Hard
    rule 5), and the callee's final output becomes the ``match`` step's
    return value + named output."""
    registry = PipelineRegistry()
    registry.register(
        "on-cat",
        Pipeline(steps=[TransformStep(value="'cat: ' + pipe", output="r")]),
    )
    registry.register(
        "on-dog",
        Pipeline(steps=[TransformStep(value="'dog: ' + pipe", output="r")]),
    )

    outer = Pipeline(steps=[
        TransformStep(value="ctx.kind", output="carried"),  # pipe_data = "cat"
        MatchStep(
            on="ctx.kind",
            cases={
                "cat": MatchCase(pipeline="on-cat", pass_=[("brief", "ctx.brief")]),
                "dog": MatchCase(pipeline="on-dog", pass_=[("brief", "ctx.brief")]),
            },
            output="matched",
        ),
        TransformStep(value="ctx.matched + '!'", output="final"),
    ])

    result = await PipelineExecutor().run(
        outer,
        {"kind": "cat", "brief": "hi", "secret": "nope"},
        tool_dispatch=lambda *_a, **_k: None,
        state_log=None,
        run_id="run-match-happy",
        pipeline_registry=registry,
    )

    assert result.named_stores["matched"] == "cat: cat"
    assert result.named_stores["final"] == "cat: cat!"
    assert result.pipe_data == "cat: cat!"
    # Outer flat key for the match's N2 result; selected case's sub-step under
    # the dotted scope. The UNSELECTED case ("dog") never ran — no dotted key.
    assert result.completed_step_results["1"] == "cat: cat"
    assert result.completed_step_results["1.match.0"] == "cat: cat"


@pytest.mark.asyncio
async def test_match_runs_default_when_no_case_matches():
    """Tier 2: an ``on`` value matching no case LABEL runs ``default``'s
    callee instead."""
    registry = PipelineRegistry()
    registry.register(
        "known",
        Pipeline(steps=[TransformStep(value="'known'", output="r")]),
    )
    registry.register(
        "fallback",
        Pipeline(steps=[TransformStep(value="'fallback'", output="r")]),
    )
    outer = Pipeline(steps=[
        MatchStep(
            on="ctx.kind",
            cases={"known": MatchCase(pipeline="known", pass_=[])},
            default=MatchCase(pipeline="fallback", pass_=[]),
            output="out",
        ),
    ])
    result = await PipelineExecutor().run(
        outer, {"kind": "unrecognized"},
        tool_dispatch=lambda *_a, **_k: None, state_log=None,
        run_id="run-match-default", pipeline_registry=registry,
    )
    assert result.named_stores["out"] == "fallback"


@pytest.mark.asyncio
async def test_match_fails_cleanly_when_no_case_and_no_default():
    """Tier 2: an ``on`` value matching no case and no ``default`` present
    fails the step with a clear error, not a silent no-op or KeyError."""
    outer = Pipeline(steps=[
        MatchStep(on="ctx.kind", cases={"known": MatchCase(pipeline="known", pass_=[])}),
    ])
    with pytest.raises(PipelineExecutionError) as exc:
        await PipelineExecutor().run(
            outer, {"kind": "unrecognized"},
            tool_dispatch=lambda *_a, **_k: None, state_log=None,
            run_id="run-match-nodefault", pipeline_registry=PipelineRegistry(),
        )
    assert "matched no case" in str(exc.value)


@pytest.mark.asyncio
async def test_match_pass_isolation_denies_unpassed_store():
    """Tier 2: the selected case's callee referencing a caller store NOT in
    its own ``pass:[...]`` fails the step — structural, not silent None."""
    registry = PipelineRegistry()
    registry.register(
        "leaky",
        Pipeline(steps=[TransformStep(value="ctx.secret", output="x")]),
    )
    outer = Pipeline(steps=[
        MatchStep(on="ctx.kind", cases={"x": MatchCase(pipeline="leaky", pass_=[])}, output="o"),
    ])
    with pytest.raises(PipelineExecutionError):
        await PipelineExecutor().run(
            outer, {"kind": "x", "secret": "s"},
            tool_dispatch=lambda *_a, **_k: None, state_log=None,
            run_id="run-match-iso", pipeline_registry=registry,
        )


@pytest.mark.asyncio
async def test_selected_case_failure_fails_the_match_step():
    """Tier 2: the selected case's callee step failure propagates out of the
    ``match`` as a caller step failure (never silently swallowed)."""
    registry = PipelineRegistry()
    registry.register(
        "boom",
        Pipeline(steps=[TransformStep(value="ctx.missing_field", output="x")]),
    )
    outer = Pipeline(steps=[
        MatchStep(on="ctx.kind", cases={"x": MatchCase(pipeline="boom", pass_=[])}, output="o"),
    ])
    with pytest.raises(PipelineExecutionError):
        await PipelineExecutor().run(
            outer, {"kind": "x"},
            tool_dispatch=lambda *_a, **_k: None, state_log=None,
            run_id="run-match-boom", pipeline_registry=registry,
        )


@pytest.mark.asyncio
async def test_unregistered_match_case_target_fails_cleanly():
    """Tier 2: a ``match`` case pointing at an unregistered pipeline name
    fails the step with a clear error, not an unhandled KeyError."""
    outer = Pipeline(steps=[
        MatchStep(on="ctx.kind", cases={"x": MatchCase(pipeline="nope", pass_=[])}, output="o"),
    ])
    with pytest.raises(PipelineExecutionError) as exc:
        await PipelineExecutor().run(
            outer, {"kind": "x"},
            tool_dispatch=lambda *_a, **_k: None, state_log=None,
            run_id="run-match-missing", pipeline_registry=PipelineRegistry(),
        )
    assert "not registered" in str(exc.value)


# ── dotted-path R4 recovery: the CLAUDE.md truncate-falsify gate ────────────


def _append_dispatch(state_log: StateLog, out_file, crash):
    """A REAL side-effecting tool (mirrors the `call` primitive's own recovery
    test): each call appends a line to ``out_file`` AND a WAL entry (so R4 gens
    land at distinct durable seqs). The exactly-once probe is the FILE — a
    re-fired step leaves a duplicate line."""

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


def _two_write_case() -> Pipeline:
    """A 2-step case callee that writes ``A`` then ``B`` — both side-effecting,
    so a crash between them leaves exactly ``A`` on disk."""
    return Pipeline(steps=[
        ToolStep(name="append", args={"line": "A"}, output="a"),
        ToolStep(name="append", args={"line": "B"}, output="b"),
    ])


def _outer_matching(case_name: str) -> Pipeline:
    return Pipeline(steps=[
        TransformStep(value="ctx.seed", output="carried"),
        MatchStep(
            on="ctx.kind",
            cases={"go": MatchCase(pipeline=case_name, pass_=[("seed", "ctx.seed")])},
            output="match_out",
        ),
    ])


@pytest.mark.asyncio
async def test_mid_match_snapshot_uses_dotted_keys_and_isolates_outer_stores(tmp_path):
    """Tier 2: a genuine MID-CASE crash (sub-step 0 recorded, sub-step 1
    failed) records the finished sub-step under ``f"{i}.match.0"`` and keeps
    ``step_index`` at the OUTER ``match`` index — and the case's local ``a``
    store does NOT appear in the persisted OUTER ``named_stores`` (pass:[...]
    isolation on the recovery axis)."""
    from reyn.core.events.pipeline_recovery import latest_pipeline_state

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"
    crash = {"line": "B"}
    dispatch = _append_dispatch(state_log, out_file, crash)

    registry = PipelineRegistry()
    registry.register("case-pipe", _two_write_case())
    with pytest.raises(RuntimeError):
        await PipelineExecutor().run(
            _outer_matching("case-pipe"), {"seed": 5, "kind": "go"},
            tool_dispatch=dispatch, state_log=state_log,
            run_id="run-match-dotted", pipeline_registry=registry,
        )
    await state_log.flush()

    snap = latest_pipeline_state("run-match-dotted", state_log)
    assert "1.match.0" in snap["completed_step_results"]
    assert "1.match.1" not in snap["completed_step_results"]
    assert "1" not in snap["completed_step_results"]
    assert snap["step_index"] == 1
    assert "a" not in snap["named_stores"]
    assert snap["named_stores"].get("carried") == 5
    assert out_file.read_text(encoding="utf-8").splitlines() == ["A"]


@pytest.mark.asyncio
async def test_truncate_falsify_match_resumes_case_substeps_exactly_once(tmp_path):
    """Tier 2: MANDATORY CLAUDE.md recovery gate for the `match` primitive. A
    run crashes MID-SELECTED-CASE (sub-step 0 wrote ``A`` + recorded its R4
    gen + dotted key; sub-step 1 failed before writing ``B``). The WAL is then
    truncated BELOW those source events. ``resume`` must reconstruct from the
    generation FILE, REPLAY the finished sub-step exactly-once (``A`` is NOT
    written again), execute ONLY the remaining sub-step (writes ``B`` once),
    and complete — threading the case's final output out. RED if a case
    sub-step rode a truncatable WAL event, or if resume re-ran the whole
    ``match`` instead of replaying the completed sub-steps."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"
    crash = {"line": "B"}
    dispatch = _append_dispatch(state_log, out_file, crash)

    registry = PipelineRegistry()
    registry.register("case-pipe", _two_write_case())
    outer = _outer_matching("case-pipe")

    with pytest.raises(RuntimeError):
        await PipelineExecutor().run(
            outer, {"seed": 5, "kind": "go"},
            tool_dispatch=dispatch, state_log=state_log,
            run_id="run-match-tf", pipeline_registry=registry,
        )
    await state_log.flush()
    assert out_file.read_text(encoding="utf-8").splitlines() == ["A"]

    for i in range(50):
        await state_log.append("inbox_put", n=100 + i)
    await state_log.truncate_below(40)
    await state_log.flush()
    surviving = {e["seq"] for e in state_log.iter_from(0)}
    assert all(s >= 40 for s in surviving), "the case sub-step's WAL entry is truncated away"

    crash.clear()
    resumed = await PipelineExecutor().resume(
        "run-match-tf", pipeline=outer,
        tool_dispatch=dispatch, state_log=state_log, pipeline_registry=registry,
    )

    assert out_file.read_text(encoding="utf-8").splitlines() == ["A", "B"]
    assert resumed.completed_step_results["1.match.0"] == {"wrote": "A"}
    assert resumed.completed_step_results["1.match.1"] == {"wrote": "B"}
    assert resumed.named_stores["match_out"] == {"wrote": "B"}
    assert resumed.pipe_data == {"wrote": "B"}
    assert resumed.step_index == 2
