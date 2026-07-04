"""Tier 1: `call` primitive contract — serde round-trip, DSL parse, analyzer facet.

The `call` step (R7) plugs into the three sibling dispatch tables the foundation
established: serde's ``ENCODERS``/``DECODERS`` (work-order persistence — the
``pass_`` field ⇄ wire key ``"pass"`` split), the parser's ``_STEP_PARSERS``
(Appendix B ``call = {pipeline, pass, output}``), and the analyzer's
``ANALYZER_FACETS`` (P4 seam). This pins each contract with NON-DEFAULT values.

Real YAML parse + real JSON round-trip; no mocks.
"""
from __future__ import annotations

import json

import pytest

from reyn.core.pipeline.analyzer import analyze_step
from reyn.core.pipeline.executor import CallStep, Pipeline, ToolStep
from reyn.core.pipeline.parser import PipelineParseError, parse_pipeline_dsl
from reyn.core.pipeline.schema import SchemaRegistry
from reyn.core.pipeline.serde import pipeline_from_dict, pipeline_to_dict


def test_call_serde_round_trip_non_default_values():
    """Tier 1: a ``CallStep`` with non-default ``pass_`` + ``output`` round-trips
    dataclass -> dict -> JSON -> dataclass, and the wire form uses the Appendix-B
    key ``"pass"`` (not the Python field name ``pass_``)."""
    pipeline = Pipeline(steps=[
        CallStep(pipeline="sub-flow", pass_=["brief", "budget"], output="result"),
        ToolStep(name="noop", args={}),
    ])
    wire = pipeline_to_dict(pipeline)
    call_dict = wire["steps"][0]
    assert call_dict == {
        "kind": "call", "pipeline": "sub-flow",
        "pass": ["brief", "budget"], "output": "result",
    }
    assert "pass_" not in call_dict  # the wire key is the Appendix-B `pass`
    assert pipeline_from_dict(json.loads(json.dumps(wire))) == pipeline


def test_call_parses_from_dsl():
    """Tier 1: the DSL ``call`` step parses to a ``CallStep`` (moved out of the
    not-yet-supported set) with ``pass``/``output`` honored."""
    text = """
pipeline: outer
steps:
  - call:
      pipeline: inner
      pass: [brief]
      output: summary
"""
    parsed = parse_pipeline_dsl(text, SchemaRegistry())
    assert parsed.steps == [CallStep(pipeline="inner", pass_=["brief"], output="summary")]


def test_call_dsl_requires_literal_pipeline_name():
    """Tier 1: an empty/missing ``pipeline`` name is a parse error (Hard rule 2 —
    the target is a static literal, and it must be present)."""
    missing = "pipeline: o\nsteps:\n  - call:\n      pass: [x]\n"
    with pytest.raises(PipelineParseError):
        parse_pipeline_dsl(missing, SchemaRegistry())


def test_call_analyzer_facet_registered_and_passes_literal_target():
    """Tier 1: the P4 analyzer facet for ``call`` is registered and returns no
    problems for a valid literal target — the seam future primitives must extend."""
    assert analyze_step(CallStep(pipeline="inner", pass_=[], output=None)) == []
    # A pathological empty target (constructor-bypassing the parser) is flagged.
    problems = analyze_step(CallStep(pipeline="", pass_=[], output=None))
    assert problems and "literal pipeline name" in problems[0]
