"""Tier 2c: pipeline-R5 — AgentStep wired into PipelineExecutor.

Covers the slice ``docs/proposals/reyn-pipeline-v0.9-design-resolutions.md`` R5
describes as the eventual wiring: an :class:`~reyn.core.pipeline.executor.AgentStep`
composed into the linear executor's step loop, threading its result through R3
(pipe-data / named-store) exactly like ``transform``/``tool`` steps, and covered by
R4's step-boundary exactly-once replay on resume.

Same real-collaborator discipline as ``test_pipeline_r5_run_agent_step.py``: a REAL
``AgentRegistry``/``Session``/``MessageBus`` throughout; the ONLY faked collaborator
is the LLM completion call, injected via the real ``RouterLoopDriver._loop_observer``
seam with a concrete typed ``_ScriptedAgentReply`` (NOT ``unittest.mock``). See that
file's module docstring for the full Tier-2c rationale (LLM content is incidental to
what's under test — the run+collect *composition* into the executor).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import (
    AgentStep,
    ExprRef,
    Pipeline,
    PipelineExecutionError,
    PipelineExecutor,
    ToolStep,
    TransformStep,
)
from reyn.core.pipeline.schema import SchemaRegistry
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session

# ── real-callable LLM stub (Tier 2c: LLM is incidental — see module docstring) ──


class _ScriptedAgentReply:
    """Always answers with one fixed plain-text/JSON turn (no tool_calls)."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        self.calls += 1
        return LLMToolCallResult(
            content=self.content, tool_calls=[], finish_reason="stop",
            usage=TokenUsage(),
        )


def _registry(
    tmp_path: Path, state_log: "StateLog", scripted: "_ScriptedAgentReply | None",
) -> AgentRegistry:
    """Real AgentRegistry + real Session factory (mirrors
    ``test_pipeline_r5_run_agent_step.py``'s ``holder`` deferred-registry-ref trick).
    ``scripted`` (when given) wires the fixed LLM reply into every spawned session's
    real ``RouterLoopDriver`` via ``_loop_observer``, before the driver's first turn.

    0062: resolves the default ``"standard"`` class to a real litellm-known
    model (see ``test_pipeline_r5_run_agent_step.py``'s ``_registry`` for the
    same fix + rationale — a schema-bearing agent step now runs RouterLoop's
    model-support pre-check even though the turn's actual completion stays
    fully scripted via ``_llm_caller``)."""
    from reyn.llm.model_resolver import ModelResolver

    holder: dict = {}
    resolver = ModelResolver({"standard": "gemini/gemini-2.5-flash-lite"})

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        s = Session(
            presentation_consumer=presentation_consumer,
            intervention_bridge=intervention_bridge,
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            resolver=resolver,
        )
        if scripted is not None:
            s._loop_driver._loop_observer = (
                lambda loop: setattr(loop, "_llm_caller", scripted)
            )
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    reg.create("worker")
    return reg


# ── agent step in a linear pipeline (R3 threading) ──────────────────────────


@pytest.mark.asyncio
async def test_agent_step_in_linear_pipeline_threads_pipe_data_and_named_store(
    tmp_path: Path,
) -> None:
    """Tier 2c: transform -> agent -> transform. The agent step's scripted reply
    flows as pipe-data (R3) into the next transform AND into its named store, read
    by the downstream transform via ``ctx.NAME``."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    scripted = _ScriptedAgentReply("the leaf worker's answer")
    reg = _registry(tmp_path, state_log, scripted)

    pipeline = Pipeline(
        steps=[
            TransformStep(value="'topic: ' + ctx.topic", output="brief"),
            AgentStep(prompt="{ctx.brief}", identity="worker", output="verdict"),
            TransformStep(value="ctx.verdict + ' (reviewed)'", output="final"),
        ]
    )
    executor = PipelineExecutor()

    result = await executor.run(
        pipeline, {"topic": "reyn pipelines"},
        tool_dispatch=lambda *_a, **_k: None,
        state_log=state_log, run_id="run-agent-linear",
        registry=reg,
    )

    assert result.named_stores["verdict"] == "the leaf worker's answer"
    assert result.pipe_data == "the leaf worker's answer (reviewed)"
    assert scripted.calls == 1


# ── prompt interpolation ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_step_prompt_interpolates_ctx_before_running(tmp_path: Path) -> None:
    """Tier 2c: ``{ctx.NAME}`` in the prompt resolves to the store value BEFORE the
    agent runs — proven by having the scripted reply echo the resolved prompt text
    it was called with is not directly observable, so instead assert via a
    ``ctx.NAME.field`` dotted reference resolving correctly against a nested store,
    which would raise (not silently pass a literal `{ctx...}` string) if
    interpolation were skipped."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    scripted = _ScriptedAgentReply("ack")
    reg = _registry(tmp_path, state_log, scripted)

    pipeline = Pipeline(
        steps=[
            TransformStep(value="{summary: 'nested value', priority: 3}", output="brief"),
            AgentStep(
                prompt="please review: {ctx.brief.summary} (priority {ctx.brief.priority})",
                identity="worker",
                output="verdict",
            ),
        ]
    )
    executor = PipelineExecutor()

    result = await executor.run(
        pipeline, None,
        tool_dispatch=lambda *_a, **_k: None,
        state_log=state_log, run_id="run-agent-interp",
        registry=reg,
    )

    assert result.pipe_data == "ack"

    # A missing dotted path in the prompt raises a clear PipelineExecutionError
    # (proves the reference is actually resolved, not passed through literally).
    bad_pipeline = Pipeline(
        steps=[AgentStep(prompt="{ctx.does_not_exist}", identity="worker")]
    )
    with pytest.raises(PipelineExecutionError):
        await executor.run(
            bad_pipeline, None,
            tool_dispatch=lambda *_a, **_k: None,
            state_log=state_log, run_id="run-agent-interp-missing",
            registry=reg,
        )


