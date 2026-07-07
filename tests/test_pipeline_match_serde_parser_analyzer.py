"""Tier 1: `match` primitive contract — serde round-trip, DSL parse, analyzer facet.

The `match` step (R7, `call`'s runtime-selected sibling) plugs into the three
sibling dispatch tables the foundation established: serde's
``ENCODERS``/``DECODERS`` (work-order persistence — the ``pass_`` field ⇄
wire key ``"pass"`` split on each nested case), the parser's ``_STEP_PARSERS``
(Appendix B ``match = {on, cases, default?, output?}``), and the analyzer's
``ANALYZER_FACETS`` (P4 — path enumeration over every case target). This pins
each contract with NON-DEFAULT values.

Real YAML parse + real JSON round-trip; no mocks.
"""
from __future__ import annotations

import json

import pytest

from reyn.core.pipeline.analyzer import analyze_step
from reyn.core.pipeline.executor import MatchCase, MatchStep, Pipeline, ToolStep
from reyn.core.pipeline.parser import PipelineParseError, parse_pipeline_dsl
from reyn.core.pipeline.schema import SchemaRegistry
from reyn.core.pipeline.serde import pipeline_from_dict, pipeline_to_dict


def test_match_serde_round_trip_non_default_values():
    """Tier 1: a ``MatchStep`` with multiple cases + a ``default`` + non-default
    ``pass_``/``output`` round-trips dataclass -> dict -> JSON -> dataclass, and
    the wire form uses the Appendix-B key ``"pass"`` on each nested case (not
    the Python field name ``pass_``)."""
    pipeline = Pipeline(steps=[
        MatchStep(
            on="ctx.kind",
            cases={
                "cat": MatchCase(pipeline="on-cat", pass_=[("brief", "ctx.brief")]),
                "dog": MatchCase(
                    pipeline="on-dog",
                    pass_=[("brief", "ctx.brief"), ("budget", "ctx.budget")],
                ),
            },
            default=MatchCase(pipeline="on-other", pass_=[]),
            output="result",
        ),
        ToolStep(name="noop", args={}),
    ])
    wire = pipeline_to_dict(pipeline)
    match_dict = wire["steps"][0]
    assert match_dict == {
        "kind": "match",
        "on": "ctx.kind",
        "cases": {
            "cat": {"pipeline": "on-cat", "pass": [["brief", "ctx.brief"]]},
            "dog": {
                "pipeline": "on-dog",
                "pass": [["brief", "ctx.brief"], ["budget", "ctx.budget"]],
            },
        },
        "default": {"pipeline": "on-other", "pass": []},
        "output": "result",
    }
    assert "pass_" not in json.dumps(match_dict)  # wire key is Appendix-B `pass`
    assert pipeline_from_dict(json.loads(json.dumps(wire))) == pipeline


def test_match_serde_round_trip_no_default():
    """Tier 1: ``default=None`` round-trips as a null wire value, not a key
    that decodes into a spurious ``MatchCase``."""
    pipeline = Pipeline(steps=[
        MatchStep(on="ctx.k", cases={"a": MatchCase(pipeline="p", pass_=[])}),
    ])
    wire = json.loads(json.dumps(pipeline_to_dict(pipeline)))
    assert wire["steps"][0]["default"] is None
    assert pipeline_from_dict(wire) == pipeline


def test_match_parses_from_dsl():
    """Tier 1: the DSL ``match`` step parses to a ``MatchStep`` (moved out of
    the not-yet-supported set) with ``cases``/``default``/``output`` honored."""
    text = """
pipeline: outer
steps:
  - match:
      on: ctx.kind
      cases:
        cat:
          pipeline: on-cat
          pass:
            - brief: ctx.brief
        dog:
          pipeline: on-dog
      default:
        pipeline: on-other
      output: summary
"""
    parsed = parse_pipeline_dsl(text, SchemaRegistry())
    assert parsed.steps == [
        MatchStep(
            on="ctx.kind",
            cases={
                "cat": MatchCase(pipeline="on-cat", pass_=[("brief", "ctx.brief")]),
                "dog": MatchCase(pipeline="on-dog", pass_=[]),
            },
            default=MatchCase(pipeline="on-other", pass_=[]),
            output="summary",
        )
    ]


def test_match_dsl_requires_non_empty_cases():
    """Tier 1: a ``match`` with no ``cases`` at all is a parse error, not a
    step that silently always falls through to ``default``/failure."""
    text = "pipeline: o\nsteps:\n  - match:\n      on: ctx.kind\n      cases: {}\n"
    with pytest.raises(PipelineParseError):
        parse_pipeline_dsl(text, SchemaRegistry())


def test_match_dsl_case_requires_literal_pipeline_name():
    """Tier 1: an empty/missing ``pipeline`` name inside a case is a parse
    error (Hard rule 2 — every case target is a static literal)."""
    text = (
        "pipeline: o\nsteps:\n  - match:\n      on: ctx.kind\n      "
        "cases:\n        a:\n          pass:\n            - x: ctx.x\n"
    )
    with pytest.raises(PipelineParseError):
        parse_pipeline_dsl(text, SchemaRegistry())


def test_match_dsl_rejects_malformed_on_expression():
    """Tier 1: a malformed ``on`` R1 expression fails at parse time (same
    contract as ``transform.value``), not deep in the executor."""
    text = (
        "pipeline: o\nsteps:\n  - match:\n      on: 'ctx. .bad'\n      "
        "cases:\n        a:\n          pipeline: p\n"
    )
    with pytest.raises(PipelineParseError):
        parse_pipeline_dsl(text, SchemaRegistry())


def test_match_analyzer_facet_registered_and_enumerates_case_targets():
    """Tier 1: the P4 analyzer facet for ``match`` is registered, passes valid
    literal case/default targets, and flags a pathological empty target
    (constructor-bypassing the parser) — the path-enumeration teeth ``match``
    contributes beyond ``call``'s placeholder facet."""
    ok_step = MatchStep(
        on="ctx.kind",
        cases={
            "a": MatchCase(pipeline="pa", pass_=[]),
            "b": MatchCase(pipeline="pb", pass_=[]),
        },
        default=MatchCase(pipeline="pd", pass_=[]),
    )
    assert analyze_step(ok_step) == []

    bad_step = MatchStep(
        on="ctx.kind",
        cases={"a": MatchCase(pipeline="", pass_=[])},
        default=MatchCase(pipeline="", pass_=[]),
    )
    problems = analyze_step(bad_step)
    assert any("'a'" in p and "literal pipeline name" in p for p in problems)
    assert any("'default'" in p and "literal pipeline name" in p for p in problems)


def test_match_analyzer_facet_flags_empty_cases():
    """Tier 1: a hand-built ``MatchStep`` with an empty ``cases`` mapping
    (the parser already refuses this at DSL parse time — this pins the
    facet's own defensive check for a round-tripped/hand-built instance)."""
    problems = analyze_step(MatchStep(on="ctx.k", cases={}))
    assert problems and "no cases" in problems[0]
