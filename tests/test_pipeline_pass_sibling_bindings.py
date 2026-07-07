"""Tier 2: OS invariant — `call`/`match`'s `pass:` reaches `for_each`/`fold`'s
sibling `item`/`acc` bindings (closes the agent-step asymmetry).

Before this fix, a `for_each`/`fold` `do:` step's evaluation context was
`{"ctx": <outer named stores>, "pipe": <incoming pipe>, "item": <loop item>}`
(`fold` also adds `"acc"`) — `item`/`acc` are SIBLING top-level keys, NOT
inside `ctx`. An `agent` step's `{item}` prompt-template reference already
resolved this (`_interpolate_prompt` evaluates against the FULL context), but
`call`/`match`'s `pass:` handling looked up names in `context["ctx"]` ONLY,
so `pass: [item]` inside a `for_each`/`fold`'s `do: {call: ...}` raised
`PipelineExecutionError` even though the same `item` was directly reachable
from an `agent` step one line away.

The fix (`_resolve_pass_names` in `executor.py`, shared by `call` and
`match`): resolve each `pass:` name against `ctx` (the caller's named
stores) FIRST; if absent, fall back to the sibling step-local bindings
(`item`/`acc`/`pipe`) present in the CURRENT scope's context. `ctx` wins on
a name collision, so every pre-existing `pass:` resolution is byte-identical
— this only ADDS coverage for previously-unreachable names.

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
from reyn.core.pipeline.parser import parse_pipeline_dsl
from reyn.core.pipeline.registry import PipelineRegistry
from reyn.core.pipeline.schema import SchemaRegistry


@pytest.mark.asyncio
async def test_for_each_do_call_pass_item_forwards_the_loop_variable():
    """Tier 2: a `for_each`'s `do: {call: ..., pass: [item]}` genuinely forwards
    the loop item into the callee — `ctx.item` resolves inside the sub-pipeline,
    the exact shape the owner reported as broken (an `agent` step's `{item}` in
    the same position already worked; `call`'s `pass:` did not)."""
    registry = PipelineRegistry()
    registry.register(
        "echo_item",
        Pipeline(steps=[TransformStep(value="ctx.item + '!'", output="out")]),
    )

    dsl = """
pipeline: outer
steps:
  - for_each:
      items: ["a", "b"]
      on_error: abort
      do:
        call:
          pipeline: echo_item
          pass: [item]
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
        run_id="run-for-each-call-pass-item",
        pipeline_registry=registry,
    )

    assert result.named_stores["results"] == ["a!", "b!"]


@pytest.mark.asyncio
async def test_fold_do_call_pass_item_forwards_the_loop_item():
    """Tier 2: a `fold`'s `do: {call: ..., pass: [item]}` forwards the CURRENT
    list item (not the running accumulator) into the callee."""
    registry = PipelineRegistry()
    registry.register(
        "double_item",
        Pipeline(steps=[TransformStep(value="ctx.item * 2", output="out")]),
    )

    dsl = """
pipeline: outer
steps:
  - fold:
      items: [1, 2, 3]
      init: "0"
      do:
        call:
          pipeline: double_item
          pass: [item]
      output: total
"""
    pipeline = parse_pipeline_dsl(dsl, SchemaRegistry())
    result = await PipelineExecutor().run(
        pipeline,
        {},
        tool_dispatch=lambda *_a, **_k: None,
        state_log=None,
        run_id="run-fold-call-pass-item",
        pipeline_registry=registry,
    )

    # do's return value becomes the next acc: 2*1=2, 2*2=4, 2*3=6 -> final acc 6.
    assert result.named_stores["total"] == 6


@pytest.mark.asyncio
async def test_fold_do_call_pass_acc_forwards_the_running_accumulator():
    """Tier 2: a `fold`'s `do: {call: ..., pass: [acc]}` forwards the running
    ACCUMULATOR (not the current item) into the callee — the sibling binding
    to the `item` case above, proving both `item` and `acc` independently
    resolve through the new fallback."""
    registry = PipelineRegistry()
    registry.register(
        "bump_acc",
        Pipeline(steps=[TransformStep(value="ctx.acc + 100", output="out")]),
    )

    dsl = """
pipeline: outer
steps:
  - fold:
      items: [1, 2]
      init: "0"
      do:
        call:
          pipeline: bump_acc
          pass: [acc]
      output: total
"""
    pipeline = parse_pipeline_dsl(dsl, SchemaRegistry())
    result = await PipelineExecutor().run(
        pipeline,
        {},
        tool_dispatch=lambda *_a, **_k: None,
        state_log=None,
        run_id="run-fold-call-pass-acc",
        pipeline_registry=registry,
    )

    # iteration 1: acc=0 -> callee returns 0+100=100 (new acc).
    # iteration 2: acc=100 -> callee returns 100+100=200 (new acc, final).
    assert result.named_stores["total"] == 200


@pytest.mark.asyncio
async def test_for_each_do_match_pass_item_forwards_the_loop_variable():
    """Tier 2: `match`'s `pass:` gets the SAME sibling-binding fallback as
    `call` (both share `_resolve_pass_names`) — a `for_each.do: {match: ...,
    pass: [item]}` forwards the loop item into the selected case's callee."""
    registry = PipelineRegistry()
    registry.register(
        "shout_item",
        Pipeline(steps=[TransformStep(value="ctx.item + '!!!'", output="out")]),
    )
    registry.register(
        "whisper_item",
        Pipeline(steps=[TransformStep(value="ctx.item + '...'", output="out")]),
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
              pipeline: shout_item
              pass: [item]
            quiet:
              pipeline: whisper_item
              pass: [item]
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
        run_id="run-for-each-match-pass-item",
        pipeline_registry=registry,
    )

    assert result.named_stores["results"] == ["Hi!!!", "Yo!!!"]


@pytest.mark.asyncio
async def test_ctx_named_store_wins_over_sibling_loop_binding_on_name_collision():
    """Tier 2: backward-compat regression — when a caller's named store is
    ALSO literally called `item` (shadowing the `for_each`/`fold` loop
    binding of the same name), `pass: [item]` must resolve the `ctx` store,
    not the sibling loop variable. This is the precedence rule that keeps
    every pre-existing `pass:` resolution byte-identical: the sibling
    fallback only reaches names ctx does NOT already have."""
    registry = PipelineRegistry()
    registry.register(
        "echo_item",
        Pipeline(steps=[TransformStep(value="ctx.item", output="out")]),
    )

    dsl = """
pipeline: outer
steps:
  - for_each:
      items: ["loop-a", "loop-b"]
      on_error: abort
      do:
        call:
          pipeline: echo_item
          pass: [item]
      collect:
        transform:
          value: pipe
      output: results
"""
    pipeline = parse_pipeline_dsl(dsl, SchemaRegistry())
    # The caller's named store `item` shadows the for_each loop variable of
    # the same name — `ctx["item"]` must win over the sibling `item` binding.
    result = await PipelineExecutor().run(
        pipeline,
        {"item": "outer-named-store-value"},
        tool_dispatch=lambda *_a, **_k: None,
        state_log=None,
        run_id="run-ctx-wins-collision",
        pipeline_registry=registry,
    )

    assert result.named_stores["results"] == [
        "outer-named-store-value",
        "outer-named-store-value",
    ]


@pytest.mark.asyncio
async def test_pass_name_absent_from_ctx_and_siblings_still_raises():
    """Tier 2: a `pass:` name that is genuinely absent from BOTH the named
    stores and the sibling loop bindings still fails the step cleanly (the
    fallback only WIDENS what resolves — it never turns a real miss into a
    silent None)."""
    registry = PipelineRegistry()
    registry.register(
        "leaky",
        Pipeline(steps=[TransformStep(value="ctx.nope", output="out")]),
    )

    dsl = """
pipeline: outer
steps:
  - for_each:
      items: ["a"]
      on_error: abort
      do:
        call:
          pipeline: leaky
          pass: [nope]
      collect:
        transform:
          value: pipe
      output: results
"""
    pipeline = parse_pipeline_dsl(dsl, SchemaRegistry())
    with pytest.raises(PipelineExecutionError, match="nope"):
        await PipelineExecutor().run(
            pipeline,
            {},
            tool_dispatch=lambda *_a, **_k: None,
            state_log=None,
            run_id="run-pass-not-found",
            pipeline_registry=registry,
        )
