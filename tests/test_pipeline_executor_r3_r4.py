"""Tier 2: OS invariant — Pipeline executor R3 pipe-data threading + R4 step-boundary recovery.

Covers the thin linear-executor vertical-slice
(`docs/proposals/reyn-pipeline-v0.9-design-resolutions.md` R3 + R4):
  1. pipe-data / named-store threading through the REAL R1 expression evaluator,
  2. `verify: schema` step validation via the REAL `SchemaRegistry`,
  3. the CLAUDE.md-mandated truncate-falsify test: a recorded step-boundary
     generation must survive WAL truncation below its own key seq, and `resume`
     must reconstruct correctly from it without re-executing completed steps,
  4. exactly-once replay on resume (no double side effect for a completed tool step).

Real `StateLog` + `SchemaRegistry` throughout — no mocks, no private-state assertions.
"""
from __future__ import annotations

import json

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import (
    ExprRef,
    Pipeline,
    PipelineExecutionError,
    PipelineExecutor,
    ToolStep,
    TransformStep,
)
from reyn.core.pipeline.parser import parse_pipeline_dsl
from reyn.core.pipeline.schema import SchemaRegistry


def _shout_dispatch(name: str, args: dict) -> str:
    assert name == "shout"
    return args["text"].upper() + "!!!"


@pytest.mark.asyncio
async def test_linear_pipeline_threads_pipe_data_and_named_stores_via_real_evaluator():
    """Tier 2: transform -> tool -> transform threads pipe data + named stores (R3)
    through the REAL `expr.evaluate_expr` (string concat + bare `pipe` access +
    `ctx.NAME` named-store access), not a hand-rolled stand-in."""
    pipeline = Pipeline(
        steps=[
            TransformStep(value="'hello ' + ctx.seed", output="greeting"),
            ToolStep(name="shout", args={"text": ExprRef("pipe")}, output="shouted"),
            TransformStep(value="ctx.shouted.text + ' (done)'", output="final"),
        ]
    )
    executor = PipelineExecutor()
    result = await executor.run(
        pipeline,
        {"seed": "world"},
        tool_dispatch=_shout_dispatch,
        state_log=None,
        run_id="run-threading",
    )

    # #2425 PR-2: a str tool result maps to the flat {"text": ...} ctx shape.
    assert result.pipe_data == "HELLO WORLD!!! (done)"
    assert result.named_stores == {
        "seed": "world",
        "greeting": "hello world",
        "shouted": {"text": "HELLO WORLD!!!"},
        "final": "HELLO WORLD!!! (done)",
    }
    assert result.completed_step_results == {
        "0": "hello world",
        "1": {"text": "HELLO WORLD!!!"},
        "2": "HELLO WORLD!!! (done)",
    }


@pytest.mark.asyncio
async def test_parse_json_decodes_a_tool_result_string_field_end_to_end():
    """Tier 2: the owner use case — an MCP-style `tool` step returns a payload
    whose `content` field is a plain TEXT string that is itself JSON-encoded
    (see `op_runtime/mcp.py`'s `_execute` handler). A DSL string is parsed via
    the REAL `parse_pipeline_dsl` (not `evaluate_expr` in isolation) and run
    through the REAL `PipelineExecutor`: a `transform` step decodes that
    string with `parse_json`, and a later step reads a field off the decoded
    object via `ctx.<name>.<field>` — proving the full round-trip, not just
    the isolated combinator call."""
    dsl = """
pipeline: mcp-parse-demo
description: tool result content string parsed into a structured value
steps:
  - tool: {name: search, args: {query: "reyn"}, output: raw}
  - transform: {value: "parse_json(ctx.raw.structured.content)", output: parsed}
  - transform: {value: "ctx.parsed.count + 1", output: total}
"""
    pipeline = parse_pipeline_dsl(dsl, SchemaRegistry())

    def tool_dispatch(name: str, args: dict):
        assert name == "search"
        payload = {"count": 5, "items": ["a", "b"]}
        # a dict with no "kind" is an unregistered-kind result → the whole dict
        # becomes the sole structured attachment (#2425 PR-2 ctx shape).
        return {"content": json.dumps(payload)}

    executor = PipelineExecutor()
    result = await executor.run(
        pipeline,
        {},
        tool_dispatch=tool_dispatch,
        state_log=None,
        run_id="run-parse-json-e2e",
    )

    assert result.named_stores["parsed"] == {"count": 5, "items": ["a", "b"]}
    assert result.pipe_data == 6


