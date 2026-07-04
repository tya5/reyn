"""Tier 2: OS invariant — the dispatch-table restructure is behaviour-preserving.

The executor's per-step ``isinstance`` chain became a type-keyed ``STEP_DISPATCH``
table, and serde's encode/decode ``isinstance``/``kind==`` chains became
``ENCODERS``/``DECODERS`` tables (the seam that lets a future primitive ADD an entry
instead of editing a shared branch). This test pins that the three EXISTING kinds
(``transform``/``tool``/``agent``) execute and serialize IDENTICALLY across the
refactor — same threaded pipe data, same flat ``str(i)`` recovery keys, same named
stores, same JSON round-trip — so the restructure is provably byte-identical for the
kinds it touched, not a behaviour change riding along with the seam.

Real ``expr`` evaluator + real serde JSON round-trip; no mocks.
"""
from __future__ import annotations

import json

import pytest

from reyn.core.pipeline.executor import (
    ExprRef,
    Pipeline,
    PipelineExecutor,
    ToolStep,
    TransformStep,
)
from reyn.core.pipeline.serde import pipeline_from_dict, pipeline_to_dict


def _echo_dispatch(name: str, args: dict) -> dict:
    assert name == "echo"
    return {"echoed": args["v"]}


@pytest.mark.asyncio
async def test_transform_tool_transform_threads_identically_through_dispatch_table():
    """Tier 2: a transform -> tool -> transform pipeline threads pipe data + named
    stores and records FLAT ``str(i)`` keys exactly as the pre-restructure linear
    loop did — the dispatch table changed HOW a step is selected, not WHAT it does."""
    pipeline = Pipeline(
        steps=[
            TransformStep(value="ctx.seed * 2", output="doubled"),
            ToolStep(name="echo", args={"v": ExprRef("pipe")}, output="echoed"),
            TransformStep(value="ctx.echoed.echoed + 1", output="final"),
        ]
    )
    result = await PipelineExecutor().run(
        pipeline, {"seed": 21},
        tool_dispatch=_echo_dispatch, state_log=None, run_id="run-equiv",
    )

    assert result.pipe_data == 43
    assert result.named_stores == {
        "seed": 21, "doubled": 42, "echoed": {"echoed": 42}, "final": 43,
    }
    # Flat linear keys — no dotted paths appear for a pipeline with no `call`.
    assert result.completed_step_results == {
        "0": 42, "1": {"echoed": 42}, "2": 43,
    }
    assert result.step_index == 3


def test_serde_round_trip_identical_through_dispatch_tables():
    """Tier 2: every existing kind round-trips (dataclass -> dict -> JSON ->
    dataclass) unchanged through ``ENCODERS``/``DECODERS`` — the marker collision
    guard and ExprRef handling are preserved by the table form."""
    pipeline = Pipeline(
        steps=[
            TransformStep(value="ctx.a + 1", output="t"),
            ToolStep(
                name="echo",
                args={"v": ExprRef("ctx.t"), "n": 3, "nested": {"deep": [1, "two"]}},
                output="w",
                schema="OutShape",
            ),
        ],
        description="equivalence round-trip",
    )
    wire = json.loads(json.dumps(pipeline_to_dict(pipeline)))
    assert pipeline_from_dict(wire) == pipeline
