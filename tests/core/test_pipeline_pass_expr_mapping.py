"""Tier 2: OS invariant — `call`/`match`'s `pass:` is a NAME -> R1-EXPRESSION
mapping, not a bare-NAME list.

This supersedes the earlier bespoke `_resolve_pass_names` mechanism (ctx-then-
sibling-item/acc/pipe fallback, ctx-wins-on-collision). That mechanism invented
a NEW lookup rule the DSL didn't otherwise have. The redesign instead reuses
the DSL's EXISTING general mechanism for "evaluate something against the
current ctx/pipe/item/acc context": R1 expressions — the same ones
`transform.value`/`agent.prompt`/`match.on` already use.

`pass:` is a flat mapping `{NAME: EXPR, ...}` — `EXPR` is an R1 expression
source evaluated against the CALLER's full current context (`ctx`/`pipe`/
`item`/`acc`, whatever is in scope) at call time, and the result is bound to
`NAME` in the callee's isolated `ctx`. There is no bare-NAME shorthand: every
entry states its own expression explicitly. Each NAME has no
ordering-dependent semantics (unlike `steps:`, a genuine sequence), so the
mapping is a single flat `{NAME: EXPR}` dict rather than a list of
single-key wrappers.

Real `parse_pipeline_dsl` + `PipelineExecutor` + `PipelineRegistry` throughout
(mirrors the `parse_json` end-to-end style in `test_pipeline_executor_r3_r4.py`)
— no mocks, no private-state assertions.
"""
from __future__ import annotations

import pytest

from reyn.core.pipeline.executor import (
    Pipeline,
    PipelineExecutionError,
    PipelineExecutor,
    TransformStep,
)
from reyn.core.pipeline.parser import PipelineParseError, parse_pipeline_dsl
from reyn.core.pipeline.registry import PipelineRegistry
from reyn.core.pipeline.schema import SchemaRegistry


@pytest.mark.asyncio
async def test_for_each_do_call_pass_current_item_forwards_the_loop_variable():
    """Tier 2: a `for_each`'s `do: {call: ..., pass: {current: item}}`
    forwards the loop item into the callee under the explicit name `current`
    — `ctx.current` resolves inside the sub-pipeline, the original owner ask
    (an `agent` step's `{item}` in the same position already worked; `call`'s
    `pass:` previously could not reach `item` at all)."""
    registry = PipelineRegistry()
    registry.register(
        "echo_current",
        Pipeline(steps=[TransformStep(value="ctx.current + '!'", output="out")]),
    )

    dsl = """
pipeline: outer
steps:
  - for_each:
      items: ["a", "b"]
      on_error: abort
      do:
        call:
          pipeline: echo_current
          pass:
            current: item
      collect:
        transform:
          value: pipe
      output: results
"""
    pipeline = parse_pipeline_dsl(dsl, SchemaRegistry())
    result = await PipelineExecutor().run(
        pipeline,
        {},
        tool_dispatch=lambda *_a, **_k: None,
        state_log=None,
        run_id="run-for-each-call-pass-current",
        pipeline_registry=registry,
    )

    assert result.named_stores["results"] == ["a!", "b!"]


@pytest.mark.asyncio
async def test_fold_do_call_pass_running_forwards_the_accumulator():
    """Tier 2: a `fold`'s `do: {call: ..., pass: {running: acc}}` forwards
    the running ACCUMULATOR under the explicit name `running`."""
    registry = PipelineRegistry()
    registry.register(
        "bump_running",
        Pipeline(steps=[TransformStep(value="ctx.running + 100", output="out")]),
    )

    dsl = """
pipeline: outer
steps:
  - fold:
      items: [1, 2]
      init: "0"
      do:
        call:
          pipeline: bump_running
          pass:
            running: acc
      output: total
"""
    pipeline = parse_pipeline_dsl(dsl, SchemaRegistry())
    result = await PipelineExecutor().run(
        pipeline,
        {},
        tool_dispatch=lambda *_a, **_k: None,
        state_log=None,
        run_id="run-fold-call-pass-running",
        pipeline_registry=registry,
    )

    # iteration 1: acc=0 -> callee returns 0+100=100 (new acc).
    # iteration 2: acc=100 -> callee returns 100+100=200 (new acc, final).
    assert result.named_stores["total"] == 200