# ── structured output ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_step_schema_success_threads_parsed_value(tmp_path: Path) -> None:
    """Tier 2c: an agent step with ``schema`` set threads the parsed/validated
    value (not raw text) as pipe data / named output."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    scripted = _ScriptedAgentReply('{"verdict": "approve", "confidence": 0.9}')
    reg = _registry(tmp_path, state_log, scripted)
    schema_registry = SchemaRegistry()
    schema_registry.register("review", {
        "fields": {
            "verdict": {"type": "enum", "values": ["approve", "reject"], "required": True},
            "confidence": {"type": "number", "required": True},
        },
    })

    pipeline = Pipeline(
        steps=[
            AgentStep(
                prompt="review this", identity="worker", schema="review", output="review",
            ),
            TransformStep(value="ctx.review.verdict", output="final_verdict"),
        ]
    )
    executor = PipelineExecutor()

    result = await executor.run(
        pipeline, None,
        tool_dispatch=lambda *_a, **_k: None,
        state_log=state_log, run_id="run-agent-schema-ok",
        registry=reg, schema_registry=schema_registry,
    )

    assert result.named_stores["review"] == {"verdict": "approve", "confidence": 0.9}
    assert result.pipe_data == "approve"


@pytest.mark.asyncio
async def test_agent_step_schema_nonconforming_fails_step(tmp_path: Path) -> None:
    """Tier 2c: a non-conforming reply fails the step (``PipelineExecutionError``,
    wrapping the underlying ``AgentStepError``) — same failure path a bad tool-step
    schema takes, not a silently-accepted partial value."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    scripted = _ScriptedAgentReply("Sure — looks good to me!")
    reg = _registry(tmp_path, state_log, scripted)
    schema_registry = SchemaRegistry()
    schema_registry.register("review", {
        "fields": {"verdict": {"type": "string", "required": True}},
    })

    pipeline = Pipeline(
        steps=[AgentStep(prompt="review this", identity="worker", schema="review")]
    )
    executor = PipelineExecutor()

    with pytest.raises(PipelineExecutionError):
        await executor.run(
            pipeline, None,
            tool_dispatch=lambda *_a, **_k: None,
            state_log=state_log, run_id="run-agent-schema-bad",
            registry=reg, schema_registry=schema_registry,
        )


# ── R4 recovery: exactly-once replay of a completed agent step ──────────────


