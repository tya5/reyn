"""Tier 1: `parallel` primitive contract — serde round-trip, DSL parse,
analyzer facet.

The `parallel` step (heterogeneous NAMED-branch fan-out — the LAST
non-linear primitive) plugs into the same sibling dispatch tables
`call`/`match`/`fold`/`for_each` established: serde's ``ENCODERS``/
``DECODERS`` (work-order persistence — every branch AND ``collect``, each a
nested ``Step``, recurse through ``step_to_dict``/``step_from_dict``), the
parser's special-dispatch (Appendix B ``parallel = {on_error?, branches,
collect}`` — ``on_error`` OPTIONAL (default ``abort``, unlike ``for_each``'s
required field), ``branches``/``collect`` REQUIRED), and the analyzer's
``ANALYZER_FACETS`` (P4 seam). This pins each contract with NON-DEFAULT
values.

Real YAML parse + real JSON round-trip; no mocks.
"""
from __future__ import annotations

import json

import pytest

from reyn.core.pipeline.analyzer import analyze_step
from reyn.core.pipeline.executor import (
    AgentStep,
    CallStep,
    ParallelStep,
    Pipeline,
    ToolStep,
    TransformStep,
)
from reyn.core.pipeline.parser import PipelineParseError, parse_pipeline_dsl
from reyn.core.pipeline.schema import SchemaRegistry
from reyn.core.pipeline.serde import pipeline_from_dict, pipeline_to_dict


def test_parallel_serde_round_trip_non_default_values_with_nested_branches_and_collect():
    """Tier 1: a ``ParallelStep`` with heterogeneous named branches (one a
    ``CallStep``, one a ``ToolStep``) and a ``ToolStep`` ``collect`` round-trips
    dataclass -> dict -> JSON -> dataclass — proving every branch AND collect
    recurse through the SAME step-serde contract as a top-level step (not a
    bespoke shape). ``on_error`` persists as its plain DSL string (normalized
    only at execution)."""
    pipeline = Pipeline(steps=[
        ParallelStep(
            branches={
                "summary": CallStep(pipeline="summarize", pass_=["doc"], output="s"),
                "score": ToolStep(name="scorer", args={}, output="sc"),
            },
            collect=ToolStep(name="merge", args={}, output="merged"),
            on_error="retry(3)",
            output="results",
        ),
    ])
    wire = pipeline_to_dict(pipeline)
    par_dict = wire["steps"][0]
    assert par_dict == {
        "kind": "parallel",
        "branches": {
            "summary": {
                "kind": "call", "pipeline": "summarize", "pass": ["doc"], "output": "s",
            },
            "score": {
                "kind": "tool", "name": "scorer", "args": {}, "output": "sc", "schema": None,
            },
        },
        "collect": {"kind": "tool", "name": "merge", "args": {}, "output": "merged", "schema": None},
        "on_error": "retry(3)",
        "output": "results",
    }
    assert pipeline_from_dict(json.loads(json.dumps(wire))) == pipeline


def test_parallel_serde_round_trip_default_on_error():
    """Tier 1: a ``ParallelStep`` built without ``on_error`` persists the
    dataclass default (``"abort"``) faithfully through the round-trip."""
    pipeline = Pipeline(steps=[
        ParallelStep(
            branches={"x": TransformStep(value="1"), "y": TransformStep(value="2")},
            collect=TransformStep(value="pipe.x + pipe.y"),
        ),
    ])
    wire = pipeline_to_dict(pipeline)
    assert wire["steps"][0]["on_error"] == "abort"
    assert pipeline_from_dict(json.loads(json.dumps(wire))) == pipeline


def test_parallel_parses_from_dsl():
    """Tier 1: the DSL ``parallel`` step parses to a ``ParallelStep`` with
    every field honored, and nested ``branches:``/``collect:`` steps parse
    through the SAME per-kind parser a top-level step uses."""
    text = """
pipeline: outer
steps:
  - parallel:
      on_error: continue
      branches:
        a:
          transform:
            value: "1"
        b:
          transform:
            value: "2"
      collect:
        transform:
          value: pipe
      output: verdicts
"""
    parsed = parse_pipeline_dsl(text, SchemaRegistry())
    assert parsed.steps == [
        ParallelStep(
            branches={"a": TransformStep(value="1"), "b": TransformStep(value="2")},
            collect=TransformStep(value="pipe"),
            on_error="continue",
            output="verdicts",
        ),
    ]


