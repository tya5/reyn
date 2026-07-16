"""Tier 2c: 0062 structured output — schema constrains generation
(response_format) + AgentStep.model.

Same harness as ``test_pipeline_r5_run_agent_step.py`` (real ``AgentRegistry``
/ ``Session`` / ``RouterLoop`` / ``MessageBus`` throughout; the LLM completion
is faked via a concrete real-callable stub injected through ``RouterLoop``'s
designed ``_llm_caller`` Tier-2 test seam — never ``unittest.mock``). These
tests exercise the NEW behavior 0062 adds: a set ``schema`` now drives
provider-side ``response_format`` on the ephemeral session's answer turn
(``RouterLoop._run_structured_answer_turn``), not just post-hoc validation,
and the three DISTINCT failure modes (model-unsupported / provider-rejected-
schema / generation-non-conforming) are never conflated.

``_SequencedAgentReply`` is a concrete class with a typed ``__call__`` (NOT
``unittest.mock.MagicMock``/``AsyncMock``/``patch``) that plays back a fixed
script of responses (content strings or exceptions to raise) and records
every call's ``model``/``response_format``/``tools`` kwargs — the record is
the primary evidence for the "no wasted call" / "no re-prompt loop" /
"model selected" assertions below.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.schema import SchemaRegistry
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.model_resolver import ModelResolver
from reyn.llm.pricing import TokenUsage
from reyn.runtime.errors import (
    StructuredOutputNonConformingError,
    StructuredOutputSchemaRejectedError,
    StructuredOutputUnsupportedModelError,
)
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_api import run_agent_step

_REVIEW_SCHEMA = {
    "fields": {
        "verdict": {"type": "enum", "values": ["approve", "reject"], "required": True},
        "confidence": {"type": "number", "required": True},
    },
}


class _SequencedAgentReply:
    """Plays back ``script`` (one entry consumed per call): a ``str`` returns
    that content (no tool_calls, finish_reason=stop); an ``Exception``
    instance is raised instead. Records every call's ``model`` /
    ``response_format`` / ``tools`` kwargs in ``.calls`` — the primary
    evidence the tests below assert on (call COUNT for the re-prompt-bound /
    no-wasted-call claims, and kwarg CONTENT for the model-selection claim)."""

    def __init__(self, script: "list[Any]") -> None:
        self.script = list(script)
        self.calls: "list[dict[str, Any]]" = []

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        self.calls.append({
            "model": kwargs.get("model"),
            "response_format": kwargs.get("response_format"),
            "tools": kwargs.get("tools"),
        })
        item = self.script[len(self.calls) - 1]
        if isinstance(item, Exception):
            raise item
        return LLMToolCallResult(
            content=item, tool_calls=[], finish_reason="stop", usage=TokenUsage(),
        )


def _registry(
    tmp_path: Path, scripted: "_SequencedAgentReply | None", *, resolver: "ModelResolver | None" = None,
) -> AgentRegistry:
    """Mirrors ``test_pipeline_r5_run_agent_step.py``'s ``_registry`` — real
    ``AgentRegistry`` + real ``Session`` factory, scripted LLM wired via the
    ``_loop_observer`` Tier-2 seam. ``resolver`` defaults to a ``standard`` ->
    a REAL litellm-known model (``gemini/gemini-2.5-flash-lite`` — litellm's
    static ``supports_response_schema`` table recognizes it with NO network
    call) so the 0062 pre-check passes for tests that aren't specifically
    exercising the unsupported-model failure mode."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    holder: dict = {}
    _resolver = resolver or ModelResolver({
        "standard": "gemini/gemini-2.5-flash-lite",
        "strong": "gemini/gemini-2.5-pro",
    })

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        s = Session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            presentation_consumer=presentation_consumer,
            intervention_bridge=intervention_bridge,
            resolver=_resolver,
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


# ── success: response_format-constrained JSON parses + validates + binds ────


