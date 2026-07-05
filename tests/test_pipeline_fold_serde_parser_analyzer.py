"""Tier 1: `fold` primitive contract — serde round-trip, DSL parse, analyzer facet.

The `fold` step (sequential accumulator) plugs into the three sibling dispatch
tables `call` established: serde's ``ENCODERS``/``DECODERS`` (work-order
persistence — `do`, itself a nested `Step`, recurses through `step_to_dict`/
`step_from_dict`), the parser's ``_STEP_PARSERS`` (Appendix B ``fold =
{over?, items?, init, do, output, max_items?}``), and the analyzer's
``ANALYZER_FACETS`` (P4 seam — the `max_items`/`over` cost-bound warning).
This pins each contract with NON-DEFAULT values.

Real YAML parse + real JSON round-trip; no mocks.
"""
from __future__ import annotations

import json

import pytest

from reyn.core.pipeline.analyzer import analyze_step
from reyn.core.pipeline.executor import CallStep, FoldStep, Pipeline, TransformStep
from reyn.core.pipeline.parser import PipelineParseError, parse_pipeline_dsl
from reyn.core.pipeline.schema import SchemaRegistry
from reyn.core.pipeline.serde import pipeline_from_dict, pipeline_to_dict


def test_fold_serde_round_trip_non_default_values_with_nested_do():
    """Tier 1: a ``FoldStep`` whose ``do`` is itself a ``CallStep`` (a nested
    compositional step) round-trips dataclass -> dict -> JSON -> dataclass —
    proving `do` recurses through the SAME step-serde contract as a
    top-level step, not a bespoke shape."""
    pipeline = Pipeline(steps=[
        FoldStep(
            init="[]", do=CallStep(pipeline="summarize-one", pass_=["item"], output="r"),
            output="results", over="ctx.docs", max_items=5,
        ),
    ])
    wire = pipeline_to_dict(pipeline)
    fold_dict = wire["steps"][0]
    assert fold_dict == {
        "kind": "fold",
        "over": "ctx.docs",
        "items": None,
        "init": "[]",
        "do": {"kind": "call", "pipeline": "summarize-one", "pass": ["item"], "output": "r"},
        "output": "results",
        "max_items": 5,
    }
    assert pipeline_from_dict(json.loads(json.dumps(wire))) == pipeline


def test_fold_serde_round_trip_static_items_source():
    """Tier 1: a ``FoldStep`` using the ``items:`` static-literal-list source
    (instead of ``over``) round-trips too."""
    pipeline = Pipeline(steps=[
        FoldStep(
            init="0", do=TransformStep(value="acc + item"), output="total",
            items=[1, 2, 3],
        ),
    ])
    wire = pipeline_to_dict(pipeline)
    assert wire["steps"][0]["items"] == [1, 2, 3]
    assert wire["steps"][0]["over"] is None
    assert pipeline_from_dict(json.loads(json.dumps(wire))) == pipeline


def test_fold_parses_from_dsl():
    """Tier 1: the DSL ``fold`` step parses to a ``FoldStep`` (moved out of the
    not-yet-supported set) with ``over``/``init``/``do``/``output``/
    ``max_items`` all honored, and a nested ``do:`` step parses through the
    SAME per-kind parser a top-level step uses."""
    text = """
pipeline: outer
steps:
  - fold:
      over: ctx.numbers
      init: "0"
      do:
        transform:
          value: "acc + item"
      output: total
      max_items: 10
"""
    parsed = parse_pipeline_dsl(text, SchemaRegistry())
    assert parsed.steps == [
        FoldStep(
            init="0", do=TransformStep(value="acc + item"), output="total",
            over="ctx.numbers", max_items=10,
        ),
    ]


def test_fold_dsl_requires_output():
    """Tier 1: ``output`` has no ``?`` in Appendix B's fold grammar — a
    missing/empty ``output`` is a parse error."""
    text = """
pipeline: o
steps:
  - fold:
      items: [1, 2]
      init: "0"
      do:
        transform:
          value: "acc + item"
"""
    with pytest.raises(PipelineParseError):
        parse_pipeline_dsl(text, SchemaRegistry())


def test_fold_dsl_rejects_over_and_items_together():
    """Tier 1: ``over`` and ``items`` are mutually exclusive list sources —
    specifying both is a parse error, never a silent "one wins" resolution."""
    text = """
pipeline: o
steps:
  - fold:
      over: ctx.numbers
      items: [1, 2]
      init: "0"
      do:
        transform:
          value: "acc + item"
      output: total
"""
    with pytest.raises(PipelineParseError):
        parse_pipeline_dsl(text, SchemaRegistry())


def test_fold_dsl_rejects_unsupported_nested_do_kind():
    """Tier 1: a ``do:`` naming a still-unsupported step kind (e.g. `match`)
    fails at parse time, at the DSL text — the same "not yet supported" error
    a top-level step of that kind would raise, not a confusing failure deep
    inside fold-specific parsing."""
    text = """
pipeline: o
steps:
  - fold:
      items: [1]
      init: "0"
      do:
        match:
          on: x
          cases: {}
      output: total
"""
    with pytest.raises(PipelineParseError):
        parse_pipeline_dsl(text, SchemaRegistry())


def test_fold_analyzer_facet_flags_uncapped_over_source():
    """Tier 1: the P4 analyzer facet for `fold` is registered and flags an
    `over`-sourced fold with no `max_items` (no statically known iteration
    bound) — but passes the same fold once `max_items` is set, and always
    passes an `items`-sourced fold (a static literal list has a known length
    regardless of `max_items`)."""
    uncapped = FoldStep(
        init="0", do=TransformStep(value="acc + item"), output="total",
        over="ctx.numbers",
    )
    problems = analyze_step(uncapped)
    assert problems and "max_items" in problems[0]

    capped = FoldStep(
        init="0", do=TransformStep(value="acc + item"), output="total",
        over="ctx.numbers", max_items=5,
    )
    assert analyze_step(capped) == []

    static_source = FoldStep(
        init="0", do=TransformStep(value="acc + item"), output="total",
        items=[1, 2, 3],
    )
    assert analyze_step(static_source) == []
