"""Tier 1: Pipeline DSL parser (IS-3) — string -> exact `Pipeline` dataclass shape.

Covers `reyn.core.pipeline.parser.parse_pipeline_dsl`: the compact DSL (Appendix B
of `docs/proposals/reyn-pipeline-spec-v0.8.md`) narrowed to the linear subset
`PipelineExecutor` can run. Each parse test asserts the EXACT dataclass structure
produced (a public-contract shape: what a `PipelineRegistry` populated from disk
gets handed) — not private parser internals. The required end-to-end test proves
the anti-drift gate: a parsed pipeline (including a tool-arg `!expr` reference)
actually executes correctly through the REAL `PipelineExecutor`, not just parses.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import (
    AgentStep,
    ExprRef,
    Pipeline,
    PipelineExecutor,
    ToolStep,
    TransformStep,
)
from reyn.core.pipeline.parser import PipelineParseError, parse_pipeline_dsl
from reyn.core.pipeline.schema import SchemaRegistry
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session

# ── plain dataclass-shape contract tests ────────────────────────────────────


def test_parses_minimal_linear_pipeline_transform_tool_shell_agent() -> None:
    """Tier 1: a linear DSL text with one of each supported step kind parses
    into the exact `Pipeline`/`Step` dataclass shape the executor consumes,
    including the `!expr` tag producing `ExprRef` for tool/shell args."""
    dsl = """
pipeline: demo
description: a small linear pipeline
steps:
  - transform: {value: "ctx.x + 1", output: bumped}
  - tool: {name: search, args: {query: !expr ctx.bumped, limit: 5}, output: hits}
  - shell: {command: !expr "'echo ' + ctx.bumped", output: shelled}
  - agent: {prompt: "review {ctx.hits}", identity: worker, output: verdict}