@pytest.mark.asyncio
async def test_schema_success_constrains_generation_and_binds_output(tmp_path: Path) -> None:
    """Tier 2c: a response_format-supporting model + a conforming reply →
    the constrained call's JSON parses, validates, and is returned as the
    PARSED dict (proving run_agent_step's post-hoc parse+validate still runs
    even though generation was already provider-constrained — 0062 impl-focus
    3, belt-and-suspenders). 2 calls total: this RouterLoop's own tool-
    decision call (ADR-0035 D2 separate-decide — tools-only, no
    response_format; its free-form content is discarded once it resolves to
    PlainText) THEN the separate response_format-constrained answer-turn
    call whose JSON is what's actually returned."""
    scripted = _SequencedAgentReply([
        "(discarded tool-decision content)",
        '{"verdict": "approve", "confidence": 0.9}',
    ])
    reg = _registry(tmp_path, scripted)
    schema_registry = SchemaRegistry()
    schema_registry.register("review", _REVIEW_SCHEMA)

    result = await run_agent_step(
        reg, identity="worker", prompt="review this",
        schema="review", schema_registry=schema_registry,
    )

    assert result == {"verdict": "approve", "confidence": 0.9}
    # Tuple-unpack (not a size check): raises ValueError itself if the
    # structured-output path made anything other than EXACTLY 2 calls.
    decide_call, answer_call = scripted.calls
    # Call 0 (tool-decision): tools-only, no response_format.
    assert decide_call["response_format"] is None
    # Call 1 (the answer turn): schema-shaped response_format + NO tools
    # (ADR-0035 D2 separate-decide: never combined in the SAME call).
    rf = answer_call["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["schema"]["properties"]["verdict"]["enum"] == ["approve", "reject"]
    assert answer_call["tools"] == []


# ── failure mode (a): unsupported model — pre-check, no call wasted ─────────


@pytest.mark.asyncio
async def test_unsupported_model_precheck_raises_before_any_call(tmp_path: Path) -> None:
    """Tier 2c: failure mode (a) — a model litellm.supports_response_schema
    rejects raises StructuredOutputUnsupportedModelError BEFORE the turn's
    first LLM call. Primary evidence: ``scripted.calls`` stays EMPTY — no
    completion is ever issued (0062's explicit "no wasted call" requirement),
    proven by inspecting the call log directly rather than inferring from
    the error alone."""
    scripted = _SequencedAgentReply(['{"verdict": "approve", "confidence": 0.9}'])
    # "standard" resolves to a litellm-unknown provider/model pair →
    # supports_response_schema() is False (no provider recognizes it at all).
    resolver = ModelResolver({"standard": "not-a-real-provider/not-a-real-model"})
    reg = _registry(tmp_path, scripted, resolver=resolver)
    schema_registry = SchemaRegistry()
    schema_registry.register("review", _REVIEW_SCHEMA)

    with pytest.raises(StructuredOutputUnsupportedModelError):
        await run_agent_step(
            reg, identity="worker", prompt="review this",
            schema="review", schema_registry=schema_registry,
        )

    assert scripted.calls == []


# ── failure mode (b): provider rejects the schema — fail fast, no re-prompt ─


@pytest.mark.asyncio
async def test_schema_rejected_by_provider_fails_fast_no_reprompt_loop(tmp_path: Path) -> None:
    """Tier 2c: failure mode (b) — the LOAD-BEARING test. The scripted
    callable's FIRST entry is this RouterLoop's own tool-decision call
    (succeeds trivially, discarded once it resolves to PlainText); its
    SECOND entry — the answer-turn's one and only attempt — raises,
    simulating the provider rejecting the constrained-generation call. This
    becomes StructuredOutputSchemaRejectedError WITHOUT entering the
    failure-mode-(c) bounded re-prompt loop. Primary evidence: exactly 2
    total calls (1 tool-decision + 1 rejected structured attempt, NOT 1 + N)
    — re-prompting cannot fix an incompatible schema, so the structured path
    makes exactly ONE attempt, never more (this is the assertion that would
    catch a regression where failure modes (b) and (c) get conflated into
    one retry path)."""
    scripted = _SequencedAgentReply([
        "(discarded tool-decision content)",
        RuntimeError("400: schema violates json_schema subset"),
    ])
    reg = _registry(tmp_path, scripted)
    schema_registry = SchemaRegistry()
    schema_registry.register("review", _REVIEW_SCHEMA)

    with pytest.raises(StructuredOutputSchemaRejectedError):
        await run_agent_step(
            reg, identity="worker", prompt="review this",
            schema="review", schema_registry=schema_registry,
        )

    # Tuple-unpack (not a size check): raises ValueError itself unless the
    # structured path made EXACTLY 2 calls (1 tool-decision + 1 rejected
    # attempt — no re-prompt).
    _decide_call, _rejected_call = scripted.calls


# ── failure mode (c): generation-side non-conformance — bounded re-prompt ───


@pytest.mark.asyncio
async def test_nonconforming_json_recovers_within_reprompt_budget(tmp_path: Path) -> None:
    """Tier 2c: failure mode (c) — the tool-decision call (entry 0, discarded)
    is followed by a FIRST structured attempt that fails validation (wrong
    type for `confidence`, entry 1) fed back as a re-prompt, then a
    conforming SECOND structured attempt (entry 2) succeeds. Exactly 3 calls
    total (1 tool-decision + 1 failing attempt + 1 re-prompt), not fewer and
    not the full exhausted budget — proving the bounded re-prompt actually
    re-invokes the model rather than failing immediately or looping past
    success."""
    scripted = _SequencedAgentReply([
        "(discarded tool-decision content)",
        '{"verdict": "approve", "confidence": "not-a-number"}',
        '{"verdict": "approve", "confidence": 0.75}',
    ])
    reg = _registry(tmp_path, scripted)
    schema_registry = SchemaRegistry()
    schema_registry.register("review", _REVIEW_SCHEMA)

    result = await run_agent_step(
        reg, identity="worker", prompt="review this",
        schema="review", schema_registry=schema_registry,
    )

    assert result == {"verdict": "approve", "confidence": 0.75}
    # Tuple-unpack (not a size check): raises ValueError itself unless
    # EXACTLY 3 calls were made (1 tool-decision + 1 failing + 1 re-prompt).
    _decide_call, _bad_attempt, _good_attempt = scripted.calls


@pytest.mark.asyncio
async def test_nonconforming_json_exhausts_budget_then_typed_error(tmp_path: Path) -> None:
    """Tier 2c: failure mode (c) exhaustion — a reply that NEVER conforms
    raises StructuredOutputNonConformingError once the bounded re-prompt
    budget (default: 1 initial + 2 re-prompts = 3 structured attempts) is
    exhausted. Primary evidence: exactly 4 total calls (1 tool-decision + 3
    structured attempts) — proves the bound is enforced (not unbounded, not
    silently truncated to fewer attempts)."""
    scripted = _SequencedAgentReply([
        "(discarded tool-decision content)",
        '{"verdict": "maybe", "confidence": 0.5}',
        '{"verdict": "maybe", "confidence": 0.5}',
        '{"verdict": "maybe", "confidence": 0.5}',
    ])
    reg = _registry(tmp_path, scripted)
    schema_registry = SchemaRegistry()
    schema_registry.register("review", _REVIEW_SCHEMA)

    with pytest.raises(StructuredOutputNonConformingError):
        await run_agent_step(
            reg, identity="worker", prompt="review this",
            schema="review", schema_registry=schema_registry,
        )

    # Tuple-unpack (not a size check): raises ValueError itself unless the
    # bound produced EXACTLY 4 calls (1 tool-decision + 3 structured attempts
    # = 1 initial + 2 re-prompts), never more and never fewer.
    _decide_call, _a1, _a2, _a3 = scripted.calls


# ── AgentStep.model — resolves via model_resolver ────────────────────────────


@pytest.mark.asyncio
async def test_model_field_selects_resolved_model_class(tmp_path: Path) -> None:
    """Tier 2c: ``model="strong"`` overrides the ephemeral session's model
    the same way the ``/model`` slash command does — the LLM call's resolved
    ``ModelSpec.model`` is the "strong" class's configured model, not the
    default "standard" one. No schema involved (isolates the `model` field
    from the response_format machinery)."""
    scripted = _SequencedAgentReply(["plain text reply"])
    reg = _registry(tmp_path, scripted)

    result = await run_agent_step(
        reg, identity="worker", prompt="hi", model="strong",
    )

    assert result == "plain text reply"
    # Tuple-unpack (not a size check): raises ValueError itself unless
    # EXACTLY 1 call was made (no schema → no separate structured answer
    # turn — the tool-decision call's own plain text IS the reply).
    (only_call,) = scripted.calls
    assert only_call["model"].model == "gemini/gemini-2.5-pro"


# ── #2963: `number` range constraints (minimum/maximum) ─────────────────────
#
# The judge_output-removal co-vet found that the schema DSL had no way to
# express "0.0 to 1.0" for a `number` field — a model answering `85` (a
# 0-100 scale) against a `>= 0.6` threshold passed unchallenged, because the
# old op's `[0.0, 1.0]` docstring comment enforced nothing and the schema
# DSL's `number` type had no bound to fall back on. These tests exercise the
# fix through the SAME real pipeline entry point as the tests above
# (`run_agent_step` + real `AgentRegistry`/`Session`/`RouterLoop`, no
# hand-built schema/validate call) so the bound is proven wired into BOTH
# generation constraint (response_format) and post-hoc validation, not just
# one of the two.

_SCORE_SCHEMA = {
    "fields": {
        "score": {"type": "number", "minimum": 0.0, "maximum": 1.0, "required": True},
    },
}


@pytest.mark.asyncio
async def test_range_constraint_reaches_provider_response_format(tmp_path: Path) -> None:
    """Tier 2c: a `number` field's `minimum`/`maximum` propagate into the
    REAL `response_format` sent on the answer turn (not just a hand-built
    `to_json_schema()` call in isolation) — the "generation constraint"
    half of #2963's fix."""
    scripted = _SequencedAgentReply([
        "(discarded tool-decision content)",
        '{"score": 0.9}',
    ])
    reg = _registry(tmp_path, scripted)
    schema_registry = SchemaRegistry()
    schema_registry.register("score", _SCORE_SCHEMA)

    result = await run_agent_step(
        reg, identity="worker", prompt="score this",
        schema="score", schema_registry=schema_registry,
    )

    assert result == {"score": 0.9}
    _decide_call, answer_call = scripted.calls
    score_prop = answer_call["response_format"]["json_schema"]["schema"]["properties"]["score"]
    assert score_prop["minimum"] == 0.0
    assert score_prop["maximum"] == 1.0


@pytest.mark.asyncio
async def test_out_of_range_reply_is_reprompted_not_silently_accepted(tmp_path: Path) -> None:
    """Tier 2c: this IS the #2963 bug scenario — a model answering `85` on a
    0-100 scale against a `[0.0, 1.0]`-bound field. Before the fix, `85` was
    valid `number` JSON and would have been accepted outright (silently
    passing any `>= 0.6`-style threshold downstream); with the range bound
    wired into post-hoc `validate()`, it is treated as a non-conforming
    structured-output attempt and re-prompted — exactly like the existing
    wrong-type re-prompt test above, but for a right-TYPE, wrong-RANGE
    value. Recovers within budget once the second attempt is in-bounds."""
    scripted = _SequencedAgentReply([
        "(discarded tool-decision content)",
        '{"score": 85}',
        '{"score": 0.85}',
    ])
    reg = _registry(tmp_path, scripted)
    schema_registry = SchemaRegistry()
    schema_registry.register("score", _SCORE_SCHEMA)

    result = await run_agent_step(
        reg, identity="worker", prompt="score this",
        schema="score", schema_registry=schema_registry,
    )

    assert result == {"score": 0.85}
    # Tuple-unpack (not a size check): raises ValueError itself unless
    # EXACTLY 3 calls were made (1 tool-decision + 1 out-of-range attempt +
    # 1 in-bounds re-prompt) — proves `85` was NOT accepted on the first
    # attempt.
    _decide_call, _out_of_range_attempt, _in_bounds_attempt = scripted.calls


@pytest.mark.asyncio
async def test_out_of_range_reply_exhausts_budget_then_typed_error(tmp_path: Path) -> None:
    """Tier 2c: a model that NEVER answers in-bounds (always `85`) exhausts
    the same bounded re-prompt budget as any other non-conforming reply and
    raises `StructuredOutputNonConformingError` — the out-of-range value is
    never coerced, clamped, or let through as a last resort."""
    scripted = _SequencedAgentReply([
        "(discarded tool-decision content)",
        '{"score": 85}',
        '{"score": 85}',
        '{"score": 85}',
    ])
    reg = _registry(tmp_path, scripted)
    schema_registry = SchemaRegistry()
    schema_registry.register("score", _SCORE_SCHEMA)

    with pytest.raises(StructuredOutputNonConformingError):
        await run_agent_step(
            reg, identity="worker", prompt="score this",
            schema="score", schema_registry=schema_registry,
        )

    _decide_call, _a1, _a2, _a3 = scripted.calls