@pytest.mark.asyncio
async def test_pass_entry_is_a_genuine_computed_expression_not_just_an_alias():
    """Tier 2: a `pass:` entry's value is a FULL R1 expression, not merely a
    rename — `pass: {doubled: "item * 2"}` computes a value the old
    bare-NAME mechanism could never produce (it could only forward a NAME
    verbatim). This is the actual payoff of reusing the expression
    evaluator instead of a bespoke name-lookup mechanism."""
    registry = PipelineRegistry()
    registry.register(
        "echo_doubled",
        Pipeline(steps=[TransformStep(value="ctx.doubled", output="out")]),
    )

    dsl = """
pipeline: outer
steps:
  - for_each:
      items: [1, 2, 3]
      on_error: abort
      do:
        call:
          pipeline: echo_doubled
          pass:
            doubled: "item * 2"
      collect:
        transform:
          value: pipe
      output: results
"""
    pipeline = parse_pipeline_dsl(dsl, SchemaRegistry())
    result = await PipelineExecutor().run(
        pipeline,
        {},
        tool_dispatch=lambda *_a, **_k: None,
        state_log=None,
        run_id="run-for-each-call-pass-computed",
        pipeline_registry=registry,
    )

    assert result.named_stores["results"] == [2, 4, 6]


@pytest.mark.asyncio
async def test_pass_entry_with_missing_path_raises_pipeline_execution_error():
    """Tier 2: an invalid/missing-path expression in a `pass:` entry raises
    `PipelineExecutionError` naming the failing entry, cleanly wrapping the
    underlying expression-evaluation error the same way other steps already
    wrap `ExprEvalError` — no new error-message design needed."""
    registry = PipelineRegistry()
    registry.register(
        "leaky",
        Pipeline(steps=[TransformStep(value="ctx.x", output="out")]),
    )

    dsl = """
pipeline: outer
steps:
  - call:
      pipeline: leaky
      pass:
        x: ctx.nonexistent
"""
    pipeline = parse_pipeline_dsl(dsl, SchemaRegistry())
    with pytest.raises(PipelineExecutionError, match="x"):
        await PipelineExecutor().run(
            pipeline,
            {},
            tool_dispatch=lambda *_a, **_k: None,
            state_log=None,
            run_id="run-pass-missing-path",
            pipeline_registry=registry,
        )


@pytest.mark.asyncio
async def test_match_case_pass_entry_is_also_a_computed_expression():
    """Tier 2: `match`'s `pass:` (not just `call`'s) goes through the SAME
    `_eval_pass_entries` helper — a selected case's `pass:` entry can be a
    computed expression too, mirroring the `call` coverage above."""
    registry = PipelineRegistry()
    registry.register(
        "shout_loud",
        Pipeline(steps=[TransformStep(value="ctx.label", output="out")]),
    )
    registry.register(
        "whisper_quiet",
        Pipeline(steps=[TransformStep(value="ctx.label", output="out")]),
    )

    dsl = """
pipeline: outer
steps:
  - for_each:
      items: ["Hi", "Yo"]
      on_error: abort
      do:
        match:
          on: "'loud'"
          cases:
            loud:
              pipeline: shout_loud
              pass:
                label: "'shout-' + item"
            quiet:
              pipeline: whisper_quiet
              pass:
                label: "'whisper-' + item"
      collect:
        transform:
          value: pipe
      output: results
"""
    pipeline = parse_pipeline_dsl(dsl, SchemaRegistry())
    result = await PipelineExecutor().run(
        pipeline,
        {},
        tool_dispatch=lambda *_a, **_k: None,
        state_log=None,
        run_id="run-for-each-match-pass-computed",
        pipeline_registry=registry,
    )

    assert result.named_stores["results"] == ["shout-Hi", "shout-Yo"]


def test_pass_as_a_list_of_single_key_mappings_is_a_parse_error():
    """Tier 1: `pass:` must be a flat `{NAME: EXPR}` mapping — the superseded
    list-of-single-key-mappings surface (`pass: [{name: expr}]`) is now
    rejected at PARSE time with a clear error, not silently accepted."""
    dsl = """
pipeline: outer
steps:
  - call:
      pipeline: leaky
      pass:
        - x: ctx.nonexistent
"""
    with pytest.raises(PipelineParseError, match="pass"):
        parse_pipeline_dsl(dsl, SchemaRegistry())


def test_pass_as_a_bare_string_is_a_parse_error():
    """Tier 1: a non-mapping `pass:` (e.g. a bare string) is a clear parse
    error, not a runtime surprise."""
    dsl = """
pipeline: outer
steps:
  - call:
      pipeline: leaky
      pass: "not a mapping"
"""
    with pytest.raises(PipelineParseError, match="pass"):
        parse_pipeline_dsl(dsl, SchemaRegistry())