def test_parallel_dsl_on_error_omitted_defaults_to_abort():
    """Tier 1: Appendix B's ``on_error?:`` — omitting ``on_error`` entirely in
    the DSL parses successfully and defaults to ``"abort"`` (the ONE
    required-vs-optional divergence from ``for_each``, which fails to parse
    without it — see ``test_for_each_dsl_requires_on_error``)."""
    text = """
pipeline: o
steps:
  - parallel:
      branches:
        a:
          transform:
            value: "1"
      collect:
        transform:
          value: pipe
"""
    parsed = parse_pipeline_dsl(text, SchemaRegistry())
    step = parsed.steps[0]
    assert isinstance(step, ParallelStep)
    assert step.on_error == "abort"


def test_parallel_dsl_rejects_bad_on_error():
    """Tier 1: when given, ``on_error`` must be ``continue``/``abort``/
    ``retry(n)`` — any other value is a parse error (e.g. a bare ``retry``
    with no count)."""
    for bad in ("skip", "retry", "retry()", "continue-please"):
        text = f"""
pipeline: o
steps:
  - parallel:
      on_error: {bad}
      branches:
        a:
          transform:
            value: "1"
      collect:
        transform:
          value: pipe
"""
        with pytest.raises(PipelineParseError):
            parse_pipeline_dsl(text, SchemaRegistry())


def test_parallel_dsl_requires_branches():
    """Tier 1: ``branches`` is REQUIRED and must be a non-empty mapping —
    omitting it, or giving an empty mapping, is a parse error."""
    text_missing = """
pipeline: o
steps:
  - parallel:
      collect:
        transform:
          value: pipe
"""
    with pytest.raises(PipelineParseError):
        parse_pipeline_dsl(text_missing, SchemaRegistry())

    text_empty = """
pipeline: o
steps:
  - parallel:
      branches: {}
      collect:
        transform:
          value: pipe
"""
    with pytest.raises(PipelineParseError):
        parse_pipeline_dsl(text_empty, SchemaRegistry())


def test_parallel_dsl_requires_collect():
    """Tier 1: ``collect`` is REQUIRED (it produces the primitive's N2 result
    over the named map) — omitting it is a parse error."""
    text = """
pipeline: o
steps:
  - parallel:
      branches:
        a:
          transform:
            value: "1"
"""
    with pytest.raises(PipelineParseError):
        parse_pipeline_dsl(text, SchemaRegistry())


def test_parallel_dsl_rejects_no_max_parallel_field():
    """Tier 1: unlike ``for_each``, ``parallel`` has NO ``max_parallel`` field
    at all (Appendix B's grammar gives it none — the branch set is
    statically finite) — supplying one is an unknown-field parse error."""
    text = """
pipeline: o
steps:
  - parallel:
      max_parallel: 2
      branches:
        a:
          transform:
            value: "1"
      collect:
        transform:
          value: pipe
"""
    with pytest.raises(PipelineParseError):
        parse_pipeline_dsl(text, SchemaRegistry())


def test_parallel_dsl_rejects_malformed_nested_branch():
    """Tier 1: a ``branches:`` entry with a malformed body fails at parse
    time, at the DSL text, through the SAME per-kind parser a top-level step
    uses (not a bespoke "nested branch" validation path)."""
    text = """
pipeline: o
steps:
  - parallel:
      branches:
        bad:
          call: {}
      collect:
        transform:
          value: pipe
"""
    with pytest.raises(PipelineParseError):
        parse_pipeline_dsl(text, SchemaRegistry())


def test_parallel_analyzer_facet_flags_empty_branches_and_large_agent_fanout():
    """Tier 1: the P4 analyzer facet for `parallel` is registered and flags
    (a) an empty ``branches`` mapping and (b) a statically large agent-branch
    fan-out (>3 agent-step branches) — but passes a small, non-empty,
    non-agent-heavy fan-out."""
    empty = ParallelStep(branches={}, collect=TransformStep(value="pipe"))
    problems = analyze_step(empty)
    assert any("branches" in p for p in problems)

    heavy = ParallelStep(
        branches={
            "a": AgentStep(prompt="a", identity="w"),
            "b": AgentStep(prompt="b", identity="w"),
            "c": AgentStep(prompt="c", identity="w"),
            "d": AgentStep(prompt="d", identity="w"),
        },
        collect=TransformStep(value="pipe"),
    )
    problems = analyze_step(heavy)
    assert any("agent-step branches" in p for p in problems)

    small = ParallelStep(
        branches={"x": TransformStep(value="1"), "y": TransformStep(value="2")},
        collect=TransformStep(value="pipe"),
    )
    assert analyze_step(small) == []