@pytest.mark.asyncio
async def test_resume_replays_completed_agent_step_without_rerunning_llm_turn(
    tmp_path: Path,
) -> None:
    """Tier 2c: MANDATORY CLAUDE.md recovery gate + R5's exactly-once proof. Run a
    pipeline through a completed ``AgentStep``, truncate the WAL below its recorded
    generation seq, `resume` -> the agent step must NOT re-run (its result replays
    from the step-boundary snapshot, proven via the scripted LLM's call count),
    and execution resumes at the step after it with the correct threaded state."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    scripted = _ScriptedAgentReply("the one true answer")
    reg = _registry(tmp_path, state_log, scripted)

    step0 = TransformStep(value="'seed: ' + ctx.seed", output="brief")
    step1 = AgentStep(prompt="{ctx.brief}", identity="worker", output="verdict")
    step2 = TransformStep(value="ctx.verdict + ' (final)'", output="done")

    executor = PipelineExecutor()
    phase1 = Pipeline(steps=[step0, step1])  # run through the agent step (K=1)
    phase1_result = await executor.run(
        phase1, {"seed": "abc"},
        tool_dispatch=lambda *_a, **_k: None,
        state_log=state_log, run_id="run-agent-truncate",
        registry=reg,
    )
    await state_log.flush()
    assert phase1_result.step_index == 2
    assert scripted.calls == 1

    seq_after_phase1 = state_log.current_seq
    assert seq_after_phase1 >= 1

    # WAL head climbs well past the agent step's key seq (other activity).
    for i in range(50):
        await state_log.append("inbox_put", n=100 + i)

    await state_log.truncate_below(40)
    await state_log.flush()
    stats = state_log.last_truncate_stats
    assert stats["dropped"] >= 1, "the agent step's own WAL entries must be truncated"
    surviving_seqs = {e["seq"] for e in state_log.iter_from(0)}
    assert seq_after_phase1 not in surviving_seqs, (
        "the agent step's own WAL entries are gone — the generation FILE, not a "
        "WAL event, must be what resume reconstructs from"
    )

    full_pipeline = Pipeline(steps=[step0, step1, step2])
    resumed = await executor.resume(
        "run-agent-truncate", pipeline=full_pipeline,
        tool_dispatch=lambda *_a, **_k: None,
        state_log=state_log, registry=reg,
    )

    assert scripted.calls == 1, (
        "the agent step must NOT be re-run on resume (exactly-once) — the LLM "
        "was called exactly once total, before AND after truncate+resume"
    )
    assert resumed.step_index == 3
    assert resumed.named_stores["brief"] == "seed: abc"
    assert resumed.named_stores["verdict"] == "the one true answer"
    assert resumed.named_stores["done"] == "the one true answer (final)"
    assert resumed.pipe_data == "the one true answer (final)"


# ── structural guardrails ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_step_without_identity_or_default_fails_structurally(
    tmp_path: Path,
) -> None:
    """Tier 2: an AgentStep with no ``identity`` and no ``default_identity`` given
    to ``run`` fails with a clear PipelineExecutionError (not a bare AttributeError
    / silently picking an arbitrary identity)."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _registry(tmp_path, state_log, _ScriptedAgentReply("x"))
    pipeline = Pipeline(steps=[AgentStep(prompt="hi")])
    executor = PipelineExecutor()

    with pytest.raises(PipelineExecutionError):
        await executor.run(
            pipeline, None,
            tool_dispatch=lambda *_a, **_k: None,
            state_log=state_log, run_id="run-agent-no-identity",
            registry=reg,
        )


@pytest.mark.asyncio
async def test_agent_step_without_registry_fails_structurally(tmp_path: Path) -> None:
    """Tier 2: an AgentStep in a pipeline run with no ``registry`` fails clearly
    rather than crashing on a ``None`` call — the guard the executor must add since
    ``registry`` is optional for transform/tool-only pipelines."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    pipeline = Pipeline(steps=[AgentStep(prompt="hi", identity="worker")])
    executor = PipelineExecutor()

    with pytest.raises(PipelineExecutionError):
        await executor.run(
            pipeline, None,
            tool_dispatch=lambda *_a, **_k: None,
            state_log=state_log, run_id="run-agent-no-registry",
        )


# ── pre-existing byte-identical behavior when no agent step is present ──────


@pytest.mark.asyncio
async def test_no_agent_step_pipeline_unaffected_by_new_params(tmp_path: Path) -> None:
    """Tier 2: a transform/tool-only pipeline run with no ``registry``/
    ``default_identity`` behaves exactly as before (R3 threading untouched)."""
    def _shout(_name: str, args: dict) -> str:
        return args["text"].upper()

    pipeline = Pipeline(
        steps=[
            TransformStep(value="'hi ' + ctx.name", output="greeting"),
            ToolStep(name="shout", args={"text": ExprRef("pipe")}, output="shouted"),
        ]
    )
    executor = PipelineExecutor()
    result = await executor.run(
        pipeline, {"name": "world"},
        tool_dispatch=_shout, state_log=None, run_id="run-no-agent",
    )

    # #2425 PR-2: a str tool result maps to the flat {"text": ...} ctx shape.
    assert result.pipe_data == {"text": "HI WORLD"}
    assert result.named_stores == {
        "name": "world", "greeting": "hi world", "shouted": {"text": "HI WORLD"},
    }