@pytest.mark.asyncio
async def test_verify_schema_passes_conforming_and_fails_non_conforming():
    """Tier 2: a tool step's `verify: schema` validates its output through the REAL
    `SchemaRegistry`/`validate` — conforming output passes the step; non-conforming
    output fails the step (raises), it is never silently swallowed."""
    registry = SchemaRegistry()
    registry.register(
        "greeting_schema", {"fields": {"msg": {"type": "string", "required": True}}}
    )

    def _ok_dispatch(_name: str, _args: dict) -> dict:
        return {"msg": "hi"}

    def _bad_dispatch(_name: str, _args: dict) -> dict:
        return {"count": 1}  # missing required "msg"

    executor = PipelineExecutor()

    ok_pipeline = Pipeline(
        steps=[ToolStep(name="greet", args={}, output="g", schema="greeting_schema")]
    )
    ok_result = await executor.run(
        ok_pipeline, None,
        tool_dispatch=_ok_dispatch, state_log=None, run_id="run-schema-ok",
        schema_registry=registry,
    )
    # verify: schema validates the RAW dispatch result (unchanged); the step's ctx
    # value is still reduced to the flat text/structured shape (#2425 PR-2).
    assert ok_result.pipe_data == {"text": "", "structured": {"msg": "hi"}}

    bad_pipeline = Pipeline(
        steps=[ToolStep(name="greet", args={}, output="g", schema="greeting_schema")]
    )
    with pytest.raises(PipelineExecutionError):
        await executor.run(
            bad_pipeline, None,
            tool_dispatch=_bad_dispatch, state_log=None, run_id="run-schema-bad",
            schema_registry=registry,
        )


def _make_counting_dispatch(state_log: StateLog, call_counts: dict) -> "callable":
    """A REAL side-effecting tool: each call appends a genuine WAL entry (the
    step's side effect) before returning — so distinct steps land distinct
    `state_log.last_durable_seq` values for the truncation test to exercise."""

    async def _dispatch(name: str, args: dict) -> dict:
        assert name == "echo"
        call_counts["count"] += 1
        await state_log.append("inbox_put", n=call_counts["count"])
        return {"value": args["val"], "call": call_counts["count"]}

    return _dispatch


