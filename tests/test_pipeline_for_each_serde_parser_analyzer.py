"""Tier 1: `for_each` primitive contract — serde round-trip, DSL parse, analyzer facet.

The `for_each` step (concurrent fan-out) plugs into the same four sibling
dispatch tables `call`/`match`/`fold` established: serde's ``ENCODERS``/
``DECODERS`` (work-order persistence — ``do`` AND ``collect``, each a nested
``Step``, recurse through ``step_to_dict``/``step_from_dict``), the parser's
special-dispatch (Appendix B ``for_each = {over?, items?, max_parallel?,
on_error, do, collect}`` — ``on_error`` REQUIRED, ``collect`` REQUIRED), and the
analyzer's ``ANALYZER_FACETS`` (P4 seam — the S5 ``max_parallel``/``over``
spawn-bound warnings). This pins each contract with NON-DEFAULT values.

Real YAML parse + real JSON round-trip; no mocks.
"""
from __future__ import annotations

import json

import pytest

from reyn.core.pipeline.analyzer import analyze_step
from reyn.core.pipeline.executor import (
    CallStep,
    ForEachStep,
    Pipeline,
    ToolStep,
    TransformStep,
)
from reyn.core.pipeline.parser import PipelineParseError, parse_pipeline_dsl
from reyn.core.pipeline.schema import SchemaRegistry
from reyn.core.pipeline.serde import pipeline_from_dict, pipeline_to_dict


def test_for_each_serde_round_trip_non_default_values_with_nested_do_and_collect():
    """Tier 1: a ``ForEachStep`` whose ``do`` is a ``CallStep`` and whose
    ``collect`` is a ``ToolStep`` round-trips dataclass -> dict -> JSON ->
    dataclass — proving BOTH sub-steps recurse through the SAME step-serde
    contract as a top-level step (not a bespoke shape). ``on_error`` persists as
    its plain DSL string (normalized only at execution)."""
    pipeline = Pipeline(steps=[
        ForEachStep(
            do=CallStep(pipeline="summarize-one", pass_=["item"], output="r"),
            collect=ToolStep(name="merge", args={}, output="merged"),
            on_error="retry(3)",
            over="ctx.docs",
            max_parallel=4,
            output="results",
        ),
    ])
    wire = pipeline_to_dict(pipeline)
    fe_dict = wire["steps"][0]
    assert fe_dict == {
        "kind": "for_each",
        "over": "ctx.docs",
        "items": None,
        "max_parallel": 4,
        "on_error": "retry(3)",
        "do": {"kind": "call", "pipeline": "summarize-one", "pass": ["item"], "output": "r"},
        "collect": {"kind": "tool", "name": "merge", "args": {}, "output": "merged", "schema": None},
        "output": "results",
    }
    assert pipeline_from_dict(json.loads(json.dumps(wire))) == pipeline


def test_for_each_serde_round_trip_static_items_source():
    """Tier 1: a ``ForEachStep`` using the ``items:`` static-literal source (instead
    of ``over``), no ``max_parallel``, and ``on_error:continue`` round-trips too."""
    pipeline = Pipeline(steps=[
        ForEachStep(
            do=TransformStep(value="item + '!'"),
            collect=TransformStep(value="pipe"),
            on_error="continue",
            items=["a", "b", "c"],
        ),
    ])
    wire = pipeline_to_dict(pipeline)
    assert wire["steps"][0]["items"] == ["a", "b", "c"]
    assert wire["steps"][0]["over"] is None
    assert wire["steps"][0]["max_parallel"] is None
    assert pipeline_from_dict(json.loads(json.dumps(wire))) == pipeline


def test_for_each_parses_from_dsl():
    """Tier 1: the DSL ``for_each`` step parses to a ``ForEachStep`` (moved out of
    the not-yet-supported set) with every field honored, and nested ``do:``/
    ``collect:`` steps parse through the SAME per-kind parser a top-level step
    uses."""
    text = """
pipeline: outer
steps:
  - for_each:
      over: ctx.suspects
      max_parallel: 3
      on_error: continue
      do:
        transform:
          value: "item + '?'"
      collect:
        transform:
          value: pipe
      output: verdicts
"""
    parsed = parse_pipeline_dsl(text, SchemaRegistry())
    assert parsed.steps == [
        ForEachStep(
            do=TransformStep(value="item + '?'"),
            collect=TransformStep(value="pipe"),
            on_error="continue",
            over="ctx.suspects",
            max_parallel=3,
            output="verdicts",
        ),
    ]