"""
    pipeline = parse_pipeline_dsl(dsl, SchemaRegistry())

    assert pipeline == Pipeline(
        description="a small linear pipeline",
        steps=[
            TransformStep(value="ctx.x + 1", output="bumped"),
            ToolStep(
                name="search",
                args={"query": ExprRef("ctx.bumped"), "limit": 5},
                output="hits",
                schema=None,
            ),
            ToolStep(
                name="shell",
                args={"command": ExprRef("'echo ' + ctx.bumped")},
                output="shelled",
                schema=None,
            ),
            AgentStep(
                prompt="review {ctx.hits}", identity="worker", capabilities=None,
                schema=None, output="verdict",
            ),
        ],
    )


def test_description_defaults_to_empty_string_when_omitted() -> None:
    """Tier 1: an omitted pipeline-level `description` parses to `""`, not
    `None` (matches `Pipeline.description`'s dataclass default)."""
    dsl = """
pipeline: no-description
steps:
  - transform: {value: "1 + 1"}
"""
    pipeline = parse_pipeline_dsl(dsl, SchemaRegistry())
    assert pipeline.description == ""


def test_tool_arg_without_expr_tag_is_a_literal_not_an_exprref() -> None:
    """Tier 1: the load-bearing tool-args surface rule — an untagged value,
    even one that LOOKS like a context path, parses as a plain literal, never
    as an `ExprRef`. Only an explicit `!expr` tag produces one."""
    dsl = """
pipeline: literal-args
steps:
  - tool: {name: search, args: {query: "ctx.brief", count: 3, tags: [a, b]}}
"""
    pipeline = parse_pipeline_dsl(dsl, SchemaRegistry())
    step = pipeline.steps[0]
    assert isinstance(step, ToolStep)
    # A bare string that merely LOOKS like a path is a literal string, not
    # resolved — only an explicit `!expr` tag produces an ExprRef.
    assert step.args == {"query": "ctx.brief", "count": 3, "tags": ["a", "b"]}


def test_agent_capabilities_tools_maps_to_flat_list() -> None:
    """Tier 1: Appendix B's `capabilities: {tools: [LIT*]}` shape maps onto
    `AgentStep.capabilities`'s flat `list[str]`."""
    dsl = """
pipeline: caps
steps:
  - agent: {prompt: "go", identity: worker, capabilities: {tools: [read_file, search]}}
"""
    pipeline = parse_pipeline_dsl(dsl, SchemaRegistry())
    step = pipeline.steps[0]
    assert isinstance(step, AgentStep)
    assert step.capabilities == ["read_file", "search"]


def test_schema_document_registers_into_given_registry() -> None:
    """Tier 1: a `schema:` document in the same DSL text registers into the
    caller-supplied `SchemaRegistry`, and a step's `schema: REF` resolves
    against it (name-only reference — same registry the caller later hands
    to `PipelineExecutor.run`)."""
    dsl = """
schema: review
fields:
  verdict: {type: enum, values: [approve, reject], required: true}
  confidence: {type: number, required: true}
---
pipeline: with-schema
steps:
  - agent: {prompt: "review", identity: worker, schema: review, output: review}
"""
    registry = SchemaRegistry()
    pipeline = parse_pipeline_dsl(dsl, registry)

    assert registry.has("review")
    step = pipeline.steps[0]
    assert isinstance(step, AgentStep)
    assert step.schema == "review"


# ── negative tests: unsupported constructs must raise, never silently drop ──


@pytest.mark.parametrize("kind", ["for_each", "parallel", "fold", "match", "call"])
def test_nonlinear_step_kind_raises_naming_the_construct(kind: str) -> None:
    """Tier 1: the HARD CONTRACT — a step kind the linear executor cannot run
    is a `PipelineParseError` naming that kind, never a silently-accepted
    step the executor would later choke on or (worse) ignore."""
    dsl = f"""
pipeline: rejects-nonlinear
steps:
  - {kind}: {{}}
"""
    with pytest.raises(PipelineParseError, match=kind):
        parse_pipeline_dsl(dsl, SchemaRegistry())


@pytest.mark.parametrize("key", ["input", "defaults", "refine"])
def test_unsupported_pipeline_level_key_raises(key: str) -> None:
    """Tier 1: a pipeline-level `input`/`defaults`/`refine` block — real
    Appendix-B grammar the executor has no runtime concept of — is a
    `PipelineParseError` naming the key, not a silently-dropped field."""
    dsl = f"""
pipeline: rejects-metadata
{key}: {{}}
steps:
  - transform: {{value: "1", output: x}}
"""
    with pytest.raises(PipelineParseError, match=key):
        parse_pipeline_dsl(dsl, SchemaRegistry())


def test_agent_budget_field_raises_not_silently_dropped() -> None:
    """Tier 1: `agent.budget` is real Appendix-B grammar `AgentStep` has no
    field for — rejected, not silently dropped."""
    dsl = """
pipeline: rejects-budget
steps:
  - agent: {prompt: "go", identity: worker, budget: {max_turns: 3}}
"""
    with pytest.raises(PipelineParseError, match="budget"):
        parse_pipeline_dsl(dsl, SchemaRegistry())


def test_tool_timeout_field_raises_not_silently_dropped() -> None:
    """Tier 1: `tool.timeout` is real Appendix-B grammar `ToolStep` has no
    field for — rejected, not silently dropped."""
    dsl = """
pipeline: rejects-timeout
steps:
  - tool: {name: search, timeout: 30}
"""
    with pytest.raises(PipelineParseError, match="timeout"):
        parse_pipeline_dsl(dsl, SchemaRegistry())


def test_malformed_transform_expr_raises_at_parse_time() -> None:
    """Tier 1: a malformed R1 expression in `transform.value` fails at DSL
    parse time (`expr.parse` is invoked eagerly), not at executor run time."""
    dsl = """
pipeline: bad-expr
steps:
  - transform: {value: "ctx. + +", output: x}
"""
    with pytest.raises(PipelineParseError):
        parse_pipeline_dsl(dsl, SchemaRegistry())


def test_nested_expr_tag_inside_list_raises() -> None:
    """Tier 1: `!expr` is only honored as the WHOLE value of an args entry —
    one buried inside a nested list is a parse error, not a silently-passed
    inert tag object that would reach `tool_dispatch` unresolved."""
    dsl = """
pipeline: bad-nested-expr
steps:
  - tool: {name: search, args: {tags: [!expr ctx.a, "b"]}}
"""
    with pytest.raises(PipelineParseError, match="nested"):
        parse_pipeline_dsl(dsl, SchemaRegistry())


def test_no_pipeline_document_raises() -> None:
    """Tier 1: a text with only `schema:` documents and no `pipeline:`
    document is a `PipelineParseError`, not an ambiguous `None` return."""
    dsl = """
schema: only_a_schema
fields:
  a: {type: string, required: true}
"""
    with pytest.raises(PipelineParseError, match="exactly one"):
        parse_pipeline_dsl(dsl, SchemaRegistry())


def test_two_pipeline_documents_raises() -> None:
    """Tier 1: a text with more than one `pipeline:` document is a
    `PipelineParseError` (ambiguous "which pipeline did I just parse")."""
    dsl = """
pipeline: one
steps:
  - transform: {value: "1", output: x}
---
pipeline: two
steps:
  - transform: {value: "2", output: y}
"""
    with pytest.raises(PipelineParseError, match="exactly one"):
        parse_pipeline_dsl(dsl, SchemaRegistry())


# ── required end-to-end test: parse -> REAL PipelineExecutor ────────────────


class _ScriptedAgentReply:
    """Always answers with one fixed plain-text turn (no tool_calls) — the
    same real-callable LLM stub the executor's own R5 tests use (NOT
    `unittest.mock`); the LLM is incidental to what this test proves."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def __call__(self, **kwargs) -> LLMToolCallResult:
        self.calls += 1
        return LLMToolCallResult(
            content=self.content, tool_calls=[], finish_reason="stop",
            usage=TokenUsage(),
        )


def _agent_registry(
    tmp_path: Path, state_log: "StateLog", scripted: "_ScriptedAgentReply",
) -> AgentRegistry:
    holder: dict = {}

    def _factory(profile) -> Session:
        s = Session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
        )
        s._loop_driver._loop_observer = (
            lambda loop: setattr(loop, "_llm_caller", scripted)
        )
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    reg.create("worker")
    return reg


@pytest.mark.asyncio
async def test_parsed_pipeline_runs_end_to_end_through_real_executor(
    tmp_path: Path,
) -> None:
    """Tier 2c: parse (Tier 1 concern) + real executor wiring in one test — a
    DSL string with one `transform`, one `tool` (a literal arg AND an
    `!expr`-tagged arg), and one `agent` step parses, then runs through the
    REAL `PipelineExecutor` with a fake `tool_dispatch` — proving a parsed
    `!expr` tool arg actually resolves against the live context at run time,
    not just parses into an `ExprRef`."""
    dsl = """
pipeline: e2e-demo
description: transform -> tool (literal + expr arg) -> agent
steps:
  - transform: {value: "'reyn-' + ctx.topic", output: brief}
  - tool: {name: search, args: {query: !expr ctx.brief, limit: 3}, output: hits}
  - agent: {prompt: "summarize {ctx.hits}", identity: worker, output: verdict}
"""
    pipeline = parse_pipeline_dsl(dsl, SchemaRegistry())

    calls: list[tuple[str, dict]] = []

    def tool_dispatch(name: str, args: dict):
        calls.append((name, args))
        return {"query": args["query"], "limit": args["limit"], "results": ["a", "b"]}

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    scripted = _ScriptedAgentReply("pipelines are cool")
    registry = _agent_registry(tmp_path, state_log, scripted)
    executor = PipelineExecutor()

    result = await executor.run(
        pipeline, {"topic": "dsl"},
        tool_dispatch=tool_dispatch,
        state_log=state_log, run_id="run-is3-e2e",
        registry=registry,
    )

    # The transform step resolved (R1 evaluator).
    assert result.named_stores["brief"] == "reyn-dsl"
    # The tool step's literal arg passed through untouched, and its `!expr`
    # arg resolved against the live context (the parsed ExprRef, not the
    # literal text "ctx.brief") — this is the anti-drift assertion.
    assert calls == [("search", {"query": "reyn-dsl", "limit": 3})]
    assert result.named_stores["hits"] == {
        "query": "reyn-dsl", "limit": 3, "results": ["a", "b"],
    }
    # The agent step ran (scripted reply threaded as pipe data / named output).
    assert result.named_stores["verdict"] == "pipelines are cool"
    assert result.pipe_data == "pipelines are cool"
    assert scripted.calls == 1