@pytest.mark.asyncio
async def test_truncate_falsify_generation_survives_wal_truncation_below_its_seq(tmp_path):
    """Tier 2: MANDATORY CLAUDE.md recovery gate. Run a pipeline through step K (a
    real StateLog); truncate the WAL below step K's recorded generation seq;
    `resume` must still reconstruct `named_stores`/`pipe_data` correctly and resume
    at step K+1 WITHOUT re-executing steps <= K (proven via a call-counting
    tool_dispatch). RED if pipeline recovery rode a truncatable WAL event instead
    of a truncation-surviving generation FILE."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    call_counts = {"count": 0}
    dispatch = _make_counting_dispatch(state_log, call_counts)

    step0 = TransformStep(value="ctx.seed + 1", output="t0")
    step1 = ToolStep(name="echo", args={"val": ExprRef("ctx.t0")}, output="t1")
    step2 = ToolStep(name="echo", args={"val": ExprRef("pipe.structured.value")}, output="t2")
    step3 = ToolStep(name="echo", args={"val": ExprRef("pipe.structured.value")}, output="t3")

    executor = PipelineExecutor()
    phase1 = Pipeline(steps=[step0, step1, step2])  # run through step K=2 (0-indexed)
    phase1_result = await executor.run(
        phase1, {"seed": 10},
        tool_dispatch=dispatch, state_log=state_log, run_id="run-truncate",
    )
    await state_log.flush()
    assert phase1_result.step_index == 3
    assert call_counts["count"] == 2, "steps 1 and 2 each made exactly one tool call"

    # the two tool steps' side effects (WAL appends) landed at seq 1 and 2 —
    # step K=2's generation is keyed at seq 2.
    seq_after_phase1 = state_log.current_seq
    assert seq_after_phase1 == 2

    # the WAL head climbs well past the generation's key seq (simulating other
    # agents' activity advancing the retention floor).
    for i in range(50):
        await state_log.append("inbox_put", n=100 + i)

    # GC truncates the WAL below floor 40 — seq 1 and 2 (step K's own WAL
    # entries) are dropped from wal.jsonl.
    await state_log.truncate_below(40)
    await state_log.flush()
    stats = state_log.last_truncate_stats
    assert stats["dropped"] >= 2, "the early tool-step WAL entries must be truncated"
    surviving_seqs = {e["seq"] for e in state_log.iter_from(0)}
    assert 1 not in surviving_seqs and 2 not in surviving_seqs, (
        "step K's own WAL entries are gone from the WAL — the generation FILE, "
        "not a WAL event, must be what resume reconstructs from"
    )

    full_pipeline = Pipeline(steps=[step0, step1, step2, step3])
    resumed = await executor.resume(
        "run-truncate", pipeline=full_pipeline,
        tool_dispatch=dispatch, state_log=state_log,
    )

    assert call_counts["count"] == 3, (
        "steps 0-2 must NOT be re-executed on resume (exactly-once) — only step 3 "
        "(the first step with no recorded result) makes a new tool call"
    )
    assert resumed.step_index == 4
    assert resumed.named_stores["t0"] == 11
    assert resumed.named_stores["t1"] == {"text": "", "structured": {"value": 11, "call": 1}}
    assert resumed.named_stores["t2"] == {"text": "", "structured": {"value": 11, "call": 2}}
    assert resumed.named_stores["t3"] == {"text": "", "structured": {"value": 11, "call": 3}}
    assert resumed.pipe_data == resumed.named_stores["t3"]


@pytest.mark.asyncio
async def test_exactly_once_resume_does_not_replay_completed_tool_side_effect(tmp_path):
    """Tier 2: a crash immediately after a tool step completes (before advancing)
    must not re-run that tool's side effect on resume — its result comes from the
    step-boundary generation snapshot, not re-execution."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    call_counts = {"count": 0}
    dispatch = _make_counting_dispatch(state_log, call_counts)

    step0 = ToolStep(name="echo", args={"val": 1}, output="a")
    step1 = ToolStep(name="echo", args={"val": 2}, output="b")

    executor = PipelineExecutor()
    # simulate a crash after step0 completes: only step0 is in the run pipeline.
    crashed_result = await executor.run(
        Pipeline(steps=[step0]), None,
        tool_dispatch=dispatch, state_log=state_log, run_id="run-exactly-once",
    )
    await state_log.flush()
    assert crashed_result.step_index == 1
    assert call_counts["count"] == 1

    resumed = await executor.resume(
        "run-exactly-once", pipeline=Pipeline(steps=[step0, step1]),
        tool_dispatch=dispatch, state_log=state_log,
    )

    assert call_counts["count"] == 2, "step0 must not be re-run; only step1 is new"
    assert resumed.named_stores["a"] == {"text": "", "structured": {"value": 1, "call": 1}}
    assert resumed.named_stores["b"] == {"text": "", "structured": {"value": 2, "call": 2}}
    assert resumed.step_index == 2


@pytest.mark.asyncio
async def test_resume_with_no_snapshot_runs_from_scratch(tmp_path):
    """Tier 2: `resume` on a run_id with no recorded generation behaves as
    run-from-scratch (the no-snapshot-yet contract)."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    executor = PipelineExecutor()
    pipeline = Pipeline(steps=[TransformStep(value="1 + 1", output="two")])

    result = await executor.resume(
        "never-run-before", pipeline=pipeline,
        tool_dispatch=lambda *_a, **_k: None, state_log=state_log,
    )

    assert result.pipe_data == 2
    assert result.named_stores == {"two": 2}