def test_for_each_dsl_requires_on_error():
    """Tier 1: ``on_error`` has no ``?`` in Appendix B's for_each grammar (a
    fan-out author MUST state the completeness policy) — omitting it is a parse
    error, NOT a silent default to abort (that is ``parallel``'s optional field)."""
    text = """
pipeline: o
steps:
  - for_each:
      items: [1, 2]
      do:
        transform:
          value: item
      collect:
        transform:
          value: pipe
"""
    with pytest.raises(PipelineParseError):
        parse_pipeline_dsl(text, SchemaRegistry())


def test_for_each_dsl_rejects_bad_on_error():
    """Tier 1: ``on_error`` must be ``continue``/``abort``/``retry(n)`` — any other
    value is a parse error (e.g. a bare ``retry`` with no count)."""
    for bad in ("skip", "retry", "retry()", "continue-please"):
        text = f"""
pipeline: o
steps:
  - for_each:
      items: [1]
      on_error: {bad}
      do:
        transform:
          value: item
      collect:
        transform:
          value: pipe
"""
        with pytest.raises(PipelineParseError):
            parse_pipeline_dsl(text, SchemaRegistry())


def test_for_each_dsl_requires_collect():
    """Tier 1: ``collect`` is REQUIRED (it produces the primitive's N2 result) —
    omitting it is a parse error."""
    text = """
pipeline: o
steps:
  - for_each:
      items: [1, 2]
      on_error: abort
      do:
        transform:
          value: item
"""
    with pytest.raises(PipelineParseError):
        parse_pipeline_dsl(text, SchemaRegistry())


def test_for_each_dsl_rejects_over_and_items_together():
    """Tier 1: ``over`` and ``items`` are mutually exclusive list sources —
    specifying both is a parse error, never a silent "one wins" resolution."""
    text = """
pipeline: o
steps:
  - for_each:
      over: ctx.xs
      items: [1, 2]
      on_error: abort
      do:
        transform:
          value: item
      collect:
        transform:
          value: pipe
"""
    with pytest.raises(PipelineParseError):
        parse_pipeline_dsl(text, SchemaRegistry())


def test_for_each_dsl_rejects_malformed_nested_step():
    """Tier 1: a ``do:``/``collect:`` naming ANY step kind still validates that
    kind's own body — a malformed nested step (here a ``parallel`` with an
    empty ``branches``) fails at parse time, at the DSL text, through the
    SAME per-kind parser a top-level step uses (not a bespoke "nested step"
    validation path). (``parallel`` is now a fully-supported step kind — see
    ``test_pipeline_parallel_primitive.py`` / ``test_pipeline_parallel_serde_
    parser_analyzer.py`` — this test only pins that a malformed nested body,
    of any kind, still fails cleanly.)"""
    text = """
pipeline: o
steps:
  - for_each:
      items: [1]
      on_error: abort
      do:
        parallel:
          branches: {}
      collect:
        transform:
          value: pipe
"""
    with pytest.raises(PipelineParseError):
        parse_pipeline_dsl(text, SchemaRegistry())


def test_for_each_analyzer_facet_flags_uncapped_and_over_sources():
    """Tier 1: the P4 analyzer facet for `for_each` is registered and flags (a) a
    fan-out with no ``max_parallel`` (fan-out width not statically known) and (b)
    an ``over``-sourced fan-out (item count not statically known) — but passes a
    fully-static, capped fan-out."""
    unbounded = ForEachStep(
        do=TransformStep(value="item"), collect=TransformStep(value="pipe"),
        on_error="abort", over="ctx.xs",
    )
    problems = analyze_step(unbounded)
    assert any("max_parallel" in p for p in problems)
    assert any("over" in p for p in problems)

    static_capped = ForEachStep(
        do=TransformStep(value="item"), collect=TransformStep(value="pipe"),
        on_error="abort", items=[1, 2, 3], max_parallel=2,
    )
    assert analyze_step(static_capped) == []
